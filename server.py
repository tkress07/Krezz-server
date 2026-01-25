from flask import Flask, request, jsonify, send_file, abort
import stripe
import os
import uuid
import json
import time
from datetime import datetime
import requests
from urllib.parse import urljoin

app = Flask(__name__)

# -----------------------------
# Stripe config
# -----------------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET")
if not stripe.api_key or not STRIPE_ENDPOINT_SECRET:
    raise ValueError("❌ Stripe environment variables not set.")

# -----------------------------
# Local storage (Render disk)
# -----------------------------
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATA_PATH = os.getenv("ORDER_DATA_PATH", "/data/order_data.json")
ORDER_DATA = {}

def load_order_data():
    global ORDER_DATA
    try:
        with open(DATA_PATH, "r") as f:
            ORDER_DATA = json.load(f)
        print(f"✅ Loaded ORDER_DATA ({len(ORDER_DATA)} orders)")
    except Exception:
        ORDER_DATA = {}
        print("ℹ️ No prior ORDER_DATA found (starting fresh)")

def save_order_data():
    try:
        with open(DATA_PATH, "w") as f:
            json.dump(ORDER_DATA, f)
    except Exception as e:
        print(f"❌ Failed to persist ORDER_DATA: {e}")

load_order_data()

# -----------------------------
# Slant config
# -----------------------------
SLANT_API_KEY = os.getenv("SLANT_API_KEY")
SLANT_PLATFORM_ID = os.getenv("SLANT_PLATFORM_ID")
SLANT_BASE_URL = os.getenv("SLANT_BASE_URL", "https://slant3dapi.com/v2/api").rstrip("/")
SLANT_FILES_ENDPOINT = os.getenv("SLANT_FILES_ENDPOINT") or f"{SLANT_BASE_URL}/files"
SLANT_DEFAULT_FILAMENT_ID = os.getenv("SLANT_DEFAULT_FILAMENT_ID")  # recommended to set (ex: PETG BLACK)
SLANT_WEBHOOK_SECRET = os.getenv("SLANT_WEBHOOK_SECRET")  # optional unless you use /slant/webhook

_FILAMENT_CACHE = {"ts": 0, "data": None}
_FILAMENT_CACHE_TTL_SEC = 600  # 10 minutes


def slant_headers():
    if not SLANT_API_KEY:
        return None
    return {
        "Authorization": f"Bearer {SLANT_API_KEY}",
        "Accept": "application/json",
    }


def slant_get_filaments_cached():
    """
    Fetch filaments from Slant (cached) so we can resolve filamentId.
    """
    now = time.time()
    if _FILAMENT_CACHE["data"] is not None and (now - _FILAMENT_CACHE["ts"]) < _FILAMENT_CACHE_TTL_SEC:
        return _FILAMENT_CACHE["data"]

    headers = slant_headers()
    if not headers:
        return None

    url = f"{SLANT_BASE_URL}/filaments"
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code != 200:
        print(f"❌ Slant filaments fetch failed: {r.status_code} {r.text[:500]}")
        return None

    payload = r.json()
    data = payload.get("data") or []
    _FILAMENT_CACHE["ts"] = now
    _FILAMENT_CACHE["data"] = data
    return data


def normalize_country_iso2(country_val: str) -> str:
    if not country_val:
        return "US"
    c = country_val.strip().lower()
    # Common cases from iOS form
    if c in ("us", "usa", "united states", "united states of america"):
        return "US"
    # If user passes already iso2 (2 letters)
    if len(country_val.strip()) == 2:
        return country_val.strip().upper()
    return "US"


def resolve_filament_id(shipping_info: dict) -> str:
    """
    Pick a filament publicId based on shippingInfo.material and shippingInfo.color.
    Fallback to SLANT_DEFAULT_FILAMENT_ID.
    """
    material = (shipping_info.get("material") or "").upper()
    color = (shipping_info.get("color") or "").strip().lower()

    # Decide profile preference
    # Your UI sends things like "PETG - Durable, flexible (Recommended)"
    want_profile = "PETG" if "PETG" in material else "PLA"

    filaments = slant_get_filaments_cached() or []

    # Try exact profile + color match
    for f in filaments:
        if not f.get("available", True):
            continue
        if (f.get("profile") or "").upper() == want_profile and (f.get("color") or "").lower() == color:
            return f.get("publicId")

    # Try profile match, any color
    for f in filaments:
        if not f.get("available", True):
            continue
        if (f.get("profile") or "").upper() == want_profile:
            return f.get("publicId")

    # Fall back to env default
    if SLANT_DEFAULT_FILAMENT_ID:
        return SLANT_DEFAULT_FILAMENT_ID

    # Absolute last resort: first available filament from API
    if filaments:
        return filaments[0].get("publicId")

    raise RuntimeError("No filament available and SLANT_DEFAULT_FILAMENT_ID not set.")


def slant_upload_stl(job_id: str, stl_path: str) -> str:
    """
    Upload STL to Slant Files API and return publicFileServiceId.
    Endpoint is controlled by SLANT_FILES_ENDPOINT.
    """
    headers = slant_headers()
    if not headers:
        raise RuntimeError("SLANT_API_KEY not configured")
    if not os.path.exists(stl_path):
        raise RuntimeError(f"STL not found on server for job_id={job_id}: {stl_path}")

    # multipart upload
    with open(stl_path, "rb") as f:
        files = {
            "file": (f"{job_id}.stl", f, "application/sla")
        }
        data = {
            # Some APIs need platformId; harmless if ignored
            "platformId": SLANT_PLATFORM_ID or "",
            "name": f"{job_id}.stl",
            "type": "STL",
        }

        print(f"➡️ Uploading STL to Slant Files: job_id={job_id} endpoint={SLANT_FILES_ENDPOINT}")
        r = requests.post(SLANT_FILES_ENDPOINT, headers=headers, files=files, data=data, timeout=60)

    if r.status_code >= 400:
        raise RuntimeError(f"Slant file upload failed: status={r.status_code} body={r.text[:800]}")

    payload = r.json() if "application/json" in (r.headers.get("Content-Type") or "") else {}
    # Try a bunch of common response shapes:
    # - { data: { publicId: "..." } }
    # - { data: { publicFileServiceId: "..." } }
    # - { publicId: "..." }
    data_obj = payload.get("data") if isinstance(payload, dict) else None
    public_id = None

    if isinstance(data_obj, dict):
        public_id = data_obj.get("publicFileServiceId") or data_obj.get("publicId")
    if not public_id and isinstance(payload, dict):
        public_id = payload.get("publicFileServiceId") or payload.get("publicId")

    if not public_id:
        raise RuntimeError(f"Slant file upload succeeded but no publicFileServiceId/publicId found. Response: {str(payload)[:800]}")

    print(f"✅ Slant file uploaded: job_id={job_id} publicFileServiceId={public_id}")
    return public_id


def slant_draft_order(order_id: str, shipping: dict, items: list) -> str:
    """
    Draft an order in Slant and return publicOrderId.
    """
    headers = slant_headers()
    if not headers:
        raise RuntimeError("SLANT_API_KEY not configured")
    if not SLANT_PLATFORM_ID:
        raise RuntimeError("SLANT_PLATFORM_ID not configured")

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
            raise RuntimeError(f"Missing publicFileServiceId for job_id={it.get('job_id')}")
        slant_items.append({
            "type": "PRINT",
            "publicFileServiceId": pfsid,
            "filamentId": filament_id,
            "quantity": int(it.get("quantity", 1)),
            "name": it.get("name", "Krezz Mold"),
            "SKU": it.get("SKU") or it.get("sku") or it.get("job_id", ""),
        })

    payload = {
        "customer": {
            "platformId": SLANT_PLATFORM_ID,
            "details": {
                "email": email,
                "address": {
                    "name": full_name,
                    "line1": line1,
                    "line2": line2,
                    "city": city,
                    "state": state,
                    "zip": zip_code,
                    "country": country
                }
            }
        },
        "items": slant_items,
        "metadata": {
            "internalOrderId": order_id,
            "source": "KREZZ_SERVER",
            "jobIds": [it.get("job_id") for it in items]
        }
    }

    url = f"{SLANT_BASE_URL}/orders"
    print(f"➡️ Drafting Slant order: endpoint={url}")
    r = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload, timeout=30)

    if r.status_code >= 400:
        raise RuntimeError(f"Slant draft failed: status={r.status_code} body={r.text[:1200]}")

    resp = r.json() if r.text else {}
    # Most likely: resp.data.publicId OR resp.publicId
    data_obj = resp.get("data") if isinstance(resp, dict) else None
    public_order_id = None

    if isinstance(data_obj, dict):
        public_order_id = data_obj.get("publicId") or data_obj.get("publicOrderId")
    if not public_order_id and isinstance(resp, dict):
        public_order_id = resp.get("publicId") or resp.get("publicOrderId")

    if not public_order_id:
        raise RuntimeError(f"Draft succeeded but could not find public order id in response: {str(resp)[:1200]}")

    print(f"✅ Slant order drafted: publicOrderId={public_order_id}")
    return public_order_id


def slant_process_order(public_order_id: str):
    """
    Process an already drafted Slant order.
    """
    headers = slant_headers()
    if not headers:
        raise RuntimeError("SLANT_API_KEY not configured")

    url = f"{SLANT_BASE_URL}/orders/{public_order_id}"
    print(f"➡️ Processing Slant order: endpoint={url}")
    r = requests.post(url, headers={**headers, "Content-Type": "application/json"}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Slant process failed: status={r.status_code} body={r.text[:1200]}")
    print(f"✅ Slant order processed: publicOrderId={public_order_id}")
    return r.json() if r.text else {"success": True}


def submit_paid_order_to_slant(order_id: str):
    """
    Idempotent: only submit once.
    """
    data = ORDER_DATA.get(order_id) or {}
    status = data.get("status")

    # Already sent?
    if status in ("submitted_to_slant", "slant_processing", "in_production"):
        print(f"ℹ️ Order already submitted to Slant: order_id={order_id} status={status}")
        return

    if not SLANT_API_KEY or not SLANT_PLATFORM_ID:
        raise RuntimeError("Missing SLANT_API_KEY or SLANT_PLATFORM_ID")

    items = data.get("items", [])
    shipping = data.get("shipping", {}) or {}

    # Ensure each item has publicFileServiceId by uploading STL
    for it in items:
        job_id = it.get("job_id")
        if not job_id:
            raise RuntimeError("Item missing job_id")

        if not it.get("publicFileServiceId"):
            stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
            pfsid = slant_upload_stl(job_id, stl_path)
            it["publicFileServiceId"] = pfsid

    # Draft then process
    public_order_id = slant_draft_order(order_id, shipping, items)
    data["slant"] = data.get("slant", {})
    data["slant"]["publicOrderId"] = public_order_id
    data["status"] = "slant_drafted"
    save_order_data()

    process_resp = slant_process_order(public_order_id)
    data["slant"]["processResponse"] = process_resp
    data["status"] = "submitted_to_slant"
    save_order_data()


# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def index():
    return "✅ Krezz server is live (Stripe + Slant)."


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    try:
        data = request.get_json(silent=True) or {}
        print("📥 /create-checkout-session payload:", data)

        items = data.get("items", [])
        shipping_info = data.get("shippingInfo", {})

        if not items:
            return jsonify({"error": "No items provided"}), 400

        order_id = data.get("order_id") or str(uuid.uuid4())

        # Normalize items
        normalized_items = []
        for it in items:
            job_id = it.get("job_id") or it.get("jobId") or it.get("id")
            if not job_id:
                job_id = str(uuid.uuid4())
            it["job_id"] = job_id
            # allow quantity (default 1)
            it["quantity"] = int(it.get("quantity", 1))
            normalized_items.append(it)

        ORDER_DATA[order_id] = {
            "items": normalized_items,
            "shipping": shipping_info,
            "status": "created",
            "created_at": datetime.utcnow().isoformat()
        }
        save_order_data()

        line_items = [{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": it.get("name", "Beard Mold")},
                "unit_amount": int(it.get("price", 7500)),
            },
            "quantity": int(it.get("quantity", 1))
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
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_ENDPOINT_SECRET)
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
            "stripe_session_id": session["id"],
            "amount_total": session.get("amount_total"),
            "currency": session.get("currency"),
            "created": datetime.utcfromtimestamp(session["created"]).isoformat(),
            "email": session.get("customer_email", "unknown"),
            "status": "paid"
        }
        save_order_data()
        print(f"✅ Payment confirmed for order_id: {order_id}")

        # Submit to Slant
        try:
            print(f"➡️ Submitting to Slant: order_id={order_id}")
            submit_paid_order_to_slant(order_id)
            print(f"✅ Slant submission complete: order_id={order_id}")
        except Exception as e:
            # Do NOT fail Stripe webhook (Stripe will retry). Just log & store error.
            print(f"❌ Slant submit exception: {e}")
            ORDER_DATA[order_id]["slant_error"] = str(e)
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
        "slant_error": data.get("slant_error")
    })


@app.route("/upload", methods=["POST"])
def upload_stl():
    job_id = request.form.get("job_id")
    file = request.files.get("file")
    if not job_id or not file:
        return jsonify({"error": "Missing job_id or file"}), 400

    save_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    file.save(save_path)
    print(f"✅ Uploaded STL job_id={job_id} -> {save_path}")
    return jsonify({"success": True, "path": save_path})


@app.route("/stl/<job_id>.stl", methods=["GET"])
def serve_stl(job_id):
    stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        return abort(404)
    return send_file(stl_path, mimetype="application/sla", as_attachment=True,
                     download_name=f"mold_{job_id}.stl")


# Debug helpers (optional but super useful)
@app.route("/debug/slant/filaments", methods=["GET"])
def debug_slant_filaments():
    try:
        filaments = slant_get_filaments_cached()
        return jsonify({"ok": True, "count": len(filaments or []), "data": filaments})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/debug/slant/upload/<job_id>", methods=["POST"])
def debug_slant_upload(job_id):
    """
    Manually test file upload after you already uploaded an STL to /upload.
    """
    try:
        stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
        pfsid = slant_upload_stl(job_id, stl_path)
        return jsonify({"ok": True, "job_id": job_id, "publicFileServiceId": pfsid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
