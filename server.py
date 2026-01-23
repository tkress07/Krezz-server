from flask import Flask, request, jsonify, send_file, abort
import stripe
import os
import uuid
import json
import requests
import hmac
import hashlib
from datetime import datetime

app = Flask(__name__)

# -------------------- STRIPE --------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_ENDPOINT_SECRET")
if not stripe.api_key or not endpoint_secret:
    raise ValueError("❌ Stripe environment variables not set.")

# -------------------- STORAGE --------------------
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

# -------------------- SLANT --------------------
SLANT_API_KEY = os.getenv("SLANT_API_KEY")              # sl_...
SLANT_PLATFORM_ID = os.getenv("SLANT_PLATFORM_ID")      # UUID from Slant dashboard
SLANT_WEBHOOK_SECRET = os.getenv("SLANT_WEBHOOK_SECRET")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Slant base from known integrations
SLANT_BASE_URL = os.getenv("SLANT_BASE_URL", "https://www.slant3dapi.com/api").rstrip("/")
# You can override if needed without code changes
SLANT_ORDERS_PATH = os.getenv("SLANT_ORDERS_PATH", "/orders")  # may be "/order" depending on API

def slant_headers():
    # Slant uses "api-key" header in known implementations
    return {
        "api-key": SLANT_API_KEY or "",
        "Content-Type": "application/json"
    }

def public_stl_url(job_id: str) -> str:
    # Slant can often pull from a public file URL (fileURL pattern is common)
    if not PUBLIC_BASE_URL:
        return ""
    return f"{PUBLIC_BASE_URL}/stl/{job_id}.stl"

def mark_slant_state(order_id: str, **kwargs):
    ORDER_DATA.setdefault(order_id, {})
    ORDER_DATA[order_id].setdefault("slant", {})
    ORDER_DATA[order_id]["slant"].update(kwargs)
    save_order_data()

def submit_order_to_slant(order_id: str):
    """
    Called AFTER Stripe confirms payment (webhook).
    Creates a Slant order (or orders) for the paid items.
    """
    if not SLANT_API_KEY or not SLANT_PLATFORM_ID:
        raise RuntimeError("SLANT_API_KEY or SLANT_PLATFORM_ID missing (check Render env vars)")

    data = ORDER_DATA.get(order_id) or {}
    items = data.get("items") or []
    shipping = data.get("shipping") or {}

    if not items:
        raise RuntimeError("No items found on server for this order_id")

    # Map your shipping payload keys into something Slant-style.
    # (Adjust keys if your iOS shipping payload uses different names.)
    full_name = shipping.get("fullName", "")
    email = shipping.get("email", "")
    phone = shipping.get("phone", "")
    address1 = shipping.get("addressLine1", "")
    address2 = shipping.get("addressLine2", "")
    city = shipping.get("city", "")
    state = shipping.get("state", "")
    postal = shipping.get("zipCode", "")
    country = shipping.get("country", "US")

    created = []
    errors = []

    for it in items:
        job_id = (it.get("job_id") or "").strip()
        name = it.get("name", "Beard Mold")
        qty = int(it.get("quantity", 1) or 1)

        stl_url = public_stl_url(job_id)
        if not stl_url:
            errors.append({"job_id": job_id, "error": "PUBLIC_BASE_URL not set; cannot build STL URL"})
            continue

        # This payload shape is intentionally simple and uses the common "fileURL" idea.
        # You may need to tweak field names to match Slant's exact API.
        payload = {
            "platformId": SLANT_PLATFORM_ID,
            "externalOrderId": order_id,
            "itemName": name,
            "quantity": qty,
            "fileURL": stl_url,     # common pattern
            "shipToName": full_name,
            "shipToEmail": email,
            "shipToPhone": phone,
            "shipToStreet1": address1,
            "shipToStreet2": address2,
            "shipToCity": city,
            "shipToState": state,
            "shipToPostalCode": postal,
            "shipToCountry": country,
            "jobId": job_id
        }

        url = f"{SLANT_BASE_URL}{SLANT_ORDERS_PATH}"
        print(f"📤 Submitting to Slant: order_id={order_id} job_id={job_id} url={url}")

        resp = requests.post(url, headers=slant_headers(), json=payload, timeout=30)

        if resp.status_code in (200, 201):
            try:
                j = resp.json()
            except Exception:
                j = {"raw": resp.text}

            created.append({
                "job_id": job_id,
                "status_code": resp.status_code,
                "response": j
            })
        else:
            # IMPORTANT: log body so you can see the real Slant error message
            err = {
                "job_id": job_id,
                "status_code": resp.status_code,
                "body": (resp.text or "")[:2000]
            }
            print(f"❌ Slant submit failed: {err}")
            errors.append(err)

    mark_slant_state(
        order_id,
        submitted=True if created else False,
        submitted_at=datetime.utcnow().isoformat(),
        created=created,
        errors=errors
    )

    return {"created": created, "errors": errors}

# -------------------- ROUTES --------------------
@app.route("/")
def index():
    return "✅ Krezz server is live and ready to receive Stripe & Slant events."

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
            "status": "created"
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
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        print(f"❌ Webhook error: {e}")
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

        # ✅ Submit to Slant after payment
        try:
            result = submit_order_to_slant(order_id)
            print(f"✅ Slant submit result: created={len(result['created'])} errors={len(result['errors'])}")
        except Exception as e:
            print(f"❌ Slant submit exception for order_id={order_id}: {e}")
            mark_slant_state(order_id, submitted=False, error=str(e))

    return jsonify(success=True)

@app.route("/slant/webhook", methods=["POST"])
def slant_webhook():
    # Some platforms sign webhooks; exact header name may vary.
    # This endpoint at least logs what Slant sends and stores it under the order.
    raw = request.data
    headers = dict(request.headers)
    body_text = raw.decode("utf-8", errors="replace")

    print("📦 Slant webhook received:", headers)
    print("📦 Slant webhook body:", body_text[:2000])

    # If Slant sends an orderId in payload, store it
    try:
        data = request.get_json(silent=True) or {}
        order_id = data.get("externalOrderId") or data.get("order_id") or data.get("orderId")
        if order_id:
            ORDER_DATA.setdefault(order_id, {})
            ORDER_DATA[order_id].setdefault("slant_events", [])
            ORDER_DATA[order_id]["slant_events"].append({
                "received_at": datetime.utcnow().isoformat(),
                "payload": data
            })
            save_order_data()
    except Exception as e:
        print("❌ Failed to parse Slant webhook JSON:", e)

    return jsonify({"ok": True})

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
        "slant_events": data.get("slant_events", []),
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
