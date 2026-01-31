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

# Linux file-locking (Render is Linux)
import fcntl

app = Flask(__name__)

# ============================================================
# Small utils
# ============================================================
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


def is_uuid_like(s: str) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except Exception:
        return False


# ============================================================
# Config
# ============================================================
@dataclass(frozen=True)
class Config:
    # Stripe
    stripe_secret_key: str
    stripe_endpoint_secret: str

    # Storage
    upload_dir: str
    order_data_path: str

    # Slant
    slant_api_key: str
    slant_platform_id: str
    slant_base_url: str
    slant_timeout_sec: int
    slant_debug: bool
    slant_auto_submit: bool

    # Derived endpoints (per current Slant OpenAPI)
    slant_filaments_endpoint: str
    slant_orders_endpoint: str
    slant_files_direct_upload_endpoint: str
    slant_files_confirm_upload_endpoint: str

    @staticmethod
    def load() -> "Config":
        stripe_secret_key = env_str("STRIPE_SECRET_KEY")
        stripe_endpoint_secret = env_str("STRIPE_ENDPOINT_SECRET")
        if not stripe_secret_key or not stripe_endpoint_secret:
            raise ValueError("❌ Missing STRIPE_SECRET_KEY and/or STRIPE_ENDPOINT_SECRET")

        upload_dir = env_str("UPLOAD_DIR", "/data/uploads")
        os.makedirs(upload_dir, exist_ok=True)

        order_data_path = env_str("ORDER_DATA_PATH", "/data/order_data.json")
        os.makedirs(os.path.dirname(order_data_path), exist_ok=True)

        slant_api_key = env_str("SLANT_API_KEY")
        slant_platform_id = env_str("SLANT_PLATFORM_ID")
        slant_base_url = env_str("SLANT_BASE_URL", "https://slant3dapi.com/v2/api").rstrip("/")

        slant_timeout_sec = int(env_str("SLANT_TIMEOUT_SEC", "60") or 60)
        slant_debug = env_bool("SLANT_DEBUG", False)

        # IMPORTANT: set SLANT_AUTO_SUBMIT="1" on Render for launch
        slant_auto_submit = env_bool("SLANT_AUTO_SUBMIT", False)

        # Endpoints per spec
        slant_filaments_endpoint = f"{slant_base_url}/filaments"
        slant_orders_endpoint = f"{slant_base_url}/orders"
        slant_files_direct_upload_endpoint = f"{slant_base_url}/files/direct-upload"
        slant_files_confirm_upload_endpoint = f"{slant_base_url}/files/confirm-upload"

        cfg = Config(
            stripe_secret_key=stripe_secret_key,
            stripe_endpoint_secret=stripe_endpoint_secret,
            upload_dir=upload_dir,
            order_data_path=order_data_path,
            slant_api_key=slant_api_key,
            slant_platform_id=slant_platform_id,
            slant_base_url=slant_base_url,
            slant_timeout_sec=slant_timeout_sec,
            slant_debug=slant_debug,
            slant_auto_submit=slant_auto_submit,
            slant_filaments_endpoint=slant_filaments_endpoint,
            slant_orders_endpoint=slant_orders_endpoint,
            slant_files_direct_upload_endpoint=slant_files_direct_upload_endpoint,
            slant_files_confirm_upload_endpoint=slant_files_confirm_upload_endpoint,
        )

        print("✅ Boot config:")
        print("   UPLOAD_DIR:", cfg.upload_dir)
        print("   ORDER_DATA_PATH:", cfg.order_data_path)
        print("   SLANT_BASE_URL:", cfg.slant_base_url)
        print("   SLANT_DEBUG:", cfg.slant_debug)
        print("   SLANT_AUTO_SUBMIT:", cfg.slant_auto_submit)
        print("   SLANT_API_KEY present:", bool(cfg.slant_api_key), "masked:", mask_secret(cfg.slant_api_key))
        print("   SLANT_PLATFORM_ID present:", bool(cfg.slant_platform_id), "len:", len(cfg.slant_platform_id or ""))

        return cfg


CFG = Config.load()
stripe.api_key = CFG.stripe_secret_key

# ============================================================
# Order data persistence (cross-process safe on Linux)
# ============================================================
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

# ============================================================
# Slant client
# ============================================================
class SlantError(RuntimeError):
    def __init__(self, status: int, body: str, where: str):
        super().__init__(f"{where}: status={status} body={body[:1600]}")
        self.status = status
        self.body = body
        self.where = where


def slant_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "Authorization": f"Bearer {CFG.slant_api_key}",
        "Accept": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def slant_parse_json(resp: requests.Response, where: str) -> dict:
    text = resp.text or ""
    if resp.status_code >= 400:
        raise SlantError(resp.status_code, text, where)
    try:
        payload = resp.json() if text else {}
    except Exception:
        payload = {}
    # Slant wraps many responses like: { success, message, data }
    if isinstance(payload, dict) and payload.get("success") is False:
        raise SlantError(resp.status_code, text, where)
    return payload if isinstance(payload, dict) else {}


_FILAMENT_CACHE = {"ts": 0.0, "data": None}
_FILAMENT_CACHE_TTL_SEC = 600  # 10 minutes


def slant_get_filaments_cached() -> List[dict]:
    now = time.time()
    if _FILAMENT_CACHE["data"] is not None and (now - _FILAMENT_CACHE["ts"]) < _FILAMENT_CACHE_TTL_SEC:
        return _FILAMENT_CACHE["data"]

    r = requests.get(CFG.slant_filaments_endpoint, headers=slant_headers(), timeout=CFG.slant_timeout_sec)
    payload = slant_parse_json(r, "Slant GET /filaments")
    data = payload.get("data") or []
    if not isinstance(data, list):
        data = []
    _FILAMENT_CACHE["ts"] = now
    _FILAMENT_CACHE["data"] = data
    return data


def resolve_filament_id(shipping_info: dict) -> str:
    material = (shipping_info.get("material") or "").upper()
    color = (shipping_info.get("color") or "").strip().lower()
    want_profile = "PETG" if "PETG" in material else "PLA"

    filaments = slant_get_filaments_cached()

    # Exact match
    for f in filaments:
        if not f.get("available", True):
            continue
        if (f.get("profile") or "").upper() == want_profile and (f.get("color") or "").lower() == color:
            pid = f.get("publicId")
            if pid:
                return pid

    # Fallback: any filament with the right profile
    for f in filaments:
        if not f.get("available", True):
            continue
        if (f.get("profile") or "").upper() == want_profile and f.get("publicId"):
            return f["publicId"]

    raise RuntimeError("No filament available for your platform/material selection.")


def slant_direct_upload_file(job_id: str, stl_path: str, owner_id: Optional[str] = None) -> str:
    """
    Official flow:
      1) POST /files/direct-upload  -> presignedUrl + filePlaceholder
      2) PUT STL to presignedUrl
      3) POST /files/confirm-upload -> returns File with publicFileServiceId
    """
    if not CFG.slant_api_key or not (CFG.slant_platform_id or "").strip():
        raise RuntimeError("Slant is not configured (missing SLANT_API_KEY or SLANT_PLATFORM_ID).")

    pid = (CFG.slant_platform_id or "").strip()
    if not is_uuid_like(pid):
        raise RuntimeError("SLANT_PLATFORM_ID must be a UUID.")

    if not os.path.exists(stl_path):
        raise RuntimeError(f"STL not found: {stl_path}")

    # 1) Request presigned URL
    init_payload = {
        "name": f"{job_id}.stl",
        "platformId": pid,
    }
    if owner_id:
        init_payload["ownerId"] = owner_id

    if CFG.slant_debug:
        print("🧪 Slant direct-upload init:", {"endpoint": CFG.slant_files_direct_upload_endpoint, "platformId": pid})

    r1 = requests.post(
        CFG.slant_files_direct_upload_endpoint,
        headers=slant_headers({"Content-Type": "application/json"}),
        json=init_payload,
        timeout=CFG.slant_timeout_sec,
    )
    p1 = slant_parse_json(r1, "Slant POST /files/direct-upload")
    data1 = p1.get("data") or {}
    presigned_url = data1.get("presignedUrl")
    file_placeholder = data1.get("filePlaceholder")

    if not presigned_url or not file_placeholder:
        raise RuntimeError(f"Slant direct-upload did not return presignedUrl/filePlaceholder: {str(p1)[:900]}")

    # 2) PUT bytes to S3 presigned URL
    with open(stl_path, "rb") as f:
        put_resp = requests.put(
            presigned_url,
            data=f,
            headers={"Content-Type": "model/stl"},
            timeout=CFG.slant_timeout_sec,
        )
    if put_resp.status_code >= 400:
        # fallback content-type (some presigned URLs don't want model/stl)
        with open(stl_path, "rb") as f2:
            put_resp2 = requests.put(
                presigned_url,
                data=f2,
                headers={"Content-Type": "application/octet-stream"},
                timeout=CFG.slant_timeout_sec,
            )
        if put_resp2.status_code >= 400:
            raise RuntimeError(f"Presigned PUT failed: {put_resp.status_code}/{put_resp2.status_code}")

    # 3) Confirm upload
    confirm_payload = {"filePlaceholder": file_placeholder}
    r3 = requests.post(
        CFG.slant_files_confirm_upload_endpoint,
        headers=slant_headers({"Content-Type": "application/json"}),
        json=confirm_payload,
        timeout=CFG.slant_timeout_sec,
    )
    p3 = slant_parse_json(r3, "Slant POST /files/confirm-upload")
    file_obj = p3.get("data") or {}

    pfsid = file_obj.get("publicFileServiceId")
    if not pfsid:
        raise RuntimeError(f"Confirm upload missing publicFileServiceId: {str(p3)[:900]}")

    print(f"✅ Slant file uploaded+confirmed: job_id={job_id} publicFileServiceId={pfsid}")
    return pfsid


def slant_draft_order(order_id: str, shipping: dict, items: list) -> str:
    """
    Per Slant OpenAPI:
      POST /orders requires: platformId, customer(details.email+address), items[]
    """
    pid = (CFG.slant_platform_id or "").strip()
    if not pid:
        raise RuntimeError("SLANT_PLATFORM_ID missing.")
    if not is_uuid_like(pid):
        raise RuntimeError("SLANT_PLATFORM_ID must be a UUID.")

    email = shipping.get("email") or "unknown@test.com"
    full_name = shipping.get("fullName") or shipping.get("name") or "Customer"
    line1 = shipping.get("addressLine") or shipping.get("line1") or ""
    line2 = shipping.get("addressLine2") or shipping.get("line2") or ""
    city = shipping.get("city") or ""
    state = shipping.get("state") or ""
    zip_code = shipping.get("zipCode") or shipping.get("zip") or ""
    country = normalize_country_iso2(shipping.get("country") or "US")

    filament_id = resolve_filament_id(shipping)
    if not is_uuid_like(filament_id):
        raise RuntimeError(f"Resolved filamentId is not UUID-like: {filament_id}")

    slant_items = []
    for it in items:
        pfsid = it.get("publicFileServiceId")
        if not pfsid:
            raise RuntimeError(f"Missing publicFileServiceId for job_id={it.get('job_id')}")
        if not is_uuid_like(pfsid):
            raise RuntimeError(f"publicFileServiceId is not UUID-like: {pfsid}")

        slant_items.append({
            "type": "PRINT",
            "quantity": int(it.get("quantity", 1)),
            "publicFileServiceId": pfsid,
            "filamentId": filament_id,
        })

    if not slant_items:
        raise RuntimeError("No items to send to Slant draft order.")

    payload = {
        "platformId": pid,
        # ownerId is "your application's user ID" (using order_id is acceptable as a stable identifier)
        "ownerId": order_id,
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
    }

    if CFG.slant_debug:
        print("🧪 Slant draft order payload (sanitized):", {
            "platformId": pid,
            "ownerId": order_id,
            "items_count": len(slant_items),
            "first_item": slant_items[0],
        })

    r = requests.post(
        CFG.slant_orders_endpoint,
        headers=slant_headers({"Content-Type": "application/json"}),
        json=payload,
        timeout=CFG.slant_timeout_sec,
    )
    resp = slant_parse_json(r, "Slant POST /orders (draft)")
    data = resp.get("data") or {}
    order_obj = data.get("order") or {}
    public_order_id = order_obj.get("publicId")

    if not public_order_id:
        raise RuntimeError(f"Draft succeeded but missing data.order.publicId: {str(resp)[:1200]}")

    print(f"✅ Slant order drafted: publicOrderId={public_order_id}")
    return public_order_id


def slant_process_order(public_order_id: str) -> dict:
    url = f"{CFG.slant_orders_endpoint}/{public_order_id}/process"
    r = requests.post(url, headers=slant_headers(), timeout=CFG.slant_timeout_sec)
    resp = slant_parse_json(r, "Slant POST /orders/{id}/process")
    return resp


def submit_paid_order_to_slant(order_id: str) -> None:
    data = ORDER_DATA.get(order_id) or {}
    status = data.get("status")

    if status in ("submitted_to_slant", "slant_processing", "in_production"):
        print(f"ℹ️ Order already submitted: order_id={order_id} status={status}")
        return

    if not CFG.slant_api_key or not (CFG.slant_platform_id or "").strip():
        raise RuntimeError("Slant is disabled (missing SLANT_API_KEY or SLANT_PLATFORM_ID).")

    data["status"] = "slant_submitting"
    ORDER_DATA[order_id] = data
    save_order_data()

    items = data.get("items", []) or []
    shipping = data.get("shipping", {}) or {}

    # Upload each STL to Slant (direct-upload + confirm) to get publicFileServiceId
    for it in items:
        job_id = it.get("job_id")
        if not job_id:
            raise RuntimeError("Item missing job_id")

        if not it.get("publicFileServiceId"):
            stl_path = os.path.join(CFG.upload_dir, f"{job_id}.stl")
            pfsid = slant_direct_upload_file(job_id, stl_path, owner_id=order_id)
            it["publicFileServiceId"] = pfsid

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

    print(f"✅ Slant submission complete: order_id={order_id} publicOrderId={public_order_id}")


# ============================================================
# Routes
# ============================================================
@app.route("/")
def index():
    return "✅ Krezz server is live (Stripe + Slant)."


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "time": utc_iso(),
        "slant_configured": bool(CFG.slant_api_key) and bool((CFG.slant_platform_id or "").strip()),
        "slant_auto_submit": CFG.slant_auto_submit,
        "slant_base_url": CFG.slant_base_url,
        "has_slant_platform_id": bool((CFG.slant_platform_id or "").strip()),
        "upload_dir": CFG.upload_dir,
        "orders": len(ORDER_DATA),
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
            job_id = it.get("job_id") or it.get("jobId") or it.get("id")
            if not job_id:
                job_id = str(uuid.uuid4())
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
            # NOTE: Stripe Live mode typically requires https success/cancel URLs.
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

        # Submit to Slant (never fail webhook)
        if not CFG.slant_auto_submit:
            print("🟡 SLANT_AUTO_SUBMIT=0, skipping Slant submission (testing mode).")
            return jsonify(success=True)

        try:
            print(f"➡️ Submitting to Slant: order_id={order_id}")
            submit_paid_order_to_slant(order_id)
        except Exception as e:
            print(f"❌ Slant submit exception: {e}")
            ORDER_DATA[order_id]["slant_error"] = str(e)
            ORDER_DATA[order_id]["status"] = "slant_failed"
            save_order_data()

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


# -------------------- Debug endpoints -------------------- #

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
        pfsid = slant_direct_upload_file(job_id, stl_path, owner_id="debug")
        return jsonify({"ok": True, "job_id": job_id, "publicFileServiceId": pfsid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/debug/slant/submit/<order_id>", methods=["POST"])
def debug_slant_submit(order_id):
    try:
        submit_paid_order_to_slant(order_id)
        return jsonify({"ok": True, "order_id": order_id, "slant": (ORDER_DATA.get(order_id) or {}).get("slant", {})})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(env_str("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
