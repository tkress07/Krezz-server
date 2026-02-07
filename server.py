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
    # Stripe
    stripe_secret_key: str
    stripe_endpoint_secret: str
    stripe_success_url: str
    stripe_cancel_url: str
    stripe_livemode_required: bool

    # Server
    public_base_url: str
    upload_dir: str
    order_data_path: str

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

    # Slant file create
    slant_file_url_field: str  # usually "URL"

    @staticmethod
    def load() -> "Config":
        # Stripe
        stripe_secret_key = env_str("STRIPE_SECRET_KEY")
        stripe_endpoint_secret = env_str("STRIPE_ENDPOINT_SECRET")
        if not stripe_secret_key or not stripe_endpoint_secret:
            raise ValueError("Missing STRIPE_SECRET_KEY and/or STRIPE_ENDPOINT_SECRET")

        public_base_url = env_str("PUBLIC_BASE_URL", "").rstrip("/")
        if not public_base_url.startswith("https://"):
            # You want https for Stripe + Slant fetch
            raise ValueError("PUBLIC_BASE_URL must be https://...")

        stripe_success_url = env_str(
            "STRIPE_SUCCESS_URL",
            f"{public_base_url}/success?session_id={{CHECKOUT_SESSION_ID}}",
        )
        stripe_cancel_url = env_str(
            "STRIPE_CANCEL_URL",
            f"{public_base_url}/cancel",
        )
        stripe_livemode_required = env_bool("STRIPE_LIVEMODE_REQUIRED", True)

        upload_dir = env_str("UPLOAD_DIR", "/data/uploads")
        os.makedirs(upload_dir, exist_ok=True)

        order_data_path = env_str("ORDER_DATA_PATH", "/data/order_data.json")
        os.makedirs(os.path.dirname(order_data_path), exist_ok=True)

        # Slant
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

        slant_file_url_field = env_str("SLANT_FILE_URL_FIELD", "URL") or "URL"

        cfg = Config(
            stripe_secret_key=stripe_secret_key,
            stripe_endpoint_secret=stripe_endpoint_secret,
            stripe_success_url=stripe_success_url,
            stripe_cancel_url=stripe_cancel_url,
            stripe_livemode_required=stripe_livemode_required,
            public_base_url=public_base_url,
            upload_dir=upload_dir,
            order_data_path=order_data_path,
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
            slant_file_url_field=slant_file_url_field,
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
        print("   SLANT_DEBUG:", cfg.slant_debug)
        print("   SLANT_AUTO_SUBMIT:", cfg.slant_auto_submit)
        print("   SLANT_REQUIRE_LIVE_STRIPE:", cfg.slant_require_live_stripe)
        print("   SLANT_FILE_URL_FIELD:", cfg.slant_file_url_field)
        print("   SLANT_BASE_URL:", cfg.slant_base_url)
        print("   SLANT_TIMEOUT_SEC:", cfg.slant_timeout_sec)
        print("   SLANT_API_KEY:", mask_secret(cfg.slant_api_key))
        print("   SLANT_PLATFORM_ID:", mask_secret(cfg.slant_platform_id))
        return cfg

CFG = Config.load()
stripe.api_key = CFG.stripe_secret_key

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "KrezzServer/2.1"})

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
# Slant client
# ----------------------------
class SlantError(RuntimeError):
    def __init__(self, status: int, body: str, where: str, headers: Optional[Dict[str, str]] = None):
        mini = {}
        if headers:
            mini = {k: v for k, v in headers.items() if k.lower() in ("content-type", "x-request-id", "cf-ray")}
        super().__init__(f"{where}: status={status} headers={mini} body={body[:1800]}")
        self.status = status
        self.body = body
        self.where = where
        self.headers = mini

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

    probe = stl_probe(stl_url)
    print("🧪 STL PROBE", json.dumps(probe, ensure_ascii=False, default=str))

    url_field = CFG.slant_file_url_field or "URL"

    # IMPORTANT: only send the field Slant explicitly asks for: "URL"
    payload = {
        "platformId": pid,
        "name": f"{job_id}.stl",
        url_field: stl_url,
        "type": "STL",
    }

    # Retry on 5xx (Slant sometimes throws 500 while fetching)
    last_resp: Optional[requests.Response] = None
    for attempt in range(1, 4):
        r = HTTP.post(
            CFG.slant_files_endpoint,
            headers=slant_headers({"Content-Type": "application/json"}),
            json=payload,
            timeout=slant_timeout(),
        )
        last_resp = r

        mini_headers = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "x-request-id", "cf-ray")}
        print("🧪 SLANT_HTTP", json.dumps({
            "where": "Slant POST /files",
            "attempt": attempt,
            "status": r.status_code,
            "mini_headers": mini_headers,
            "body_snippet": (r.text or "")[:1400],
            "sent_keys": list(payload.keys()),
        }, ensure_ascii=False, default=str))

        if r.status_code < 400:
            resp = _safe_json(r)
            pfsid = parse_slant_file_public_id(resp)
            print(f"✅ Slant file created: job_id={job_id} publicFileServiceId={pfsid}")
            return pfsid

        # retry only on 5xx
        if r.status_code >= 500:
            time.sleep(1.5 * attempt)
            continue

        # non-5xx: stop (it’s a validation/auth issue)
        raise SlantError(r.status_code, r.text, "Slant POST /files", headers=dict(r.headers))

    if last_resp is None:
        raise SlantError(500, "No response", "Slant POST /files (no response)")
    raise SlantError(last_resp.status_code, last_resp.text, "Slant POST /files (retries exhausted)", headers=dict(last_resp.headers))

def slant_upload_stl(job_id: str, stl_path: str) -> str:
    if not os.path.exists(stl_path):
        raise RuntimeError(f"STL not found on server: {stl_path}")
    stl_url = f"{CFG.public_base_url}/stl-full/{job_id}.stl"
    return slant_create_file_by_url(job_id, stl_url)

# ----------------------------
# Async Slant submission (file only demo)
# ----------------------------
def submit_to_slant_async(order_id: str, job_id: str) -> None:
    def _run():
        print(f"🧵 Slant async started: order_id={order_id} job_id={job_id}")
        try:
            stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
            pfsid = slant_upload_stl(job_id, stl_path)
            STORE.update(order_id, lambda o: (dict(o, slant_file={"publicFileServiceId": pfsid}), True))
            print(f"🧵 Slant async finished: order_id={order_id}")
        except Exception as e:
            tb = traceback.format_exc()
            print(f"❌ Slant async exception: {e}\n{tb}")
            STORE.update(order_id, lambda o: (dict(o, slant_error=str(e), slant_error_trace=tb[:8000]), True))
    threading.Thread(target=_run, daemon=True).start()

# ----------------------------
# Routes
# ----------------------------
@app.route("/")
def index():
    return "✅ Krezz server is live (Stripe + Webhook + STL)."

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "time": utc_iso(),
        "public_base_url": CFG.public_base_url,
        "orders": STORE.count(),
        "slant_enabled": CFG.slant_enabled,
        "slant_auto_submit": CFG.slant_auto_submit,
    })

@app.route("/success")
def success():
    session_id = request.args.get("session_id", "")
    return f"<html><body><h3>✅ Payment success</h3><p>session_id={session_id}</p></body></html>"

@app.route("/cancel")
def cancel():
    return "<html><body><h3>❌ Checkout canceled</h3></body></html>"

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

# STL endpoint (served as octet-stream for max compatibility)
@app.route("/stl-full/<job_id>.stl", methods=["GET", "HEAD"])
def serve_stl_full(job_id: str):
    stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        return abort(404)

    resp = send_file(
        stl_path,
        mimetype="application/octet-stream",
        as_attachment=False,
        conditional=True,
        etag=True,
        last_modified=None,
    )
    # Remove content-disposition if Flask adds it
    resp.headers.pop("Content-Disposition", None)
    resp.headers["Cache-Control"] = "no-store"
    return resp

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
                "unit_amount": int(it.get("price", 7500)),
            },
            "quantity": int(it.get("quantity", 1)),
        } for it in normalized_items]

        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=line_items,
            success_url=CFG.stripe_success_url,
            cancel_url=CFG.stripe_cancel_url,
            metadata={"order_id": order_id},
        )

        print(f"✅ Created checkout session: {session.id} order_id={order_id} livemode={bool(session.get('livemode', False))}")
        return jsonify({"url": session.url, "order_id": order_id})

    except Exception as e:
        tb = traceback.format_exc()
        print(f"❌ Error in checkout session: {e}\n{tb}")
        return jsonify({"error": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, CFG.stripe_endpoint_secret)
    except Exception as e:
        print(f"❌ Stripe webhook signature error: {e}")
        return "Webhook error", 400

    event_type = event.get("type")
    event_id = event.get("id")
    livemode = bool(event.get("livemode", False))
    print(f"📦 Stripe event: {event_type} ({event_id}) livemode={livemode}")

    # Safety: ignore test events if you require live
    if CFG.stripe_livemode_required and not livemode:
        print("🟡 Ignoring TEST webhook because STRIPE_LIVEMODE_REQUIRED=true")
        return jsonify(success=True)

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")

        if not order_id:
            print("❌ Missing order_id in Stripe metadata")
            return jsonify(success=True)

        def _apply_paid(order_obj: Dict[str, Any]):
            seen = order_obj.get("stripe_event_ids") or []
            if event_id in seen:
                return order_obj, False
            order_obj["stripe_event_ids"] = (seen + [event_id])[-30:]

            order_obj["status"] = "paid"
            order_obj["payment"] = {
                "stripe_session_id": session.get("id"),
                "amount_total": session.get("amount_total"),
                "currency": session.get("currency"),
                "created": datetime.utcfromtimestamp(session["created"]).isoformat() + "Z",
                "email": session.get("customer_email") or "unknown",
                "status": "paid",
                "livemode": bool(session.get("livemode", livemode)),
            }
            return order_obj, True

        STORE.update(order_id, _apply_paid)
        print(f"✅ Payment marked paid for order_id={order_id}")

        # Demo: kick off Slant file upload for the first item (optional)
        if CFG.slant_enabled and CFG.slant_auto_submit:
            first_job = None
            order = STORE.get(order_id) or {}
            its = order.get("items", []) or []
            if its:
                first_job = its[0].get("job_id")
            if first_job:
                print(f"➡️ Queueing Slant file upload: order_id={order_id} job_id={first_job}")
                submit_to_slant_async(order_id, first_job)

    return jsonify(success=True)

@app.route("/order-data/<order_id>", methods=["GET"])
def get_order_data(order_id: str):
    data = STORE.get(order_id)
    if not data:
        return jsonify({"error": "Order ID not found"}), 404
    return jsonify({"order_id": order_id, **data})

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

if __name__ == "__main__":
    port = int(env_str("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
