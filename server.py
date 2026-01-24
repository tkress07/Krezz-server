from flask import Flask, request, jsonify, send_file, abort
import stripe
import os
import uuid
import json
import hmac
import hashlib
import base64
from datetime import datetime
import requests

app = Flask(__name__)

# -------------------------
# Stripe config (required)
# -------------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET")
if not stripe.api_key or not STRIPE_ENDPOINT_SECRET:
    raise ValueError("❌ Stripe environment variables not set (STRIPE_SECRET_KEY / STRIPE_ENDPOINT_SECRET).")

# -------------------------
# Slant config (optional, but required to submit to Slant)
# -------------------------
SLANT_API_KEY = os.getenv("SLANT_API_KEY")
SLANT_PLATFORM_ID = os.getenv("SLANT_PLATFORM_ID")
SLANT_WEBHOOK_SECRET = os.getenv("SLANT_WEBHOOK_SECRET")  # for /slant/webhook verification

# Make Slant endpoint configurable so you can fix 404s without code changes.
# Your logs show you're posting to https://www.slant3dapi.com/api/orders and getting 404.
# Keep this env var so you can swap to the correct route once you confirm in Slant docs.
SLANT_ORDER_URL = os.getenv("SLANT_ORDER_URL", "https://www.slant3dapi.com/api/orders")

# Your public base URL (used to build STL URLs that Slant can fetch)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://krezz-server.onrender.com").rstrip("/")

# -------------------------
# Storage (Render disk)
# -------------------------
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

# -------------------------
# Helpers
# -------------------------
def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def slant_configured():
    return bool(SLANT_API_KEY and SLANT_PLATFORM_ID)

def stl_public_url(job_id: str) -> str:
    return f"{PUBLIC_BASE_URL}/stl/{job_id}.stl"

def build_slant_payload(order_id: str) -> dict:
    """
    IMPORTANT: Slant's exact order schema must match their docs.
    This is a reasonable "starter payload" using:
    - platformId
    - external order id
    - line items with a file URL (your server hosts the STL)
    - shipping info from your checkout request

    If Slant requires a different shape (e.g., fileServiceId upload step), adjust here.
    """
    data = ORDER_DATA.get(order_id, {})
    items = data.get("items", [])
    shipping = data.get("shipping", {})

    # Normalize shipping fields (keep whatever you already collect)
    ship_to = {
        "fullName": shipping.get("fullName") or shipping.get("name") or "",
        "addressLine": shipping.get("addressLine") or shipping.get("address1") or "",
        "addressLine2": shipping.get("addressLine2") or shipping.get("address2") or "",
        "city": shipping.get("city") or "",
        "state": shipping.get("state") or "",
        "zipCode": shipping.get("zipCode") or shipping.get("zip") or "",
        "country": shipping.get("country") or "US",
        "phone": shipping.get("phone") or "",
        "isResidential": bool(shipping.get("isResidential", True)),
    }

    slant_items = []
    for it in items:
        job_id = it.get("job_id")
        if not job_id:
            continue
        slant_items.append({
            "externalId": job_id,
            "name": it.get("name", "Beard Mold"),
            "quantity": int(it.get("quantity", 1)),
            "fileUrl": stl_public_url(job_id),  # Slant fetches STL from your server
            "material": it.get("material", "PLA"),
            "color": it.get("color", "Black"),
        })

    payload = {
        "platformId": SLANT_PLATFORM_ID,
        "externalOrderId": order_id,
        "items": slant_items,
        "shipping": ship_to,
    }
    return payload

def post_to_slant_with_fallback(payload: dict) -> dict:
    """
    Tries SLANT_ORDER_URL first, then a couple of common variations if Slant returns 404.
    This prevents you from being stuck while you confirm the exact endpoint in docs.
    """
    if not slant_configured():
        return {"ok": False, "status_code": None, "body": "SLANT_API_KEY / SLANT_PLATFORM_ID not configured"}

    # Candidate URLs to try if the first one returns 404
    candidates = [
        SLANT_ORDER_URL,
        "https://slant3dapi.com/api/orders",
        "https://www.slant3dapi.com/api/order",
        "https://slant3dapi.com/api/order",
    ]

    headers = {
        "Content-Type": "application/json",
        "api-key": SLANT_API_KEY,  # Slant uses "api-key" on other endpoints like /api/slicer :contentReference[oaicite:1]{index=1}
    }

    last = None
    for url in candidates:
        try:
            print(f"➡️ Slant endpoint: {url}")
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            body = resp.text
            if resp.status_code == 404:
                # wrong path — try next candidate
                print(f"❌ Slant 404 at {url} (trying next)")
                last = {"ok": False, "status_code": resp.status_code, "body": body, "url": url}
                continue

            # For anything else, stop and return it (success or a real validation/auth error)
            ok = 200 <= resp.status_code < 300
            return {"ok": ok, "status_code": resp.status_code, "body": body, "url": url}

        except Exception as e:
            last = {"ok": False, "status_code": None, "body": str(e), "url": url}

    return last or {"ok": False, "status_code": None, "body": "Unknown Slant submit failure"}

def submit_order_to_slant(order_id: str) -> dict:
    payload = build_slant_payload(order_id)

    if not payload.get("items"):
        msg = "No items with job_id found to submit"
        print(f"❌ {msg} for order_id={order_id}")
        return {"ok": False, "status_code": None, "body": msg}

    print(f"➡️ Submitting to Slant: order_id={order_id}")
    result = post_to_slant_with_fallback(payload)

    # Save result to ORDER_DATA for the app to display
    ORDER_DATA.setdefault(order_id, {})
    ORDER_DATA[order_id]["slant"] = {
        "submitted_at": now_iso(),
        "submitted": bool(result.get("ok")),
        "status_code": result.get("status_code"),
        "response_body": result.get("body"),
        "endpoint_used": result.get("url"),
        "payload_preview": {
            "platformId": payload.get("platformId"),
            "items_count": len(payload.get("items", [])),
        }
    }

    # Update high-level status
    if result.get("ok"):
        ORDER_DATA[order_id]["status"] = "submitted_to_slant"
        print(f"✅ Slant accepted order_id={order_id}")
    else:
        ORDER_DATA[order_id]["status"] = "slant_submit_failed"
        print(f"❌ Slant submit failed for order_id={order_id}: {result}")

    save_order_data()
    return result

def verify_slant_signature(raw_body: bytes, header_value: str) -> bool:
    """
    We don't know Slant's exact signature scheme/headers from your screenshots.
    This is a generic HMAC-SHA256 verifier that supports:
    - hex signatures
    - base64 signatures

    If your Slant webhook uses a different format, adjust accordingly.
    """
    if not SLANT_WEBHOOK_SECRET:
        return True  # if you haven't configured it, skip verification

    secret = SLANT_WEBHOOK_SECRET.encode("utf-8")
    digest = hmac.new(secret, raw_body, hashlib.sha256).digest()

    # compare against hex
    try:
        if hmac.compare_digest(digest.hex(), header_value.strip()):
            return True
    except Exception:
        pass

    # compare against base64
    try:
        if hmac.compare_digest(base64.b64encode(digest).decode("utf-8"), header_value.strip()):
            return True
    except Exception:
        pass

    return False

# -------------------------
# Routes
# -------------------------
@app.route("/")
def index():
    return "✅ Krezz server is live."

@app.route("/health")
def health():
    return jsonify({
        "stripe_configured": bool(stripe.api_key and STRIPE_ENDPOINT_SECRET),
        "slant_configured": slant_configured(),
        "public_base_url": PUBLIC_BASE_URL,
        "slant_order_url": SLANT_ORDER_URL,
        "orders_loaded": len(ORDER_DATA),
    })

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
            "created_at": now_iso(),
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

        ORDER_DATA.setdefault(order_id, {"items": [], "shipping": {}, "status": "created"})
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

        # Try to submit to Slant immediately after payment
        if slant_configured():
            submit_order_to_slant(order_id)
        else:
            print("⚠️ Slant not configured yet; skipping submit.")

    return jsonify(success=True)

@app.route("/slant/webhook", methods=["POST"])
def slant_webhook():
    raw = request.data
    sig = (
        request.headers.get("X-Slant-Signature")
        or request.headers.get("Slant-Signature")
        or request.headers.get("X-Signature")
        or ""
    )

    # Verify if a signature header is present
    if sig and not verify_slant_signature(raw, sig):
        print("❌ Slant webhook signature verification failed")
        return "Invalid signature", 400

    try:
        evt = request.get_json(force=True)
    except Exception:
        evt = None

    print(f"📦 Slant webhook received: {evt}")

    # Try to update local order state (best effort — depends on Slant's payload schema)
    if isinstance(evt, dict):
        order_id = evt.get("externalOrderId") or evt.get("orderId") or evt.get("order_id")
        status = evt.get("status") or evt.get("state")
        tracking = evt.get("trackingNumber") or evt.get("tracking") or None

        if order_id:
            ORDER_DATA.setdefault(order_id, {})
            ORDER_DATA[order_id].setdefault("slant", {})
            if status:
                ORDER_DATA[order_id]["status"] = f"slant_{status}"
                ORDER_DATA[order_id]["slant"]["status"] = status
            if tracking:
                ORDER_DATA[order_id]["slant"]["tracking"] = tracking
            ORDER_DATA[order_id]["slant"]["last_webhook_at"] = now_iso()
            save_order_data()
            print(f"✅ Updated order_id={order_id} from Slant webhook")

    return jsonify(success=True)

@app.route("/slant/resubmit/<order_id>", methods=["POST"])
def slant_resubmit(order_id):
    if not slant_configured():
        return jsonify({"error": "Slant not configured"}), 400
    if order_id not in ORDER_DATA:
        return jsonify({"error": "Order ID not found"}), 404
    result = submit_order_to_slant(order_id)
    return jsonify(result)

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
    return jsonify({"success": True, "path": save_path, "public_url": stl_public_url(job_id)})

@app.route("/stl/<job_id>.stl", methods=["GET"])
def serve_stl(job_id):
    stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        return abort(404)
    return send_file(stl_path, mimetype="application/sla", as_attachment=True,
                     download_name=f"mold_{job_id}.stl")
