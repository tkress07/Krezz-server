# server.py
from __future__ import annotations

import os
import uuid
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
import stripe
from flask import Flask, request, jsonify, send_file, abort


# -------------------------------------------------------------------
# App
# -------------------------------------------------------------------
app = Flask(__name__)

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
# Stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET", "").strip()
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Storage (Render persistent disk)
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATA_PATH = os.getenv("ORDER_DATA_PATH", "/data/order_data.json")

# Public base URL (so Slant can fetch STL from your server, especially from the webhook)
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

# Slant
SLANT_API_KEY = (os.getenv("SLANT_API_KEY") or "").strip()
SLANT_BASE_URL = (os.getenv("SLANT_BASE_URL") or "https://slant3dapi.com/v2/api").rstrip("/")
# You can override if docs specify a single canonical endpoint.
# We’ll try these in order if not set.
SLANT_ORDER_ENDPOINT = (os.getenv("SLANT_ORDER_ENDPOINT") or "").strip()  # e.g. "/order" or "/orders"

# Optional: if you want to store & forward extra info without risking schema errors,
# keep it inside "comments" only if your Slant endpoint supports it.
SLANT_ALLOW_COMMENTS = (os.getenv("SLANT_ALLOW_COMMENTS") or "true").lower() in ("1", "true", "yes")


# -------------------------------------------------------------------
# In-memory order store + persistence
# -------------------------------------------------------------------
ORDER_DATA: Dict[str, Any] = {}


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def load_order_data() -> None:
    global ORDER_DATA
    try:
        with open(DATA_PATH, "r") as f:
            ORDER_DATA = json.load(f)
        print(f"✅ Loaded ORDER_DATA ({len(ORDER_DATA)} orders) from {DATA_PATH}")
    except Exception:
        ORDER_DATA = {}
        print("ℹ️ No prior ORDER_DATA found (starting fresh)")


def save_order_data() -> None:
    try:
        with open(DATA_PATH, "w") as f:
            json.dump(ORDER_DATA, f)
    except Exception as e:
        print(f"❌ Failed to persist ORDER_DATA: {e}")


load_order_data()


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def slant_headers() -> Dict[str, str]:
    if not SLANT_API_KEY:
        raise RuntimeError("SLANT_API_KEY not configured")
    return {
        "Authorization": f"Bearer {SLANT_API_KEY}",
        "Accept": "application/json",
    }


def normalize_country_iso2(country_val: str) -> str:
    if not country_val:
        return "US"
    c = country_val.strip().lower()
    if c in ("us", "usa", "united states", "united states of america"):
        return "US"
    if len(country_val.strip()) == 2:
        return country_val.strip().upper()
    return "US"


def normalize_bool_string(val: Any, default: str = "true") -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "y"):
        return "true"
    if s in ("false", "0", "no", "n"):
        return "false"
    return default


def normalize_phone(phone: str) -> str:
    """
    Slant may accept E.164 (+1...) or digits. We'll keep leading '+' if present,
    and remove all other non-digits.
    """
    if not phone:
        return ""
    phone = phone.strip()
    keep_plus = phone.startswith("+")
    digits = "".join(ch for ch in phone if ch.isdigit())
    if keep_plus and digits:
        return "+" + digits
    return digits


def build_public_stl_url(job_id: str) -> str:
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL not configured (needed so Slant can fetch the STL)")
    return f"{PUBLIC_BASE_URL}/stl/{job_id}.stl"


def ensure_order_exists(order_id: str) -> Dict[str, Any]:
    if order_id not in ORDER_DATA:
        ORDER_DATA[order_id] = {
            "items": [],
            "shipping": {},
            "status": "created",
            "created_at": _now_iso(),
        }
        save_order_data()
    return ORDER_DATA[order_id]


def normalize_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for it in items or []:
        it = dict(it or {})
        job_id = it.get("job_id") or it.get("jobId") or it.get("id")
        if not job_id:
            job_id = str(uuid.uuid4())
        it["job_id"] = job_id
        it["quantity"] = int(it.get("quantity", 1) or 1)
        it["name"] = it.get("name", "Krezz Mold")
        it["price"] = int(it.get("price", 7500) or 7500)  # cents
        out.append(it)
    return out


# -------------------------------------------------------------------
# Slant: Place order using fileURL (recommended path)
# -------------------------------------------------------------------
def slant_place_order_payload(
    order_number: str,
    shipping: Dict[str, Any],
    item: Dict[str, Any],
) -> Dict[str, Any]:
    job_id = item.get("job_id")
    if not job_id:
        raise RuntimeError("Item missing job_id")

    iso2 = normalize_country_iso2(shipping.get("country") or "US")
    is_res = normalize_bool_string(shipping.get("isResidential"), default="true")

    full_name = shipping.get("fullName") or shipping.get("name") or ""
    email = shipping.get("email") or ""
    phone = normalize_phone(shipping.get("phone") or "")

    line1 = shipping.get("addressLine") or shipping.get("line1") or ""
    line2 = shipping.get("addressLine2") or shipping.get("line2") or ""
    city = shipping.get("city") or ""
    state = shipping.get("state") or ""
    zip_code = shipping.get("zipCode") or shipping.get("zip") or ""

    filename = f"{job_id}.stl"
    file_url = build_public_stl_url(job_id)

    payload: Dict[str, Any] = {
        "orderNumber": order_number,
        "filename": filename,
        "fileURL": file_url,

        "email": email,
        "phone": phone,
        "name": full_name,

        # Billing (mirror shipping if you don't collect separately)
        "bill_to_street_1": line1,
        "bill_to_street_2": line2,
        "bill_to_city": city,
        "bill_to_state": state,
        "bill_to_zip": zip_code,
        "bill_to_country_as_iso": iso2,
        "bill_to_is_US_residential": is_res,

        # Shipping
        "ship_to_name": full_name,
        "ship_to_street_1": line1,
        "ship_to_street_2": line2,
        "ship_to_city": city,
        "ship_to_state": state,
        "ship_to_zip": zip_code,
        "ship_to_country_as_iso": iso2,
        "ship_to_is_US_residential": is_res,

        # Item info (single-line item schema)
        "order_item_name": item.get("name", "Krezz Mold"),
        "order_quantity": str(int(item.get("quantity", 1))),
        "order_sku": item.get("SKU") or item.get("sku") or job_id,
        "order_item_color": (shipping.get("color") or "Black"),
    }

    # Optional extra info — only if Slant endpoint accepts "comments"
    if SLANT_ALLOW_COMMENTS:
        material = shipping.get("material") or ""
        if material:
            payload["comments"] = f"Material preference: {material}"

    return payload


def slant_post_place_order(payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = slant_headers()
    candidates: List[str] = []

    if SLANT_ORDER_ENDPOINT:
        ep = SLANT_ORDER_ENDPOINT if SLANT_ORDER_ENDPOINT.startswith("/") else f"/{SLANT_ORDER_ENDPOINT}"
        candidates.append(ep)
    else:
        # Try both; different installs/docs sometimes differ.
        candidates.extend(["/order", "/orders"])

    last_err: Optional[str] = None
    for ep in candidates:
        url = f"{SLANT_BASE_URL}{ep}"
        print(f"➡️ Slant placeOrder POST {url} orderNumber={payload.get('orderNumber')} fileURL={payload.get('fileURL')}")
        r = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload, timeout=90)

        if r.status_code < 400:
            try:
                return r.json() if r.text else {"success": True}
            except Exception:
                return {"success": True, "raw": r.text}

        # If it's a not-found/method mismatch, try next candidate endpoint.
        if r.status_code in (404, 405):
            last_err = f"status={r.status_code} body={r.text[:1200]}"
            continue

        # Otherwise, fail fast.
        raise RuntimeError(f"Slant placeOrder failed: status={r.status_code} body={r.text[:1200]}")

    raise RuntimeError(f"Slant placeOrder failed on endpoints {candidates}. Last error: {last_err}")


def submit_paid_order_to_slant(order_id: str) -> None:
    """
    Idempotent submission: if already submitted, no-op.
    Supports multiple items by placing one Slant order per item
    (orderNumber becomes `${order_id}-1`, `${order_id}-2`, etc.).
    """
    data = ensure_order_exists(order_id)

    status = data.get("status")
    if status in ("submitted_to_slant", "in_production"):
        print(f"ℹ️ Order already submitted: order_id={order_id} status={status}")
        return

    if not SLANT_API_KEY:
        raise RuntimeError("Missing SLANT_API_KEY")
    if not PUBLIC_BASE_URL:
        raise RuntimeError("Missing PUBLIC_BASE_URL (Slant must fetch /stl/<job_id>.stl)")

    items = data.get("items", []) or []
    shipping = data.get("shipping", {}) or {}
    if not items:
        raise RuntimeError("No items found in ORDER_DATA for this order")

    # Track submissions
    data.setdefault("slant", {})
    data["slant"].setdefault("placedOrders", [])

    placed: List[Dict[str, Any]] = []
    for idx, item in enumerate(items, start=1):
        # Ensure STL exists locally (uploaded earlier)
        job_id = item.get("job_id")
        if not job_id:
            raise RuntimeError("Item missing job_id")
        stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
        if not os.path.exists(stl_path):
            raise RuntimeError(f"STL not found on server for job_id={job_id}: {stl_path}")

        # One Slant order per item to match their single-file order schema
        order_number = order_id if len(items) == 1 else f"{order_id}-{idx}"

        payload = slant_place_order_payload(order_number, shipping, item)
        resp = slant_post_place_order(payload)

        placed.append({
            "orderNumber": order_number,
            "job_id": job_id,
            "response": resp,
        })

    data["slant"]["placedOrders"] = placed
    data["status"] = "submitted_to_slant"
    data["slant_error"] = None
    save_order_data()


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.route("/")
def index():
    return "✅ Krezz server is live (Stripe + Slant via fileURL)."


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "time": _now_iso(),
        "stripe_configured": bool(STRIPE_SECRET_KEY and STRIPE_ENDPOINT_SECRET),
        "slant_configured": bool(SLANT_API_KEY),
        "public_base_url": PUBLIC_BASE_URL,
        "upload_dir": UPLOAD_DIR,
        "orders": len(ORDER_DATA),
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

    # IMPORTANT: as_attachment=False so Slant can fetch it like a normal file
    # Use a more standard STL mime type
    return send_file(stl_path, mimetype="model/stl", as_attachment=False)


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    try:
        if not STRIPE_SECRET_KEY:
            return jsonify({"error": "Stripe not configured (missing STRIPE_SECRET_KEY)"}), 500

        data = request.get_json(silent=True) or {}
        print("📥 /create-checkout-session payload:", data)

        items = normalize_items(data.get("items", []))
        shipping_info = data.get("shippingInfo", {}) or {}

        if not items:
            return jsonify({"error": "No items provided"}), 400

        order_id = (data.get("order_id") or str(uuid.uuid4())).strip()

        ORDER_DATA[order_id] = {
            "items": items,
            "shipping": shipping_info,
            "status": "created",
            "created_at": _now_iso(),
        }
        save_order_data()

        line_items = []
        for it in items:
            line_items.append({
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": it.get("name", "Beard Mold")},
                    "unit_amount": int(it.get("price", 7500)),
                },
                "quantity": int(it.get("quantity", 1)),
            })

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=line_items,
            # iOS deep link
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
    if not STRIPE_ENDPOINT_SECRET:
        return "Webhook not configured", 500

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

        data = ensure_order_exists(order_id)

        # Idempotency guard: if already paid/submitted, skip rework
        if data.get("status") in ("paid", "submitted_to_slant", "in_production"):
            print(f"ℹ️ Webhook received but order already handled: order_id={order_id} status={data.get('status')}")
            return jsonify(success=True)

        data["status"] = "paid"
        data["payment"] = {
            "stripe_session_id": session.get("id"),
            "amount_total": session.get("amount_total"),
            "currency": session.get("currency"),
            "created": datetime.utcfromtimestamp(session["created"]).isoformat() if session.get("created") else _now_iso(),
            "email": session.get("customer_email", "unknown"),
            "status": "paid",
        }
        save_order_data()
        print(f"✅ Payment confirmed for order_id: {order_id}")

        # Submit to Slant (do not fail webhook)
        try:
            print(f"➡️ Submitting to Slant: order_id={order_id}")
            submit_paid_order_to_slant(order_id)
            print(f"✅ Slant submission complete: order_id={order_id}")
        except Exception as e:
            print(f"❌ Slant submit exception: {e}")
            data["slant_error"] = str(e)
            data["status"] = "paid"  # keep paid; you can retry manually
            save_order_data()

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
        "slant_error": data.get("slant_error"),
    })


# -------------------------------------------------------------------
# Debug helpers
# -------------------------------------------------------------------
@app.route("/debug/slant/placeorder/<order_id>", methods=["POST"])
def debug_slant_placeorder(order_id):
    """
    Manual retry after an order is paid (or to test end-to-end).
    """
    try:
        ensure_order_exists(order_id)
        submit_paid_order_to_slant(order_id)
        return jsonify({"ok": True, "order_id": order_id, "slant": ORDER_DATA[order_id].get("slant", {})})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/debug/stl-url/<job_id>", methods=["GET"])
def debug_stl_url(job_id):
    try:
        return jsonify({"ok": True, "job_id": job_id, "url": build_public_stl_url(job_id)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# -------------------------------------------------------------------
# Entrypoint
# -------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
