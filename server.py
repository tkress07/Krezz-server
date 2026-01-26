from __future__ import annotations

import os
import re
import json
import uuid
import time
import stripe
import fcntl
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, request, jsonify, send_file, abort

app = Flask(__name__)

# -----------------------------
# Config
# -----------------------------
def must_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise ValueError(f"Missing required env var: {name}")
    return v

# Stripe
stripe.api_key = must_env("STRIPE_SECRET_KEY")
STRIPE_ENDPOINT_SECRET = must_env("STRIPE_ENDPOINT_SECRET")

# Storage
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATA_PATH = os.getenv("ORDER_DATA_PATH", "/data/order_data.json")
os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)

# Public base URL (CRITICAL so Slant can fetch the STL)
# Example: https://krezz-server.onrender.com
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Slant
# Per public docs/mock: production base is https://api.slant3d.com/api and auth is api-key header. :contentReference[oaicite:2]{index=2}
SLANT_API_KEY = must_env("SLANT_API_KEY")
SLANT_API_BASES = [
    os.getenv("SLANT_API_BASE", "https://api.slant3d.com/api").rstrip("/"),
    # fallback (some people get keys working here too)
    "https://slant3dapi.com/api",
]

SLANT_TIMEOUT_SEC = int(os.getenv("SLANT_TIMEOUT_SEC", "30"))
SLANT_VALIDATE_SLICE = os.getenv("SLANT_VALIDATE_SLICE", "0") == "1"

# Your app defaults
DEFAULT_PROFILE = os.getenv("SLANT_DEFAULT_PROFILE", "PETG").upper()  # PLA or PETG
DEFAULT_COLOR_TAG = os.getenv("SLANT_DEFAULT_COLOR_TAG", "black").lower()

# In-memory cache
ORDER_DATA: Dict[str, Any] = {}
_FILAMENT_CACHE = {"ts": 0.0, "data": None}
_FILAMENT_CACHE_TTL = 10 * 60  # 10 minutes


# -----------------------------
# Persistence helpers (atomic + file lock)
# -----------------------------
def _with_locked_file(path: str, mode: str):
    f = open(path, mode)
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    return f

def load_order_data() -> None:
    global ORDER_DATA
    if not os.path.exists(DATA_PATH):
        ORDER_DATA = {}
        print("ℹ️ No prior ORDER_DATA found (starting fresh)")
        return

    try:
        with _with_locked_file(DATA_PATH, "r") as f:
            raw = f.read().strip()
            ORDER_DATA = json.loads(raw) if raw else {}
        print(f"✅ Loaded ORDER_DATA ({len(ORDER_DATA)} orders) from {DATA_PATH}")
    except Exception as e:
        ORDER_DATA = {}
        print(f"❌ Failed loading ORDER_DATA, starting fresh. Error: {e}")

def save_order_data() -> None:
    # Atomic write: temp file then rename
    try:
        dirpath = os.path.dirname(DATA_PATH) or "."
        fd, tmp_path = tempfile.mkstemp(prefix="order_data_", suffix=".json", dir=dirpath)
        with os.fdopen(fd, "w") as tmp:
            json.dump(ORDER_DATA, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, DATA_PATH)
    except Exception as e:
        print(f"❌ Failed to persist ORDER_DATA: {e}")

load_order_data()


# -----------------------------
# Small normalization helpers
# -----------------------------
def now_iso() -> str:
    return datetime.utcnow().isoformat()

def boolish(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")

def normalize_country_iso2(country_val: str) -> str:
    if not country_val:
        return "US"
    c = country_val.strip().lower()
    if c in ("us", "usa", "united states", "united states of america"):
        return "US"
    if len(country_val.strip()) == 2:
        return country_val.strip().upper()
    return "US"

def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    s = phone.strip()
    # Keep leading + if present, strip everything else to digits
    plus = s.startswith("+")
    digits = re.sub(r"\D+", "", s)
    if plus:
        return f"+{digits}"
    # If 10 digits assume US
    if len(digits) == 10:
        return f"+1{digits}"
    if digits:
        return f"+{digits}"
    return ""

def stl_public_url(job_id: str) -> str:
    if not PUBLIC_BASE_URL:
        # Fallback: attempt to derive from request. This is less reliable behind proxies.
        base = request.host_url.rstrip("/")
        return f"{base}/stl/{job_id}.stl"
    return f"{PUBLIC_BASE_URL}/stl/{job_id}.stl"


# -----------------------------
# Slant API client (v1 "official format")
# -----------------------------
def slant_headers() -> Dict[str, str]:
    # Official mock/docs show api-key header. :contentReference[oaicite:3]{index=3}
    return {
        "api-key": SLANT_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
        # harmless extra (some deployments accept bearer too)
        "Authorization": f"Bearer {SLANT_API_KEY}",
    }

def slant_request(method: str, path: str, *, json_body: Any = None, timeout: int = SLANT_TIMEOUT_SEC) -> Tuple[int, str, Optional[Dict[str, Any]]]:
    """
    Try SLANT_API_BASES in order until one returns a response (even error).
    Return (status_code, text, parsed_json_or_none)
    """
    last = None
    for base in SLANT_API_BASES:
        url = f"{base}{path}"
        try:
            r = requests.request(method, url, headers=slant_headers(), json=json_body, timeout=timeout)
            text = r.text or ""
            parsed = None
            if "application/json" in (r.headers.get("Content-Type") or ""):
                try:
                    parsed = r.json()
                except Exception:
                    parsed = None
            return r.status_code, text, parsed
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"Slant request failed for all bases. Last error: {last}")

def slant_get_filaments_cached() -> List[Dict[str, Any]]:
    now = time.time()
    if _FILAMENT_CACHE["data"] is not None and (now - _FILAMENT_CACHE["ts"]) < _FILAMENT_CACHE_TTL:
        return _FILAMENT_CACHE["data"]

    # Per mock/docs: GET /api/filament exists. :contentReference[oaicite:4]{index=4}
    status, text, parsed = slant_request("GET", "/filament", json_body=None)
    if status >= 400:
        print(f"❌ Slant filaments fetch failed: {status} {text[:500]}")
        return []

    filaments = (parsed or {}).get("filaments") or []
    _FILAMENT_CACHE["ts"] = now
    _FILAMENT_CACHE["data"] = filaments
    return filaments

def resolve_slant_filament(shipping_info: Dict[str, Any]) -> Tuple[str, str]:
    """
    Returns (order_item_color, profile)
    Filament model includes filament/profile/colorTag. :contentReference[oaicite:5]{index=5}
    """
    material = (shipping_info.get("material") or "").upper()
    color = (shipping_info.get("color") or DEFAULT_COLOR_TAG).strip().lower()

    want_profile = "PETG" if "PETG" in material else "PLA"

    filaments = slant_get_filaments_cached()

    # Try exact profile + colorTag match
    for f in filaments:
        if (f.get("profile") or "").upper() == want_profile and (f.get("colorTag") or "").lower() == color:
            return (f.get("filament") or f"{want_profile} {color}".upper()), want_profile

    # Try any filament with profile
    for f in filaments:
        if (f.get("profile") or "").upper() == want_profile:
            return (f.get("filament") or f"{want_profile} {color}".upper()), want_profile

    # Fallback
    return (f"{want_profile} {color}".upper(), want_profile)

def slant_slice_validate(file_url: str) -> None:
    # Per types: SliceRequest is { fileURL }. :contentReference[oaicite:6]{index=6}
    # Per mock: endpoint is POST /api/slicer. :contentReference[oaicite:7]{index=7}
    status, text, parsed = slant_request("POST", "/slicer", json_body={"fileURL": file_url}, timeout=60)
    if status >= 400:
        raise RuntimeError(f"Slant slicer failed: status={status} body={text[:1200]}")
    # Optional: check parsed["data"]["price"] etc (SliceResponse). :contentReference[oaicite:8]{index=8}
    print(f"✅ Slant slicer ok for fileURL={file_url}")

def slant_place_order(order_obj: Dict[str, Any]) -> str:
    """
    Official format uses POST /api/order with an ARRAY body. :contentReference[oaicite:9]{index=9}
    Response: { orderId: "..." } :contentReference[oaicite:10]{index=10}
    """
    status, text, parsed = slant_request("POST", "/order", json_body=[order_obj], timeout=60)
    if status >= 400:
        raise RuntimeError(f"Slant placeOrder failed: status={status} body={text[:1500]}")

    order_id = (parsed or {}).get("orderId")
    if not order_id:
        raise RuntimeError(f"Slant placeOrder succeeded but missing orderId. Response: {str(parsed)[:800]}")
    return str(order_id)


# -----------------------------
# Building a Slant order object
# -----------------------------
def build_slant_order(order_id: str, item: Dict[str, Any], shipping: Dict[str, Any]) -> Dict[str, Any]:
    """
    Matches generated Order type (flat snake_case, fileURL, string quantities). :contentReference[oaicite:11]{index=11}
    """
    full_name = shipping.get("fullName") or shipping.get("name") or "Customer"
    email = shipping.get("email") or "unknown@example.com"
    phone = normalize_phone(shipping.get("phone") or "")

    line1 = shipping.get("addressLine") or shipping.get("line1") or ""
    line2 = shipping.get("addressLine2") or shipping.get("line2") or ""
    city = shipping.get("city") or ""
    state = shipping.get("state") or ""
    zip_code = shipping.get("zipCode") or shipping.get("zip") or ""
    country = normalize_country_iso2(shipping.get("country") or "US")

    is_res = "true" if boolish(shipping.get("isResidential")) else "false"

    job_id = item.get("job_id")
    if not job_id:
        raise RuntimeError("Item missing job_id")
    file_url = stl_public_url(job_id)

    # Pick filament/profile
    order_item_color, profile = resolve_slant_filament(shipping)

    qty = str(int(item.get("quantity", 1)))
    item_name = item.get("name") or "Krezz Mold"

    order_obj = {
        "email": email,
        "phone": phone,
        "name": full_name,

        "orderNumber": order_id,

        "filename": f"{job_id}.stl",
        "fileURL": file_url,

        # billing
        "bill_to_street_1": line1,
        "bill_to_street_2": line2 or None,
        "bill_to_city": city,
        "bill_to_state": state,
        "bill_to_zip": zip_code,
        "bill_to_country_as_iso": country,
        "bill_to_is_US_residential": is_res,

        # shipping
        "ship_to_name": full_name,
        "ship_to_street_1": line1,
        "ship_to_street_2": line2 or None,
        "ship_to_city": city,
        "ship_to_state": state,
        "ship_to_zip": zip_code,
        "ship_to_country_as_iso": country,
        "ship_to_is_US_residential": is_res,

        # item
        "order_item_name": item_name,
        "order_quantity": qty,          # string per spec :contentReference[oaicite:12]{index=12}
        "order_sku": item.get("SKU") or item.get("sku") or job_id,
        "order_item_color": order_item_color,

        # Some Slant examples include profile in the order object. :contentReference[oaicite:13]{index=13}
        "profile": profile,
    }

    # remove Nones so you don't send nulls unless needed
    return {k: v for k, v in order_obj.items() if v is not None}


def submit_paid_order_to_slant(order_id: str) -> None:
    data = ORDER_DATA.get(order_id) or {}
    if not data:
        raise RuntimeError(f"Order not found: {order_id}")

    # Idempotency: don’t resubmit if already placed
    slant_meta = data.get("slant") or {}
    if slant_meta.get("orderId"):
        print(f"ℹ️ Already placed with Slant: order_id={order_id} slantOrderId={slant_meta['orderId']}")
        return

    items = data.get("items") or []
    shipping = data.get("shipping") or {}
    if not items:
        raise RuntimeError("Order has no items")

    # For now: place one Slant order per item (simple + predictable).
    # If you prefer: you can bundle items, but Slant’s format is “one fileURL per order object”.
    placed = []
    for it in items:
        job_id = it.get("job_id")
        if not job_id:
            raise RuntimeError("Item missing job_id")

        stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
        if not os.path.exists(stl_path):
            raise RuntimeError(f"STL missing on server for job_id={job_id}: {stl_path}")

        file_url = stl_public_url(job_id)

        # Optional preflight: confirm Slant can fetch/slice your URL
        if SLANT_VALIDATE_SLICE:
            slant_slice_validate(file_url)

        order_obj = build_slant_order(order_id, it, shipping)
        print(f"➡️ Slant placeOrder POST {SLANT_API_BASES[0]}/order orderNumber={order_id} fileURL={order_obj.get('fileURL')}")
        slant_order_id = slant_place_order(order_obj)
        placed.append({"job_id": job_id, "slantOrderId": slant_order_id})

    data["slant"] = {"orders": placed, "placed_at": now_iso()}
    data["status"] = "submitted_to_slant"
    ORDER_DATA[order_id] = data
    save_order_data()
    print(f"✅ Slant submission complete: order_id={order_id} placed={placed}")


# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def index():
    return "✅ Krezz server is live (Stripe + Slant v1 order flow)."

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})


@app.route("/upload", methods=["POST"])
def upload_stl():
    job_id = request.form.get("job_id")
    file = request.files.get("file")
    if not job_id or not file:
        return jsonify({"error": "Missing job_id or file"}), 400

    # Optional: basic safety check
    if not job_id or len(job_id) > 128:
        return jsonify({"error": "Invalid job_id"}), 400

    save_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    file.save(save_path)
    print(f"✅ Uploaded STL job_id={job_id} -> {save_path}")
    return jsonify({"success": True, "path": save_path})


@app.route("/stl/<job_id>.stl", methods=["GET"])
def serve_stl(job_id):
    stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        return abort(404)

    # IMPORTANT: serve as a normal file (not forced download) so Slant fetchers don’t choke.
    resp = send_file(stl_path, mimetype="application/sla", as_attachment=False)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    try:
        payload = request.get_json(silent=True) or {}
        print("📥 /create-checkout-session payload:", payload)

        items = payload.get("items", [])
        shipping_info = payload.get("shippingInfo", {})
        if not items:
            return jsonify({"error": "No items provided"}), 400

        order_id = payload.get("order_id") or str(uuid.uuid4())

        normalized_items = []
        for it in items:
            job_id = it.get("job_id") or it.get("jobId") or it.get("id")
            if not job_id:
                job_id = str(uuid.uuid4())
            it["job_id"] = job_id
            it["quantity"] = int(it.get("quantity", 1))
            normalized_items.append(it)

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
            "quantity": int(it.get("quantity", 1))
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

        data = ORDER_DATA.get(order_id) or {"items": [], "shipping": {}, "status": "created"}
        data["status"] = "paid"
        data["payment"] = {
            "stripe_session_id": session.get("id"),
            "amount_total": session.get("amount_total"),
            "currency": session.get("currency"),
            "created": datetime.utcfromtimestamp(session["created"]).isoformat(),
            "email": session.get("customer_email", "unknown"),
            "status": "paid",
        }
        ORDER_DATA[order_id] = data
        save_order_data()
        print(f"✅ Payment confirmed for order_id: {order_id}")

        try:
            print(f"➡️ Submitting to Slant: order_id={order_id}")
            submit_paid_order_to_slant(order_id)
        except Exception as e:
            print(f"❌ Slant submit exception: {e}")
            data = ORDER_DATA.get(order_id) or {}
            data["slant_error"] = str(e)
            data["status"] = data.get("status") or "paid"
            ORDER_DATA[order_id] = data
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


# -----------------------------
# Debug endpoints
# -----------------------------
@app.route("/debug/slant/filaments", methods=["GET"])
def debug_slant_filaments():
    try:
        filaments = slant_get_filaments_cached()
        return jsonify({"ok": True, "count": len(filaments), "filaments": filaments})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/debug/slant/place/<order_id>", methods=["POST"])
def debug_slant_place(order_id):
    try:
        submit_paid_order_to_slant(order_id)
        return jsonify({"ok": True, "order_id": order_id, "slant": ORDER_DATA.get(order_id, {}).get("slant")})
    except Exception as e:
        return jsonify({"ok": False, "order_id": order_id, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
