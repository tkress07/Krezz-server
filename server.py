from flask import Flask, request, jsonify
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

@app.route('/')
def index():
    return '✅ Krezz server is live and ready to receive Stripe events.'

# -------------------- CREATE CHECKOUT SESSION --------------------
@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        data = request.get_json()
        items = data.get("items", [])
        if not items:
            return jsonify({"error": "No items provided"}), 400

        job_id = str(uuid.uuid4())
        ORDER_DATA[job_id] = items

        line_items = []
        for item in items:
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": { "name": item.get("name", "Beard Mold") },
                    "unit_amount": int(item.get("price", 7500)),
                },
                "quantity": 1
            })

        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            line_items=line_items,
            success_url=f'krezzapp://order-confirmed?job_id={job_id}',  # 🔁 Deep link
            cancel_url='https://krezzapp.com/cancel',
            metadata={ "job_id": job_id }
        )

        print(f"✅ Created checkout session: {session.id}")
        return jsonify({ "url": session.url })

    except Exception as e:
        print(f"❌ Error in checkout session: {e}")
        return jsonify({"error": str(e)}), 500

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
            ORDER_DATA[job_id] = []

        for item in ORDER_DATA[job_id]:
            item.update(order)

        print(f"✅ Payment confirmed for job_id: {job_id}")

    return jsonify(success=True)

# -------------------- GET ORDER INFO --------------------
@app.route('/order/<job_id>', methods=['GET'])
def get_order(job_id):
    return jsonify(ORDER_DATA.get(job_id, []))

# -------------------- RUN --------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
