from flask import Flask, request, jsonify, send_file, abort
import stripe
import os
import uuid
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone

# If you don't already have requests in requirements.txt, add it.
import requests

app = Flask(__name__)

# ----------------------------
# Stripe config
# ----------------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET")

if not stripe.api_key or not STRIPE_ENDPOINT_SECRET:
    raise ValueError("❌ Stripe environment variables not set.")

# ----------------------------
# Slant config (server-only)
# ----------------------------
SLANT_API_KEY = os.getenv("SLANT_API_KEY")  # e.g. sl-...
SLANT_PLATFORM_ID = os.getenv("SLANT_PLATFORM_ID")  # from Slant dashboard platform
SLANT_WEBHOOK_SECRET = os.getenv("SLANT_WEBHOOK_SECRET")  # from Slant dashboard platform
SLANT_BASE_URL = os.getenv("SLANT_BASE_URL", "https://www.slant3dapi.com/api")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://krezz-server.onrender.com").rstrip("/")
SLANT_VERIFY_WEBHOOKS = os.getenv("SLANT_VERIFY_WEBHOOKS", "true").lower() == "true"

# ----------------------------
# Storage
# ----------------------------
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATA_PATH = os.getenv("ORDER_DATA_PATH", "/data/order_data.json")
ORDER_DATA = {}

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

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
# Helpers
# ----------------------------
def public_stl_url(job_id: str) -> str:
    return f"{PUBLIC_BASE_URL}/stl/{job_id}.stl"

def normalize_bool_str(v) -> str:
    s = str(v).strip().lower()
    return "true" if s in ("true", "1", "yes", "y") else "false"

def slant_headers():
    if not SLANT_API_KEY:
        raise ValueError("SLANT_API_KEY not set")
    return {
        "api-key": SLANT_API_KEY,
        "Content-Type": "application/json",
    }

def safe_json(obj, max_len=5000):
    """Ensure something serializable and not enormous."""
    try:
        s = json.dumps(obj)
        if len(s) > max_len:
            return {"truncated": True, "preview": s[:max_len]}
        return obj
    except Exception:
        return {"unserializable": True, "type": str(type(obj))}

def slant_submit_order(order_id: str):
    """
    Submit manufacturing order to Slant.
    - Idempotent: won't submit twice.
    - Stores response/errors under ORDER_DATA[order_id]["slant"].
    """
    if order_id not in ORDER_DATA:
        raise ValueError(f"order_id not found: {order_id}")

    o = ORDER_DATA[order_id]
    slant_state = o.get("slant", {})

    if slant_state.get("submitted") is True:
        print(f"ℹ️ Slant already submitted for order_id={order_id} (skipping)")
        return slant_state

    if not SLANT_API_KEY:
        raise ValueError("SLANT_API_KEY not configured")
    if not SLANT_PLATFORM_ID:
        print("⚠️ SLANT_PLATFORM_ID not configured (continuing, but Slant may require it)")
    if not SLANT_WEBHOOK_SECRET:
        print("⚠️ SLANT_WEBHOOK_SECRET not configured (webhook verification may fail)")

    items = o.get("items", [])
    shipping = o.get("shipping", {}) or {}

    # Basic shipping fields from your payload
    full_name = shipping.get("fullName", "")
    email = shipping.get("email", "")
    phone = shipping.get("phone", "")
    street_1 = shipping.get("addressLine", "")
    city = shipping.get("city", "")
    state = shipping.get("state", "")
    zip_code = shipping.get("zipCode", "")
    country = shipping.get("country", "United States")
    is_res = normalize_bool_str(shipping.get("isResidential", "true"))

    color = shipping.get("color", "White")
    material = shipping.get("material", "PLA")
    # Simple profile mapping; adjust to exact Slant profile names once you confirm them.
    profile = "PLA" if "PLA" in str(material).upper() else str(material)

    # Build Slant payload (one per STL/job_id)
    slant_payload = []
    for it in items:
        job_id = it.get("job_id")
        name = it.get("name", "Beard Mold")
        qty = 1

        if not job_id:
            continue

        slant_payload.append({
            # Recommended: include your platform id if Slant expects it
            "platformId": SLANT_PLATFORM_ID,

            # Identify order
            "orderNumber": order_id,

            # STL access
            "filename": f"{job_id}.stl",
            "fileURL": public_stl_url(job_id),

            # Item details
            "order_item_name": name,
            "order_quantity": str(qty),
            "order_item_color": color,
            "profile": profile,

            # Customer contact
            "email": email,
            "phone": phone,
            "name": full_name,

            # Billing (using shipping as billing for now)
            "bill_to_street_1": street_1,
            "bill_to_city": city,
            "bill_to_state": state,
            "bill_to_zip": zip_code,
            "bill_to_country_as_iso": "US",
            "bill_to_is_US_residential": is_res,

            # Shipping
            "ship_to_name": full_name,
            "ship_to_street_1": street_1,
            "ship_to_city": city,
            "ship_to_state": state,
            "ship_to_zip": zip_code,
            "ship_to_country_as_iso": "US",
            "ship_to_is_US_residential": is_res,
        })

    if not slant_payload:
        raise ValueError("No valid items with job_id to submit to Slant")

    # Submit
    url = f"{SLANT_BASE_URL}/order"
    print(f"📤 Submitting to Slant: order_id={order_id} url={url} items={len(slant_payload)}")

    resp = requests.post(url, headers=slant_headers(), json=slant_payload, timeout=30)

    # Store response no matter what (for debugging)
    slant_state = {
        "submitted": resp.ok,
        "submitted_at": utc_now_iso(),
        "http_status": resp.status_code,
        "response": safe_json(resp.json() if resp.content else {}),
    }

    if resp.ok:
        ORDER_DATA[order_id]["status"] = "submitted_to_slant"
        ORDER_DATA[order_id]["slant"] = slant_state
        save_order_data()
        print(f"✅ Slant submitted for order_id={order_id}")
        return slant_state

    # Failure
    try:
        body_text = resp.text[:2000]
    except Exception:
        body_text = "<unreadable>"

    slant_state["error"] = f"Slant submit failed: HTTP {resp.status_code}"
    slant_state["body_preview"] = body_text

    ORDER_DATA[order_id]["status"] = "slant_submit_failed"
    ORDER_DATA[order_id]["slant"] = slant_state
    save_order_data()

    print(f"❌ Slant submit failed for order_id={order_id}: HTTP {resp.status_code}")
    return slant_state

def verify_slant_signature(raw_body: bytes, headers) -> bool:
    """
    Slant signature header name/format may vary.
    This verifier tries common patterns:
    - hex HMAC-SHA256
    - base64 HMAC-SHA256
    - versioned header like: "v1=...."
    """
    if not SLANT_WEBHOOK_SECRET:
        # If you haven't set the secret yet, we can't verify.
        return False

    # Common possible header names
    sig = (
        headers.get("X-Slant-Signature")
        or headers.get("x-slant-signature")
        or headers.get("Webhook-Signature")
        or headers.get("webhook-signature")
        or headers.get("X-Webhook-Signature")
        or headers.get("x-webhook-signature")
        or ""
    ).strip()

    if not sig:
        return False

    secret = SLANT_WEBHOOK_SECRET.encode("utf-8")
    mac = hmac.new(secret, raw_body, hashlib.sha256).digest()

    expected_hex = mac.hex()
    expected_b64 = base64.b64encode(mac).decode("utf-8")

    # Handle "v1=..." style
    candidates = [sig]
    if "," in sig:
        candidates = [p.strip() for p in sig.split(",") if p.strip()]
    if " " in sig:
        candidates += [p.strip() for p in sig.split(" ") if p.strip()]

    cleaned = []
    for c in candidates:
        if "=" in c and c.lower().startswith("v1"):
            _, val = c.split("=", 1)
            cleaned.append(val.strip())
        else:
            cleaned.append(c)

    for c in cleaned:
        if hmac.compare_digest(c, expected_hex) or hmac.compare_digest(c, expected_b64):
            return True

    return False

def extract_order_id_from_slant_event(evt: dict) -> str | None:
    # Try common places an order number might appear
    return (
        evt.get("orderNumber")
        or evt.get("order_id")
        or evt.get("orderId")
        or (evt.get("data") or {}).get("orderNumber")
        or (evt.get("data") or {}).get("order_id")
    )

def extract_status_from_slant_event(evt: dict) -> str | None:
    return (
        evt.get("status")
        or evt.get("state")
        or evt.get("event")
        or evt.get("eventType")
        or (evt.get("data") or {}).get("status")
        or (evt.get("data") or {}).get("state")
    )

def maybe_promote_status_from_slant(order_id: str, slant_status: str | None):
    if not slant_status:
        return

    s = slant_status.lower()
    # Best-effort mapping
    if "ship" in s:
        ORDER_DATA[order_id]["status"] = "shipped"
    elif "print" in s or "production" in s or "manufactur" in s:
        ORDER_DATA[order_id]["status"] = "in_production"
    elif "received" in s or "created" in s or "queued" in s:
        ORDER_DATA[order_id]["status"] = "submitted_to_slant"

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

        # Normalize items
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
            "created_at": utc_now_iso()
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
            "created": datetime.fromtimestamp(session["created"], tz=timezone.utc).isoformat(),
            "email": session.get("customer_email", "unknown"),
            "status": "paid"
        }
        ORDER_DATA[order_id]["paid_at"] = utc_now_iso()
        save_order_data()
        print(f"✅ Payment confirmed for order_id: {order_id}")

        # ✅ NEW: submit to Slant after payment
        try:
            slant_state = slant_submit_order(order_id)
            print(f"✅ Slant state for order_id={order_id}: submitted={slant_state.get('submitted')}")
        except Exception as e:
            print(f"❌ Slant submit exception for order_id={order_id}: {e}")
            ORDER_DATA[order_id]["status"] = "slant_submit_failed"
            ORDER_DATA[order_id]["slant"] = {
                "submitted": False,
                "submitted_at": utc_now_iso(),
                "error": str(e)
            }
            save_order_data()

    return jsonify(success=True)

@app.route("/slant/webhook", methods=["POST"])
def slant_webhook():
    raw = request.data

    if SLANT_VERIFY_WEBHOOKS:
        ok = verify_slant_signature(raw, request.headers)
        if not ok:
            # Print headers to help you find the right signature header name during setup
            print("❌ Slant webhook signature failed or missing.")
            print("🔎 Slant webhook headers (subset):",
                  {k: v for k, v in dict(request.headers).items()
                   if "sig" in k.lower() or "hook" in k.lower()})
            return "Bad signature", 400
    else:
        print("⚠️ SLANT_VERIFY_WEBHOOKS=false (accepting unsigned webhooks)")

    evt = request.get_json(silent=True) or {}
    print("📦 Slant webhook event:", evt)

    order_id = extract_order_id_from_slant_event(evt)
    if not order_id:
        print("⚠️ Slant webhook missing order id; storing as last_unmatched_event")
        ORDER_DATA.setdefault("_slant_unmatched", {})
        ORDER_DATA["_slant_unmatched"]["last_event"] = evt
        save_order_data()
        return jsonify(success=True)

    ORDER_DATA.setdefault(order_id, {"items": [], "shipping": {}, "status": "created"})
    ORDER_DATA[order_id].setdefault("slant", {})

    slant_status = extract_status_from_slant_event(evt)
    ORDER_DATA[order_id]["slant"]["last_event"] = evt
    ORDER_DATA[order_id]["slant"]["last_event_at"] = utc_now_iso()
    if slant_status:
        ORDER_DATA[order_id]["slant"]["last_status"] = slant_status

    # Try to capture common tracking fields if they exist
    tracking = (
        evt.get("tracking")
        or evt.get("trackingNumber")
        or (evt.get("data") or {}).get("tracking")
        or (evt.get("data") or {}).get("trackingNumber")
    )
    if tracking:
        ORDER_DATA[order_id]["slant"]["tracking"] = tracking

    maybe_promote_status_from_slant(order_id, slant_status)

    save_order_data()
    print(f"✅ Slant webhook applied to order_id={order_id} status={ORDER_DATA[order_id].get('status')}")
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
    # as_attachment=False is friendlier for Slant to fetch programmatically
    return send_file(
        stl_path,
        mimetype="application/octet-stream",
        as_attachment=False,
        download_name=f"mold_{job_id}.stl"
    )

# Optional: manual re-submit endpoint for debugging (server-only)
@app.route("/slant/submit/<order_id>", methods=["POST"])
def manual_slant_submit(order_id):
    try:
        state = slant_submit_order(order_id)
        return jsonify({"success": True, "slant": state})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400
