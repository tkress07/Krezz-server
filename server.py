import os
import stripe
from flask import Flask, request, jsonify

app = Flask(__name__)

# Load environment variables
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_ENDPOINT_SECRET")

@app.route('/')
def index():
    return '✅ Krezz server is live and ready to receive Stripe events.'

@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        # Verify signature and parse event
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except stripe.error.SignatureVerificationError as e:
        print(f"❌ Signature verification failed: {e}")
        return "Invalid signature", 400
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return "Webhook error", 400

    # Log received event type
    print(f"📦 Received event: {event['type']}")

    # Handle specific event
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        print("✅ Checkout session completed.")
        print(f"🔗 Session ID: {session.get('id')}")
        # TODO: trigger mold delivery or update status

    return jsonify(success=True)

if __name__ == '__main__':
    # Use 0.0.0.0 so Render can expose it
    app.run(host='0.0.0.0', port=5000)
