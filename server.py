from __future__ import annotations

import os
import uuid
import json
import time
import tempfile
import threading
import traceback
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import fcntl
import requests
import stripe
from flask import Flask, request, jsonify, send_file, abort, Response

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

def env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
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
    stripe_livemode_required: bool

    public_base_url: str
    upload_dir: str
    order_data_path: str

    stripe_success_url: str
    stripe_cancel_url: str
    app_deeplink_scheme: str  # e.g. "krezzapp"

    # Slant
    slant_api_key: str
    slant_platform_id: str
    slant_base_url: str
    slant_files_endpoint: str
    slant_filaments_endpoint: str
    slant_orders_endpoint: str
    slant_timeout_sec: int

    slant_enabled: bool
    slant_debug: bool
    slant_auto_submit: bool

    slant_require_live_stripe: bool
    slant_allow_test_stripe: bool
    slant_test_order_allowlist: List[str]

    slant_file_url_field: str         # e.g. "URL"
    slant_files_payload_mode: str     # "json" or "form" or "auto"

    @property
    def stripe_is_live_key(self) -> bool:
        return (self.stripe_secret_key or "").startswith("sk_live_")

    @staticmethod
    def load() -> "Config":
        stripe_secret_key = env_str("STRIPE_SECRET_KEY")
        stripe_endpoint_secret = env_str("STRIPE_ENDPOINT_SECRET")
        if not stripe_secret_key or not stripe_endpoint_secret:
            raise ValueError("Missing STRIPE_SECRET_KEY and/or STRIPE_ENDPOINT_SECRET")

        stripe_livemode_required = env_bool("STRIPE_LIVEMODE_REQUIRED", True)

        public_base_url = env_str("PUBLIC_BASE_URL", "").rstrip("/")
        if not public_base_url:
            raise ValueError("Missing PUBLIC_BASE_URL (required for Stripe success/cancel URLs)")

        upload_dir = env_str("UPLOAD_DIR", "/data/uploads")
        os.makedirs(upload_dir, exist_ok=True)

        order_data_path = env_str("ORDER_DATA_PATH", "/data/order_data.json")
        os.makedirs(os.path.dirname(order_data_path), exist_ok=True)

        app_deeplink_scheme = env_str("APP_DEEPLINK_SCHEME", "krezzapp").strip() or "krezzapp"

        # Stripe Checkout requires HTTPS URLs. Use our /success page, then deep-link back into the app.
        stripe_success_url = env_str(
            "STRIPE_SUCCESS_URL",
            f"{public_base_url}/success?session_id={{CHECKOUT_SESSION_ID}}",
        )
        stripe_cancel_url = env_str(
            "STRIPE_CANCEL_URL",
            f"{public_base_url}/cancel",
        )

        # Slant config
        slant_api_key = env_str("SLANT_API_KEY")
        slant_platform_id = env_str("SLANT_PLATFORM_ID")

        slant_base_url = env_str("SLANT_BASE_URL", "https://slant3dapi.com/v2/api").rstrip("/")
        slant_files_endpoint = env_str("SLANT_FILES_ENDPOINT", f"{slant_base_url}/files")
        slant_filaments_endpoint = env_str("SLANT_FILAMENTS_ENDPOINT", f"{slant_base_url}/filaments")
        slant_orders_endpoint = env_str("SLANT_ORDERS_ENDPOINT", f"{slant_base_url}/orders")

        slant_timeout_sec = env_int("SLANT_TIMEOUT_SEC", 240)

        slant_enabled = bool(slant_api_key)
        slant_debug = env_bool("SLANT_DEBUG", False)
        slant_auto_submit = env_bool("SLANT_AUTO_SUBMIT", False)

        slant_require_live_stripe = env_bool("SLANT_REQUIRE_LIVE_STRIPE", True)

        # Backward compat with your older env var name:
        # If SLANT_SUBMIT_ON_TEST_STRIPE=true, treat that as "allow test stripe".
        compat_submit_on_test = env_bool("SLANT_SUBMIT_ON_TEST_STRIPE", False)
        slant_allow_test_stripe = env_bool("SLANT_ALLOW_TEST_STRIPE", False) or compat_submit_on_test

        allowlist_raw = env_str("SLANT_TEST_ORDER_ALLOWLIST", "")
        slant_test_order_allowlist = [x.strip() for x in allowlist_raw.split(",") if x.strip()]

        slant_file_url_field = env_str("SLANT_FILE_URL_FIELD", "URL") or "URL"
        slant_files_payload_mode = env_str("SLANT_FILES_PAYLOAD_MODE", "auto").lower()
        if slant_files_payload_mode not in ("auto", "json", "form"):
            slant_files_payload_mode = "auto"

        cfg = Config(
            stripe_secret_key=stripe_secret_key,
            stripe_endpoint_secret=stripe_endpoint_secret,
            stripe_livemode_required=stripe_livemode_required,
            public_base_url=public_base_url,
            upload_dir=upload_dir,
            order_data_path=order_data_path,
            stripe_success_url=stripe_success_url,
            stripe_cancel_url=stripe_cancel_url,
            app_deeplink_scheme=app_deeplink_scheme,

            slant_api_key=slant_api_key,
            slant_platform_id=slant_platform_id,
            slant_base_url=slant_base_url,
            slant_files_endpoint=slant_files_endpoint,
            slant_filaments_endpoint=slant_filaments_endpoint,
            slant_orders_endpoint=slant_orders_endpoint,
            slant_timeout_sec=slant_timeout_sec,
            slant_enabled=slant_enabled,
            slant_debug=slant_debug,
            slant_auto_submit=slant_auto_submit,
            slant_require_live_stripe=slant_require_live_stripe,
            slant_allow_test_stripe=slant_allow_test_stripe,
            slant_test_order_allowlist=slant_test_order_allowlist,
            slant_file_url_field=slant_file_url_field,
            slant_files_payload_mode=slant_files_payload_mode,
        )

        print("✅ Boot config:")
        print("   PUBLIC_BASE_URL:", cfg.public_base_url)
        print("   STRIPE_SUCCESS_URL:", cfg.stripe_success_url)
        print("   STRIPE_CANCEL_URL:", cfg.stripe_cancel_url)
        print("   STRIPE_LIVEMODE_REQUIRED:", cfg.stripe_livemode_required)
        print("   STRIPE_SECRET_KEY:", mask_secret(cfg.stripe_secret_key))
        print("   STRIPE_ENDPOINT_SECRET:", mask_secret(cfg.stripe_endpoint_secret))
        print("   UPLOAD_DIR:", cfg.upload_dir)
        print("   ORDER_DATA_PATH:", cfg.order_data_path)

        print("   SLANT_ENABLED:", cfg.slant_enabled)
        print("   SLANT_AUTO_SUBMIT:", cfg.slant_auto_submit)
        print("   SLANT_REQUIRE_LIVE_STRIPE:", cfg.slant_require_live_stripe)
        print("   SLANT_ALLOW_TEST_STRIPE:", cfg.slant_allow_test_stripe)
        print("   SLANT_FILE_URL_FIELD:", cfg.slant_file_url_field)
        print("   SLANT_FILES_PAYLOAD_MODE:", cfg.slant_files_payload_mode)

        if cfg.stripe_livemode_required and not cfg.stripe_is_live_key:
            raise ValueError("STRIPE_LIVEMODE_REQUIRED=true but STRIPE_SECRET_KEY is not sk_live_...")

        return cfg

CFG = Config.load()
stripe.api_key = CFG.stripe_secret_key

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "KrezzServer/2.0"})

# ----------------------------
# Order storage (safe across workers)
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
# Slant client (optional)
# ----------------------------
class SlantError(RuntimeError):
    def __init__(self, status: int, body: str, where: str, headers: Optional[Dict[str, str]] = None):
        mini = {}
        if headers:
            mini = {k: v for k, v in headers.items() if k.lower() in ("content-type", "x-request-id", "cf-ray")}
        super().__init__(f"{where}: status={status} headers={mini} body={body[:1600]}")
        self.status = status
        self.body = body
        self.where = where

def slant_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "Accept": "application/json",
        "Authorization": f"Bearer {CFG.slant_api_key}",
        "api-key": CFG.slant_api_key,
    }
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

def stl_probe(url: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"url": url}
    try:
        hr = HTTP.head(url, timeout=(10, 20), allow_redirects=True)
        out.update({"head_status": hr.status_code, "head_len": hr.headers.get("Content-Length"), "head_type": hr.headers.get("Content-Type")})
    except Exception as e:
        out["head_error"] = str(e)
    try:
        gr = HTTP.get(url, timeout=(10, 30), allow_redirects=True)
        out.update({"get_status": gr.status_code, "get_len": gr.headers.get("Content-Length"), "get_type": gr.headers.get("Content-Type")})
    except Exception as e:
        out["get_error"] = str(e)
    return out

def _post_slant_files(payload: Dict[str, Any]) -> requests.Response:
    mode = CFG.slant_files_payload_mode
    if mode == "json":
        return HTTP.post(
            CFG.slant_files_endpoint,
            headers=slant_headers({"Content-Type": "application/json"}),
            json=payload,
            timeout=slant_timeout(),
        )
    if mode == "form":
        return HTTP.post(
            CFG.slant_files_endpoint,
            headers=slant_headers(),
            data={k: str(v) for k, v in payload.items()},
            timeout=slant_timeout(),
        )

    # auto: try JSON first, then form if Slant complains about required field
    r = HTTP.post(
        CFG.slant_files_endpoint,
        headers=slant_headers({"Content-Type": "application/json"}),
        json=payload,
        timeout=slant_timeout(),
    )
    if r.status_code == 400 and ("required field" in (r.text or "").lower() or "URL is a required field" in (r.text or "")):
        r = HTTP.post(
            CFG.slant_files_endpoint,
            headers=slant_headers(),
            data={k: str(v) for k, v in payload.items()},
            timeout=slant_timeout(),
        )
    return r

def parse_slant_file_public_id(payload: dict) -> str:
    data_obj = payload.get("data") if isinstance(payload, dict) else None
    public_id = None
    if isinstance(data_obj, dict):
        public_id = data_obj.get("publicFileServiceId") or data_obj.get("publicId")
    if not public_id and isinstance(payload, dict):
        public_id = payload.get("publicFileServiceId") or payload.get("publicId")
    if not public_id:
        raise RuntimeError(f"Slant response missing file public id: {str(payload)[:1200]}")
    return public_id

def slant_create_file_by_url(job_id: str, stl_url: str) -> str:
    pid = (CFG.slant_platform_id or "").strip()
    if not pid:
        raise RuntimeError("SLANT_PLATFORM_ID is missing/blank at runtime.")

    if CFG.slant_debug:
        print("🧪 STL PROBE", json.dumps(stl_probe(stl_url), ensure_ascii=False, default=str))

    url_field = CFG.slant_file_url_field or "URL"
    base = {"platformId": pid, "name": f"{job_id}.stl", "type": "STL"}

    attempts: List[Dict[str, Any]] = []
    p1 = dict(base); p1[url_field] = stl_url; attempts.append(p1)
    for extra_field in ("fileURL", "fileUrl", "url", "URL"):
        p = dict(base); p[extra_field] = stl_url; attempts.append(p)

    last_err = None
    for i, payload in enumerate(attempts, start=1):
        r = _post_slant_files(payload)
        if CFG.slant_debug:
            print("🧪 Slant /files attempt", i, "status", r.status_code, "body", (r.text or "")[:800])
        if r.status_code < 400:
            resp = _safe_json(r)
            pfsid = parse_slant_file_public_id(resp)
            print(f"✅ Slant file created: job_id={job_id} publicFileServiceId={pfsid}")
            return pfsid
        last_err = f"status={r.status_code} body={(r.text or '')[:800]}"

    raise SlantError(500, last_err or "Unknown error", "Slant create_file_by_url exhausted attempts")

def slant_upload_stl(job_id: str, stl_path: str) -> str:
    if not os.path.exists(stl_path):
        raise RuntimeError(f"STL not found on server: {stl_path}")
    stl_url = f"{CFG.public_base_url}/stl-full/{job_id}.stl"
    return slant_create_file_by_url(job_id, stl_url)

# Filaments cache
_FILAMENT_CACHE = {"ts": 0.0, "data": None}
_FILAMENT_CACHE_TTL_SEC = 600

def slant_get_filaments_cached() -> List[dict]:
    now = time.time()
    if _FILAMENT_CACHE["data"] is not None and (now - _FILAMENT_CACHE["ts"]) < _FILAMENT_CACHE_TTL_SEC:
        return _FILAMENT_CACHE["data"]

    r = HTTP.get(CFG.slant_filaments_endpoint, headers=slant_headers(), timeout=slant_timeout())
    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant get_filaments", headers=dict(r.headers))

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

    email = shipping.get("email") or "unknown@example.com"
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

    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant process_order", headers=dict(r.headers))

    return _safe_json(r) if (r.text or "").strip() else {"success": True}

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

    for it in items:
        job_id = it.get("job_id")
        if not job_id:
            raise RuntimeError("Item missing job_id")
        if not it.get("publicFileServiceId"):
            stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
            it["publicFileServiceId"] = slant_upload_stl(job_id, stl_path)
            def _persist_items(order_obj: Dict[str, Any]):
                order_obj["items"] = items
                order_obj["status"] = "slant_files_uploaded"
                return order_obj, True
            STORE.update(order_id, _persist_items)

    public_order_id = slant_draft_order(order_id, shipping, items)

    def _persist_draft(order_obj: Dict[str, Any]):
        order_obj["slant"] = {"publicOrderId": public_order_id, "at": utc_iso()}
        order_obj["status"] = "slant_drafted"
        order_obj["items"] = items
        return order_obj, True
    STORE.update(order_id, _persist_draft)

    process_resp = slant_process_order(public_order_id)

    def _persist_processed(order_obj: Dict[str, Any]):
        sl = order_obj.get("slant") or {}
        sl["processResponse"] = process_resp
        sl["submitted_at"] = utc_iso()
        order_obj["slant"] = sl
        order_obj["status"] = "submitted_to_slant"
        return order_obj, True
    STORE.update(order_id, _persist_processed)

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
            def _fail(order_obj: Dict[str, Any]):
                order_obj["status"] = "slant_failed"
                order_obj["slant_error"] = str(e)
                order_obj["slant_error_trace"] = tb[:8000]
                return order_obj, True
            STORE.update(order_id, _fail)
    threading.Thread(target=_run, daemon=True).start()

# ----------------------------
# Routes
# ----------------------------
@app.route("/")
def index():
    return "✅ Krezz server is live (Stripe Checkout + optional Slant)."

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "time": utc_iso(),
        "stripe_key_live": CFG.stripe_is_live_key,
        "slant_enabled": CFG.slant_enabled,
        "slant_auto_submit": CFG.slant_auto_submit,
        "public_base_url": CFG.public_base_url,
        "upload_dir": CFG.upload_dir,
        "orders": STORE.count(),
    })

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

# 200-only STL endpoint
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
    out: Dict[str, Any] = {"ok": True, "job_id": job_id, "path": stl_path, "size_bytes": size}

    try:
        with open(stl_path, "rb") as f:
            header = f.read(80)
            tri_count_bytes = f.read(4)
        if len(tri_count_bytes) == 4:
            tri_count = struct.unpack("<I", tri_count_bytes)[0]
            expect = 84 + tri_count * 50
            out["binary_stl_triangles"] = tri_count
            out["binary_stl_expected_size"] = expect
            out["binary_stl_size_match"] = (expect == size)
            out["header_preview"] = header[:32].decode("latin-1", errors="replace")
    except Exception as e:
        out["binary_stl_check_error"] = str(e)

    out["public_url"] = f"{CFG.public_base_url}/stl-full/{job_id}.stl"
    return jsonify(out)

@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    try:
        data = request.get_json(silent=True) or {}

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
                "unit_amount": int(it.get("price", 7500)),  # cents
            },
            "quantity": int(it.get("quantity", 1)),
        } for it in normalized_items]

        # Helpful: if you collect email in-app, pass it to Stripe
        customer_email = shipping_info.get("email")

        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=line_items,
            success_url=CFG.stripe_success_url,
            cancel_url=CFG.stripe_cancel_url,
            customer_email=customer_email if customer_email else None,
            metadata={"order_id": order_id},
            client_reference_id=order_id,
        )

        print(f"✅ Created checkout session: {session.id} order_id={order_id} livemode={session.get('livemode')}")
        return jsonify({"url": session.url, "order_id": order_id})

    except Exception as e:
        tb = traceback.format_exc()
        print(f"❌ Error in checkout session: {e}\n{tb}")
        return jsonify({"error": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()  # raw bytes
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, CFG.stripe_endpoint_secret)
    except Exception as e:
        print(f"❌ Stripe webhook signature error: {e}")
        return "Webhook error", 400

    event_type = event.get("type")
    event_id = event.get("id")
    livemode = bool(event.get("livemode", False))
    print(f"📦 Stripe event: {event_type} ({event_id}) livemode={livemode}")

    def mark_paid(session_obj: Dict[str, Any]):
        order_id = (session_obj.get("metadata") or {}).get("order_id") or session_obj.get("client_reference_id")
        if not order_id:
            print("❌ Missing order_id in Stripe session metadata/client_reference_id")
            return None

        def _apply(order_obj: Dict[str, Any]):
            seen = order_obj.get("stripe_event_ids") or []
            if event_id in seen:
                return order_obj, False
            order_obj["stripe_event_ids"] = (seen + [event_id])[-30:]

            order_obj["status"] = "paid"
            order_obj["payment"] = {
                "stripe_session_id": session_obj.get("id"),
                "payment_status": session_obj.get("payment_status"),
                "amount_total": session_obj.get("amount_total"),
                "currency": session_obj.get("currency"),
                "created": datetime.utcfromtimestamp(session_obj["created"]).isoformat() + "Z",
                "email": session_obj.get("customer_details", {}).get("email") or session_obj.get("customer_email"),
                "livemode": bool(session_obj.get("livemode", livemode)),
            }
            return order_obj, True

        STORE.update(order_id, _apply)
        print(f"✅ Payment marked paid for order_id={order_id}")
        return order_id

    # Handle events
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session = event["data"]["object"]
        # Only mark paid if payment_status is actually paid
        if session.get("payment_status") == "paid":
            order_id = mark_paid(session)
            if order_id and CFG.slant_enabled and CFG.slant_auto_submit:
                is_live = bool(session.get("livemode", livemode))
                if CFG.slant_require_live_stripe and not is_live:
                    if CFG.slant_allow_test_stripe or (order_id in CFG.slant_test_order_allowlist):
                        print(f"➡️ Queueing Slant submit (TEST allowlisted): order_id={order_id}")
                        submit_to_slant_async(order_id)
                    else:
                        print("🟡 Blocking Slant auto-submit because Stripe is TEST mode.")
                else:
                    print(f"➡️ Queueing Slant submit: order_id={order_id}")
                    submit_to_slant_async(order_id)
        else:
            print(f"🟡 checkout.session event but payment_status={session.get('payment_status')} (not paid yet)")

    if event_type == "checkout.session.async_payment_failed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id") or session.get("client_reference_id")
        if order_id:
            def _fail(order_obj: Dict[str, Any]):
                order_obj["status"] = "payment_failed"
                order_obj["payment"] = {
                    "stripe_session_id": session.get("id"),
                    "payment_status": session.get("payment_status"),
                    "livemode": bool(session.get("livemode", livemode)),
                    "failed_at": utc_iso(),
                }
                return order_obj, True
            STORE.update(order_id, _fail)
            print(f"❌ Payment failed for order_id={order_id}")

    return jsonify(success=True)

@app.route("/success", methods=["GET"])
def success_page():
    """
    Stripe redirects here (HTTPS), we retrieve session -> order_id -> deep link into the app.
    """
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return Response("Missing session_id", status=400)

    try:
        sess = stripe.checkout.Session.retrieve(session_id)
        order_id = (sess.get("metadata") or {}).get("order_id") or sess.get("client_reference_id") or ""
        deep = f"{CFG.app_deeplink_scheme}://order-confirmed?order_id={order_id}&session_id={session_id}"

        html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Order Confirmed</title>
  </head>
  <body style="font-family: -apple-system, system-ui, Arial; padding: 24px;">
    <h2>✅ Payment received</h2>
    <p>Opening Krezz…</p>
    <p style="margin-top:16px;">
      If nothing happens, tap:
      <a href="{deep}">Open Krezz App</a>
    </p>
    <script>
      window.location.href = "{deep}";
      setTimeout(function() {{
        document.querySelector("p").textContent = "If the app didn’t open, use the link below.";
      }}, 1200);
    </script>
  </body>
</html>"""
        return Response(html, mimetype="text/html")
    except Exception as e:
        return Response(f"Could not verify session: {e}", status=500)

@app.route("/cancel", methods=["GET"])
def cancel_page():
    return Response(
        "<h3>Payment canceled.</h3><p>You can close this page and return to the app.</p>",
        mimetype="text/html",
    )

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

@app.route("/debug/slant/ping", methods=["GET"])
def debug_slant_ping():
    if not CFG.slant_enabled:
        return jsonify({"ok": False, "error": "Slant disabled"}), 400
    try:
        filaments = slant_get_filaments_cached()
        return jsonify({"ok": True, "filaments_count": len(filaments)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/debug/slant/submit/<order_id>", methods=["POST"])
def debug_slant_submit(order_id):
    if not CFG.slant_enabled:
        return jsonify({"ok": False, "error": "Slant disabled"}), 400
    try:
        submit_paid_order_to_slant(order_id)
        return jsonify({"ok": True})
    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"ok": False, "error": str(e), "trace": tb[:4000]}), 500

if __name__ == "__main__":
    port = int(env_str("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
