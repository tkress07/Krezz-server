from flask import Flask, request, jsonify, send_file, abort
import os
import uuid
import json
from datetime import datetime
import stripe
import requests

app = Flask(__name__)

# ----------------------------
# Stripe config
# ----------------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET")

if not stripe.api_key or not STRIPE_ENDPOINT_SECRET:
    raise ValueError("❌ Stripe env vars not set: STRIPE_SECRET_KEY / STRIPE_ENDPOINT_SECRET")

# ----------------------------
# Storage (Render disk)
# ----------------------------
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

# ----------------------------
# Slant config
# ----------------------------
SLANT_API_KEY = os.getenv("SLANT_API_KEY")
SLANT_PLATFORM_ID = os.getenv("SLANT_PLATFORM_ID")
SLANT_WEBHOOK_SECRET = os.getenv("SLANT_WEBHOOK_SECRET")  # used only for inbound slant webhooks (optional)

# Per your pasted docs, base URL should be:
# https://slant3dapi.com/v2/api
SLANT_BASE_URL = os.getenv("SLANT_BASE_URL", "https://slant3dapi.com/v2/api")

# Sensible defaults (you can override via env vars)
SLANT_FILES_ENDPOINT = os.getenv("SLANT_FILES_ENDPOINT", f"{SLANT_BASE_URL}/files")
SLANT_ORDERS_ENDPOINT = os.getenv("SLANT_ORDERS_ENDPOINT", f"{SLANT_BASE_URL}/orders")

# You MUST set this (from Slant Filaments tab)
SLANT_DEFAULT_FILAMENT_ID = os.getenv("SLANT_DEFAULT_FILAMENT_ID")

def slant_headers():
    if not SLANT_API_KEY:
        raise RuntimeError("SLANT_API_KEY not configured")
    return {
        "Authorization": f"Bearer {SLANT_API_KEY}",
        "Accept": "application/json",
    }

def pick_filament_id(shipping_info: dict) -> str:
    """
    Minimal approach:
    - Use SLANT_DEFAULT_FILAMENT_ID
    Later you can map shipping_info['material'] to different filament IDs.
    """
    if not SLANT_DEFAULT_FILAMENT_ID:
        raise RuntimeError("Missing SLANT_DEFAULT_FILAMENT_ID (set this from Slant Filaments tab)")
    return SLANT_DEFAULT_FILAMENT_ID

def extract_public_file_id(resp_json: dict) -> str:
    """
    Slant docs refer to publicFileServiceId.
    Be flexible in case the API returns slightly different keys.
    """
    candidates = [
        resp_json.get("publicFileServiceId"),
        resp_json.get("public_file_service_id"),
        resp_json.get("publicId"),
        resp_json.get("public_id"),
        resp_json.get("id"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    # Sometimes it might be nested
    if isinstance(resp_json.get("data"), dict):
        return extract_public_file_id(resp_json["data"])
    raise RuntimeError(f"Could not find public file id in response: {resp_json}")

def extract_public_order_id(resp_json: dict) -> str:
    """
    Orders docs talk about 'public ID' returned from draft.
    Handle common variants.
    """
    candidates = [
        resp_json.get("publicId"),
        resp_json.get("public_id"),
        resp_json.get("publicOrderId"),
        resp_json.get("public_order_id"),
        resp_json.get("id"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    if isinstance(resp_json.get("data"), dict):
        return extract_public_order_id(resp_json["data"])
    raise RuntimeError(f"Could not find public order id in response: {resp_json}")

def upload_stl_to_slant(job_id: str) -> str:
    """
    Uploads /data/uploads/{job_id}.stl to Slant Files API.
    Returns publicFileServiceId.
    """
    stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        raise RuntimeError(f"STL not found for job_id={job_id} at {stl_path}")

    print(f"➡️ Uploading STL to Slant Files: job_id={job_id} endpoint={SLANT_FILES_ENDPOINT}")

    with open(stl_path, "rb") as f:
        files = {
            # Many APIs use "file". If Slant expects a different key, change here.
            "file": (f"{job_id}.stl", f, "application/sla")
        }
        # Some APIs also want platformId; harmless if ignored.
        data = {}
        if SLANT_PLATFORM_ID:
            data["platformId"] = SLANT_PLATFORM_ID

        r = requests.post(
            SLANT_FILES_ENDPOINT,
            headers=slant_headers(),
            files=files,
            data=data,
            timeout=120
        )

    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}

    if r.status_code >= 300:
        raise RuntimeError(f"Slant file upload failed status={r.status_code} body={body}")

    public_file_id = extract_public_file_id(body)
    print(f"✅ Slant file uploaded job_id={job_id} publicFileServiceId={public_file_id}")
    return public_file_id

def draft_slant_order(order_id: str) -> str:
    """
    Drafts an order in Slant (does not process/charge yet).
    Returns publicOrderId.
    """
    order = ORDER_DATA.get(order_id) or {}
    shipping = order.get("shipping", {}) or {}
    items = order.get("items", []) or []

    if not SLANT_PLATFORM_ID:
        raise RuntimeError("SLANT_PLATFORM_ID not configured")

    # Build Slant items (PRINT)
    filament_id = pick_filament_id(shipping)

    slant_items = []
    for it in items:
        job_id = it.get("job_id")
        if not job_id:
            raise RuntimeError(f"Missing job_id on item: {it}")

        # Ensure we have a publicFileServiceId
        public_file_id = it.get("publicFileServiceId") or it.get("public_file_service_id")
        if not public_file_id:
            public_file_id = upload_stl_to_slant(job_id)
            it["publicFileServiceId"] = public_file_id  # store back
            ORDER_DATA[order_id]["items"] = items
            save_order_data()

        slant_items.append({
            "type": "PRINT",
            "publicFileServiceId": public_file_id,
            "filamentId": filament_id,
            "quantity": int(it.get("quantity", 1)),
            "name": it.get("name", "KrezzCut Mold"),
            "SKU": job_id,
        })

    # Map your shipping fields to Slant address schema
    address = {
        "name": shipping.get("fullName") or shipping.get("name") or "Customer",
        "line1": shipping.get("addressLine") or shipping.get("line1") or "",
        "line2": shipping.get("line2") or "",
        "city": shipping.get("city") or "",
        "state": shipping.get("state") or "",
        "zip": shipping.get("zipCode") or shipping.get("zip") or "",
        "country": "US" if (shipping.get("country") in ["United States", "US", "USA", None, ""]) else shipping.get("country"),
    }

    email = shipping.get("email") or "unknown@email.com"

    order_payload = {
        "customer": {
            "platformId": SLANT_PLATFORM_ID,
            "details": {
                "email": email,
                "address": address
            }
        },
        "items": slant_items,
        "metadata": {
            "orderId": order_id,
            "source": "KREZZ_STRIPE_WEBHOOK"
        }
    }

    print(f"➡️ Drafting Slant order: endpoint={SLANT_ORDERS_ENDPOINT}")
    r = requests.post(
        SLANT_ORDERS_ENDPOINT,
        headers={**slant_headers(), "Content-Type": "application/json"},
        json=order_payload,
        timeout=60
    )

    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}

    if r.status_code >= 300:
        raise RuntimeError(f"Slant draft order failed status={r.status_code} body={body}")

    public_order_id = extract_public_order_id(body)
    print(f"✅ Slant order drafted publicOrderId={public_order_id}")

    ORDER_DATA[order_id].setdefault("slant", {})
    ORDER_DATA[order_id]["slant"]["draft"] = {
        "publicOrderId": public_order_id,
        "draft_response": body,
        "drafted_at": datetime.utcnow().isoformat()
    }
    save_order_data()
    return public_order_id

def process_slant_order(order_id: str, public_order_id: str) -> dict:
    """
    Processes the order (charges your Slant payment method + sends to production).
    """
    url = f"{SLANT_ORDERS_ENDPOINT}/{public_order_id}"
    print(f"➡️ Processing Slant order: {url}")

    r = requests.post(
        url,
        headers={**slant_headers(), "Content-Type": "application/json"},
        timeout=60
    )

    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}

    if r.status_code >= 300:
        raise RuntimeError(f"Slant process order failed status={r.status_code} body={body}")

    print(f"✅ Slant order processed publicOrderId={public_order_id}")
    ORDER_DATA[order_id].setdefault("slant", {})
    ORDER_DATA[order_id]["slant"]["processed"] = {
        "processed_response": body,
        "processed_at": datetime.utcnow().isoformat()
    }
    ORDER_DATA[order_id]["status"] = "submitted_to_slant"
    save_order_data()
    return body

def submit_order_to_slant(order_id: str) -> dict:
    """
    Idempotent-ish submit:
    - If already processed, do nothing.
    - If drafted but not processed, process.
    - If neither, draft then process.
    """
    order = ORDER_DATA.get(order_id)
    if not order:
        raise RuntimeError(f"Unknown order_id: {order_id}")

    slant_state = order.get("slant", {}) or {}
    if slant_state.get("processed"):
        print(f"ℹ️ Order already processed in Slant: order_id={order_id}")
        return slant_state["processed"]

    draft = slant_state.get("draft") or {}
    public_order_id = draft.get("publicOrderId")
    if not public_order_id:
        public_order_id = draft_slant_order(order_id)

    return process_slant_order(order_id, public_order_id)

# ----------------------------
# Routes
# ----------------------------
@app.route("/")
def index():
    return "✅ Krezz server is live."

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

        normalized_items = []
        for it in items:
            job_id = it.get("job_id") or it.get("jobId") or it.get("id")
            if not job_id:
                job_id = str(uuid.uuid4())
            it["job_id"] = job_id
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
        print(f"❌ Stripe webhook verify error: {e}")
        return "Webhook error", 400

    print(f"📦 Stripe event: {event['type']}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")

        if not order_id:
            print("❌ Missing order_id in Stripe metadata")
            return jsonify(success=True)

        ORDER_DATA.setdefault(order_id, {"items": [], "shipping": {}, "status": "created"})

        ORDER_DATA[order_id]["status"] = "paid"
        ORDER_DATA[order_id]["payment"] = {
            "stripe_session_id": session.get("id"),
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
            result = submit_order_to_slant(order_id)
            print(f"✅ Slant submit OK for order_id={order_id}")
        except Exception as e:
            # Do NOT fail the Stripe webhook response; just log and store error
            print(f"❌ Slant submit exception for order_id={order_id}: {e}")
            ORDER_DATA[order_id].setdefault("slant", {})
            ORDER_DATA[order_id]["slant"]["error"] = {
                "message": str(e),
                "at": datetime.utcnow().isoformat()
            }
            ORDER_DATA[order_id]["status"] = "slant_submit_failed"
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
        "slant": data.get("slant", {})
    })

@app.route("/slant/submit/<order_id>", methods=["POST"])
def manual_slant_submit(order_id):
    """
    Manual retry endpoint (handy when you fix env vars or endpoints).
    """
    try:
        result = submit_order_to_slant(order_id)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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
    return send_file(
        stl_path,
        mimetype="application/sla",
        as_attachment=True,
        download_name=f"mold_{job_id}.stl"
    )
