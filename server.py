from __future__ import annotations

import fcntl
import json
import os
import threading
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import stripe
from flask import Flask, abort, jsonify, make_response, request, send_file

app = Flask(__name__)

# ----------------------------
# Helpers
# ----------------------------

def utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip()

def env_bool(name: str, default: bool = False) -> bool:
    v = env_str(name, "")
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "y", "on")

def env_int(name: str, default: int) -> int:
    v = env_str(name, "")
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default

def safe_uuid(s: str) -> str:
    return str(uuid.UUID(str(s)))

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

# ----------------------------
# Config
# ----------------------------

PUBLIC_BASE_URL = env_str("PUBLIC_BASE_URL", "http://localhost:10000").rstrip("/")
UPLOAD_DIR = env_str("UPLOAD_DIR", "/data/uploads")
ORDER_DATA_PATH = env_str("ORDER_DATA_PATH", "/data/order_data.json")

STRIPE_SECRET_KEY = env_str("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = env_str("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SUCCESS_URL = env_str(
    "STRIPE_SUCCESS_URL",
    f"{PUBLIC_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}"
)
STRIPE_CANCEL_URL = env_str("STRIPE_CANCEL_URL", f"{PUBLIC_BASE_URL}/cancel")

DEFAULT_PRICE_CENTS = env_int("DEFAULT_PRICE_CENTS", 7500)
DEV_PRICE_OVERRIDE_CENTS = env_int("DEV_PRICE_OVERRIDE_CENTS", 0)  # if >0, forces checkout price

APP_URL_SCHEME = env_str("APP_URL_SCHEME", "krezz").strip()
APP_DEEPLINK_PATH = env_str("APP_DEEPLINK_PATH", "checkout-success").strip()

# Optional Slant toggles (kept OFF by default)
SLANT_ENABLED = env_bool("SLANT_ENABLED", False)
SLANT_AUTO_SUBMIT = env_bool("SLANT_AUTO_SUBMIT", False)

ensure_dir(UPLOAD_DIR)

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

def boot_log() -> None:
    print("✅ Boot config:")
    for k, v in [
        ("PUBLIC_BASE_URL", PUBLIC_BASE_URL),
        ("UPLOAD_DIR", UPLOAD_DIR),
        ("ORDER_DATA_PATH", ORDER_DATA_PATH),
        ("STRIPE_SUCCESS_URL", STRIPE_SUCCESS_URL),
        ("STRIPE_CANCEL_URL", STRIPE_CANCEL_URL),
        ("DEFAULT_PRICE_CENTS", DEFAULT_PRICE_CENTS),
        ("DEV_PRICE_OVERRIDE_CENTS", DEV_PRICE_OVERRIDE_CENTS),
        ("APP_URL_SCHEME", APP_URL_SCHEME),
        ("APP_DEEPLINK_PATH", APP_DEEPLINK_PATH),
        ("STRIPE_WEBHOOK_SECRET", "(set)" if STRIPE_WEBHOOK_SECRET else "(missing)"),
        ("SLANT_ENABLED", SLANT_ENABLED),
        ("SLANT_AUTO_SUBMIT", SLANT_AUTO_SUBMIT),
    ]:
        print(f"   {k}: {v}")

boot_log()

# ----------------------------
# JSON store with flock
# ----------------------------

def with_store_lock(fn):
    def wrapper(*args, **kwargs):
        ensure_dir(os.path.dirname(ORDER_DATA_PATH) or ".")
        with open(ORDER_DATA_PATH, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read().strip()
                store = json.loads(raw) if raw else {}
                if not isinstance(store, dict):
                    store = {}
                result = fn(store, *args, **kwargs)
                f.seek(0)
                f.truncate()
                f.write(json.dumps(store, indent=2, sort_keys=True))
                f.flush()
                os.fsync(f.fileno())
                return result
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return wrapper

@with_store_lock
def store_get(store: Dict[str, Any], key: str) -> Any:
    return store.get(key)

@with_store_lock
def store_merge(store: Dict[str, Any], key: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    cur = store.get(key) or {}
    if not isinstance(cur, dict):
        cur = {}
    cur.update(patch)
    store[key] = cur
    return cur

# ----------------------------
# STL routes
# ----------------------------

def stl_path_for(job_id: str) -> str:
    return os.path.join(UPLOAD_DIR, f"{job_id}.stl")

@app.get("/")
def root():
    return "OK", 200

@app.post("/upload")
def upload():
    job_id = request.form.get("job_id") or request.args.get("job_id") or str(uuid.uuid4())
    job_id = safe_uuid(job_id)

    if "file" not in request.files:
        return jsonify({"error": "missing file"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400

    path = stl_path_for(job_id)
    f.save(path)

    stl_url_raw = f"{PUBLIC_BASE_URL}/stl-raw/{job_id}.stl"
    print(f"✅ Uploaded STL job_id={job_id} -> {path}")
    return jsonify({"job_id": job_id, "stl_url_raw": stl_url_raw})

@app.get("/stl-raw/<path:filename>")
def stl_raw(filename: str):
    if not filename.endswith(".stl"):
        abort(404)
    base = os.path.basename(filename)
    path = os.path.join(UPLOAD_DIR, base)
    if not os.path.exists(path):
        abort(404)

    return send_file(
        path,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=base,
        conditional=True,
        etag=True,
        max_age=3600,
        last_modified=os.path.getmtime(path),
    )

# ----------------------------
# Checkout
# ----------------------------

def _normalize_items(items: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or it.get("title") or "Krezz Item")
        qty = int(it.get("quantity") or 1)
        qty = max(1, min(qty, 99))
        price_cents = it.get("price_cents") or it.get("priceCents") or it.get("price")
        try:
            price_cents = int(price_cents) if price_cents is not None else None
        except Exception:
            price_cents = None
        out.append({"name": name, "quantity": qty, "price_cents": price_cents})
    return out

def _line_items_from_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    line_items: List[Dict[str, Any]] = []
    for it in items:
        amount = DEV_PRICE_OVERRIDE_CENTS if DEV_PRICE_OVERRIDE_CENTS > 0 else (it.get("price_cents") or DEFAULT_PRICE_CENTS)
        amount = max(50, int(amount))  # safeguard >= 50 cents
        line_items.append({
            "quantity": int(it.get("quantity") or 1),
            "price_data": {
                "currency": "usd",
                "unit_amount": amount,
                "product_data": {"name": str(it.get("name") or "Krezz Item")},
            }
        })
    if not line_items:
        amount = DEV_PRICE_OVERRIDE_CENTS if DEV_PRICE_OVERRIDE_CENTS > 0 else DEFAULT_PRICE_CENTS
        line_items = [{
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": int(amount),
                "product_data": {"name": "Krezz Item"},
            }
        }]
    return line_items

@app.post("/create-checkout-session")
def create_checkout_session():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "STRIPE_SECRET_KEY not set"}), 500

    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id") or data.get("orderId") or str(uuid.uuid4())
    order_id = safe_uuid(order_id)

    items = _normalize_items(data.get("items") or [])
    shipping = data.get("shippingInfo") or {}

    store_merge(order_id, {
        "order_id": order_id,
        "status": "created",
        "created_at": utc_iso(),
        "items": items,
        "shippingInfo": shipping,
    })

    success_url = STRIPE_SUCCESS_URL
    if "order_id=" not in success_url:
        joiner = "&" if "?" in success_url else "?"
        success_url = f"{success_url}{joiner}order_id={order_id}"

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=_line_items_from_items(items),
        success_url=success_url,
        cancel_url=STRIPE_CANCEL_URL,
        client_reference_id=order_id,
        metadata={"order_id": order_id},
        allow_promotion_codes=False,
    )

    store_merge(order_id, {
        "stripe_session_id": session["id"],
        "status": "checkout_created",
        "updated_at": utc_iso(),
    })

    print(f"📥 /create-checkout-session payload: {{'keys': {list(data.keys())}}}")
    print(f"✅ Created checkout session: {session['id']} order_id={order_id}")

    return jsonify({
        "order_id": order_id,
        "session_id": session["id"],
        "checkout_url": session["url"],
    })

@app.get("/order-data/<order_id>")
def order_data(order_id: str):
    try:
        order_id = safe_uuid(order_id)
    except Exception:
        return jsonify({"error": "invalid order_id"}), 400
    od = store_get(order_id)
    if not od:
        return jsonify({"error": "not found"}), 404
    return jsonify(od)

# ----------------------------
# Webhook
# ----------------------------

def _extract_order_id_from_session_obj(obj: Dict[str, Any]) -> Optional[str]:
    v = obj.get("client_reference_id")
    if v:
        try:
            return safe_uuid(v)
        except Exception:
            pass
    md = obj.get("metadata") or {}
    if isinstance(md, dict):
        v2 = md.get("order_id") or md.get("orderId")
        if v2:
            try:
                return safe_uuid(v2)
            except Exception:
                pass
    return None

@app.post("/webhook")
def webhook():
    if not STRIPE_WEBHOOK_SECRET:
        print("❌ STRIPE_WEBHOOK_SECRET not set (cannot verify webhooks)")
        return "bad", 400

    payload = request.get_data(cache=False, as_text=False)
    sig = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print(f"❌ Webhook signature verification failed: {e}")
        return "bad", 400

    event_type = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    print(f"📦 Stripe event: {event_type} ({event.get('id')}) livemode={bool(event.get('livemode'))}")

    if event_type == "checkout.session.completed":
        order_id = _extract_order_id_from_session_obj(obj)
        if order_id:
            store_merge(order_id, {
                "status": "paid",
                "paid_at": utc_iso(),
                "stripe_session_id": obj.get("id"),
                "payment_status": obj.get("payment_status"),
                "amount_total": obj.get("amount_total"),
                "currency": obj.get("currency"),
            })
            print(f"✅ Payment confirmed for order_id: {order_id}")

    return "ok", 200

# ----------------------------
# Success page (deep link)
# ----------------------------

def _deeplink(order_id: str, session_id: str) -> str:
    from urllib.parse import urlencode
    qs = urlencode({"order_id": order_id, "session_id": session_id})
    scheme = APP_URL_SCHEME.strip().lower()
    path = APP_DEEPLINK_PATH.strip().lstrip("/")
    return f"{scheme}://{path}?{qs}"

@app.get("/success")
def success():
    session_id = request.args.get("session_id", "")
    order_id = request.args.get("order_id", "")

    try:
        order_id_norm = safe_uuid(order_id) if order_id else "unknown"
    except Exception:
        order_id_norm = order_id or "unknown"

    deeplink = _deeplink(order_id_norm, session_id or "unknown")

    html = f"""
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Payment successful</title>
  <style>
    body {{ font-family: -apple-system, system-ui, Segoe UI, Roboto, Helvetica, Arial; padding: 24px; }}
    .btn {{
      display: inline-block; padding: 14px 18px; border-radius: 12px;
      background: #000; color: #fff; text-decoration: none; font-weight: 600;
    }}
    code {{ background: #f3f3f3; padding: 2px 6px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h2>✅ Payment successful</h2>
  <p>You can close this window and return to the app.</p>

  <p><a class="btn" href="{deeplink}">Open Krezz App</a></p>

  <p style="margin-top:18px;">
    <strong>Order:</strong> <code>{order_id_norm}</code><br/>
    <strong>Session:</strong> <code>{session_id or "unknown"}</code>
  </p>

  <p style="margin-top:18px; font-size: 14px; color:#444;">
    If you see “address is invalid”, your iOS app is not registered for the URL scheme <code>{APP_URL_SCHEME}</code>.
    In Xcode: Target → Info → URL Types → URL Schemes.
  </p>

  <script>
    setTimeout(function() {{
      try {{ window.location.href = "{deeplink}"; }} catch(e) {{}}
    }}, 200);
  </script>
</body>
</html>
"""
    return make_response(html, 200, {"Content-Type": "text/html; charset=utf-8"})

@app.get("/cancel")
def cancel():
    return "Canceled", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
