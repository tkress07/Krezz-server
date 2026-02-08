from __future__ import annotations

import os
import uuid
import json
import time
import html
import tempfile
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import fcntl
import requests
import stripe
from flask import Flask, request, jsonify, send_file, abort, make_response

APP_VERSION = "KrezzServer/1.5"

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

    slant_file_url_field: str          # usually "URL"
    slant_stl_route: str               # "raw" or "full"
    slant_send_bearer: bool            # send Authorization: Bearer

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

        # Stripe redirect URLs (server pages)
        # You can override in Render env vars if desired.
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

        slant_file_url_field = env_str("SLANT_FILE_URL_FIELD", "URL")  # your logs show "URL"
        slant_stl_route = env_str("SLANT_STL_ROUTE", "raw").lower()    # "raw" recommended
        slant_send_bearer = env_bool("SLANT_SEND_BEARER", True)        # MUST be True to avoid 401

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
        )

        print("✅ Boot config:")
        print("   PUBLIC_BASE_URL:", cfg.public_base_url)
        print("   UPLOAD_DIR:", cfg.upload_dir)
        print("   ORDER_DATA_PATH:", cfg.order_data_path)
        print("   STRIPE_SUCCESS_URL:", cfg.stripe_success_url_tmpl)
        print("   STRIPE_CANCEL_URL:", cfg.stripe_cancel_url_tmpl)
        print("   SLANT_ENABLED:", cfg.slant_enabled)
        print("   SLANT_DEBUG:", cfg.slant_debug)
        print("   SLANT_AUTO_SUBMIT:", cfg.slant_auto_submit)
        print("   SLANT_REQUIRE_LIVE_STRIPE:", cfg.slant_require_live_stripe)
        print("   SLANT_BASE_URL:", cfg.slant_base_url)
        print("   SLANT_FILES_ENDPOINT:", cfg.slant_files_endpoint)
        print("   SLANT_TIMEOUT_SEC:", cfg.slant_timeout_sec)
        print("   SLANT_FILE_URL_FIELD:", cfg.slant_file_url_field)
        print("   SLANT_STL_ROUTE:", cfg.slant_stl_route)
        print("   SLANT_SEND_BEARER:", cfg.slant_send_bearer)
        print("   SLANT_API_KEY:", mask_secret(cfg.slant_api_key))
        print("   SLANT_PLATFORM_ID:", mask_secret(cfg.slant_platform_id))
        return cfg

CFG = Config.load()
stripe.api_key = CFG.stripe_secret_key

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": APP_VERSION})

def build_success_url(order_id: str) -> str:
    return CFG.stripe_success_url_tmpl.replace("{ORDER_ID}", order_id)

def build_cancel_url(order_id: str) -> str:
    return CFG.stripe_cancel_url_tmpl.replace("{ORDER_ID}", order_id)

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

STORE = OrderStore(CFG.order_data_path)

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
    h: Dict[str, str] = {"Accept": "application/json"}
    if CFG.slant_api_key:
        # Slant explicitly told you Bearer is required (your 401 error)
        if CFG.slant_send_bearer:
            h["Authorization"] = f"Bearer {CFG.slant_api_key}"
        # Some endpoints also accept api-key; safe to include
        h["api-key"] = CFG.slant_api_key

    pid = (CFG.slant_platform_id or "").strip()
    if pid:
        h["X-Platform-Id"] = pid

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
    # handle multiple shapes
    data_obj = payload.get("data") if isinstance(payload, dict) else None
    candidates = []
    if isinstance(data_obj, dict):
        candidates += [
            data_obj.get("publicFileServiceId"),
            data_obj.get("publicId"),
            data_obj.get("id"),
        ]
    candidates += [
        payload.get("publicFileServiceId") if isinstance(payload, dict) else None,
        payload.get("publicId") if isinstance(payload, dict) else None,
        payload.get("id") if isinstance(payload, dict) else None,
    ]
    for c in candidates:
        if c:
            return str(c)
    raise RuntimeError(f"Slant response missing file id: {str(payload)[:1200]}")

def stl_probe_head(url: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"url": url}
    try:
        hr = HTTP.head(url, timeout=(10, 20), allow_redirects=True)
        out.update({
            "head_status": hr.status_code,
            "head_len": hr.headers.get("Content-Length"),
            "head_type": hr.headers.get("Content-Type"),
        })
    except Exception as e:
        out["head_error"] = str(e)
    return out

def slant_create_file_by_url(job_id: str, stl_url: str) -> str:
    pid = (CFG.slant_platform_id or "").strip()
    if not pid:
        raise RuntimeError("SLANT_PLATFORM_ID is missing/blank at runtime.")

    probe = stl_probe_head(stl_url)
    print("🧪 STL PROBE", json.dumps(probe, ensure_ascii=False, default=str))

    payload = {
        "platformId": pid,
        "name": f"{job_id}.stl",
        "filename": f"{job_id}.stl",
        # The key that mattered in your earlier logs:
        CFG.slant_file_url_field: stl_url,  # typically "URL"
    }

    print("🧪 Slant create file request", json.dumps({
        "endpoint": CFG.slant_files_endpoint,
        "payload_keys": list(payload.keys()),
        "url_field": CFG.slant_file_url_field,
        "stl_url": stl_url
    }, ensure_ascii=False))

    r = HTTP.post(
        CFG.slant_files_endpoint,
        headers=slant_headers({"Content-Type": "application/json"}),
        json=payload,
        timeout=slant_timeout(),
    )

    # ALWAYS log response
    print("🧪 SLANT_HTTP", json.dumps({
        "where": "POST /files",
        "status": r.status_code,
        "headers": {k: v for k, v in r.headers.items()},
        "body_snippet": (r.text or "")[:1400],
    }, ensure_ascii=False, default=str))

    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant POST /files", headers=dict(r.headers))

    resp = _safe_json(r)
    file_id = parse_slant_file_public_id(resp)
    print(f"✅ Slant file created: job_id={job_id} publicFileServiceId={file_id}")
    return file_id

def slant_upload_stl(job_id: str, stl_path: str) -> str:
    if not os.path.exists(stl_path):
        raise RuntimeError(f"STL not found on server: {stl_path}")

    if not CFG.public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL missing (needed so Slant can fetch STL).")

    route = "stl-raw" if CFG.slant_stl_route == "raw" else "stl-full"
    stl_url = f"{CFG.public_base_url}/{route}/{job_id}.stl"
    return slant_create_file_by_url(job_id, stl_url)

# --- Filaments cache ---
_FILAMENT_CACHE = {"ts": 0.0, "data": None}
_FILAMENT_CACHE_TTL_SEC = 600

def slant_get_filaments_cached() -> List[dict]:
    now = time.time()
    if _FILAMENT_CACHE["data"] is not None and (now - _FILAMENT_CACHE["ts"]) < _FILAMENT_CACHE_TTL_SEC:
        return _FILAMENT_CACHE["data"]

    r = HTTP.get(CFG.slant_filaments_endpoint, headers=slant_headers(), timeout=slant_timeout())
    _slant_log("SLANT_HTTP", {
        "where": "GET /filaments",
        "status": r.status_code,
        "body_snippet": (r.text or "")[:1200],
    })
    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant GET /filaments", headers=dict(r.headers))

    payload = _safe_json(r)
    data = payload.get("data") or []
    _FILAMENT_CACHE["ts"] = now
    _FILAMENT_CACHE["data"] = data
    return data

def resolve_filament_id(shipping_info: dict) -> str:
    material = (shipping_info.get("material") or "").upper()
    color = (shipping_info.get("color") or "").strip().lower()
    want_profile = "PETG" if "PETG" in material else "PLA"

    filaments = slant_get_filaments_cached()

    for f in filaments:
        if not f.get("available", True):
            continue
        if (f.get("profile") or "").upper() == want_profile and (f.get("color") or "").lower() == color:
            if f.get("publicId"):
                return f["publicId"]

    for f in filaments:
        if not f.get("available", True):
            continue
        if (f.get("profile") or "").upper() == want_profile and f.get("publicId"):
            return f["publicId"]

    if filaments and filaments[0].get("publicId"):
        return filaments[0]["publicId"]

    raise RuntimeError("No filament available (Slant filaments returned none with publicId).")

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

    missing = [k for k, v in {
        "line1": line1, "city": city, "state": state, "zip": zip_code, "country": country, "email": email
    }.items() if not str(v).strip()]
    if missing:
        raise RuntimeError(f"Shipping info missing required fields: {missing}")

    filament_id = resolve_filament_id(shipping)

    slant_items = []
    for it in items:
        pfsid = it.get("publicFileServiceId")
        if not pfsid:
            continue
        slant_items.append({
            "type": "PRINT",
            "publicFileServiceId": pfsid,
            "filamentId": filament_id,
            "quantity": int(it.get("quantity", 1)),
            "name": it.get("name", "Krezz Mold"),
            "SKU": it.get("SKU") or it.get("sku") or it.get("job_id", ""),
        })

    if not slant_items:
        raise RuntimeError("Order has no valid Slant items (publicFileServiceId missing).")

    payload = {
        "platformId": pid,
        "customer": {
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
                }
            }
        },
        "items": slant_items,
        "metadata": {"internalOrderId": order_id},
    }

    r = HTTP.post(
        CFG.slant_orders_endpoint,
        headers=slant_headers({"Content-Type": "application/json"}),
        json=payload,
        timeout=slant_timeout(),
    )

    print("🧪 SLANT_HTTP", json.dumps({
        "where": "POST /orders (draft)",
        "status": r.status_code,
        "body_snippet": (r.text or "")[:1400],
    }, ensure_ascii=False, default=str))

    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant POST /orders (draft)", headers=dict(r.headers))

    resp = _safe_json(r)
    data_obj = resp.get("data") if isinstance(resp, dict) else None
    public_order_id = None
    if isinstance(data_obj, dict):
        public_order_id = data_obj.get("publicId") or data_obj.get("publicOrderId")
    if not public_order_id and isinstance(resp, dict):
        public_order_id = resp.get("publicId") or resp.get("publicOrderId")

    if not public_order_id:
        raise RuntimeError(f"Draft succeeded but no public order id returned: {str(resp)[:1600]}")

    print(f"✅ Slant order drafted: publicOrderId={public_order_id}")
    return public_order_id

def slant_process_order(public_order_id: str) -> dict:
    url1 = f"{CFG.slant_orders_endpoint}/{public_order_id}/process"
    url2 = f"{CFG.slant_orders_endpoint}/{public_order_id}"

    r = HTTP.post(url1, headers=slant_headers(), timeout=slant_timeout())
    if r.status_code == 404:
        r = HTTP.post(url2, headers=slant_headers(), timeout=slant_timeout())

    print("🧪 SLANT_HTTP", json.dumps({
        "where": "POST /orders process",
        "status": r.status_code,
        "body_snippet": (r.text or "")[:1400],
    }, ensure_ascii=False, default=str))

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

    _set_slant_step(order_id, "uploading_files")
    _set_order_status(order_id, "slant_submitting")

    for it in items:
        job_id = it.get("job_id")
        if not job_id:
            raise RuntimeError("Item missing job_id")

        if not it.get("publicFileServiceId"):
            stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
            it["publicFileServiceId"] = slant_upload_stl(job_id, stl_path)

            _set_slant_step(order_id, "file_uploaded", {
                "last_job_id": job_id,
                "last_publicFileServiceId": it["publicFileServiceId"],
            })

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
    return jsonify({
        "ok": True,
        "time": utc_iso(),
        "version": APP_VERSION,
        "slant_enabled": CFG.slant_enabled,
        "slant_auto_submit": CFG.slant_auto_submit,
        "slant_base_url": CFG.slant_base_url,
        "has_slant_platform_id": bool((CFG.slant_platform_id or "").strip()),
        "public_base_url": CFG.public_base_url,
        "upload_dir": CFG.upload_dir,
        "orders": STORE.count(),
    })

# --- success/cancel pages ---
@app.route("/success", methods=["GET"])
def success():
    session_id = (request.args.get("session_id") or "").strip()
    order_id = (request.args.get("order_id") or "").strip()

    receipt_url = ""
    # Fallback: if order_id missing, try to pull from Stripe session metadata
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

    # HTML-escape user-controlled strings
    esc_order = html.escape(order_id or "")
    esc_sess = html.escape(session_id or "")
    esc_app  = html.escape(app_url)

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
    // iOS often blocks automatic deep-links unless the user taps,
    // but we still try once.
    setTimeout(function() {{
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
    job_id = request.form.get("job_id")
    file = request.files.get("file")
    if not job_id or not file:
        return jsonify({"error": "Missing job_id or file"}), 400

    save_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
    file.save(save_path)
    print(f"✅ Uploaded STL job_id={job_id} -> {save_path}")
    return jsonify({"success": True, "path": save_path})

# RAW (recommended for Slant): octet-stream, full response, no range/conditional
@app.route("/stl-raw/<job_id>.stl", methods=["GET", "HEAD"])
def serve_stl_raw(job_id: str):
    stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        return abort(404)

    resp = send_file(
        stl_path,
        mimetype="application/octet-stream",
        as_attachment=False,
        download_name=f"{job_id}.stl",
        conditional=False,
        etag=False,
        last_modified=None,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp

# FULL: model/stl
@app.route("/stl-full/<job_id>.stl", methods=["GET", "HEAD"])
def serve_stl_full(job_id: str):
    stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        return abort(404)

    resp = send_file(
        stl_path,
        mimetype="model/stl",
        as_attachment=False,
        download_name=f"{job_id}.stl",
        conditional=False,
        etag=False,
        last_modified=None,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/debug/stl/info/<job_id>", methods=["GET"])
def debug_stl_info(job_id: str):
    stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        return jsonify({"ok": False, "error": "not found"}), 404

    size = os.path.getsize(stl_path)
    route = "stl-raw" if CFG.slant_stl_route == "raw" else "stl-full"
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "path": stl_path,
        "size_bytes": size,
        "public_url": f"{CFG.public_base_url}/{route}/{job_id}.stl",
    })

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

        order_id = data.get("order_id") or str(uuid.uuid4())

        normalized_items = []
        for it in items:
            job_id = it.get("job_id") or it.get("jobId") or it.get("id") or str(uuid.uuid4())
            it["job_id"] = job_id
            it["quantity"] = int(it.get("quantity", 1))
            normalized_items.append(it)

        STORE.upsert(order_id, {
            "items": normalized_items,
            "shipping": shipping_info,
            "status": "created",
            "created_at": utc_iso(),
        })

        line_items = [{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": it.get("name", "Beard Mold")},
                "unit_amount": int(it.get("price", 7500)),
            },
            "quantity": int(it.get("quantity", 1)),
        } for it in normalized_items]

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=line_items,
            # IMPORTANT: Stripe redirects here after payment
            success_url=build_success_url(order_id),
            cancel_url=build_cancel_url(order_id),
            metadata={"order_id": order_id},
        )

        print(f"✅ Created checkout session: {session.id} order_id={order_id}")
        return jsonify({"url": session.url, "order_id": order_id})

    except Exception as e:
        tb = traceback.format_exc()
        print(f"❌ Error in checkout session: {e}\n{tb}")
        return jsonify({"error": str(e)}), 500

# --- webhook ---
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

            order_obj["status"] = "paid"
            order_obj["payment"] = {
                "stripe_session_id": session.get("id"),
                "amount_total": session.get("amount_total"),
                "currency": session.get("currency"),
                "created": datetime.utcfromtimestamp(session["created"]).isoformat() + "Z",
                "email": session.get("customer_email", "unknown"),
                "status": "paid",
                "livemode": bool(session.get("livemode", livemode)),
            }
            return order_obj, True

        STORE.update(order_id, _apply_payment)
        print(f"✅ Payment confirmed for order_id: {order_id}")

        # Slant gate
        if CFG.slant_enabled and CFG.slant_auto_submit:
            if CFG.slant_require_live_stripe and not bool(session.get("livemode", livemode)):
                print("🟡 Blocking Slant auto-submit (Stripe TEST). Set SLANT_REQUIRE_LIVE_STRIPE=false to allow test.")
            else:
                print(f"➡️ Queueing Slant submit: order_id={order_id}")
                submit_to_slant_async(order_id)
        else:
            print(f"🟡 SLANT_AUTO_SUBMIT={int(CFG.slant_auto_submit)} skipping Slant submission.")

    return jsonify(success=True)

# --- order status ---
@app.route("/order-data/<order_id>", methods=["GET"])
def get_order_data(order_id):
    data = STORE.get(order_id)
    if not data:
        return jsonify({"error": "Order ID not found"}), 404
    return jsonify({
        "order_id": order_id,
        "status": data.get("status", "created"),
        "payment": data.get("payment", {}),
        "items": data.get("items", []),
        "shipping": data.get("shipping", {}),
        "slant": data.get("slant", {}),
        "slant_error": data.get("slant_error"),
        "slant_error_trace": data.get("slant_error_trace"),
    })

# --- debug helpers ---
@app.route("/debug/slant/ping", methods=["GET"])
def debug_slant_ping():
    try:
        filaments = slant_get_filaments_cached()
        return jsonify({"ok": True, "filaments_count": len(filaments)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/debug/slant/upload/<job_id>", methods=["POST"])
def debug_slant_upload(job_id):
    try:
        stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
        pfsid = slant_upload_stl(job_id, stl_path)
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

if __name__ == "__main__":
    port = int(env_str("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
