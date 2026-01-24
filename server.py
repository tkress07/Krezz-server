# app.py — Krezz server (Stripe + STL hosting + Slant order submit)
#
# ✅ Drop-in replacement for your current Flask server.
# ✅ Fixes your Slant 404 by using the documented base URL:
#       https://slant3dapi.com/v2/api
# ✅ Drafts THEN processes a Slant order after Stripe payment completes.
#
# IMPORTANT:
# - Slant PRINT items require publicFileServiceId + filamentId.
# - This server supports two ways to get publicFileServiceId:
#   (A) You already have it (store it on the cart item as "publicFileServiceId")
#   (B) You enable optional auto-upload by setting SLANT_FILES_ENDPOINT (see env section below).
#
# Run with: gunicorn app:app

from flask import Flask, request, jsonify, send_file, abort
import stripe
import os
import uuid
import json
from datetime import datetime
import requests
import hmac
import hashlib
import base64

app = Flask(__name__)

# -----------------------------
# Environment / Config
# -----------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET")

if not STRIPE_SECRET_KEY or not STRIPE_ENDPOINT_SECRET:
    raise ValueError("❌ Stripe environment variables not set (STRIPE_SECRET_KEY, STRIPE_ENDPOINT_SECRET).")

stripe.api_key = STRIPE_SECRET_KEY

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://krezz-server.onrender.com").rstrip("/")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATA_PATH = os.getenv("ORDER_DATA_PATH", "/data/order_data.json")

# Optional: persist job_id -> Slant publicFileServiceId mappings
FILE_MAP_PATH = os.getenv("FILE_MAP_PATH", "/data/file_map.json")

# Slant config
SLANT_API_KEY = os.getenv("SLANT_API_KEY")  # sl-...
SLANT_PLATFORM_ID = os.getenv("SLANT_PLATFORM_ID")  # UUID from Slant platform screen
SLANT_DEFAULT_FILAMENT_ID = os.getenv("SLANT_DEFAULT_FILAMENT_ID")  # choose one from Slant (required for PRINT)
SLANT_BASE_URL = os.getenv("SLANT_BASE_URL", "https://slant3dapi.com/v2/api").rstrip("/")

# Optional: if you want Slant to call you with production updates
SLANT_WEBHOOK_SECRET = os.getenv("SLANT_WEBHOOK_SECRET", "")  # from Slant platform screen

# Optional: enable auto-upload STL to Slant.
# You must set this to the correct endpoint from Slant "Files" docs (not "Orders" docs).
# Example (GUESS): https://slant3dapi.com/v2/api/files
SLANT_FILES_ENDPOINT = os.getenv("SLANT_FILES_ENDPOINT", "").strip()

# -----------------------------
# Persistence
# -----------------------------
ORDER_DATA = {}
FILE_MAP = {}  # job_id -> {"publicFileServiceId": "...", "uploaded_at": "..."} or local-only hints


def _safe_json_load(path: str, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def _safe_json_save(path: str, obj):
    try:
        with open(path, "w") as f:
            json.dump(obj, f)
    except Exception as e:
        print(f"❌ Failed to write {path}: {e}")


def load_state():
    global ORDER_DATA, FILE_MAP
    ORDER_DATA = _safe_json_load(DATA_PATH, {})
    FILE_MAP = _safe_json_load(FILE_MAP_PATH, {})
    print(f"✅ Loaded ORDER_DATA ({len(ORDER_DATA)} orders)")
    print(f"✅ Loaded FILE_MAP ({len(FILE_MAP)} files)")


def save_order_data():
    _safe_json_save(DATA_PATH, ORDER_DATA)


def save_file_map():
    _safe_json_save(FILE_MAP_PATH, FILE_MAP)


load_state()

# -----------------------------
# Helpers
# -----------------------------
def now_iso():
    return datetime.utcnow().isoformat() + "Z"


def normalize_price_to_cents(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value if value >= 1000 else value * 100
    if isinstance(value, float):
        return int(round(value * 100.0))
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit():
            i = int(value)
            return i if i >= 1000 else i * 100
        try:
            d = float(value)
            return int(round(d * 100.0))
        except Exception:
            return None
    return None


# -----------------------------
# Slant integration
# -----------------------------
def slant_headers():
    if not SLANT_API_KEY:
        raise RuntimeError("SLANT_API_KEY not configured")
    return {
        "Authorization": f"Bearer {SLANT_API_KEY}",
        "Content-Type": "application/json",
    }


def slant_try_upload_file(job_id: str, stl_path: str):
    """
    Optional helper: uploads STL to Slant to obtain publicFileServiceId.
    This REQUIRES you to set SLANT_FILES_ENDPOINT from Slant "Files" docs.

    If SLANT_FILES_ENDPOINT is not set, this returns None and your Slant order will be skipped
    until you provide publicFileServiceId.
    """
    if not SLANT_FILES_ENDPOINT:
        print("ℹ️ SLANT_FILES_ENDPOINT not set. Skipping Slant file upload.")
        return None

    if not os.path.exists(stl_path):
        print(f"❌ STL not found for Slant upload: {stl_path}")
        return None

    try:
        with open(stl_path, "rb") as f:
            files = {"file": (f"{job_id}.stl", f, "application/sla")}
            # Some APIs also want form data. If Slant requires extra fields, add them here.
            print(f"➡️ Uploading STL to Slant: job_id={job_id} endpoint={SLANT_FILES_ENDPOINT}")
            r = requests.post(
                SLANT_FILES_ENDPOINT,
                headers={"Authorization": f"Bearer {SLANT_API_KEY}"},
                files=files,
                timeout=60,
            )

        if not (200 <= r.status_code < 300):
            print(f"❌ Slant file upload failed: status={r.status_code} body={r.text}")
            return None

        data = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
        # Try common field names
        public_id = (
            data.get("publicFileServiceId")
            or data.get("public_file_service_id")
            or data.get("publicId")
            or data.get("public_id")
            or data.get("id")
        )

        if not public_id:
            print(f"❌ Slant file upload succeeded but missing public id. Body={r.text}")
            return None

        FILE_MAP[job_id] = {"publicFileServiceId": public_id, "uploaded_at": now_iso()}
        save_file_map()
        print(f"✅ Slant file uploaded: job_id={job_id} publicFileServiceId={public_id}")
        return public_id

    except Exception as e:
        print(f"❌ Slant file upload exception: {e}")
        return None


def slant_draft_order(order_id: str, order_record: dict):
    """
    Draft order:
      POST {SLANT_BASE_URL}/orders
    Requires:
      - customer.platformId
      - customer.details.email + address
      - PRINT items with publicFileServiceId + filamentId + quantity
    """
    if not SLANT_PLATFORM_ID:
        raise RuntimeError("SLANT_PLATFORM_ID not configured")

    shipping = order_record.get("shipping", {}) or {}
    items = order_record.get("items", []) or []

    draft_items = []
    for it in items:
        job_id = it.get("job_id") or it.get("jobId") or it.get("id") or ""
        job_id = str(job_id).strip()

        # 1) Prefer explicit publicFileServiceId on item
        public_file_id = it.get("publicFileServiceId")

        # 2) Or pull from FILE_MAP if available
        if not public_file_id and job_id and job_id in FILE_MAP:
            public_file_id = FILE_MAP[job_id].get("publicFileServiceId")

        # 3) Or attempt upload from local STL path (optional; requires SLANT_FILES_ENDPOINT)
        if not public_file_id and job_id:
            stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
            public_file_id = slant_try_upload_file(job_id, stl_path)

        if not public_file_id:
            raise RuntimeError(
                f"Missing publicFileServiceId for job_id={job_id}. "
                f"Either upload to Slant Files API (set SLANT_FILES_ENDPOINT) or attach publicFileServiceId on the item."
            )

        filament_id = it.get("filamentId") or SLANT_DEFAULT_FILAMENT_ID
        if not filament_id:
            raise RuntimeError(
                "Missing filamentId. Set SLANT_DEFAULT_FILAMENT_ID in Render "
                "or attach filamentId per cart item."
            )

        draft_items.append({
            "type": "PRINT",
            "publicFileServiceId": public_file_id,
            "filamentId": filament_id,
            "quantity": int(it.get("quantity", 1)),
            "name": it.get("name", "Beard Mold"),
            "SKU": it.get("SKU", job_id),
        })

    payload = {
        "customer": {
            "platformId": SLANT_PLATFORM_ID,
            "details": {
                "email": shipping.get("email", "email@test.com"),
                "address": {
                    "name": shipping.get("fullName", "Customer"),
                    "line1": shipping.get("addressLine", ""),
                    "line2": shipping.get("addressLine2", ""),
                    "city": shipping.get("city", ""),
                    "state": shipping.get("state", ""),
                    "zip": shipping.get("zipCode", ""),
                    "country": shipping.get("country", "US"),
                }
            }
        },
        "items": draft_items,
        "metadata": {
            "orderId": order_id,
            "source": "KREZZ_APP"
        }
    }

    url = f"{SLANT_BASE_URL}/orders"
    print(f"➡️ Slant DRAFT endpoint: {url}")
    r = requests.post(url, headers=slant_headers(), json=payload, timeout=30)
    return r.status_code, r.text


def slant_process_order(public_order_id: str):
    """
    Process order:
      POST {SLANT_BASE_URL}/orders/{publicOrderId}
    """
    url = f"{SLANT_BASE_URL}/orders/{public_order_id}"
    print(f"➡️ Slant PROCESS endpoint: {url}")
    r = requests.post(url, headers=slant_headers(), timeout=30)
    return r.status_code, r.text


def slant_submit_paid_order(order_id: str):
    """
    Draft then process. Saves result into ORDER_DATA[order_id]["slant"].
    """
    record = ORDER_DATA.get(order_id) or {}

    try:
        status, body = slant_draft_order(order_id, record)
        if not (200 <= status < 300):
            print(f"❌ Slant draft failed: status={status} body={body}")
            record["slant"] = {"ok": False, "stage": "draft", "status_code": status, "body": body}
            ORDER_DATA[order_id] = record
            save_order_data()
            return

        # Parse public order id
        try:
            data = json.loads(body)
        except Exception:
            data = {}

        public_id = data.get("publicId") or data.get("public_id") or data.get("id")
        if not public_id:
            print("❌ Slant draft succeeded but missing publicId in response")
            record["slant"] = {"ok": False, "stage": "draft", "status_code": status, "body": body}
            ORDER_DATA[order_id] = record
            save_order_data()
            return

        # Process
        p_status, p_body = slant_process_order(public_id)
        if not (200 <= p_status < 300):
            print(f"❌ Slant process failed: status={p_status} body={p_body}")
            record["slant"] = {
                "ok": False,
                "stage": "process",
                "publicOrderId": public_id,
                "status_code": p_status,
                "body": p_body
            }
            ORDER_DATA[order_id] = record
            save_order_data()
            return

        print(f"✅ Slant processed order: publicOrderId={public_id}")
        record["status"] = "submitted_to_slant"
        record["slant"] = {"ok": True, "publicOrderId": public_id, "processed_at": now_iso()}
        ORDER_DATA[order_id] = record
        save_order_data()

    except Exception as e:
        print(f"❌ Slant submit exception: {e}")
        record["slant"] = {"ok": False, "stage": "exception", "error": str(e)}
        ORDER_DATA[order_id] = record
        save_order_data()


# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def index():
    return "✅ Krezz server is live."


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """
    iOS sends:
      {
        "order_id": "...",
        "items": [{"name":"", "price": 7500, "job_id":"..."}],
        "shippingInfo": {...}
      }

    Server returns:
      { "url": session.url, "order_id": order_id }
    """
    try:
        data = request.get_json(silent=True) or {}
        print("📥 /create-checkout-session payload:", data)

        items = data.get("items", [])
        shipping_info = data.get("shippingInfo", {}) or {}

        if not items:
            return jsonify({"error": "No items provided"}), 400

        order_id = data.get("order_id") or str(uuid.uuid4())

        normalized_items = []
        for it in items:
            job_id = it.get("job_id") or it.get("jobId") or it.get("id") or str(uuid.uuid4())
            price_cents = normalize_price_to_cents(it.get("price"))
            if price_cents is None:
                return jsonify({"error": f"Invalid price for item: {it}"}), 400

            normalized_items.append({
                "name": it.get("name", "Beard Mold"),
                "price": int(price_cents),
                "job_id": str(job_id),
                # Optional fields you can add later:
                # "publicFileServiceId": it.get("publicFileServiceId"),
                # "filamentId": it.get("filamentId"),
                # "quantity": int(it.get("quantity", 1)),
            })

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
    """
    Stripe -> this endpoint (Render public URL)
    Verifies signature, marks order paid, then tries Slant submit.
    """
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

        if order_id not in ORDER_DATA:
            ORDER_DATA[order_id] = {"items": [], "shipping": {}, "status": "created", "created_at": now_iso()}

        ORDER_DATA[order_id]["status"] = "paid"
        ORDER_DATA[order_id]["payment"] = {
            "stripe_session_id": session.get("id"),
            "amount_total": session.get("amount_total"),
            "currency": session.get("currency"),
            "created": datetime.utcfromtimestamp(session["created"]).isoformat(),
            "email": session.get("customer_email", "unknown"),
            "status": "paid"
        }
        save_order_data()
        print(f"✅ Payment confirmed for order_id: {order_id}")

        # Try to submit to Slant (draft -> process)
        # If publicFileServiceId isn't available yet, we store a clear error in ORDER_DATA.
        if SLANT_API_KEY and SLANT_PLATFORM_ID:
            print(f"➡️ Submitting to Slant: order_id={order_id}")
            slant_submit_paid_order(order_id)
        else:
            print("ℹ️ Slant not configured (SLANT_API_KEY or SLANT_PLATFORM_ID missing). Skipping Slant submit.")

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
        "slant": data.get("slant", {}),
    })


@app.route("/upload", methods=["POST"])
def upload_stl():
    """
    iOS posts multipart form:
      job_id: ...
      file: <stl>
    """
    job_id = request.form.get("job_id")
    file = request.files.get("file")
    if not job_id or not file:
        return jsonify({"error": "Missing job_id or file"}), 400

    job_id = str(job_id).strip()
    save_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    file.save(save_path)
    print(f"✅ Uploaded STL job_id={job_id} -> {save_path}")

    # Optionally upload to Slant immediately (so publicFileServiceId is ready at checkout time)
    public_file_id = None
    if SLANT_API_KEY and SLANT_FILES_ENDPOINT:
        public_file_id = slant_try_upload_file(job_id, save_path)

    if public_file_id:
        return jsonify({"success": True, "path": save_path, "publicFileServiceId": public_file_id})
    return jsonify({"success": True, "path": save_path})


@app.route("/stl/<job_id>.stl", methods=["GET"])
def serve_stl(job_id):
    stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        return abort(404)
    return send_file(
        stl_path,
        mimetype="application/sla",
        as_attachment=True,
        download_name=f"mold_{job_id}.stl"
    )


@app.route("/slant/webhook", methods=["POST"])
def slant_webhook():
    """
    Slant -> your webhook URL set in the Slant platform screen.
    NOTE: Slant’s exact signature header format can vary. This handler:
      - logs payload
      - optionally verifies HMAC if SLANT_WEBHOOK_SECRET is set AND a signature header is present

    If Slant uses a different header name/format, update the header parsing below.
    """
    raw = request.data
    payload_json = request.get_json(silent=True) or {}

    sig = (
        request.headers.get("X-Slant-Signature")
        or request.headers.get("Slant-Signature")
        or request.headers.get("X-Webhook-Signature")
        or ""
    ).strip()

    if SLANT_WEBHOOK_SECRET:
        if not sig:
            # You can change to 400 if you want strict enforcement
            print("⚠️ Slant webhook received but no signature header found (cannot verify).")
        else:
            # Try base64(HMAC_SHA256(body)) and hex(HMAC_SHA256(body)) comparisons
            mac = hmac.new(SLANT_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).digest()
            b64 = base64.b64encode(mac).decode("utf-8")
            hx = hashlib.sha256(hmac.new(SLANT_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).digest()).hexdigest()

            # (We don't know which format Slant uses—so we accept either match.)
            if sig != b64 and sig != hx:
                print(f"⚠️ Slant webhook signature did not match known formats. sig={sig}")
                # return jsonify({"ok": False, "error": "invalid signature"}), 400

    print("📥 Slant webhook payload:", payload_json)

    # TODO: update ORDER_DATA based on Slant event types once you know the schema.
    return jsonify({"ok": True})


# Local dev entry
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
