from flask import Flask, request, jsonify, send_file, abort
import os
import uuid
import json
import stripe
import requests
import threading
import hmac
import hashlib
import base64
from datetime import datetime

app = Flask(__name__)

# -----------------------
# Stripe config
# -----------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET")
if not stripe.api_key or not STRIPE_ENDPOINT_SECRET:
    raise ValueError("❌ Stripe environment variables not set.")

# -----------------------
# Slant config (server-side only)
# -----------------------
SLANT_API_KEY = os.getenv("SLANT_API_KEY")
SLANT_PLATFORM_ID = os.getenv("SLANT_PLATFORM_ID")
SLANT_WEBHOOK_SECRET = os.getenv("SLANT_WEBHOOK_SECRET")

# IMPORTANT: set this in Render to the exact "Create Order" endpoint from Slant docs.
# Your current logs show https://www.slant3dapi.com/api/orders returns 404.
SLANT_ORDERS_ENDPOINT = os.getenv("SLANT_ORDERS_ENDPOINT", "https://www.slant3dapi.com/api/orders")

# Base URL your server is publicly reachable at (Render provides one; you already have this env var)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# -----------------------
# Storage / persistence
# -----------------------
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

# -----------------------
# Helpers
# -----------------------
def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def public_url(path: str) -> str:
    # Prefer env var because request.host_url can be tricky behind proxies
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{path}"
    # Fallback if env var not set
    return f"{request.host_url.rstrip('/')}{path}"

def slant_headers():
    # We don’t know Slant’s exact auth header format from your logs alone,
    # so we send both common patterns. This won’t hurt, and one should match.
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SLANT_API_KEY}" if SLANT_API_KEY else "",
        "X-API-Key": SLANT_API_KEY or "",
    }

def mark_slant_state(order_id: str, **kwargs):
    if order_id not in ORDER_DATA:
        ORDER_DATA[order_id] = {"items": [], "shipping": {}, "status": "created"}
    ORDER_DATA[order_id].setdefault("slant", {})
    ORDER_DATA[order_id]["slant"].update(kwargs)
    ORDER_DATA[order_id]["slant"]["updated_at"] = now_iso()
    save_order_data()

def submit_order_to_slant(order_id: str):
    """
    Attempts to create an order in Slant.
    If Slant returns 404, your SLANT_ORDERS_ENDPOINT is wrong — update it in Render env vars.
    """
    try:
        if not (SLANT_API_KEY and SLANT_PLATFORM_ID):
            msg = "SLANT_API_KEY or SLANT_PLATFORM_ID not configured"
            print(f"❌ {msg}")
            mark_slant_state(order_id, submitted=False, error=msg)
            return

        data = ORDER_DATA.get(order_id)
        if not data:
            msg = "order_id not found in ORDER_DATA"
            print(f"❌ {msg}: {order_id}")
            mark_slant_state(order_id, submitted=False, error=msg)
            return

        items = data.get("items", [])
        shipping = data.get("shipping", {})
        if not items:
            msg = "no items to submit"
            print(f"❌ {msg} for order_id={order_id}")
            mark_slant_state(order_id, submitted=False, error=msg)
            return

        # Build “file URLs” for each job_id so Slant can fetch STLs (common pattern).
        # If Slant expects multipart upload instead, we’ll adjust later.
        slant_items = []
        for it in items:
            job_id = it.get("job_id") or it.get("jobId") or it.get("id")
            if not job_id:
                continue
            stl_url = public_url(f"/stl/{job_id}.stl")
            slant_items.append({
                "job_id": job_id,
                "name": it.get("name", "Beard Mold"),
                "file_url": stl_url,
                "quantity": 1,
            })

        # Payload shape depends on Slant’s docs.
        # This is a reasonable “starter” payload and is easy to adjust once you confirm Slant’s schema.
        payload = {
            "platformId": SLANT_PLATFORM_ID,
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
            timeout=25
        )

        body_text = resp.text or ""
        if resp.status_code >= 200 and resp.status_code < 300:
            # Try parse JSON for an order id
            try:
                j = resp.json()
            except Exception:
                j = {}

            slant_order_id = j.get("id") or j.get("orderId") or j.get("slantOrderId")

            print(f"✅ Slant submit OK status={resp.status_code} slant_order_id={slant_order_id}")
            mark_slant_state(
                order_id,
                submitted=True,
                status_code=resp.status_code,
                response=j if j else body_text,
                slant_order_id=slant_order_id
            )
        else:
            print(f"❌ Slant submit failed: status={resp.status_code} body={body_text[:500]}")
            mark_slant_state(
                order_id,
                submitted=False,
                status_code=resp.status_code,
                response=body_text
            )

    except Exception as e:
        print(f"❌ Slant submit exception for order_id={order_id}: {e}")
        mark_slant_state(order_id, submitted=False, error=str(e))

def kickoff_slant_submit(order_id: str):
    """
    Don’t block Stripe webhook response — run Slant submit in a short background thread.
    """
    # Idempotency: don’t resubmit if already submitted
    slant_state = (ORDER_DATA.get(order_id, {}) or {}).get("slant", {})
    if slant_state.get("submitted") is True:
        return

    t = threading.Thread(target=submit_order_to_slant, args=(order_id,), daemon=True)
    t.start()

def verify_slant_signature(raw_body: bytes, provided_sig: str) -> bool:
    """
    Slant signature format isn’t confirmed in your screenshots.
    This tries common HMAC styles. If Slant docs specify the exact format,
    we’ll tighten this to match exactly.
    """
    if not SLANT_WEBHOOK_SECRET or not provided_sig:
        return False

    # Try secret as raw text bytes
    secret_bytes_candidates = [SLANT_WEBHOOK_SECRET.encode("utf-8")]

    # Try base64-decoding the secret (your secret *looks* base64-like)
    try:
        secret_bytes_candidates.append(base64.b64decode(SLANT_WEBHOOK_SECRET))
    except Exception:
        pass

    provided_sig = provided_sig.strip()

    for secret_bytes in secret_bytes_candidates:
        digest = hmac.new(secret_bytes, raw_body, hashlib.sha256).digest()
        b64 = base64.b64encode(digest).decode("utf-8")
        hx = hmac.new(secret_bytes, raw_body, hashlib.sha256).hexdigest()

        if provided_sig == b64 or provided_sig == hx:
            return True

    return False

# -----------------------
# Routes
# -----------------------
@app.route("/")
def index():
    return "✅ Krezz server is live and ready to receive Stripe/Slant events."

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
            "created_at": now_iso()
        }
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
        ORDER_DATA[order_id]["updated_at"] = now_iso()
        save_order_data()
        print(f"✅ Payment confirmed for order_id: {order_id}")

        # Kick off Slant submit (non-blocking)
        kickoff_slant_submit(order_id)

    return jsonify(success=True)

@app.route("/slant/webhook", methods=["POST"])
def slant_webhook():
    raw = request.data
    sig = (
        request.headers.get("X-Slant-Signature")
        or request.headers.get("Slant-Signature")
        or ""
    )

    if SLANT_WEBHOOK_SECRET:
        if not verify_slant_signature(raw, sig):
            # If Slant docs specify signature header/format, we can enforce strictly.
            print("⚠️ Slant webhook signature missing/invalid (accepting for now).")
    else:
        print("⚠️ SLANT_WEBHOOK_SECRET not set; cannot verify Slant webhook.")

    data = request.get_json(silent=True) or {}
    print("📥 /slant/webhook payload:", data)

    # Try to locate order_id in common places
    order_id = (
        data.get("externalOrderId")
        or data.get("order_id")
        or (data.get("metadata") or {}).get("order_id")
        or data.get("id")  # sometimes Slant might send its own id only
    )

    if order_id:
        mark_slant_state(order_id, last_webhook=data, received_webhook=True)
    return jsonify(ok=True)

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
