import os
import uuid
import stripe
import subprocess
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

# Stripe configuration from environment variables
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_ENDPOINT_SECRET")

@app.route('/')
def index():
    return '✅ Krezz server is live and ready to receive requests.'

# -------------------- UPLOAD MOLDS --------------------
@app.route('/upload', methods=['POST'])
def upload():
    job_id = str(uuid.uuid4())
    raw_path = f"/tmp/{job_id}_raw.stl"
    final_path = f"/tmp/{job_id}.stl"

    try:
        with open(raw_path, "wb") as f:
            f.write(request.data)

        print(f"📥 Uploaded mold to {raw_path}")
        print(f"⚙️ Running Blender to process mold...")

        subprocess.run([
            "blender", "--background", "--python", "blender_script.py",
            "--", raw_path, final_path
        ], check=True)

        print(f"✅ Mold processed and saved to {final_path}")
        return jsonify({ "job_id": job_id }), 200

    except Exception as e:
        print(f"❌ Upload or Blender error: {e}")
        return jsonify({ "error": str(e) }), 500

# -------------------- POLL FOR STATUS --------------------
@app.route('/status/<job_id>', methods=['GET'])
def status(job_id):
    result_path = f"/tmp/{job_id}.stl"
    if os.path.exists(result_path):
        print(f"📤 Returning STL for job {job_id}")
        return send_file(result_path, mimetype='application/sla')
    else:
        return jsonify({ "status": "processing" }), 404

# -------------------- STRIPE WEBHOOK --------------------
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
        print("✅ Stripe checkout completed.")
        print(f"🧾 Session ID: {session.get('id')}")

    return jsonify(success=True)

# -------------------- RUN SERVER --------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
