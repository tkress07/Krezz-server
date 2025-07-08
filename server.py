from flask import Flask, request, jsonify
import stripe
import os

app = Flask(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_ENDPOINT_SECRET")

@app.route('/')
def index():
    return '✅ Krezz server is live and ready to receive Stripe events.'

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': { 'name': 'Custom Beard Mold' },
                    'unit_amount': 7500,
                },
                'quantity': 1,
            }],
            success_url='https://your-app.com/success',
            cancel_url='https://your-app.com/cancel'
        )
        return jsonify({ 'url': session.url })
    except Exception as e:
        return jsonify({ 'error': str(e) }), 500

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
        print(f"❌ Unexpected error: {e}")
        return "Webhook error", 400

    print(f"📦 Event: {event['type']}")
    return jsonify(success=True)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
