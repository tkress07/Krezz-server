import os
import stripe
import subprocess
from flask import Flask, request, jsonify

app = Flask(__name__)

# Environment vars
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
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except stripe.error.SignatureVerificationError as e:
        print(f"❌ Signature verification failed: {e}")
        return "Invalid signature", 400
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return "Webhook error", 400

    print(f"📦 Received event: {event['type']}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        print("✅ Checkout session completed.")
        session_id = session.get('id')

        # Simulate STL input path
        input_path = f"/tmp/{session_id}_raw.stl"
        output_path = f"/tmp/{session_id}_processed.stl"

        print(f"⚙️ Running Blender: input → {input_path}, output → {output_path}")

        try:
            subprocess.run([
                "blender", "--background",
                "--python", "blender/process_mold.py",
                "--", input_path, output_path
            ], check=True)
            print("✅ Blender finished processing.")
        except subprocess.CalledProcessError as e:
            print(f"❌ Blender error: {e}")
            return "Mold processing failed", 500

        # Optional: Upload processed mold to cloud or notify mobile app

    return jsonify(success=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
