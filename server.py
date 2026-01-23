from flask import Flask, request, jsonify, send_file, abort
import os
import uuid
import json
import hmac
import hashlib
import base64
from datetime import datetime

import stripe
import requests

app = Flask(__name__)

# -----------------------------
# Stripe config
# -----------------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
stripe_endpoint_secret = os.getenv("STRIPE_ENDPOINT_SECRET")
if not stripe.api_key or not stripe_endpoint_secret:
    raise ValueError("❌ Stripe environment variables not set (STRIPE_SECRET_KEY / STRIPE_ENDPOINT_SECRET).")

# -----------------------------
# Slant config (server-side only)
# -----------------------------
SLANT_API_KEY = os.getenv("SLANT_API_KEY", "").strip()
SLANT_PLATFORM_ID = os.getenv("SLANT_PLATFORM_ID", "").strip()
SLANT_WEBHOOK_SECRET = os.getenv("SLANT_WEBHOOK_SECRET", "").strip()

# Public base URL for generating public STL links (IMPORTANT for Slant)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

# Slant API base/endpoint (make configurable to fix 404 fast)
SLANT_API_BASE = os.getenv("SLANT_API_BASE", "https://www.slant3dapi.com").strip().rstrip("/")
# Strongly recommended: set this env var once you confirm the exact path in Slant docs.
SLANT_ORDERS_ENDPOINT = os.getenv(
    "SLANT_ORDERS_ENDPOINT",
    f"{SLANT_API_BASE}/api/orders"  # <-- common guess; override via env if your docs differ
).strip()

# -----------------------------
# Storage
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
# Helpers
# -----------------------------
def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def ensure_order(order_id: str):
    if order_id not in ORDER_DATA:
        ORDER_DATA[order_id] = {
            "items": [],
            "shipping": {},
            "status": "created",
            "created_at": now_iso(),
        }

def slant_headers():
    # We don't know Slant's exact auth header format from your screenshots.
    # Sending multiple common variants usually doesn't hurt.
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if SLANT_API_KEY:
        headers["Authorization"] = f"Bearer {SLANT_API_KEY}"
        headers["X-Api-Key"] = SLANT_API_KEY
    if SLANT_PLATFORM_ID:
        headers["X-Platform-Id"] = SLANT_PLATFORM_ID
    return headers

def build_public_stl_url(job_id: str) -> str:
    # Slant must be able to fetch the STL from the internet.
    # So this MUST be an https URL that loads in a browser.
    if not PUBLIC_BASE_URL:
        raise ValueError("PUBLIC_BASE_URL is not set. Example: https://krezz-server.onrender.com")
    return f"{PUBLIC_BASE_URL}/stl/{job_id}.stl"

def submit_order_to_slant(order_id: str):
    """
    Called after payment is confirmed.
    Creates a Slant order (or job) based on ORDER_DATA.
    """
    if not SLANT_API_KEY or not SLANT_PLATFORM_ID:
        raise ValueError("Slant env vars missing: SLANT_API_KEY and/or SLANT_PLATFORM_ID")

    ensure_order(order_id)
    order = ORDER_DATA[order_id]
    items = order.get("items", [])
    shipping = order.get("shipping", {})

    if not items:
        raise ValueError("No items on order; cannot submit to Slant.")

    # Build payload(s)
    # NOTE: Slant may require a specific schema. This is a reasonable starting point.
    # If Slant returns 400/404, we will log the response text so you can adjust fields fast.
    slant_items = []
    for it in items:
        job_id = (it.get("job_id") or "").strip()
        if not job_id:
            raise ValueError(f"Missing job_id in item: {it}")

        file_url = build_public_stl_url(job_id)
        slant_items.append({
            "name": it.get("name", "Beard Mold"),
            "quantity": 1,
            "fileUrl": file_url,     # <-- common pattern
            "jobId": job_id,         # <-- keep your id for traceability
        })

    payload = {
        "platformId": SLANT_PLATFORM_ID,  # some APIs want this in-body
        "externalOrderId": order_id,
        "items": slant_items,
        "shipping": shipping,
    }

    print(f"➡️ Submitting to Slant: order_id={order_id}")
    print(f"➡️ Slant endpoint: {SLANT_ORDERS_ENDPOINT}")

    resp = requests.post(
        SLANT_ORDERS_ENDPOINT,
        headers=slant_headers(),
        json=payload,
        timeout=30,
    )

    # Always log full failure details (this is what will solve your 404)
    if resp.status_code >= 300:
        body_text = resp.text[:4000]  # keep logs readable
        print(f"❌ Slant submit failed: status={resp.status_code} body={body_text}")

        order["slant"] = order.get("slant", {})
        order["slant"]["submitted"] = False
        order["slant"]["error_status"] = resp.status_code
        order["slant"]["error_body"] = body_text
        order["slant"]["last_attempt_at"] = now_iso()
        save_order_data()
        return {"ok": False, "status_code": resp.status_code, "body": body_text}

    data = {}
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    print(f"✅ Slant submit success: {data}")

    order["status"] = "submitted_to_slant"
    order["slant"] = {
        "submitted": True,
        "response": data,
        "submitted_at": now_iso(),
    }
    save_order_data()
    return {"ok": True, "data": data}

def verify_slant_signature(raw_body: bytes, provided_sig: str) -> bool:
    """
    Signature formats vary by vendor.
    We attempt:
    - HMAC-SHA256 raw body with secret as UTF-8
    - HMAC-SHA256 raw body with secret base64-decoded (your secret *looks* base64-ish)
    Compare against hex or base64 signatures.
    """
    if not SLANT_WEBHOOK_SECRET or not provided_sig:
        return False

    candidates = []

    # 1) secret as utf-8 bytes
    secret_bytes = SLANT_WEBHOOK_SECRET.encode("utf-8")
    digest = hmac.new(secret_bytes, raw_body, hashlib.sha256).digest()
    candidates.append(digest)

    # 2) secret as base64-decoded bytes (if possible)
    try:
        secret_b64 = base64.b64decode(SLANT_WEBHOOK_SECRET)
        digest2 = hmac.new(secret_b64, raw_body, hashlib.sha256).digest()
        candidates.append(digest2)
    except Exception:
        pass

    # Normalize provided signature
    provided_sig = provided_sig.strip()

    for d in candidates:
        hex_sig = d.hex()
        b64_sig = base64.b64encode(d).decode("utf-8")

        if hmac.compare_digest(provided_sig, hex_sig):
            return True
        if hmac.compare_digest(provided_sig, b64_sig):
            return True

    return False

# -----------------------------
# Routes
# -----------------------------
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

        ensure_order(order_id)
        ORDER_DATA[order_id].update({
            "items": normalized_items,
            "shipping": shipping_info,
            "status": "created",
            "updated_at": now_iso(),
        })
        save_order_data()

        line_items = [{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": it.get("name", "Beard Mold")},
                "unit_amount": int(it.get("price", 7500)),
            },
            "quantity": 1
        } for it in normalized_items]

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=line_items,
            success_url=f"krezzapp://order-confirmed?order_id={order_id}",
            cancel_url="https://krezzcut.com/cancel",
            client_reference_id=order_id,
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
        event = stripe.Webhook.construct_event(payload, sig_header, stripe_endpoint_secret)
    except Exception as e:
        print(f"❌ Stripe webhook error: {e}")
        return "Webhook error", 400

    print(f"📦 Stripe event: {event['type']}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id") or session.get("client_reference_id")

        if not order_id:
            print("❌ Missing order_id in Stripe session")
            return jsonify(success=True)

        ensure_order(order_id)

        ORDER_DATA[order_id]["status"] = "paid"
        ORDER_DATA[order_id]["payment"] = {
            "stripe_session_id": session.get("id"),
            "amount_total": session.get("amount_total"),
            "currency": session.get("currency"),
            "created": datetime.utcfromtimestamp(session["created"]).isoformat(),
            "email": session.get("customer_email", "unknown"),
            "status": "paid"
        }
        ORDER_DATA[order_id]["updated_at"] = now_iso()
        save_order_data()
        print(f"✅ Payment confirmed for order_id: {order_id}")

        # 🔥 Submit to Slant immediately after payment (server-side)
        try:
            result = submit_order_to_slant(order_id)
            print(f"🧾 Slant submit result: {result}")
        except Exception as e:
            print(f"❌ Slant submit exception for order_id={order_id}: {e}")
            ORDER_DATA[order_id]["slant"] = ORDER_DATA[order_id].get("slant", {})
            ORDER_DATA[order_id]["slant"]["submitted"] = False
            ORDER_DATA[order_id]["slant"]["exception"] = str(e)
            ORDER_DATA[order_id]["slant"]["last_attempt_at"] = now_iso()
            save_order_data()

    return jsonify(success=True)

@app.route("/slant/webhook", methods=["POST"])
def slant_webhook():
    raw = request.data

    # Slant signature header name may vary—check your Slant docs.
    provided_sig = (
        request.headers.get("X-Slant-Signature")
        or request.headers.get("Slant-Signature")
        or request.headers.get("X-Signature")
        or ""
    )

    # If you want to temporarily bypass verification during setup:
    if os.getenv("SLANT_VERIFY_WEBHOOK", "1") == "1":
        if not verify_slant_signature(raw, provided_sig):
            print("❌ Slant webhook signature verification failed.")
            return "Invalid signature", 400

    evt = {}
    try:
        evt = request.get_json(silent=True) or {}
    except Exception:
        evt = {}

    print(f"📦 Slant webhook event received: keys={list(evt.keys())}")

    # You’ll need to map Slant’s payload fields here once you see an example.
    # Common patterns:
    # - evt["externalOrderId"] or evt["metadata"]["externalOrderId"]
    # - evt["status"], evt["tracking"]
    external_order_id = evt.get("externalOrderId") or evt.get("orderId") or evt.get("metadata", {}).get("externalOrderId")

    if external_order_id:
        ensure_order(external_order_id)
        ORDER_DATA[external_order_id]["slant"] = ORDER_DATA[external_order_id].get("slant", {})
        ORDER_DATA[external_order_id]["slant"]["webhook_last_event"] = evt
        ORDER_DATA[external_order_id]["slant"]["webhook_received_at"] = now_iso()

        # Optional: set high-level status if present
        if "status" in evt:
            ORDER_DATA[external_order_id]["status"] = f"slant_{evt.get('status')}"
        save_order_data()

    return jsonify({"ok": True})

@app.route("/slant/retry/<order_id>", methods=["POST"])
def slant_retry(order_id):
    ensure_order(order_id)
    try:
        result = submit_order_to_slant(order_id)
        return jsonify(result), (200 if result.get("ok") else 400)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/order-data/<order_id>", methods=["GET"])
def get_order_data(order_id):
    data = ORDER_DATA.get(order_id)
    if not data:
        return jsonify({"error": "Order ID not found"}), 404
    return jsonify({
        "order_id": order_id,
        "status": data.get("status", "created"),
        "payment": data.get("payment", {}),
        "slant": data.get("slant", {}),
        "items": data.get("items", []),
        "shipping": data.get("shipping", {})
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
    return send_file(
        stl_path,
        mimetype="application/sla",
        as_attachment=True,
        download_name=f"mold_{job_id}.stl"
    )
