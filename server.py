from __future__ import annotations

import os
import uuid
import json
import time
import html
import tempfile
import threading
import traceback
import hmac
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # fallback to UTC day boundaries
from typing import Any, Dict, List, Optional, Tuple

import fcntl
import requests
import stripe
from flask import Flask, request, jsonify, send_file, abort, make_response

APP_VERSION = "KrezzServer/1.8"  # bumped (Slant shipped webhook support)

app = Flask(__name__)

# ----------------------------
# Utils
# ----------------------------
def utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def mask_secret(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}…{s[-keep:]}"


def normalize_country_iso2(country_val: str) -> str:
    if not country_val:
        return "US"
    c = country_val.strip().lower()
    if c in ("us", "usa", "united states", "united states of america"):
        return "US"
    if len(country_val.strip()) == 2:
        return country_val.strip().upper()
    return "US"


def req_id() -> str:
    rid = request.headers.get("X-Request-Id") or request.headers.get("X-Request-ID")
    return (rid or str(uuid.uuid4()))[:64]


# ----------------------------
# Config
# ----------------------------
@dataclass(frozen=True)
class Config:
    stripe_secret_key: str
    stripe_endpoint_secret: str

    public_base_url: str
    upload_dir: str
    order_data_path: str

    stripe_success_url_tmpl: str
    stripe_cancel_url_tmpl: str

    slant_enabled: bool
    slant_debug: bool
    slant_auto_submit: bool
    slant_require_live_stripe: bool

    slant_api_key: str
    slant_platform_id: str
    slant_base_url: str
    slant_files_endpoint: str
    slant_filaments_endpoint: str
    slant_orders_endpoint: str
    slant_timeout_sec: int

    slant_file_url_field: str
    slant_stl_route: str
    slant_send_bearer: bool

    # ✅ NEW: Slant webhook secret (for order.shipped verification)
    slant_webhook_secret: str

    require_stl_before_checkout: bool
    auto_submit_on_upload_if_paid: bool

    # ✅ NEW: Daily order cap + quota storage
    daily_order_cap: int
    quota_data_path: str
    quota_tz: str
    quota_reservation_ttl_sec: int
    quota_prune_days: int


    @staticmethod
    def load() -> "Config":
        stripe_secret_key = env_str("STRIPE_SECRET_KEY")
        stripe_endpoint_secret = env_str("STRIPE_ENDPOINT_SECRET")
        if not stripe_secret_key or not stripe_endpoint_secret:
            raise ValueError("Missing STRIPE_SECRET_KEY and/or STRIPE_ENDPOINT_SECRET")

        public_base_url = env_str("PUBLIC_BASE_URL", "").rstrip("/")
        if not public_base_url:
            raise ValueError("Missing PUBLIC_BASE_URL (example: https://krezz-server.onrender.com)")

        upload_dir = env_str("UPLOAD_DIR", "/data/uploads")
        os.makedirs(upload_dir, exist_ok=True)

        order_data_path = env_str("ORDER_DATA_PATH", "/data/order_data.json")
        os.makedirs(os.path.dirname(order_data_path), exist_ok=True)

        # ✅ NEW: Daily quota config (cap orders/day)
        daily_order_cap = safe_int(env_str("SLANT_DAILY_ORDER_CAP", "100"), 100)

        quota_data_path = env_str("DAILY_QUOTA_PATH", "/data/daily_quota.json")
        os.makedirs(os.path.dirname(quota_data_path), exist_ok=True)

        quota_tz = env_str("DAILY_QUOTA_TZ", "America/New_York")
        quota_reservation_ttl_sec = safe_int(env_str("DAILY_QUOTA_RESERVE_TTL_SEC", "86400"), 86400)  # 24h
        quota_prune_days = safe_int(env_str("DAILY_QUOTA_PRUNE_DAYS", "14"), 14)


        success_tmpl = env_str(
            "STRIPE_SUCCESS_URL",
            f"{public_base_url}/success?order_id={{ORDER_ID}}&session_id={{CHECKOUT_SESSION_ID}}",
        )
        cancel_tmpl = env_str(
            "STRIPE_CANCEL_URL",
            f"{public_base_url}/cancel?order_id={{ORDER_ID}}",
        )

        # Slant
        slant_api_key = env_str("SLANT_API_KEY")
        slant_platform_id = env_str("SLANT_PLATFORM_ID")

        slant_base_url = env_str("SLANT_BASE_URL", "https://slant3dapi.com/v2/api").rstrip("/")
        slant_files_endpoint = env_str("SLANT_FILES_ENDPOINT", f"{slant_base_url}/files")
        slant_filaments_endpoint = env_str("SLANT_FILAMENTS_ENDPOINT", f"{slant_base_url}/filaments")
        slant_orders_endpoint = env_str("SLANT_ORDERS_ENDPOINT", f"{slant_base_url}/orders")
        slant_timeout_sec = safe_int(env_str("SLANT_TIMEOUT_SEC", "240"), 240)

        slant_enabled = bool(slant_api_key)
        slant_debug = env_bool("SLANT_DEBUG", False)
        slant_auto_submit = env_bool("SLANT_AUTO_SUBMIT", False)
        slant_require_live_stripe = env_bool("SLANT_REQUIRE_LIVE_STRIPE", True)

        # ✅ Now actually honored everywhere we build file payloads
        slant_file_url_field = env_str("SLANT_FILE_URL_FIELD", "URL")

        # ✅ Recommendation: set SLANT_STL_ROUTE=full
        slant_stl_route = env_str("SLANT_STL_ROUTE", "full").lower()

        slant_send_bearer = env_bool("SLANT_SEND_BEARER", True)

        # ✅ NEW: Slant webhook secret for signature verification
        slant_webhook_secret = env_str("SLANT_WEBHOOK_SECRET")

        require_stl_before_checkout = env_bool("REQUIRE_STL_BEFORE_CHECKOUT", True)
        auto_submit_on_upload_if_paid = env_bool("AUTO_SUBMIT_ON_UPLOAD_IF_PAID", True)

        cfg = Config(
            stripe_secret_key=stripe_secret_key,
            stripe_endpoint_secret=stripe_endpoint_secret,
            public_base_url=public_base_url,
            upload_dir=upload_dir,
            order_data_path=order_data_path,
            stripe_success_url_tmpl=success_tmpl,
            stripe_cancel_url_tmpl=cancel_tmpl,
            slant_enabled=slant_enabled,
            slant_debug=slant_debug,
            slant_auto_submit=slant_auto_submit,
            slant_require_live_stripe=slant_require_live_stripe,
            slant_api_key=slant_api_key,
            slant_platform_id=slant_platform_id,
            slant_base_url=slant_base_url,
            slant_files_endpoint=slant_files_endpoint,
            slant_filaments_endpoint=slant_filaments_endpoint,
            slant_orders_endpoint=slant_orders_endpoint,
            slant_timeout_sec=slant_timeout_sec,
            slant_file_url_field=slant_file_url_field,
            slant_stl_route=slant_stl_route,
            slant_send_bearer=slant_send_bearer,
            slant_webhook_secret=slant_webhook_secret,
            require_stl_before_checkout=require_stl_before_checkout,
            auto_submit_on_upload_if_paid=auto_submit_on_upload_if_paid,
            daily_order_cap=daily_order_cap,
            quota_data_path=quota_data_path,
            quota_tz=quota_tz,
            quota_reservation_ttl_sec=quota_reservation_ttl_sec,
            quota_prune_days=quota_prune_days,

        )

        print("✅ Boot config:")
        print("   PUBLIC_BASE_URL:", cfg.public_base_url)
        print("   UPLOAD_DIR:", cfg.upload_dir)
        print("   ORDER_DATA_PATH:", cfg.order_data_path)
        print("   STRIPE_SUCCESS_URL:", cfg.stripe_success_url_tmpl)
        print("   STRIPE_CANCEL_URL:", cfg.stripe_cancel_url_tmpl)
        print("   SLANT_ENABLED:", cfg.slant_enabled)
        print("   SLANT_AUTO_SUBMIT:", cfg.slant_auto_submit)
        print("   SLANT_REQUIRE_LIVE_STRIPE:", cfg.slant_require_live_stripe)
        print("   SLANT_BASE_URL:", cfg.slant_base_url)
        print("   SLANT_FILES_ENDPOINT:", cfg.slant_files_endpoint)
        print("   SLANT_FILAMENTS_ENDPOINT:", cfg.slant_filaments_endpoint)
        print("   SLANT_ORDERS_ENDPOINT:", cfg.slant_orders_endpoint)
        print("   SLANT_TIMEOUT_SEC:", cfg.slant_timeout_sec)
        print("   SLANT_FILE_URL_FIELD:", cfg.slant_file_url_field)
        print("   SLANT_STL_ROUTE:", cfg.slant_stl_route)
        print("   SLANT_SEND_BEARER:", cfg.slant_send_bearer)
        print("   REQUIRE_STL_BEFORE_CHECKOUT:", cfg.require_stl_before_checkout)
        print("   AUTO_SUBMIT_ON_UPLOAD_IF_PAID:", cfg.auto_submit_on_upload_if_paid)
        print("   SLANT_API_KEY:", mask_secret(cfg.slant_api_key))
        print("   SLANT_PLATFORM_ID:", mask_secret(cfg.slant_platform_id))
        print("   SLANT_WEBHOOK_SECRET:", mask_secret(cfg.slant_webhook_secret))
        print("   SLANT_DAILY_ORDER_CAP:", cfg.daily_order_cap)
        print("   DAILY_QUOTA_PATH:", cfg.quota_data_path)
        print("   DAILY_QUOTA_TZ:", cfg.quota_tz)

        return cfg


CFG = Config.load()
stripe.api_key = CFG.stripe_secret_key

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": APP_VERSION})


def build_success_url(order_id: str) -> str:
    return CFG.stripe_success_url_tmpl.replace("{ORDER_ID}", order_id)


def build_cancel_url(order_id: str) -> str:
    return CFG.stripe_cancel_url_tmpl.replace("{ORDER_ID}", order_id)


def stl_path_for(job_id: str) -> str:
    return os.path.join(CFG.upload_dir, f"{job_id}.stl")


def stl_exists(job_id: str) -> bool:
    return os.path.exists(stl_path_for(job_id))


# ----------------------------
# Order storage (file + lock)
# ----------------------------
class OrderStore:
    def __init__(self, path: str):
        self.path = path
        self.lock_path = path + ".lock"
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _lock(self):
        lf = open(self.lock_path, "w")
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        return lf

    def _read_unlocked(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r") as f:
            raw = f.read().strip()
            return json.loads(raw) if raw else {}

    def _write_unlocked(self, data: Dict[str, Any]) -> None:
        dirpath = os.path.dirname(self.path)
        os.makedirs(dirpath, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="order_data_", suffix=".json", dir=dirpath)
        try:
            with os.fdopen(fd, "w") as tmp:
                json.dump(data, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def count(self) -> int:
        lf = self._lock()
        try:
            data = self._read_unlocked()
            return len(data)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()

    def get(self, order_id: str) -> Optional[Dict[str, Any]]:
        lf = self._lock()
        try:
            data = self._read_unlocked()
            obj = data.get(order_id)
            return dict(obj) if isinstance(obj, dict) else None
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()

    def upsert(self, order_id: str, order_obj: Dict[str, Any]) -> None:
        lf = self._lock()
        try:
            data = self._read_unlocked()
            data[order_id] = order_obj
            self._write_unlocked(data)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()

    def update(self, order_id: str, fn) -> Tuple[Dict[str, Any], bool]:
        lf = self._lock()
        try:
            data = self._read_unlocked()
            order = data.get(order_id)
            if not isinstance(order, dict):
                order = {"items": [], "shipping": {}, "status": "created", "created_at": utc_iso()}

            new_order, changed = fn(dict(order))
            data[order_id] = new_order
            if changed:
                self._write_unlocked(data)
            return new_order, changed
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()

    # ✅ NEW: Find internal order_id by Slant public order id (SLANT_...).
    def find_by_slant_public_order_id(self, public_id: str) -> Optional[str]:
        public_id = (public_id or "").strip()
        if not public_id:
            return None

        lf = self._lock()
        try:
            data = self._read_unlocked() or {}
            for oid, obj in data.items():
                if not isinstance(obj, dict):
                    continue

                sl = obj.get("slant") or {}
                if str(sl.get("publicOrderId") or "").strip() == public_id:
                    return oid

                ful = obj.get("fulfillment") or {}
                if str(ful.get("slant_public_id") or "").strip() == public_id:
                    return oid

            return None
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()

class DailyQuotaStore:
    """
    Tracks a per-day cap using a JSON file + fcntl lock (safe across gunicorn workers).

    Data format:
    {
      "YYYY-MM-DD": {
        "cap": 100,
        "paid_orders": { "order_id": {"paid_at": "...", "stripe_session_id": "..."} },
        "reservations": { "order_id": {"reserved_at": "...", "expires_at": 1234567890, "stripe_session_id": "..."} }
      },
      ...
    }
    """

    def __init__(self, path: str, daily_cap: int, tz_name: str, reservation_ttl_sec: int, prune_days: int):
        self.path = path
        self.lock_path = path + ".lock"
        self.daily_cap = int(daily_cap or 0) if daily_cap else 0
        self.tz_name = (tz_name or "").strip() or "UTC"
        self.reservation_ttl_sec = int(reservation_ttl_sec or 86400)
        self.prune_days = int(prune_days or 14)
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _lock(self):
        lf = open(self.lock_path, "w")
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        return lf

    def _read_unlocked(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r") as f:
            raw = f.read().strip()
            return json.loads(raw) if raw else {}

    def _write_unlocked(self, data: Dict[str, Any]) -> None:
        dirpath = os.path.dirname(self.path)
        os.makedirs(dirpath, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="quota_", suffix=".json", dir=dirpath)
        try:
            with os.fdopen(fd, "w") as tmp:
                json.dump(data, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def _now_local(self) -> datetime:
        if ZoneInfo is None:
            return datetime.utcnow()
        try:
            return datetime.now(ZoneInfo(self.tz_name))
        except Exception:
            return datetime.utcnow()

    def day_key(self) -> str:
        # Local day key (defaults to UTC if ZoneInfo missing/bad tz)
        return self._now_local().strftime("%Y-%m-%d")

    def next_reset_iso(self) -> str:
        now = self._now_local()
        # next local midnight
        nxt = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
        # Return ISO-ish string; if UTC fallback, it's UTC midnight
        return nxt.isoformat()

    def _ensure_day_obj(self, data: Dict[str, Any], day: str) -> Dict[str, Any]:
        obj = data.get(day)
        if not isinstance(obj, dict):
            obj = {}
        obj.setdefault("cap", int(self.daily_cap))
        obj.setdefault("paid_orders", {})
        obj.setdefault("reservations", {})
        # always keep current cap value
        obj["cap"] = int(self.daily_cap)
        data[day] = obj
        return obj

    def _cleanup_unlocked(self, data: Dict[str, Any], today_key: str) -> None:
        # prune old day buckets
        try:
            today = datetime.strptime(today_key, "%Y-%m-%d").date()
        except Exception:
            return

        keep_after = today - timedelta(days=max(self.prune_days, 1))
        for day in list(data.keys()):
            try:
                d = datetime.strptime(day, "%Y-%m-%d").date()
            except Exception:
                continue
            if d < keep_after:
                data.pop(day, None)

        # expire reservations (all days we kept)
        now_epoch = int(time.time())
        for day, obj in list(data.items()):
            if not isinstance(obj, dict):
                continue
            reservations = obj.get("reservations")
            if not isinstance(reservations, dict):
                continue
            for oid, r in list(reservations.items()):
                exp = None
                if isinstance(r, dict):
                    exp = r.get("expires_at")
                try:
                    exp_i = int(exp) if exp is not None else None
                except Exception:
                    exp_i = None
                if exp_i is not None and exp_i <= now_epoch:
                    reservations.pop(oid, None)
            obj["reservations"] = reservations

    def stats(self, day: Optional[str] = None) -> Dict[str, Any]:
        day = (day or "").strip() or self.day_key()
        lf = self._lock()
        try:
            data = self._read_unlocked() or {}
            self._cleanup_unlocked(data, day)
            obj = self._ensure_day_obj(data, day)
            paid = obj.get("paid_orders") if isinstance(obj.get("paid_orders"), dict) else {}
            resv = obj.get("reservations") if isinstance(obj.get("reservations"), dict) else {}
            return {
                "day": day,
                "cap": int(obj.get("cap") or self.daily_cap),
                "paid_count": len(paid),
                "reserved_count": len(resv),
                "remaining_effective": max(0, int(obj.get("cap") or self.daily_cap) - (len(paid) + len(resv))),
                "next_reset": self.next_reset_iso(),
            }
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()

    def reserve(self, order_id: str, day: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
        """
        Reserve a slot for this order_id (idempotent).
        Returns (ok, info) where info includes 'created' flag.
        """
        order_id = (order_id or "").strip()
        if not order_id:
            return False, {"error": "missing_order_id"}

        day = (day or "").strip() or self.day_key()
        lf = self._lock()
        try:
            data = self._read_unlocked() or {}
            self._cleanup_unlocked(data, day)
            obj = self._ensure_day_obj(data, day)

            cap = int(obj.get("cap") or self.daily_cap)
            paid = obj.get("paid_orders")
            resv = obj.get("reservations")
            if not isinstance(paid, dict):
                paid = {}
            if not isinstance(resv, dict):
                resv = {}

            # already paid => ok
            if order_id in paid:
                return True, {"day": day, "cap": cap, "created": False, "status": "already_paid"}

            # already reserved => ok
            if order_id in resv:
                return True, {"day": day, "cap": cap, "created": False, "status": "already_reserved"}

            if cap > 0 and (len(paid) + len(resv)) >= cap:
                return False, {
                    "day": day,
                    "cap": cap,
                    "paid_count": len(paid),
                    "reserved_count": len(resv),
                    "next_reset": self.next_reset_iso(),
                    "status": "cap_reached",
                }

            exp = int(time.time()) + int(self.reservation_ttl_sec or 86400)
            resv[order_id] = {"reserved_at": utc_iso(), "expires_at": exp}
            obj["reservations"] = resv
            obj["paid_orders"] = paid
            data[day] = obj
            self._write_unlocked(data)
            return True, {"day": day, "cap": cap, "created": True, "status": "reserved", "expires_at": exp}
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()

    def attach_session(self, order_id: str, session_id: str, expires_at: Optional[int] = None, day: Optional[str] = None) -> None:
        """
        Add Stripe session info to a reservation (best-effort).
        """
        order_id = (order_id or "").strip()
        if not order_id:
            return
        day = (day or "").strip() or self.day_key()

        lf = self._lock()
        try:
            data = self._read_unlocked() or {}
            self._cleanup_unlocked(data, day)
            obj = self._ensure_day_obj(data, day)
            resv = obj.get("reservations")
            if not isinstance(resv, dict):
                resv = {}

            r = resv.get(order_id)
            if not isinstance(r, dict):
                return

            if session_id:
                r["stripe_session_id"] = session_id
            if expires_at is not None:
                try:
                    r["expires_at"] = int(expires_at)
                except Exception:
                    pass

            resv[order_id] = r
            obj["reservations"] = resv
            data[day] = obj
            self._write_unlocked(data)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()

    def release_reservation(self, order_id: str) -> bool:
        """
        Remove a reservation for this order_id (searches all kept days).
        """
        order_id = (order_id or "").strip()
        if not order_id:
            return False

        lf = self._lock()
        try:
            data = self._read_unlocked() or {}
            changed = False
            for day, obj in list(data.items()):
                if not isinstance(obj, dict):
                    continue
                resv = obj.get("reservations")
                if not isinstance(resv, dict):
                    continue
                if order_id in resv:
                    resv.pop(order_id, None)
                    obj["reservations"] = resv
                    data[day] = obj
                    changed = True
            if changed:
                self._write_unlocked(data)
            return changed
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()

    def mark_paid(self, order_id: str, session_id: str = "", day: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
        """
        Mark an order as paid for the day bucket (idempotent).
        Removes any reservation.
        """
        order_id = (order_id or "").strip()
        if not order_id:
            return False, {"error": "missing_order_id"}

        day = (day or "").strip() or self.day_key()
        lf = self._lock()
        try:
            data = self._read_unlocked() or {}
            self._cleanup_unlocked(data, day)
            obj = self._ensure_day_obj(data, day)

            cap = int(obj.get("cap") or self.daily_cap)
            paid = obj.get("paid_orders")
            resv = obj.get("reservations")
            if not isinstance(paid, dict):
                paid = {}
            if not isinstance(resv, dict):
                resv = {}

            if order_id in paid:
                return True, {"day": day, "cap": cap, "status": "already_paid"}

            # hard cap: paid count cannot exceed cap
            if cap > 0 and len(paid) >= cap:
                return False, {
                    "day": day,
                    "cap": cap,
                    "paid_count": len(paid),
                    "reserved_count": len(resv),
                    "next_reset": self.next_reset_iso(),
                    "status": "paid_cap_reached",
                }

            # remove reservation if exists
            if order_id in resv:
                resv.pop(order_id, None)

            paid[order_id] = {"paid_at": utc_iso(), "stripe_session_id": (session_id or "").strip()}
            obj["paid_orders"] = paid
            obj["reservations"] = resv
            data[day] = obj
            self._write_unlocked(data)

            return True, {
                "day": day,
                "cap": cap,
                "paid_count": len(paid),
                "reserved_count": len(resv),
                "status": "paid_recorded",
            }
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()



STORE = OrderStore(CFG.order_data_path)

QUOTA = DailyQuotaStore(
    path=CFG.quota_data_path,
    daily_cap=CFG.daily_order_cap,
    tz_name=CFG.quota_tz,
    reservation_ttl_sec=CFG.quota_reservation_ttl_sec,
    prune_days=CFG.quota_prune_days,
)

def stl_make_single_solid_inplace(path: str) -> Dict[str, Any]:
    import os
    import json
    import traceback
    import trimesh

    info: Dict[str, Any] = {"path": path, "changed": False}

    if not os.path.exists(path):
        info["error"] = "missing_file"
        return info

    try:
        loaded = trimesh.load(path, force="mesh", process=False)

        if isinstance(loaded, trimesh.Scene):
            meshes = [g for g in loaded.dump() if isinstance(g, trimesh.Trimesh)]
            if not meshes:
                info["error"] = "scene_has_no_meshes"
                return info
            mesh = trimesh.util.concatenate(meshes)
        else:
            mesh = loaded

        mesh.process(validate=True)

        comps = mesh.split(only_watertight=False)
        info["components_before"] = len(comps)
        info["faces_before_total"] = int(mesh.faces.shape[0])

        if len(comps) <= 1:
            info["note"] = "already_single_component"
            return info

        welded = trimesh.boolean.union(comps, engine="manifold", check_volume=False)

        if welded is None:
            info["error"] = "union_returned_none"
            return info

        welded.process(validate=True)
        comps_after = welded.split(only_watertight=False)

        info["components_after"] = len(comps_after)
        info["faces_after_total"] = int(welded.faces.shape[0])

        backup = path + ".preweld"
        if not os.path.exists(backup):
            try:
                os.replace(path, backup)
            except Exception:
                with open(path, "rb") as fsrc, open(backup, "wb") as fdst:
                    fdst.write(fsrc.read())

        out_bytes = welded.export(file_type="stl")

        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            if isinstance(out_bytes, (bytes, bytearray)):
                f.write(out_bytes)
            else:
                f.write(str(out_bytes).encode("utf-8"))
        os.replace(tmp, path)

        info["changed"] = True
        info["backup"] = backup
        return info

    except Exception as e:
        info["error"] = str(e)
        info["trace"] = traceback.format_exc()[:6000]
        return info



# ----------------------------
# Slant client
# ----------------------------
class SlantError(RuntimeError):
    def __init__(self, status: int, body: str, where: str, headers: Optional[Dict[str, str]] = None):
        mini = {}
        if headers:
            for k in ("content-type", "x-request-id", "cf-ray", "date"):
                for hk, hv in headers.items():
                    if hk.lower() == k:
                        mini[hk] = hv
        super().__init__(f"{where}: status={status} headers={mini} body={(body or '')[:1600]}")
        self.status = status
        self.body = body
        self.where = where


def slant_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Slant auth supports Bearer token; some setups also accept api-key.
    We include both (api-key is harmless) and control Authorization with SLANT_SEND_BEARER.
    """
    h: Dict[str, str] = {"Accept": "application/json"}

    api_key = (CFG.slant_api_key or "").strip()
    if api_key:
        if CFG.slant_send_bearer:
            h["Authorization"] = f"Bearer {api_key}"
        h["api-key"] = api_key

    if extra:
        h.update(extra)
    return h


def slant_timeout() -> Tuple[int, int]:
    return (10, CFG.slant_timeout_sec)


def _safe_json(r: requests.Response) -> Dict[str, Any]:
    try:
        return r.json() if (r.text or "").strip() else {}
    except Exception:
        return {"_raw": (r.text or "")[:4000]}


def _slant_log(where: str, obj: Dict[str, Any]) -> None:
    if CFG.slant_debug:
        print(f"🧪 {where} {json.dumps(obj, ensure_ascii=False, default=str)[:4000]}")


def parse_slant_file_public_id(payload: dict) -> str:
    data_obj = payload.get("data") if isinstance(payload, dict) else None
    candidates = []
    if isinstance(data_obj, dict):
        candidates += [data_obj.get("publicFileServiceId"), data_obj.get("publicId"), data_obj.get("id")]
    if isinstance(payload, dict):
        candidates += [payload.get("publicFileServiceId"), payload.get("publicId"), payload.get("id")]
    for c in candidates:
        if c:
            return str(c)
    raise RuntimeError(f"Slant response missing file id: {str(payload)[:1200]}")


def stl_probe_head(url: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"url": url}
    try:
        hr = HTTP.head(url, timeout=(10, 45), allow_redirects=True)
        out.update(
            {
                "head_status": hr.status_code,
                "head_len": hr.headers.get("Content-Length"),
                "head_type": hr.headers.get("Content-Type"),
                "head_ranges": hr.headers.get("Accept-Ranges"),
            }
        )
    except Exception as e:
        out["head_error"] = str(e)
    return out


# ----------------------------
# Slant filaments (cache + robust parsing)
# ----------------------------
_FILAMENT_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_FILAMENT_CACHE_TTL_SEC: int = safe_int(env_str("SLANT_FILAMENTS_CACHE_TTL_SEC", "600"), 600)


def _extract_list_from_slant_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("filaments", "items", "results", "data"):
                v = data.get(key)
                if isinstance(v, list):
                    return v
    if isinstance(payload, list):
        return payload
    return []


def _extract_filament_id(f: Dict[str, Any]) -> Optional[str]:
    for k in ("filamentId", "id", "publicId", "publicFilamentId", "uuid"):
        v = f.get(k)
        if v:
            return str(v)
    return None


def _filament_available(f: Dict[str, Any]) -> bool:
    v = f.get("available", None)
    if v is None:
        v = f.get("isAvailable", None)
    if v is None:
        return True
    return bool(v)


def _filament_name(f: Dict[str, Any]) -> str:
    for k in ("name", "displayName", "title"):
        v = f.get(k)
        if v:
            return str(v)
    prof = f.get("profile")
    if isinstance(prof, dict) and prof.get("name"):
        return str(prof.get("name"))
    return ""


def _filament_profile(f: Dict[str, Any]) -> str:
    p = f.get("profile") or f.get("material") or f.get("type") or f.get("materialProfile")
    if isinstance(p, dict):
        return str(p.get("name") or p.get("profile") or p.get("type") or "")
    return str(p or "")


def _filament_color(f: Dict[str, Any]) -> str:
    return str(f.get("color") or f.get("colour") or "")


def slant_get_filaments() -> List[Dict[str, Any]]:
    endpoint = (CFG.slant_filaments_endpoint or "").strip()
    if not endpoint:
        endpoint = f"{CFG.slant_base_url.rstrip('/')}/filaments"

    r = HTTP.get(
        endpoint,
        headers=slant_headers({"Content-Type": "application/json"}),
        timeout=slant_timeout(),
    )

    _slant_log(
        "SLANT_HTTP GET /filaments",
        {
            "endpoint": endpoint,
            "status": r.status_code,
            "headers": {k: v for k, v in r.headers.items()},
            "body_snippet": (r.text or "")[:1400],
        },
    )

    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant GET /filaments", headers=dict(r.headers))

    payload = _safe_json(r)
    items = _extract_list_from_slant_payload(payload)
    if not items:
        raise RuntimeError(f"Slant filaments response not a list: {str(payload)[:1200]}")
    return items


def slant_get_filaments_cached(force: bool = False) -> List[Dict[str, Any]]:
    now = time.time()
    if (
        not force
        and _FILAMENT_CACHE["data"] is not None
        and (now - float(_FILAMENT_CACHE["ts"] or 0.0)) < _FILAMENT_CACHE_TTL_SEC
    ):
        return _FILAMENT_CACHE["data"]

    data = slant_get_filaments()
    _FILAMENT_CACHE["ts"] = now
    _FILAMENT_CACHE["data"] = data
    return data


# ----------------------------
# Slant files upload (URL)
# ----------------------------
def slant_create_file_by_url(job_id: str, stl_url: str) -> str:
    pid = (CFG.slant_platform_id or "").strip()
    if not pid:
        raise RuntimeError("SLANT_PLATFORM_ID is missing/blank at runtime.")

    probe = stl_probe_head(stl_url)
    print("🧪 STL PROBE", json.dumps(probe, ensure_ascii=False, default=str))

    payload = {
        CFG.slant_file_url_field: stl_url,
        "name": job_id,
        "platformId": pid,
        "type": "stl",
        "ownerId": job_id,
    }

    print(
        "🧪 Slant create file request",
        json.dumps({"endpoint": CFG.slant_files_endpoint, "payload": payload}, ensure_ascii=False, default=str),
    )

    r = HTTP.post(
        CFG.slant_files_endpoint,
        headers=slant_headers({"Content-Type": "application/json"}),
        json=payload,
        timeout=slant_timeout(),
    )

    print(
        "🧪 SLANT_HTTP",
        json.dumps(
            {
                "where": "POST /files",
                "status": r.status_code,
                "headers": {k: v for k, v in r.headers.items()},
                "body_snippet": (r.text or "")[:1400],
            },
            ensure_ascii=False,
            default=str,
        ),
    )

    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant POST /files", headers=dict(r.headers))

    resp = _safe_json(r)
    file_id = parse_slant_file_public_id(resp)
    if not file_id:
        raise RuntimeError(f"Could not parse publicFileServiceId from Slant response: {resp}")

    print(f"✅ Slant file created: job_id={job_id} publicFileServiceId={file_id}")
    return file_id


def slant_upload_stl(job_id: str) -> str:
    p = stl_path_for(job_id)
    if not os.path.exists(p):
        raise RuntimeError(f"STL not found on server: {p}")

    # ✅ Add this block
    if env_bool("STL_BOOL_WELD_BEFORE_SLANT", True):
        weld_info = stl_make_single_solid_inplace(p)
        print("🧩 STL WELD INFO:", json.dumps(weld_info, ensure_ascii=False, default=str)[:4000])

    route = "stl-full" if CFG.slant_stl_route == "full" else "stl-raw"
    stl_url = f"{CFG.public_base_url}/{route}/{job_id}.stl"
    return slant_create_file_by_url(job_id, stl_url)

@app.route("/debug/stl/components/<job_id>", methods=["GET"])
def debug_stl_components(job_id: str):
    import trimesh
    p = stl_path_for(job_id)
    if not os.path.exists(p):
        return jsonify({"ok": False, "error": "not found", "path": p}), 404

    m = trimesh.load(p, force="mesh", process=False)
    if isinstance(m, trimesh.Scene):
        meshes = [g for g in m.dump() if isinstance(g, trimesh.Trimesh)]
        m = trimesh.util.concatenate(meshes) if meshes else None
    if m is None:
        return jsonify({"ok": False, "error": "no mesh found"}), 500

    m.process(validate=True)
    comps = m.split(only_watertight=False)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "components": len(comps),
            "faces_total": int(m.faces.shape[0]),
            "faces_by_component": [int(c.faces.shape[0]) for c in comps[:20]],
        }
    )


# ----------------------------
# Filament resolution
# ----------------------------
def resolve_filament_id(shipping_info: dict) -> str:
    shipping_info = shipping_info or {}

    explicit = shipping_info.get("filamentId") or shipping_info.get("filament_id")
    if explicit:
        return str(explicit)

    env_default = env_str("SLANT_DEFAULT_FILAMENT_ID", "")
    if env_default:
        return env_default

    material_raw = str(shipping_info.get("material") or "").strip().upper()
    color_raw = str(shipping_info.get("color") or "").strip().lower()

    want_profile = ""
    if "PETG" in material_raw:
        want_profile = "PETG"
    elif "PLA" in material_raw:
        want_profile = "PLA"

    filaments = slant_get_filaments_cached()
    if not filaments:
        raise RuntimeError("Slant /filaments returned empty list.")

    def norm(s: Any) -> str:
        return str(s or "").strip().lower()

    if want_profile and color_raw:
        for f in filaments:
            if not _filament_available(f):
                continue
            prof = norm(_filament_profile(f))
            col = norm(_filament_color(f))
            if want_profile.lower() in prof and color_raw == col:
                fid = _extract_filament_id(f)
                if fid:
                    return fid

    if want_profile:
        for f in filaments:
            if not _filament_available(f):
                continue
            prof = norm(_filament_profile(f))
            if want_profile.lower() in prof:
                fid = _extract_filament_id(f)
                if fid:
                    return fid

    if color_raw:
        for f in filaments:
            if not _filament_available(f):
                continue
            col = norm(_filament_color(f))
            if color_raw == col:
                fid = _extract_filament_id(f)
                if fid:
                    return fid

    wanted_tokens = ("pla", "black")
    for f in filaments:
        if not _filament_available(f):
            continue
        name = norm(_filament_name(f))
        prof = norm(_filament_profile(f))
        if all(t in (name + " " + prof) for t in wanted_tokens):
            fid = _extract_filament_id(f)
            if fid:
                return fid

    for f in filaments:
        if not _filament_available(f):
            continue
        fid = _extract_filament_id(f)
        if fid:
            return fid

    raise RuntimeError("No filament available (could not extract filamentId from Slant filaments).")


# ----------------------------
# Slant orders
# ----------------------------
def slant_draft_order(order_id: str, shipping: dict, items: list) -> str:
    pid = (CFG.slant_platform_id or "").strip()
    if not pid:
        raise RuntimeError("SLANT_PLATFORM_ID is missing/blank at runtime.")

    email = shipping.get("email") or "unknown@test.com"
    full_name = shipping.get("fullName") or shipping.get("name") or "Customer"
    line1 = shipping.get("addressLine") or shipping.get("line1") or ""
    line2 = shipping.get("addressLine2") or shipping.get("line2") or ""
    city = shipping.get("city") or ""
    state = shipping.get("state") or ""
    zip_code = shipping.get("zipCode") or shipping.get("zip") or ""
    country = normalize_country_iso2(shipping.get("country") or "US")

    missing = [
        k
        for k, v in {
            "line1": line1,
            "city": city,
            "state": state,
            "zip": zip_code,
            "country": country,
            "email": email,
        }.items()
        if not str(v).strip()
    ]
    if missing:
        raise RuntimeError(f"Shipping info missing required fields: {missing}")

    filament_id = resolve_filament_id(shipping)

    slant_items = []
    for it in items or []:
        pfsid = it.get("publicFileServiceId")
        if not pfsid:
            continue
        slant_items.append(
            {
                "type": "PRINT",
                "publicFileServiceId": pfsid,
                "filamentId": filament_id,
                "quantity": int(it.get("quantity", 1)),
                "name": it.get("name", "Krezz Mold"),
                "sku": it.get("SKU") or it.get("sku") or it.get("job_id", ""),
            }
        )

    if not slant_items:
        raise RuntimeError("Order has no valid Slant items (publicFileServiceId missing).")

    customer_details = {
        "details": {
            "email": email,
            "address": {
                "name": full_name,
                "line1": line1,
                "line2": line2,
                "city": city,
                "state": state,
                "zip": zip_code,
                "country": country,
            },
        }
    }

    payload_root = {
        "platformId": pid,
        "customer": customer_details,
        "items": slant_items,
        "metadata": {"internalOrderId": order_id},
    }

    payload_customer = {
        "customer": {**customer_details, "platformId": pid},
        "items": slant_items,
        "metadata": {"internalOrderId": order_id},
    }

    last_err: Optional[Exception] = None

    for label, payload in (("root_platformId", payload_root), ("customer_platformId", payload_customer)):
        r = HTTP.post(
            CFG.slant_orders_endpoint,
            headers=slant_headers({"Content-Type": "application/json"}),
            json=payload,
            timeout=slant_timeout(),
        )

        print(
            "🧪 SLANT_HTTP",
            json.dumps(
                {
                    "where": f"POST /orders (draft) [{label}]",
                    "status": r.status_code,
                    "body_snippet": (r.text or "")[:1400],
                },
                ensure_ascii=False,
            ),
        )

        if r.status_code >= 400:
            last_err = SlantError(
                r.status_code,
                r.text,
                f"Slant POST /orders (draft) [{label}]",
                headers=dict(r.headers),
            )
            continue

        resp = _safe_json(r)

        public_order_id = None
        if isinstance(resp, dict):
            data_obj = resp.get("data")
            if isinstance(data_obj, dict):
                order_obj = data_obj.get("order")
                if isinstance(order_obj, dict):
                    public_order_id = (
                        order_obj.get("publicId")
                        or order_obj.get("publicOrderId")
                        or order_obj.get("id")
                    )

                if not public_order_id:
                    public_order_id = (
                        data_obj.get("publicId")
                        or data_obj.get("publicOrderId")
                        or data_obj.get("id")
                    )

            if not public_order_id:
                public_order_id = (
                    resp.get("publicId")
                    or resp.get("publicOrderId")
                    or resp.get("id")
                )

        if not public_order_id:
            raise RuntimeError(f"Draft succeeded but no public order id returned: {str(resp)[:1600]}")

        print(f"✅ Slant order drafted: publicOrderId={public_order_id} via {label}")
        return str(public_order_id)

    raise last_err or RuntimeError("Slant draft failed for unknown reason.")


def slant_process_order(public_order_id: str) -> dict:
    url1 = f"{CFG.slant_orders_endpoint}/{public_order_id}/process"
    url2 = f"{CFG.slant_orders_endpoint}/{public_order_id}"

    r = HTTP.post(url1, headers=slant_headers(), timeout=slant_timeout())
    if r.status_code == 404:
        r = HTTP.post(url2, headers=slant_headers(), timeout=slant_timeout())

    print(
        "🧪 SLANT_HTTP",
        json.dumps(
            {
                "where": "POST /orders process",
                "status": r.status_code,
                "body_snippet": (r.text or "")[:1400],
            },
            ensure_ascii=False,
        ),
    )

    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant process_order", headers=dict(r.headers))

    return _safe_json(r) if (r.text or "").strip() else {"success": True}


# ----------------------------
# Slant submission (async)
# ----------------------------
def _set_order_status(order_id: str, status: str, extra: Optional[Dict[str, Any]] = None) -> None:
    def _fn(order: Dict[str, Any]):
        order["status"] = status
        order["status_at"] = utc_iso()
        if extra:
            order.update(extra)
        return order, True

    STORE.update(order_id, _fn)


def _set_slant_step(order_id: str, step: str, extra: Optional[Dict[str, Any]] = None) -> None:
    def _fn(order: Dict[str, Any]):
        sl = order.get("slant") or {}
        sl["step"] = step
        sl["step_at"] = utc_iso()
        if extra:
            sl.update(extra)
        order["slant"] = sl
        return order, True

    STORE.update(order_id, _fn)


def _set_slant_failed(order_id: str, err: str, tb: str = "") -> None:
    def _fn(order: Dict[str, Any]):
        order["slant_error"] = err
        order["slant_error_trace"] = (tb or "")[:8000]
        order["status"] = "slant_failed"
        sl = order.get("slant") or {}
        sl["step"] = "failed"
        sl["step_at"] = utc_iso()
        order["slant"] = sl
        return order, True

    STORE.update(order_id, _fn)


def missing_stls_for_items(items: List[dict]) -> List[str]:
    missing: List[str] = []
    for it in items or []:
        jid = (it.get("job_id") or "").strip()
        if not jid:
            continue
        if not stl_exists(jid):
            missing.append(jid)
    return missing


def submit_paid_order_to_slant(order_id: str) -> None:
    order = STORE.get(order_id) or {}
    status = order.get("status")

    if status in ("submitted_to_slant",):
        print(f"🟡 Slant already done for order_id={order_id} status={status}, skipping.")
        return

    if not CFG.slant_enabled:
        raise RuntimeError("Slant disabled: SLANT_API_KEY missing")

    items = order.get("items", []) or []
    shipping = order.get("shipping", {}) or {}
    if not items:
        raise RuntimeError("ORDER_DATA has no items for this order_id (cannot submit).")

    missing = missing_stls_for_items(items)
    if missing:
        raise RuntimeError(f"Missing STL(s) on server for job_id(s): {missing}")

    _set_slant_step(order_id, "uploading_files")
    _set_order_status(order_id, "slant_submitting")

    for it in items:
        job_id = (it.get("job_id") or "").strip()
        if not job_id:
            raise RuntimeError("Item missing job_id")

        if not it.get("publicFileServiceId"):
            it["publicFileServiceId"] = slant_upload_stl(job_id)
            _set_slant_step(
                order_id,
                "file_uploaded",
                {"last_job_id": job_id, "last_publicFileServiceId": it["publicFileServiceId"]},
            )

    def _persist_items(order_obj: Dict[str, Any]):
        order_obj["items"] = items
        return order_obj, True

    STORE.update(order_id, _persist_items)

    _set_slant_step(order_id, "drafting_order")
    public_order_id = slant_draft_order(order_id, shipping, items)

    _set_slant_step(order_id, "processing_order", {"publicOrderId": public_order_id})
    process_resp = slant_process_order(public_order_id)

    def _persist_done(order_obj: Dict[str, Any]):
        sl = order_obj.get("slant") or {}
        sl["publicOrderId"] = public_order_id
        sl["processResponse"] = process_resp
        sl["step"] = "submitted"
        sl["step_at"] = utc_iso()
        order_obj["slant"] = sl
        order_obj["status"] = "submitted_to_slant"
        order_obj["items"] = items
        return order_obj, True

    STORE.update(order_id, _persist_done)

    print(f"✅ Slant submission complete: order_id={order_id} publicOrderId={public_order_id}")


def submit_to_slant_async(order_id: str) -> None:
    def _run():
        print(f"🧵 Slant async started: order_id={order_id}")
        try:
            submit_paid_order_to_slant(order_id)
            print(f"🧵 Slant async finished: order_id={order_id}")
        except Exception as e:
            tb = traceback.format_exc()
            print(f"❌ Slant async exception: {e}\n{tb}")
            _set_slant_failed(order_id, str(e), tb)

    threading.Thread(target=_run, daemon=True).start()


# ----------------------------
# Routes
# ----------------------------
@app.route("/")
def index():
    return "✅ Krezz server is live (Stripe + Slant)."


@app.route("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "time": utc_iso(),
            "version": APP_VERSION,
            "slant_enabled": CFG.slant_enabled,
            "slant_auto_submit": CFG.slant_auto_submit,
            "slant_base_url": CFG.slant_base_url,
            "has_slant_platform_id": bool((CFG.slant_platform_id or "").strip()),
            "has_slant_webhook_secret": bool((CFG.slant_webhook_secret or "").strip()),
            "public_base_url": CFG.public_base_url,
            "upload_dir": CFG.upload_dir,
            "orders": STORE.count(),
            "filaments_cache_ttl_sec": _FILAMENT_CACHE_TTL_SEC,
            "slant_file_url_field": CFG.slant_file_url_field,
            "slant_stl_route": CFG.slant_stl_route,
        }
    )


# --- success/cancel pages ---
@app.route("/success", methods=["GET"])
def success():
    session_id = (request.args.get("session_id") or "").strip()
    order_id = (request.args.get("order_id") or "").strip()

    receipt_url = ""
    try:
        if session_id:
            s = stripe.checkout.Session.retrieve(session_id, expand=["payment_intent.charges"])
            if not order_id:
                order_id = ((s.get("metadata") or {}).get("order_id") or "").strip()

            pi = s.get("payment_intent")
            if isinstance(pi, dict):
                charges = (pi.get("charges") or {}).get("data") or []
                if charges and isinstance(charges[0], dict):
                    receipt_url = charges[0].get("receipt_url") or ""
    except Exception:
        pass

    app_url = f"krezzapp://order-confirmed?order_id={order_id}&session_id={session_id}"

    esc_order = html.escape(order_id or "")
    esc_sess = html.escape(session_id or "")
    esc_app = html.escape(app_url)

    receipt_html = ""
    if receipt_url:
        receipt_html = f'<p><a href="{html.escape(receipt_url)}" target="_blank">View card receipt</a></p>'

    page = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Payment successful</title>
</head>
<body style="font-family:-apple-system,system-ui; padding:24px;">
  <h2>✅ Payment successful</h2>
  <p><b>Order ID:</b> {esc_order if esc_order else "unknown"}</p>
  <p><b>Session:</b><br><code style="word-break:break-all;">{esc_sess}</code></p>

  <p style="margin-top:18px;">
    <a id="openApp" href="{esc_app}" style="font-size:18px;">Open Krezz App</a>
  </p>
  {receipt_html}

  <p style="color:#666; margin-top:18px;">
    If the app doesn’t open automatically, tap “Open Krezz App”.
  </p>

  <script>
    setTimeout(function () {{
      window.location.href = "{esc_app}";
    }}, 350);
  </script>
</body>
</html>
"""
    return make_response(page, 200)


@app.route("/cancel", methods=["GET"])
def cancel():
    order_id = (request.args.get("order_id") or "").strip()

    # ✅ Release quota reservation if user cancels
    if order_id:
        try:
            released = QUOTA.release_reservation(order_id)
            print(f"🧮 QUOTA release (cancel): order_id={order_id} released={released}")
        except Exception as e:
            print(f"🧯 QUOTA release (cancel) error: {e}")

    esc_order = html.escape(order_id)
    page = f"""
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Payment canceled</title></head>
<body style="font-family:-apple-system,system-ui; padding:24px;">
  <h2>❌ Payment canceled</h2>
  <p>Order ID: <b>{esc_order if esc_order else "unknown"}</b></p>
  <p><a href="krezzapp://checkout-canceled?order_id={esc_order}">Return to Krezz App</a></p>
</body></html>
"""
    return make_response(page, 200)



# --- uploads + STL serving ---
@app.route("/upload", methods=["POST"])
def upload_stl():
    job_id = (request.form.get("job_id") or "").strip()
    order_id = (request.form.get("order_id") or "").strip()
    file = request.files.get("file")

    if not job_id or not file:
        return jsonify({"error": "Missing job_id or file"}), 400

    save_path = stl_path_for(job_id)
    file.save(save_path)
    print(f"✅ Uploaded STL job_id={job_id} -> {save_path} order_id={order_id or 'none'}")

    if order_id:

        def _note_upload(order_obj: Dict[str, Any]):
            u = order_obj.get("uploads") or []
            u.append({"job_id": job_id, "path": save_path, "at": utc_iso()})
            order_obj["uploads"] = u[-50:]
            return order_obj, True

        STORE.update(order_id, _note_upload)

        if CFG.slant_enabled and CFG.slant_auto_submit and CFG.auto_submit_on_upload_if_paid:
            order = STORE.get(order_id) or {}
            if order.get("status") == "paid_waiting_for_stl":
                missing = missing_stls_for_items(order.get("items") or [])
                if not missing:
                    print(f"➡️ Upload completed missing STLs resolved; queueing Slant submit: order_id={order_id}")
                    submit_to_slant_async(order_id)

    return jsonify({"success": True, "job_id": job_id, "path": save_path})


def _head_for_file(path: str, content_type: str):
    size = os.path.getsize(path)
    resp = make_response("", 200)
    resp.headers["Content-Type"] = content_type
    resp.headers["Content-Length"] = str(size)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/stl-raw/<job_id>.stl", methods=["GET", "HEAD"])
def serve_stl_raw(job_id: str):
    p = stl_path_for(job_id)
    if not os.path.exists(p):
        return abort(404)

    if request.method == "HEAD":
        return _head_for_file(p, "application/octet-stream")

    resp = send_file(
        p,
        mimetype="application/octet-stream",
        as_attachment=False,
        download_name=f"{job_id}.stl",
        conditional=True,
        etag=True,
    )
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


@app.route("/stl-full/<job_id>.stl", methods=["GET", "HEAD"])
def serve_stl_full(job_id: str):
    p = stl_path_for(job_id)
    if not os.path.exists(p):
        return abort(404)

    if request.method == "HEAD":
        return _head_for_file(p, "model/stl")

    resp = send_file(
        p,
        mimetype="model/stl",
        as_attachment=False,
        download_name=f"{job_id}.stl",
        conditional=True,
        etag=True,
    )
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


@app.route("/debug/stl/info/<job_id>", methods=["GET"])
def debug_stl_info(job_id: str):
    p = stl_path_for(job_id)
    if not os.path.exists(p):
        return jsonify({"ok": False, "error": "not found", "path": p}), 404
    size = os.path.getsize(p)
    route = "stl-full" if CFG.slant_stl_route == "full" else "stl-raw"
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "path": p,
            "size_bytes": size,
            "public_url": f"{CFG.public_base_url}/{route}/{job_id}.stl",
            "slant_stl_route": CFG.slant_stl_route,
        }
    )


# --- checkout session ---
# --- checkout session ---
@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    try:
        data = request.get_json(silent=True) or {}
        print("📥 /create-checkout-session payload:", {"keys": list(data.keys())})

        items = data.get("items", []) or []
        shipping_info = data.get("shippingInfo", {}) or {}
        if not items:
            return jsonify({"error": "No items provided"}), 400

        order_id = (data.get("order_id") or "").strip() or str(uuid.uuid4())

        # ✅ Pull email from shippingInfo (this is what Stripe needs for receipts)
        email = (shipping_info.get("email") or "").strip() or None
        if email:
            shipping_info["email"] = email  # keep it stored too

        normalized_items = []
        for it in items:
            job_id = (it.get("job_id") or it.get("jobId") or "").strip()
            if not job_id:
                return (
                    jsonify(
                        {
                            "error": "Missing job_id on item. You must use the SAME job_id you uploaded as the STL filename.",
                            "hint": "Upload STL first with /upload (job_id), then send that same job_id in /create-checkout-session.",
                        }
                    ),
                    400,
                )

            it["job_id"] = job_id
            it["quantity"] = int(it.get("quantity", 1))
            normalized_items.append(it)

        if CFG.require_stl_before_checkout:
            missing = missing_stls_for_items(normalized_items)
            if missing:
                return (
                    jsonify(
                        {
                            "error": "STL not uploaded yet for one or more items",
                            "missing_job_ids": missing,
                            "hint": "Make sure iOS uses the same job_id for upload AND checkout.",
                        }
                    ),
                    409,
                )
                # ✅ NEW: Daily order cap (reserve a slot BEFORE creating Stripe checkout session)
        q_ok, q_info = QUOTA.reserve(order_id)
        print(f"🧮 QUOTA reserve: order_id={order_id} ok={q_ok} info={q_info}")

        if not q_ok:
            print(f"🚫 DAILY CAP HIT: order_id={order_id} info={q_info}")
            return (
                jsonify(
                    {
                        "error": "Daily order limit reached. Please try again after the reset.",
                        "code": "DAILY_ORDER_LIMIT",
                        "quota": q_info,
                    }
                ),
                429,
            )

        quota_day = (q_info.get("day") or "").strip() or QUOTA.day_key()
        reservation_created = bool(q_info.get("created", False))


        quota_day = (q_info.get("day") or "").strip() or QUOTA.day_key()
        reservation_created = bool(q_info.get("created", False))


        STORE.upsert(
            order_id,
            {
                "items": normalized_items,
                "shipping": shipping_info,
                "status": "created",
                "created_at": utc_iso(),
                "quota_day": quota_day,
                "quota_reserved_at": utc_iso(),
            },
        )


        line_items = [
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": it.get("name", "Beard Mold")},
                    "unit_amount": int(it.get("price", 7500)),
                },
                "quantity": int(it.get("quantity", 1)),
            }
            for it in normalized_items
        ]

        idem_key = f"checkout_{order_id}"

        session_kwargs = dict(
            payment_method_types=["card"],
            mode="payment",
            line_items=line_items,
            success_url=build_success_url(order_id),
            cancel_url=build_cancel_url(order_id),
            metadata={"order_id": order_id},
            client_reference_id=order_id,  # optional but helpful
            idempotency_key=idem_key,
        )

        # ✅ This is the key change for receipts:
        if email:
            session_kwargs["customer_email"] = email
            session_kwargs["payment_intent_data"] = {"receipt_email": email}

        session = stripe.checkout.Session.create(**session_kwargs)

                # ✅ Attach Stripe session details to reservation (so it expires at Stripe's expiry)
        try:
            expires_at = None
            try:
                expires_at = session.get("expires_at")
            except Exception:
                expires_at = getattr(session, "expires_at", None)

            QUOTA.attach_session(order_id, session.get("id") or "", expires_at, day=quota_day)
            print(f"🧮 QUOTA attach_session: order_id={order_id} session={session.get('id')} expires_at={expires_at}")
        except Exception as e:
            print(f"🧯 QUOTA attach_session error: {e}")



        print(f"✅ Created checkout session: {session.id} order_id={order_id} email={email or 'none'}")
        return jsonify({"url": session.url, "order_id": order_id})

    except Exception as e:
        tb = traceback.format_exc()
        print(f"❌ Error in checkout session: {e}\n{tb}")

        # ✅ If we reserved a quota slot in this request, release it on failure
        try:
            if "reservation_created" in locals() and reservation_created and "order_id" in locals():
                QUOTA.release_reservation(order_id)
        except Exception:
            pass

        return jsonify({"error": str(e)}), 500


# --- Stripe webhook ---
@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, CFG.stripe_endpoint_secret)
    except Exception as e:
        print(f"❌ Stripe webhook error: {e}")
        return "Webhook error", 400

    event_type = event.get("type")
    event_id = event.get("id")
    livemode = bool(event.get("livemode", False))
    print(f"📦 Stripe event: {event_type} ({event_id}) livemode={livemode}")

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")
        if not order_id:
            print("❌ Missing order_id in Stripe metadata")
            return jsonify(success=True)

        def _apply_payment(order_obj: Dict[str, Any]):
            seen = order_obj.get("stripe_event_ids") or []
            if event_id in seen:
                return order_obj, False
            order_obj["stripe_event_ids"] = (seen + [event_id])[-20:]

            cd = session.get("customer_details") or {}
            email = (cd.get("email") or session.get("customer_email") or "").strip()

            if not email:
                ship = order_obj.get("shipping") or {}
                email = (ship.get("email") or "").strip()

            if not email:
                email = "unknown"

            order_obj["status"] = "paid"
            order_obj["payment"] = {
                "stripe_session_id": session.get("id"),
                "amount_total": session.get("amount_total"),
                "currency": session.get("currency"),
                "created": datetime.utcfromtimestamp(session["created"]).isoformat() + "Z",
                "email": email,
                "status": "paid",
                "livemode": bool(session.get("livemode", livemode)),
            }

            # ✅ IMPORTANT for your iOS: mark each item as paid
            items = order_obj.get("items") or []
            for it in items:
                it["status"] = "paid"
            order_obj["items"] = items

            return order_obj, True

        updated_order, changed = STORE.update(order_id, _apply_payment)
        print(f"✅ Payment confirmed for order_id: {order_id}")

        # ✅ NEW: mark paid against daily quota (idempotent)
        if changed:
            q_day = (updated_order.get("quota_day") or "").strip() or QUOTA.day_key()
            q_ok, q_info = QUOTA.mark_paid(order_id, session_id=(session.get("id") or ""), day=q_day)
            if not q_ok:
                # Safety catch (should be rare if you reserve before checkout)
                print(f"🟠 Paid but daily cap reached; holding fulfillment. info={q_info}")
                _set_order_status(order_id, "paid_cap_hold", {"quota": q_info})
                _set_slant_step(order_id, "cap_hold", {"quota": q_info})
                return jsonify(success=True)

        if CFG.slant_enabled and CFG.slant_auto_submit:
            if CFG.slant_require_live_stripe and not bool(session.get("livemode", livemode)):
                print("🟡 Blocking Slant auto-submit (Stripe TEST). Set SLANT_REQUIRE_LIVE_STRIPE=false to allow test.")
            else:
                order = STORE.get(order_id) or {}
                missing = missing_stls_for_items(order.get("items") or [])
                if missing:
                    print(f"🟡 Paid but missing STL(s): {missing} -> setting paid_waiting_for_stl")
                    _set_order_status(order_id, "paid_waiting_for_stl", {"missing_stls": missing})
                    _set_slant_step(order_id, "waiting_for_stl", {"missing_stls": missing})
                else:
                    print(f"➡️ Queueing Slant submit: order_id={order_id}")
                    submit_to_slant_async(order_id)
        else:
            print(f"🟡 SLANT_AUTO_SUBMIT={int(CFG.slant_auto_submit)} skipping Slant submission.")

    elif event_type == "checkout.session.expired":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")

        if order_id:
            # ✅ NEW: release quota reservation so it doesn't eat a daily slot forever
            try:
                QUOTA.release_reservation(order_id)
            except Exception:
                pass

            def _mark_expired(order_obj: Dict[str, Any]):
                order_obj["status"] = "checkout_expired"
                order_obj["status_at"] = utc_iso()
                return order_obj, True

            STORE.update(order_id, _mark_expired)

        return jsonify(success=True)

    return jsonify(success=True)



# --- Slant webhook (order.shipped) ---
def verify_slant_webhook_signature(raw_body: bytes) -> Tuple[bool, str]:
    secret = (getattr(CFG, "slant_webhook_secret", "") or "").strip()
    if not secret:
        return False, "SLANT_WEBHOOK_SECRET not set"

    timestamp = (request.headers.get("X-Webhook-Timestamp") or "").strip()
    sig_header = (request.headers.get("X-Webhook-Signature-256") or "").strip()

    if not timestamp or not sig_header:
        return False, "Missing X-Webhook-Timestamp or X-Webhook-Signature-256"

    try:
        ts = int(timestamp)
    except Exception:
        return False, "Bad timestamp"

    # allow ms or seconds
    ts_ms = ts * 1000 if ts < 10_000_000_000 else ts
    now_ms = int(time.time() * 1000)
    age_ms = now_ms - ts_ms

    # ✅ allow small clock skew (up to 30s in the future)
    if age_ms < -30_000 or age_ms > 5 * 60 * 1000:
        return False, "Timestamp too old"

    signed_payload = timestamp.encode("utf-8") + b"." + (raw_body or b"")
    computed = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    # ✅ support "sha256=..." and also multiple values separated by commas
    candidates = [s.strip() for s in sig_header.split(",") if s.strip()]
    for cand in candidates:
        expected = cand.replace("sha256=", "").strip()
        if hmac.compare_digest(computed, expected):
            return True, "ok"

    return False, "Bad signature"


@app.route("/slant/webhook", methods=["POST"])
@app.route("/slant-webhook", methods=["POST"])
def slant_webhook():
    raw = request.get_data(cache=False) or b""

    ok, reason = verify_slant_webhook_signature(raw)
    if not ok:
        print(f"❌ Slant webhook rejected: {reason}")
        return jsonify({"error": reason}), 401

    try:
        event = json.loads(raw.decode("utf-8"))
    except Exception:
        return jsonify({"error": "Bad JSON"}), 400

    event_type = (event.get("event_type") or "").strip()
    if event_type != "order.shipped":
        return jsonify({"ok": True, "ignored": event_type}), 200

    data_obj = event.get("data") or {}
    order_obj = data_obj.get("order") or {}

    slant_public_id = (order_obj.get("public_id") or order_obj.get("publicId") or "").strip()
    tracking_number = (order_obj.get("tracking_number") or "").strip()
    shipment_status = (order_obj.get("shipment_status") or order_obj.get("status") or "SHIPPED").strip()

    if not slant_public_id:
        return jsonify({"ok": True, "warning": "missing public_id"}), 200

    internal_order_id = STORE.find_by_slant_public_order_id(slant_public_id)
    if not internal_order_id:
        print(f"🟡 Slant shipped webhook unmatched: public_id={slant_public_id}")
        return jsonify({"ok": True, "unmatched": True}), 200

    def _apply_shipped(order_state: Dict[str, Any]):
        ful = order_state.get("fulfillment") or {}
        ful["slant_public_id"] = slant_public_id
        ful["status"] = "shipped"
        ful["shipment_status"] = shipment_status
        if tracking_number:
            ful["tracking_number"] = tracking_number
        ful["updated_at"] = utc_iso()
        order_state["fulfillment"] = ful

        # mirror a couple fields into shipping for convenience
        shipping = order_state.get("shipping") or {}
        if tracking_number:
            shipping["tracking_number"] = tracking_number
        shipping["shipment_status"] = shipment_status
        order_state["shipping"] = shipping

        # per-item fulfillment status (do NOT overwrite item["status"] = "paid")
        items = order_state.get("items") or []
        for it in items:
            t = str(it.get("type") or "").lower()
            is_digital = (t == "digital") or bool(it.get("is_digital"))
            if not is_digital:
                it["fulfillment_status"] = "shipped"
        order_state["items"] = items

        return order_state, True

    STORE.update(internal_order_id, _apply_shipped)

    print(f"✅ Slant shipped saved: order_id={internal_order_id} tracking={tracking_number}")
    return jsonify({"ok": True}), 200


# --- order status ---
@app.route("/order-data/<order_id>", methods=["GET"])
def get_order_data(order_id):
    data = STORE.get(order_id)
    if not data:
        return jsonify({"error": "Order ID not found"}), 404
    return jsonify(
        {
            "order_id": order_id,
            "status": data.get("status", "created"),
            "payment": data.get("payment", {}),
            "items": data.get("items", []),
            "shipping": data.get("shipping", {}),
            "uploads": data.get("uploads", []),
            "slant": data.get("slant", {}),
            "slant_error": data.get("slant_error"),
            "slant_error_trace": data.get("slant_error_trace"),
            "missing_stls": data.get("missing_stls"),
            "fulfillment": data.get("fulfillment", {}),  # ✅ NEW
        }
    )


# --- debug helpers ---
@app.route("/debug/slant/ping", methods=["GET"])
def debug_slant_ping():
    try:
        filaments = slant_get_filaments_cached()
        sample = []
        for f in filaments[:5]:
            sample.append(
                {
                    "id": _extract_filament_id(f),
                    "name": _filament_name(f),
                    "profile": _filament_profile(f),
                    "color": _filament_color(f),
                    "available": _filament_available(f),
                }
            )
        return jsonify({"ok": True, "filaments_count": len(filaments), "sample": sample})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/debug/slant/upload/<job_id>", methods=["POST"])
def debug_slant_upload(job_id):
    try:
        pfsid = slant_upload_stl(job_id)
        return jsonify({"ok": True, "job_id": job_id, "publicFileServiceId": pfsid})
    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"ok": False, "error": str(e), "trace": tb[:4000]}), 500


@app.route("/debug/slant/submit/<order_id>", methods=["POST"])
def debug_slant_submit(order_id):
    try:
        submit_paid_order_to_slant(order_id)
        return jsonify({"ok": True})
    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"ok": False, "error": str(e), "trace": tb[:4000]}), 500


@app.route("/debug/order/missing-stl/<order_id>", methods=["GET"])
def debug_order_missing_stl(order_id):
    order = STORE.get(order_id) or {}
    items = order.get("items") or []
    missing = missing_stls_for_items(items)
    return jsonify({"ok": True, "order_id": order_id, "missing_job_ids": missing, "upload_dir": CFG.upload_dir})


if __name__ == "__main__":
    port = int(env_str("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
