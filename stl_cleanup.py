"""Conservative STL retention audit for KrezzCut's Render persistent disk.

Default is read-only. Deletion requires --apply AND
STL_CLEANUP_ALLOW_DELETE=true. Only old STL files backed by an order that
is paid, successfully submitted to Slant, and marked shipped are eligible.
Unknown, orphaned, digital, failed, or in-flight files are NEVER deleted.

Reads the same order_data.json store used by server.py and takes its file lock.
Run inside the Krezz-server service, where /data is mounted.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _eligible_shipped_order(order: dict, cutoff: datetime) -> bool:
    payment = order.get("payment") or {}
    fulfillment = order.get("fulfillment") or {}
    slant = order.get("slant") or {}
    items = order.get("items") or []
    if not isinstance(payment, dict) or not isinstance(fulfillment, dict) or not isinstance(slant, dict):
        return False
    if not isinstance(items, list) or not items:
        return False
    if not all(isinstance(item, dict) for item in items):
        return False
    # A previously sold digital item must never lose its downloadable asset.
    for item in items:
        kind = str(item.get("type") or "").lower()
        title = str(item.get("name") or "").lower()
        if kind == "digital" or item.get("is_digital") is True or "digital" in title:
            return False
    if payment.get("fulfillment_allowed") is not True:
        return False
    if str(order.get("status") or "").lower() != "submitted_to_slant":
        return False
    if str(slant.get("step") or "").lower() != "submitted":
        return False
    if not str(slant.get("publicOrderId") or "").strip():
        return False
    if str(fulfillment.get("status") or "").lower() != "shipped":
        return False
    shipped_at = parse_time(fulfillment.get("updated_at"))
    if shipped_at is None or shipped_at > cutoff:
        return False
    return True


def audit_and_maybe_cleanup(
    upload_dir: str = "/data/uploads",
    order_data_path: str = "/data/order_data.json",
    *,
    grace_days: int = 14,
    apply: bool = False,
    now: datetime | None = None,
    max_delete: int = 100,
) -> dict:
    """Return a summary and (only if explicitly enabled) delete eligible STLs.

    Locks the same order JSON file used by server.py for the entire inspection.
    Does NOT clean untracked/orphan STLs: those must be assessed separately.
    """
    if grace_days < 14:
        raise ValueError("grace_days must be at least 14")
    if not (1 <= max_delete <= 500):
        raise ValueError("max_delete must be between 1 and 500")
    if apply and not _truthy(os.environ.get("STL_CLEANUP_ALLOW_DELETE", "")):
        raise PermissionError("Deletion disabled. STL_CLEANUP_ALLOW_DELETE=true must be set explicitly.")

    root = Path(upload_dir)
    order_path = Path(order_data_path)
    if not root.is_dir():
        raise FileNotFoundError(f"Missing upload directory: {root}")
    if not order_path.is_file():
        raise FileNotFoundError("Order data missing. Refusing cleanup or eligibility scan.")

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=grace_days)
    lockpath = str(order_path) + ".lock"
    result = {
        "mode": "DELETE" if apply else "DRY_RUN",
        "grace_days": grace_days,
        "stl_files": 0,
        "stl_total_mb": 0,
        "eligible_files": 0,
        "eligible_mb": 0,
        "untracked_files_for_manual_review": 0,
        "protected_files": 0,
        "deleted_files": 0,
        "deleted_mb": 0,
        "errors": 0,
    }
    total_bytes = 0
    eligible_bytes = 0
    deleted_bytes = 0

    with open(lockpath, "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        # Fail closed on corrupt/missing or unexpectedly empty order storage.
        with open(order_path, "r", encoding="utf-8") as handle:
            orders = json.load(handle)
        if not isinstance(orders, dict) or not orders:
            raise ValueError("Order store empty or invalid; refusing STL cleanup.")

        refs: dict[str, list[dict]] = defaultdict(list)
        for _, order in orders.items():
            if not isinstance(order, dict):
                raise ValueError("Unexpected order record shape; refusing cleanup.")
            items = order.get("items") or []
            if not isinstance(items, list):
                raise ValueError("Unexpected items shape; refusing cleanup.")
            seen: set[str] = set()
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Unexpected item shape; refusing cleanup.")
                key = str(item.get("job_id") or item.get("jobId") or "").strip()
                if key:
                    seen.add(key)
            # Also protect file names recorded by a post-payment receipt upload.
            for upload in (order.get("uploads") or []):
                if isinstance(upload, dict) and upload.get("job_id"):
                    seen.add(str(upload["job_id"]).strip())
            for job_id in seen:
                refs[job_id].append(order)

        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.name.lower().endswith(".stl"):
                    continue
                try:
                    meta = entry.stat(follow_symlinks=False)
                except OSError:
                    result["errors"] += 1
                    continue
                if not stat.S_ISREG(meta.st_mode):
                    result["protected_files"] += 1
                    continue
                result["stl_files"] += 1
                total_bytes += meta.st_size
                job_id = entry.name[:-4]
                orders_for_file = refs.get(job_id)
                if not orders_for_file:
                    result["untracked_files_for_manual_review"] += 1
                    continue
                age_ok = datetime.fromtimestamp(meta.st_mtime, timezone.utc) <= cutoff
                can_delete = age_ok and all(
                    _eligible_shipped_order(order, cutoff) for order in orders_for_file
                )
                if not can_delete:
                    result["protected_files"] += 1
                    continue
                result["eligible_files"] += 1
                eligible_bytes += meta.st_size
                if apply and result["deleted_files"] < max_delete:
                    try:
                        # Do not remove a file replaced after it was inspected.
                        latest = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(latest.st_mode) or (
                            latest.st_ino != meta.st_ino or latest.st_size != meta.st_size
                            or latest.st_mtime_ns != meta.st_mtime_ns
                        ):
                            result["errors"] += 1
                            continue
                        os.unlink(entry.path)
                        result["deleted_files"] += 1
                        deleted_bytes += meta.st_size
                    except OSError:
                        result["errors"] += 1

    result["stl_total_mb"] = round(total_bytes / 1024 / 1024, 2)
    result["eligible_mb"] = round(eligible_bytes / 1024 / 1024, 2)
    result["deleted_mb"] = round(deleted_bytes / 1024 / 1024, 2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="KrezzCut STL cleanup audit (read-only by default)")
    parser.add_argument("--apply", action="store_true", help="Delete eligible files; requires STL_CLEANUP_ALLOW_DELETE=true")
    parser.add_argument("--grace-days", type=int, default=14, help="Days after shipping (minimum 14)")
    parser.add_argument("--max-delete", type=int, default=100, help="Deletion cap per execution")
    args = parser.parse_args()
    try:
        report = audit_and_maybe_cleanup(
            upload_dir=os.getenv("UPLOAD_DIR", "/data/uploads"),
            order_data_path=os.getenv("ORDER_DATA_PATH", "/data/order_data.json"),
            grace_days=args.grace_days,
            apply=args.apply,
            max_delete=args.max_delete,
        )
    except Exception as exc:
        print(f"STL cleanup stopped safely: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
