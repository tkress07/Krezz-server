from __future__ import annotations

import os
import uuid
import json
import time
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
import stripe
from flask import Flask, request, jsonify, send_file, abort

import fcntl

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

    @staticmethod
    def load() -> "Config":
        stripe_secret_key = env_str("STRIPE_SECRET_KEY")
        stripe_endpoint_secret = env_str("STRIPE_ENDPOINT_SECRET")
        if not stripe_secret_key or not stripe_endpoint_secret:
            raise ValueError("Missing STRIPE_SECRET_KEY and/or STRIPE_ENDPOINT_SECRET")

        public_base_url = env_str("PUBLIC_BASE_URL", "").rstrip("/")
        upload_dir = env_str("UPLOAD_DIR", "/data/uploads")
        os.makedirs(upload_dir, exist_ok=True)

        order_data_path = env_str("ORDER_DATA_PATH", "/data/order_data.json")
        os.makedirs(os.path.dirname(order_data_path), exist_ok=True)

        slant_api_key = env_str("SLANT_API_KEY")
        slant_platform_id = env_str("SLANT_PLATFORM_ID")
        slant_base_url = env_str("SLANT_BASE_URL", "https://slant3dapi.com/v2/api").rstrip("/")

        slant_files_endpoint = env_str("SLANT_FILES_ENDPOINT", f"{slant_base_url}/files")
        slant_filaments_endpoint = env_str("SLANT_FILAMENTS_ENDPOINT", f"{slant_base_url}/filaments")
        slant_orders_endpoint = env_str("SLANT_ORDERS_ENDPOINT", f"{slant_base_url}/orders")

        slant_timeout_sec = int(env_str("SLANT_TIMEOUT_SEC", "30") or 30)

        slant_enabled = bool(slant_api_key)
        slant_debug = env_bool("SLANT_DEBUG", False)
        slant_auto_submit = env_bool("SLANT_AUTO_SUBMIT", False)

        cfg = Config(
            stripe_secret_key=stripe_secret_key,
            stripe_endpoint_secret=stripe_endpoint_secret,
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
        )

        print("✅ Boot config:")
        print("   PUBLIC_BASE_URL:", cfg.public_base_url or "(missing)")
        print("   UPLOAD_DIR:", cfg.upload_dir)
        print("   ORDER_DATA_PATH:", cfg.order_data_path)
        print("   SLANT_ENABLED:", cfg.slant_enabled)
        print("   SLANT_DEBUG:", cfg.slant_debug)
        print("   SLANT_AUTO_SUBMIT:", cfg.slant_auto_submit)
        print("   SLANT_BASE_URL:", cfg.slant_base_url)
        print("   SLANT_API_KEY:", mask_secret(cfg.slant_api_key))
        print("   SLANT_PLATFORM_ID:", mask_secret(cfg.slant_platform_id))
        return cfg

CFG = Config.load()
stripe.api_key = CFG.stripe_secret_key


# ----------------------------
# Persistence
# ----------------------------
ORDER_DATA: Dict[str, Any] = {}

def _locked_open(path: str, mode: str):
    f = open(path, mode)
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    return f

def load_order_data() -> None:
    global ORDER_DATA
    try:
        if not os.path.exists(CFG.order_data_path):
            ORDER_DATA = {}
            print("ℹ️ No prior ORDER_DATA found (starting fresh)")
            return
        with _locked_open(CFG.order_data_path, "r") as f:
            raw = f.read().strip()
            ORDER_DATA = json.loads(raw) if raw else {}
        print(f"✅ Loaded ORDER_DATA ({len(ORDER_DATA)} orders)")
    except Exception as e:
        ORDER_DATA = {}
        print(f"❌ Failed to load ORDER_DATA, starting fresh: {e}")

def save_order_data() -> None:
    try:
        os.makedirs(os.path.dirname(CFG.order_data_path), exist_ok=True)
        dirpath = os.path.dirname(CFG.order_data_path)
        fd, tmp_path = tempfile.mkstemp(prefix="order_data_", suffix=".json", dir=dirpath)
        with os.fdopen(fd, "w") as tmp:
            json.dump(ORDER_DATA, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, CFG.order_data_path)
    except Exception as e:
        print(f"❌ Failed to persist ORDER_DATA: {e}")

load_order_data()


# ----------------------------
# Slant client
# ----------------------------
class SlantError(RuntimeError):
    def __init__(self, status: int, body: str, where: str):
        super().__init__(f"{where}: status={status} body={body[:1600]}")
        self.status = status
        self.body = body
        self.where = where

def slant_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    pid = (CFG.slant_platform_id or "").strip()
    h = {
        "Authorization": f"Bearer {CFG.slant_api_key}",
        "Accept": "application/json",
    }
    # Many Slant endpoints are happier with this present.
    if pid:
        h["X-Platform-Id"] = pid
    if extra:
        h.update(extra)
    return h

def slant_params() -> Dict[str, str]:
    pid = (CFG.slant_platform_id or "").strip()
    return {"platformId": pid} if pid else {}

_FILAMENT_CACHE = {"ts": 0.0, "data": None}
_FILAMENT_CACHE_TTL_SEC = 600

def slant_get_filaments_cached() -> List[dict]:
    now = time.time()
    if _FILAMENT_CACHE["data"] is not None and (now - _FILAMENT_CACHE["ts"]) < _FILAMENT_CACHE_TTL_SEC:
        return _FILAMENT_CACHE["data"]

    r = requests.get(
        CFG.slant_filaments_endpoint,
        headers=slant_headers(),
        params=slant_params(),
        timeout=CFG.slant_timeout_sec
    )
    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant get_filaments")

    payload = r.json() if r.text else {}
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

    raise RuntimeError("No filament available (Slant filaments list returned none with publicId).")

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

    payload = {
        "platformId": pid,
        "name": f"{job_id}.stl",
        "type": "STL",
        "URL": stl_url,
        "url": stl_url,
        "fileUrl": stl_url,
        "fileURL": stl_url,
        "downloadUrl": stl_url,
    }

    if CFG.slant_debug:
        print("🧪 Slant create file:", {"endpoint": CFG.slant_files_endpoint, "platformId": pid, "URL": stl_url})

    r = requests.post(
        CFG.slant_files_endpoint,
        params=slant_params(),
        headers=slant_headers({"Content-Type": "application/json"}),
        json=payload,
        timeout=CFG.slant_timeout_sec,
    )
    if r.status_code >= 400:
        print(f"❌ Slant /files error: {r.status_code} {r.text}")
        raise SlantError(r.status_code, r.text, "Slant create_file_by_url")

    resp = r.json() if r.text else {}
    pfsid = parse_slant_file_public_id(resp)
    print(f"✅ Slant file created: job_id={job_id} publicFileServiceId={pfsid}")
    return pfsid

def slant_upload_stl(job_id: str, stl_path: str) -> str:
    if not os.path.exists(stl_path):
        raise RuntimeError(f"STL not found on server: {stl_path}")

    if not CFG.public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL is missing. Set it so Slant can download /stl/<job>.stl")

    stl_url = f"{CFG.public_base_url}/stl/{job_id}.stl"
    return slant_create_file_by_url(job_id, stl_url)

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
        raise RuntimeError(
            "Order has no valid Slant items. Each item must have publicFileServiceId. "
            "Run /debug/slant/upload/<job_id> first (or ensure auto-upload runs)."
        )

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

    r = requests.post(
        CFG.slant_orders_endpoint,
        params=slant_params(),
        headers=slant_headers({"Content-Type": "application/json"}),
        json=payload,
        timeout=CFG.slant_timeout_sec,
    )
    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant POST /orders (draft)")

    resp = r.json() if r.text else {}
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

    r = requests.post(url1, params=slant_params(), headers=slant_headers(), timeout=CFG.slant_timeout_sec)
    if r.status_code == 404:
        r = requests.post(url2, params=slant_params(), headers=slant_headers(), timeout=CFG.slant_timeout_sec)

    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant process_order")
    return r.json() if r.text else {"success": True}

def submit_paid_order_to_slant(order_id: str) -> None:
    data = ORDER_DATA.get(order_id) or {}
    items = data.get("items", []) or []
    shipping = data.get("shipping", {}) or {}

    if not items:
        raise RuntimeError("ORDER_DATA has no items for this order_id (cannot submit).")

    if not CFG.slant_enabled:
        raise RuntimeError("Slant disabled: SLANT_API_KEY missing")

    # upload files if needed
    for it in items:
        job_id = it.get("job_id")
        if not job_id:
            raise RuntimeError("Item missing job_id")

        if not it.get("publicFileServiceId"):
            stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
            it["publicFileServiceId"] = slant_upload_stl(job_id, stl_path)

    public_order_id = slant_draft_order(order_id, shipping, items)
    data.setdefault("slant", {})
    data["slant"]["publicOrderId"] = public_order_id
    data["status"] = "slant_drafted"
    ORDER_DATA[order_id] = data
    save_order_data()

    process_resp = slant_process_order(public_order_id)
    data["slant"]["processResponse"] = process_resp
    data["status"] = "submitted_to_slant"
    ORDER_DATA[order_id] = data
    save_order_data()

    print(f"✅ Slant submission complete: order_id={order_id}")


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
        "slant_enabled": CFG.slant_enabled,
        "slant_auto_submit": CFG.slant_auto_submit,
        "slant_base_url": CFG.slant_base_url,
        "has_slant_platform_id": bool((CFG.slant_platform_id or "").strip()),
        "public_base_url": CFG.public_base_url or None,
        "upload_dir": CFG.upload_dir,
        "orders": len(ORDER_DATA),
    })

@app.route("/debug/env")
def debug_env():
    pid = (CFG.slant_platform_id or "").strip()
    return jsonify({
        "SLANT_ENABLED": CFG.slant_enabled,
        "SLANT_DEBUG": CFG.slant_debug,
        "SLANT_AUTO_SUBMIT": CFG.slant_auto_submit,
        "SLANT_BASE_URL": CFG.slant_base_url,
        "SLANT_FILES_ENDPOINT": CFG.slant_files_endpoint,
        "SLANT_ORDERS_ENDPOINT": CFG.slant_orders_endpoint,
        "PUBLIC_BASE_URL": CFG.public_base_url or "",
        "has_SLANT_API_KEY": bool(CFG.slant_api_key),
        "SLANT_API_KEY_masked": mask_secret(CFG.slant_api_key, keep=4),
        "has_SLANT_PLATFORM_ID": bool(pid),
        "SLANT_PLATFORM_ID_len": len(pid),
        "SLANT_PLATFORM_ID_masked": mask_secret(pid, keep=4),
    })

@app.route("/debug/slant/ping", methods=["GET"])
def debug_slant_ping():
    try:
        # simplest “auth + platform” check we have access to
        filaments = slant_get_filaments_cached()
        return jsonify({"ok": True, "filaments_count": len(filaments)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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

@app.route("/stl/<job_id>.stl", methods=["GET"])
def serve_stl(job_id):
    stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        return abort(404)
    return send_file(stl_path, mimetype="model/stl", as_attachment=False, download_name=f"{job_id}.stl")

@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    try:
        data = request.get_json(silent=True) or {}
        print("📥 /create-checkout-session payload:", data)

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

        ORDER_DATA[order_id] = {
            "items": normalized_items,
            "shipping": shipping_info,
            "status": "created",
            "created_at": utc_iso(),
        }
        save_order_data()

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
            success_url=f"krezzapp://order-confirmed?order_id={order_id}",
            cancel_url="https://krezzapp.com/cancel",
            metadata={"order_id": order_id},
        )

        print(f"✅ Created checkout session: {session.id} order_id={order_id}")
        return jsonify({"url": session.url, "order_id": order_id})

    except Exception as e:
        print(f"❌ Error in checkout session: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, CFG.stripe_endpoint_secret)
    except Exception as e:
        print(f"❌ Stripe webhook error: {e}")
        return "Webhook error", 400

    print(f"📦 Stripe event: {event['type']}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")

        if not order_id:
            print("❌ Missing order_id in Stripe metadata")
            return jsonify(success=True)

        if order_id not in ORDER_DATA:
            ORDER_DATA[order_id] = {"items": [], "shipping": {}, "status": "created"}

        ORDER_DATA[order_id]["status"] = "paid"
        ORDER_DATA[order_id]["payment"] = {
            "stripe_session_id": session.get("id"),
            "amount_total": session.get("amount_total"),
            "currency": session.get("currency"),
            "created": datetime.utcfromtimestamp(session["created"]).isoformat() + "Z",
            "email": session.get("customer_email", "unknown"),
            "status": "paid",
        }
        save_order_data()
        print(f"✅ Payment confirmed for order_id: {order_id}")

        if CFG.slant_enabled and CFG.slant_auto_submit:
            try:
                print(f"➡️ Submitting to Slant: order_id={order_id}")
                submit_paid_order_to_slant(order_id)
            except Exception as e:
                print(f"❌ Slant submit exception: {e}")
                ORDER_DATA[order_id]["slant_error"] = str(e)
                ORDER_DATA[order_id]["status"] = "slant_failed"
                save_order_data()
        else:
            print(f"🟡 SLANT_AUTO_SUBMIT={int(CFG.slant_auto_submit)}, skipping Slant submission (testing mode).")

    return jsonify(success=True)

@app.route("/order-data/<order_id>", methods=["GET"])
def get_order_data(order_id):
    data = ORDER_DATA.get(order_id)
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
    })

@app.route("/debug/slant/filaments", methods=["GET"])
def debug_slant_filaments():
    try:
        filaments = slant_get_filaments_cached()
        return jsonify({"ok": True, "count": len(filaments), "data": filaments})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/debug/slant/upload/<job_id>", methods=["POST"])
def debug_slant_upload(job_id):
    try:
        stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
        pfsid = slant_upload_stl(job_id, stl_path)
        return jsonify({"ok": True, "job_id": job_id, "publicFileServiceId": pfsid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/debug/slant/submit/<order_id>", methods=["POST"])
def debug_slant_submit(order_id):
    try:
        submit_paid_order_to_slant(order_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(env_str("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
