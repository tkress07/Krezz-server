from flask import Flask, request, jsonify, send_file, abort
import stripe
import os
import uuid
from datetime import datetime

app = Flask(__name__)

# ✅ Load from environment variables (safe for GitHub)
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_ENDPOINT_SECRET")

if not stripe.api_key or not endpoint_secret:
    raise ValueError("❌ Stripe environment variables not set.")

ORDER_DATA = {}

UPLOAD_DIR = "/data/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.route('/')
def index():
    return '✅ Krezz server is live and ready to receive Stripe events.'

# -------------------- CREATE CHECKOUT SESSION --------------------
@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        data = request.get_json()
        items = data.get("items", [])
        shipping_info = data.get("shippingInfo", {})

        if not items:
            return jsonify({"error": "No items provided"}), 400

        job_id = items[0].get("job_id") or str(uuid.uuid4())

        # ✅ Store both items and shipping info
        ORDER_DATA[job_id] = {
            "items": items,
            "shipping": shipping_info
        }

        line_items = []
        for item in items:
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": item.get("name", "Beard Mold")
                    },
                    "unit_amount": int(item.get("price", 7500)),
                },
                "quantity": 1
            })

        # ✅ Pass shipping info in metadata (optional but useful)
        metadata = {
            "job_id": job_id,
            "shipping_name": shipping_info.get("fullName", ""),
            "shipping_email": shipping_info.get("email", ""),
            "shipping_phone": shipping_info.get("phone", ""),
            "shipping_address": shipping_info.get("addressLine", ""),
            "shipping_city": shipping_info.get("city", ""),
            "shipping_state": shipping_info.get("state", ""),
            "shipping_zip": shipping_info.get("zipCode", ""),
            "shipping_country": shipping_info.get("country", ""),
            "shipping_material": shipping_info.get("material", ""),
            "shipping_color": shipping_info.get("color", "")
        }

        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            line_items=line_items,
            success_url=f'krezzapp://order-confirmed?job_id={job_id}',  # 🔁 Deep link
            cancel_url='https://krezzapp.com/cancel',
            metadata=metadata
        )

        print(f"✅ Created checkout session: {session.id}")
        return jsonify({ "url": session.url })

    except Exception as e:
        print(f"❌ Error in checkout session: {e}")
        return jsonify({ "error": str(e) }), 500

# -------------------- WEBHOOK HANDLER --------------------
@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except stripe.error.SignatureVerificationError as e:
        print(f"❌ Invalid signature: {e}")
        return "Invalid signature", 400
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return "Webhook error", 400

    print(f"📦 Stripe event: {event['type']}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        job_id = session.get("metadata", {}).get("job_id")

        order = {
            "id": session["id"],
            "amount_total": session["amount_total"],
            "currency": session["currency"],
            "created": datetime.utcfromtimestamp(session["created"]).isoformat(),
            "email": session.get("customer_email", "unknown"),
            "status": "paid"
        }

        if job_id not in ORDER_DATA:
            ORDER_DATA[job_id] = { "items": [], "shipping": {} }

        for item in ORDER_DATA[job_id]["items"]:
            item.update(order)

        print(f"✅ Payment confirmed for job_id: {job_id}")

    return jsonify(success=True)

# -------------------- GET ORDER INFO --------------------
@app.route('/order-data/<job_id>', methods=['GET'])
def get_order_data(job_id):
    print(f"📥 Fetch order-data for job_id: {job_id}")
    data = ORDER_DATA.get(job_id)
    print(f"🧾 ORDER_DATA content for job_id: {data}")
    if data:
        return jsonify({
            "job_id": job_id,
            "items": data.get("items", []),
            "shipping": data.get("shipping", {})
        })
    else:
        print("❌ Job ID not found")
        return jsonify({"error": "Job ID not found"}), 404

# -------------------- SERVE STL FILE --------------------
@app.route('/stl/<job_id>.stl', methods=['GET'])
def serve_stl(job_id):
    stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")

    if not os.path.exists(stl_path):
        print(f"❌ STL not found at: {stl_path}")
        return abort(404)
    
    print(f"📤 Serving STL file: {stl_path}")
    return send_file(
        stl_path,
        mimetype='application/sla',
        as_attachment=True,
        download_name=f"mold_{job_id}.stl"
    )

# -------------------- UPLOAD STL FILE --------------------
@app.route('/upload', methods=['POST'])
def upload_stl():
    job_id = request.form.get('job_id')
    file = request.files.get('file')

    if not job_id or not file:
        return jsonify({ "error": "Missing job_id or file" }), 400

    save_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    try:
        file.save(save_path)
        print(f"✅ Uploaded STL for job_id: {job_id} -> {save_path}")
        return jsonify({ "success": True, "path": save_path })
    except Exception as e:
        print(f"❌ Failed to save STL: {e}")
        return jsonify({ "error": str(e) }), 500

# -------------------- RUN --------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
