from flask import Flask, request, jsonify
import stripe
import os

app = Flask(__name__)

# Load Stripe credentials from environment
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_ENDPOINT_SECRET")

if not stripe.api_key:
    raise ValueError("❌ STRIPE_SECRET_KEY is not set in environment.")

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
            return jsonify({ "error": "No items provided" }), 400

        line_items = []
        for item in items:
            name = item.get("name", "Beard Mold")
            price = item.get("price", 7500)  # price in cents
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": { "name": name },
                    "unit_amount": int(price),
                },
                "quantity": 1
            })

        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            line_items=line_items,
            success_url='https://example.com/success',
            cancel_url='https://example.com/cancel'
        )

        print(f"✅ Stripe Checkout Session created: {session.url}")
        return jsonify({ "url": session.url })

    except Exception as e:
        print(f"❌ Error creating checkout session: {e}")
        return jsonify({ "error": str(e) }), 500

# -------------------- STRIPE WEBHOOK LISTENER --------------------
@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except stripe.error.SignatureVerificationError as e:
        print(f"❌ Signature verification failed: {e}")
        return "Invalid signature", 400
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return "Webhook error", 400

    print(f"📦 Stripe event received: {event['type']}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        print("✅ Payment completed successfully.")
        print(f"🧾 Session ID: {session.get('id')}")

    return jsonify(success=True)

# -------------------- RUN SERVER --------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
