from __future__ import annotations

import os
import json
import uuid
import fcntl
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, quote

import requests
import stripe
from flask import Flask, request, jsonify, send_file, abort, make_response

app = Flask(__name__)

# ----------------------------
# Helpers
# ----------------------------

def utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()

def env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")

def env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except Exception:
        return default

def mask_secret(s: str, keep: int = 4) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= keep * 2:
        return "*" * len(s)
    return s[:keep] + "…" + s[-keep:]

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

# ----------------------------
# Config
# ----------------------------

PUBLIC_BASE_URL = env_str("PUBLIC_BASE_URL", "http://localhost:10000")
UPLOAD_DIR = env_str("UPLOAD_DIR", "/tmp/uploads")
ORDER_DATA_PATH = env_str("ORDER_DATA_PATH", "/tmp/order_data.json")

# Stripe
STRIPE_SECRET_KEY = env_str("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = env_str("STRIPE_WEBHOOK_SECRET", "")  # whsec_...
STRIPE_SUCCESS_URL = env_str("STRIPE_SUCCESS_URL", f"{PUBLIC_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}")
STRIPE_CANCEL_URL = env_str("STRIPE_CANCEL_URL", f"{PUBLIC_BASE_URL}/cancel")

# PRICE CONTROL
DEFAULT_PRICE_CENTS = env_int("DEFAULT_PRICE_CENTS", 1000)          # $75
DEV_PRICE_OVERRIDE_CENTS = env_int("DEV_PRICE_OVERRIDE_CENTS", 0)   # e.g. 1000 = $10

# Deep link back to app
APP_URL_SCHEME = env_str("APP_URL_SCHEME", "krezz")                 # must match iOS URL Types
APP_DEEPLINK_PATH = env_str("APP_DEEPLINK_PATH", "checkout-success")

# Slant
SLANT_ENABLED = env_bool("SLANT_ENABLED", False)
SLANT_DEBUG = env_bool("SLANT_DEBUG", True)
SLANT_AUTO_SUBMIT = env_bool("SLANT_AUTO_SUBMIT", False)
SLANT_REQUIRE_LIVE_STRIPE = env_bool("SLANT_REQUIRE_LIVE_STRIPE", True)

SLANT_BASE_URL = env_str("SLANT_BASE_URL", "https://slant3dapi.com/v2/api")
SLANT_FILES_ENDPOINT = env_str("SLANT_FILES_ENDPOINT", f"{SLANT_BASE_URL}/files")
SLANT_TIMEOUT_SEC = env_int("SLANT_TIMEOUT_SEC", 240)
SLANT_PLATFORM_ID = env_str("SLANT_PLATFORM_ID", "")
SLANT_API_KEY = env_str("SLANT_API_KEY", "")
SLANT_SEND_BEARER = env_bool("SLANT_SEND_BEARER", True)

# Slant upload mode
SLANT_UPLOAD_MODE = env_str("SLANT_UPLOAD_MODE", "url").lower()     # url | multipart
SLANT_FILE_URL_FIELD = env_str("SLANT_FILE_URL_FIELD", "URL")       # field name Slant expects

# STL serving
SLANT_STL_ROUTE = env_str("SLANT_STL_ROUTE", "raw").lower()

CORS_ALLOW_ALL = env_bool("CORS_ALLOW_ALL", True)

ensure_dir(UPLOAD_DIR)

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

def boot_log() -> None:
    print("✅ Boot config:")
    print("   PUBLIC_BASE_URL:", PUBLIC_BASE_URL)
    print("   UPLOAD_DIR:", UPLOAD_DIR)
    print("   ORDER_DATA_PATH:", ORDER_DATA_PATH)
    print("   STRIPE_SUCCESS_URL:", STRIPE_SUCCESS_URL)
    print("   STRIPE_CANCEL_URL:", STRIPE_CANCEL_URL)
    print("   DEFAULT_PRICE_CENTS:", DEFAULT_PRICE_CENTS)
    print("   DEV_PRICE_OVERRIDE_CENTS:", DEV_PRICE_OVERRIDE_CENTS)
    print("   APP_URL_SCHEME:", APP_URL_SCHEME)
    print("   APP_DEEPLINK_PATH:", APP_DEEPLINK_PATH)
    print("   SLANT_ENABLED:", SLANT_ENABLED)
    print("   SLANT_DEBUG:", SLANT_DEBUG)
    print("   SLANT_AUTO_SUBMIT:", SLANT_AUTO_SUBMIT)
    print("   SLANT_REQUIRE_LIVE_STRIPE:", SLANT_REQUIRE_LIVE_STRIPE)
    print("   SLANT_FILES_ENDPOINT:", SLANT_FILES_ENDPOINT)
    print("   SLANT_TIMEOUT_SEC:", SLANT_TIMEOUT_SEC)
    print("   SLANT_FILE_URL_FIELD:", SLANT_FILE_URL_FIELD)
    print("   SLANT_UPLOAD_MODE:", SLANT_UPLOAD_MODE)
    print("   SLANT_SEND_BEARER:", SLANT_SEND_BEARER)
    print("   SLANT_API_KEY:", mask_secret(SLANT_API_KEY))
    print("   SLANT_PLATFORM_ID:", mask_secret(SLANT_PLATFORM_ID))
    print("   STRIPE_SECRET_KEY:", mask_secret(STRIPE_SECRET_KEY))
    print("   STRIPE_WEBHOOK_SECRET:", mask_secret(STRIPE_WEBHOOK_SECRET))

boot_log()

# ----------------------------
# Order store (JSON file + lock)
# ----------------------------

def _locked_read_write_json(path: str, mutator):
    ensure_dir(os.path.dirname(path) or ".")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("{}")

    with open(path, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            raw = f.read().strip()
            data = json.loads(raw) if raw else {}
            out = mutator(data)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(data, indent=2))
            f.flush()
            os.fsync(f.fileno())
            return out
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def save_order(order_id: str, payload: Dict[str, Any]) -> None:
    def mut(data: Dict[str, Any]):
        data[order_id] = payload
    _locked_read_write_json(ORDER_DATA_PATH, mut)

def update_order(order_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    def mut(data: Dict[str, Any]):
        cur = data.get(order_id, {})
        cur.update(patch)
        data[order_id] = cur
        return cur
    return _locked_read_write_json(ORDER_DATA_PATH, mut)

def get_order(order_id: str) -> Dict[str, Any]:
    def mut(data: Dict[str, Any]):
        return data.get(order_id, {})
    return _locked_read_write_json(ORDER_DATA_PATH, mut)

# ----------------------------
# CORS
# ----------------------------

@app.after_request
def add_cors(resp):
    if CORS_ALLOW_ALL:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Stripe-Signature"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

@app.route("/", methods=["GET", "HEAD"])
def health():
    return ("ok", 200)

# ----------------------------
# STL handling
# ----------------------------

def stl_path_for_job(job_id: str) -> str:
    return os.path.join(UPLOAD_DIR, f"{job_id}.stl")

@app.route("/upload", methods=["POST"])
def upload():
    job_id = str(uuid.uuid4()).upper()
    path = stl_path_for_job(job_id)

    if "file" in request.files:
        request.files["file"].save(path)
    else:
        body = request.get_data()
        if not body:
            return jsonify({"error": "No file uploaded"}), 400
        with open(path, "wb") as out:
            out.write(body)

    size = os.path.getsize(path)
    print(f"✅ Uploaded STL job_id={job_id} -> {path} ({size} bytes)")

    return jsonify({
        "job_id": job_id,
        "stl_raw_url": f"{PUBLIC_BASE_URL}/stl-raw/{job_id}.stl",
    })

@app.route("/stl-raw/<job_id>.stl", methods=["GET", "HEAD"])
def stl_raw(job_id: str):
    path = stl_path_for_job(job_id)
    if not os.path.exists(path):
        abort(404)

    resp = send_file(
        path,
        mimetype="application/octet-stream",
        as_attachment=False,
        download_name=f"{job_id}.stl",
        conditional=True,
        max_age=3600,
        etag=True,
        last_modified=True,
    )
    resp.headers["Content-Disposition"] = f'attachment; filename="{job_id}.stl"'
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp

# ----------------------------
# Stripe checkout
# ----------------------------

def _coerce_items(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]

def _unit_amount_cents_for_item(it: Dict[str, Any]) -> int:
    # Force a dev price if set
    if DEV_PRICE_OVERRIDE_CENTS and DEV_PRICE_OVERRIDE_CENTS > 0:
        return DEV_PRICE_OVERRIDE_CENTS

    # Otherwise allow client-provided cents for dev/testing
    for k in ("unit_amount_cents", "unitAmountCents", "price_cents", "priceCents"):
        v = it.get(k)
        if v is None:
            continue
        try:
            cents = int(v)
            if cents > 0:
                return cents
        except Exception:
            pass

    # Fall back to default ($75)
    return DEFAULT_PRICE_CENTS

@app.route("/create-checkout-session", methods=["POST", "OPTIONS"])
def create_checkout_session():
    if request.method == "OPTIONS":
        return ("", 204)

    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "STRIPE_SECRET_KEY not set"}), 500

    payload = request.get_json(silent=True) or {}
    print(f"📥 /create-checkout-session payload: {{'keys': {list(payload.keys())}}}")

    order_id = (payload.get("order_id") or payload.get("orderId") or str(uuid.uuid4())).strip()
    items = _coerce_items(payload.get("items"))
    shipping_info = payload.get("shippingInfo") or {}

    if not items:
        return jsonify({"error": "Missing items"}), 400

    save_order(order_id, {
        "order_id": order_id,
        "created_at": utc_iso(),
        "status": "created",
        "items": items,
        "shippingInfo": shipping_info,
    })

    line_items = []
    for it in items:
        name = str(it.get("name") or it.get("title") or "Krezz Item")
        qty = int(it.get("quantity") or 1)

        unit_amount = _unit_amount_cents_for_item(it)
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": name},
                "unit_amount": unit_amount,
            },
            "quantity": qty,
        })

    success_url = f"{PUBLIC_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}&order_id={quote(order_id)}"
    cancel_url = f"{PUBLIC_BASE_URL}/cancel?order_id={quote(order_id)}"

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=line_items,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"order_id": order_id},
    )

    update_order(order_id, {
        "stripe_session_id": session["id"],
        "stripe_livemode": bool(session.get("livemode")),
        "status": "checkout_created",
    })

    print(f"✅ Created checkout session: {session['id']} order_id={order_id}")
    return jsonify({"id": session["id"], "url": session["url"], "order_id": order_id})

# ----------------------------
# Webhook
# ----------------------------

def _extract_receipt_url_from_session(session_id: str) -> Optional[str]:
    try:
        sess = stripe.checkout.Session.retrieve(
            session_id,
            expand=["payment_intent", "payment_intent.latest_charge"]
        )
        pi = sess.get("payment_intent")
        if isinstance(pi, dict):
            lc = pi.get("latest_charge")
            if isinstance(lc, dict):
                return lc.get("receipt_url")
        return None
    except Exception as e:
        print("⚠️ receipt_url fetch failed:", str(e))
        return None

class SlantError(Exception):
    def __init__(self, status: int, body: str, where: str):
        super().__init__(f"{where}: status={status} body={body[:500]}")
        self.status = status
        self.body = body
        self.where = where

def slant_headers() -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if SLANT_SEND_BEARER:
        h["Authorization"] = f"Bearer {SLANT_API_KEY}"
    else:
        h["Authorization"] = SLANT_API_KEY
        h["x-api-key"] = SLANT_API_KEY
    return h

def slant_create_file_by_url(job_id: str, stl_url: str) -> Dict[str, Any]:
    url_fields = [SLANT_FILE_URL_FIELD, "url", "fileUrl", "fileURL", "URL"]
    seen = set()
    url_fields = [f for f in url_fields if f and not (f in seen or seen.add(f))]

    last_err: Optional[Tuple[int, str]] = None
    for field in url_fields:
        payload = {
            "platformId": SLANT_PLATFORM_ID,
            "name": f"{job_id}.stl",
            "filename": f"{job_id}.stl",
            field: stl_url,
        }

        if SLANT_DEBUG:
            print("🧪 Slant create file request", {"endpoint": SLANT_FILES_ENDPOINT, "payload_keys": list(payload.keys()), "url_field": field})

        r = requests.post(
            SLANT_FILES_ENDPOINT,
            json=payload,
            headers=slant_headers(),
            timeout=SLANT_TIMEOUT_SEC,
        )

        if SLANT_DEBUG:
            print("🧪 SLANT_HTTP", {"where": "POST /files", "status": r.status_code, "body_snippet": r.text[:300]})

        if 200 <= r.status_code < 300:
            try:
                return r.json()
            except Exception:
                return {"raw": r.text}

        last_err = (r.status_code, r.text)

        if r.status_code in (401, 403):
            raise SlantError(r.status_code, r.text, "Slant POST /files (auth)")

    if last_err:
        raise SlantError(last_err[0], last_err[1], "Slant POST /files (url mode)")
    raise SlantError(500, "No response", "Slant POST /files (url mode)")

def submit_paid_order_to_slant(order_id: str) -> None:
    order = get_order(order_id)
    if not order:
        print("❌ Slant submit: missing order", order_id)
        return

    livemode = bool(order.get("stripe_livemode"))
    if SLANT_REQUIRE_LIVE_STRIPE and not livemode:
        print("⏭️ Skipping Slant submit (requires live Stripe). order_id=", order_id)
        return

    items = order.get("items") or []
    for it in items:
        job_id = it.get("job_id") or it.get("jobId") or it.get("jobID")
        if not job_id:
            continue

        jid = str(job_id).upper()
        file_path = stl_path_for_job(jid)
        if not os.path.exists(file_path):
            print("❌ Slant submit: STL not found:", file_path)
            continue

        stl_url = f"{PUBLIC_BASE_URL}/stl-raw/{jid}.stl"
        resp = slant_create_file_by_url(jid, stl_url)
        it["slant_file_response"] = resp

    update_order(order_id, {"items": items, "slant_submitted_at": utc_iso(), "slant_status": "submitted"})
    print("✅ Slant submit complete for order_id:", order_id)

def queue_slant_submit(order_id: str) -> None:
    def _run():
        try:
            print("🧵 Slant async started: order_id=", order_id)
            submit_paid_order_to_slant(order_id)
        except Exception as e:
            print("❌ Slant async exception:", str(e))
            update_order(order_id, {"slant_status": "error", "slant_error": str(e)})

    threading.Thread(target=_run, daemon=True).start()

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_data(cache=False, as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

    if not sig_header:
        print("❌ Webhook missing Stripe-Signature header")
        return ("bad", 400)

    if not STRIPE_WEBHOOK_SECRET:
        print("❌ STRIPE_WEBHOOK_SECRET not set (cannot verify webhooks)")
        return ("bad", 400)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print("❌ Webhook signature verification failed:", str(e))
        return ("bad", 400)

    etype = event.get("type")
    data_obj = (event.get("data") or {}).get("object") or {}
    livemode = bool(event.get("livemode"))

    print(f"📦 Stripe event: {etype} livemode={livemode}")

    if etype == "checkout.session.completed":
        order_id = (data_obj.get("metadata") or {}).get("order_id") or ""
        session_id = data_obj.get("id") or ""
        if not order_id:
            print("⚠️ session.completed but missing metadata.order_id")
            return ("ok", 200)

        receipt_url = _extract_receipt_url_from_session(session_id) if session_id else None

        update_order(order_id, {
            "status": "paid",
            "paid_at": utc_iso(),
            "stripe_livemode": livemode,
            "stripe_session_id": session_id,
            "receipt_url": receipt_url,
        })

        print("✅ Payment confirmed for order_id:", order_id)

        if SLANT_ENABLED and SLANT_AUTO_SUBMIT:
            print("➡️ Queueing Slant submit: order_id=", order_id)
            queue_slant_submit(order_id)

    return ("ok", 200)

# ----------------------------
# Success/Cancel pages
# ----------------------------

def build_deeplink(order_id: str, session_id: str) -> str:
    qs = urlencode({"order_id": order_id, "session_id": session_id})
    return f"{APP_URL_SCHEME}://{APP_DEEPLINK_PATH}?{qs}"

@app.route("/success", methods=["GET"])
def success():
    session_id = (request.args.get("session_id") or "").strip()
    order_id = (request.args.get("order_id") or "").strip()

    deeplink = build_deeplink(order_id, session_id) if (order_id or session_id) else f"{APP_URL_SCHEME}://{APP_DEEPLINK_PATH}"

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Payment successful</title>
  <style>
    body {{ font-family: -apple-system, system-ui, Segoe UI, Roboto, Helvetica, Arial; padding: 24px; }}
    .btn {{ display:inline-block; padding:14px 18px; border-radius:12px; background:#000; color:#fff; text-decoration:none; font-weight:600; }}
    .muted {{ color:#555; }}
    code {{ background:#f2f2f2; padding:2px 6px; border-radius:6px; }}
  </style>
</head>
<body>
  <h2>✅ Payment successful</h2>
  <p class="muted">You can close this window and return to the app.</p>

  <p><a class="btn" href="{deeplink}">Open Krezz App</a></p>

  <p>Order: <code>{order_id}</code></p>
  <p>Session: <code>{session_id}</code></p>

  <script>
    setTimeout(function() {{
      window.location.href = "{deeplink}";
    }}, 350);
  </script>
</body>
</html>"""
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp

@app.route("/cancel", methods=["GET"])
def cancel():
    order_id = (request.args.get("order_id") or "").strip()
    return f"Checkout canceled. order_id={order_id}", 200

# ----------------------------
# App fetch endpoints
# ----------------------------

@app.route("/order-data/<order_id>", methods=["GET"])
def order_data(order_id: str):
    data = get_order(order_id) or {}
    if not data:
        return jsonify({"error": "not_found"}), 404
    return jsonify(data)

@app.route("/order-status/<order_id>", methods=["GET"])
def order_status(order_id: str):
    data = get_order(order_id) or {}
    if not data:
        return jsonify({"error": "not_found"}), 404
    return jsonify({
        "order_id": order_id,
        "status": data.get("status"),
        "paid_at": data.get("paid_at"),
        "receipt_url": data.get("receipt_url"),
        "slant_status": data.get("slant_status"),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
