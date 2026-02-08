from __future__ import annotations

import os
import re
import io
import json
import uuid
import time
import fcntl
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import stripe
from flask import Flask, request, jsonify, send_file, abort, make_response

app = Flask(__name__)

# ----------------------------
# Helpers / config
# ----------------------------

def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return (v.strip() if v else default)

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
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "…" + s[-keep:]

def is_uuid_like(s: str) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except Exception:
        return False

def safe_job_id(s: str) -> str:
    """
    Only allow UUID-like IDs (prevents path traversal).
    """
    if not s or not is_uuid_like(s):
        return str(uuid.uuid4())
    return str(uuid.UUID(s))

PUBLIC_BASE_URL = env_str("PUBLIC_BASE_URL", "http://localhost:10000")
UPLOAD_DIR = env_str("UPLOAD_DIR", "/data/uploads")
ORDER_DATA_PATH = env_str("ORDER_DATA_PATH", "/data/order_data.json")

STRIPE_SUCCESS_URL = env_str(
    "STRIPE_SUCCESS_URL",
    f"{PUBLIC_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}"
)
STRIPE_CANCEL_URL = env_str(
    "STRIPE_CANCEL_URL",
    f"{PUBLIC_BASE_URL}/cancel"
)

# STL serving behavior
STL_MIMETYPE = env_str("STL_MIMETYPE", "application/octet-stream")

# Slant config
SLANT_ENABLED = env_bool("SLANT_ENABLED", True)
SLANT_DEBUG = env_bool("SLANT_DEBUG", True)
SLANT_AUTO_SUBMIT = env_bool("SLANT_AUTO_SUBMIT", True)
SLANT_REQUIRE_LIVE_STRIPE = env_bool("SLANT_REQUIRE_LIVE_STRIPE", True)

SLANT_BASE_URL = env_str("SLANT_BASE_URL", "https://slant3dapi.com/v2/api")
SLANT_FILES_ENDPOINT = env_str("SLANT_FILES_ENDPOINT", f"{SLANT_BASE_URL}/files")
SLANT_TIMEOUT_SEC = env_int("SLANT_TIMEOUT_SEC", 240)

# If you know the correct field, set it. If not, we’ll try variants.
SLANT_FILE_URL_FIELD = env_str("SLANT_FILE_URL_FIELD", "")
SLANT_SEND_BEARER = env_bool("SLANT_SEND_BEARER", True)

# Upload mode:
# - "url"      => Slant fetches from your public STL URL
# - "multipart" => you POST bytes directly (only works if Slant supports it)
SLANT_UPLOAD_MODE = env_str("SLANT_UPLOAD_MODE", "url").lower().strip()

SLANT_API_KEY = env_str("SLANT_API_KEY", "")
SLANT_PLATFORM_ID = env_str("SLANT_PLATFORM_ID", "")

# Optional: your app deep link scheme for the success page
APP_DEEPLINK_BASE = env_str("APP_DEEPLINK_BASE", "krezz://success")

stripe.api_key = env_str("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = env_str("STRIPE_WEBHOOK_SECRET", "")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.dirname(ORDER_DATA_PATH) or "/data", exist_ok=True)

print("✅ Boot config:")
print(f"   PUBLIC_BASE_URL: {PUBLIC_BASE_URL}")
print(f"   UPLOAD_DIR: {UPLOAD_DIR}")
print(f"   ORDER_DATA_PATH: {ORDER_DATA_PATH}")
print(f"   STRIPE_SUCCESS_URL: {STRIPE_SUCCESS_URL}")
print(f"   STRIPE_CANCEL_URL: {STRIPE_CANCEL_URL}")
print(f"   SLANT_ENABLED: {SLANT_ENABLED}")
print(f"   SLANT_DEBUG: {SLANT_DEBUG}")
print(f"   SLANT_AUTO_SUBMIT: {SLANT_AUTO_SUBMIT}")
print(f"   SLANT_REQUIRE_LIVE_STRIPE: {SLANT_REQUIRE_LIVE_STRIPE}")
print(f"   SLANT_BASE_URL: {SLANT_BASE_URL}")
print(f"   SLANT_FILES_ENDPOINT: {SLANT_FILES_ENDPOINT}")
print(f"   SLANT_TIMEOUT_SEC: {SLANT_TIMEOUT_SEC}")
print(f"   SLANT_FILE_URL_FIELD: {SLANT_FILE_URL_FIELD or '(auto)'}")
print(f"   SLANT_UPLOAD_MODE: {SLANT_UPLOAD_MODE}")
print(f"   SLANT_SEND_BEARER: {SLANT_SEND_BEARER}")
print(f"   SLANT_API_KEY: {mask_secret(SLANT_API_KEY)}")
print(f"   SLANT_PLATFORM_ID: {SLANT_PLATFORM_ID}")

# ----------------------------
# Simple JSON store with file lock
# ----------------------------

def _read_store() -> Dict[str, Any]:
    if not os.path.exists(ORDER_DATA_PATH):
        return {"orders": {}}
    try:
        with open(ORDER_DATA_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"orders": {}}

def _write_store(data: Dict[str, Any]) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="orders_", suffix=".json", dir=os.path.dirname(ORDER_DATA_PATH) or "/data")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, ORDER_DATA_PATH)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

class FileLock:
    def __init__(self, path: str):
        self.path = path
        self.f = None

    def __enter__(self):
        self.f = open(self.path + ".lock", "a+")
        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self.f.close()
            except Exception:
                pass

def store_update(fn):
    with FileLock(ORDER_DATA_PATH):
        data = _read_store()
        changed = fn(data)
        if changed:
            _write_store(data)

def store_get_order(order_id: str) -> Optional[Dict[str, Any]]:
    with FileLock(ORDER_DATA_PATH):
        data = _read_store()
        return data.get("orders", {}).get(order_id)

# ----------------------------
# Routes
# ----------------------------

@app.get("/")
def index():
    return jsonify({"ok": True, "ts": utc_iso()})

@app.get("/health")
def health():
    return "ok", 200

@app.post("/upload")
def upload_stl():
    job_id = safe_job_id(request.headers.get("X-Job-ID") or request.args.get("job_id") or "")
    filename = f"{job_id}.stl"
    path = os.path.join(UPLOAD_DIR, filename)

    if "file" in request.files:
        f = request.files["file"]
        f.save(path)
    else:
        # allow raw bytes
        raw = request.get_data() or b""
        if not raw:
            return jsonify({"error": "No file uploaded"}), 400
        with open(path, "wb") as out:
            out.write(raw)

    print(f"✅ Uploaded STL job_id={job_id} -> {path}")
    return jsonify({
        "job_id": job_id,
        "stl_url": f"{PUBLIC_BASE_URL}/stl-raw/{filename}",
        "stored_path": path
    })

@app.route("/stl-raw/<path:filename>", methods=["GET", "HEAD"])
def serve_stl_raw(filename: str):
    # only allow UUID.stl
    if not re.fullmatch(r"[0-9a-fA-F-]{36}\.stl", filename or ""):
        abort(404)
    path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(path):
        abort(404)

    # conditional=True enables Range requests and correct headers
    resp = send_file(
        path,
        mimetype=STL_MIMETYPE,
        as_attachment=True,
        download_name=filename,
        conditional=True,
        max_age=0
    )
    # Make it friendlier for external fetchers
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp

@app.post("/create-checkout-session")
def create_checkout_session():
    payload = request.get_json(force=True, silent=True) or {}
    print(f"📥 /create-checkout-session payload: {{'keys': {list(payload.keys())}}}")

    order_id = payload.get("order_id") or str(uuid.uuid4())
    items = payload.get("items") or []
    shipping = payload.get("shippingInfo") or {}

    if not isinstance(items, list) or not items:
        return jsonify({"error": "items must be a non-empty list"}), 400

    # Persist the order draft immediately so your app can fetch it later
    def _persist(data):
        data.setdefault("orders", {})
        data["orders"].setdefault(order_id, {})
        data["orders"][order_id].update({
            "order_id": order_id,
            "createdAt": data["orders"][order_id].get("createdAt") or utc_iso(),
            "updatedAt": utc_iso(),
            "status": data["orders"][order_id].get("status") or "created",
            "paid": bool(data["orders"][order_id].get("paid", False)),
            "items": items,
            "shippingInfo": shipping,
        })
        return True
    store_update(_persist)

    # Build Stripe line_items
    line_items = []
    for it in items:
        name = it.get("name") or it.get("styleName") or "Krezz Item"
        price_cents = int(it.get("price_cents") or it.get("priceCents") or 1000)
        qty = int(it.get("quantity") or 1)
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {"name": name},
                "unit_amount": price_cents,
            },
            "quantity": qty
        })

    # IMPORTANT: include order_id in the success url so the success page can deep-link the app
    success_url = f"{PUBLIC_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}&order_id={order_id}"
    cancel_url = STRIPE_CANCEL_URL

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=line_items,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "order_id": order_id,
        },
        payment_intent_data={
            "metadata": {"order_id": order_id}
        }
    )

    # Save Stripe IDs on the order
    def _save_session(data):
        o = data.setdefault("orders", {}).setdefault(order_id, {})
        o["stripe_session_id"] = session.id
        o["stripe_checkout_url"] = session.url
        o["updatedAt"] = utc_iso()
        return True
    store_update(_save_session)

    print(f"✅ Created checkout session: {session.id} order_id={order_id}")
    return jsonify({"order_id": order_id, "sessionId": session.id, "url": session.url})

@app.post("/webhook")
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print(f"❌ Webhook signature verification failed: {e}")
        return "bad signature", 400

    print(f"📦 Stripe event: {event['type']} (livemode={event.get('livemode')})")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id") or ""

        if not order_id:
            print("⚠️ checkout.session.completed but no order_id in metadata")
            return "ok", 200

        if SLANT_REQUIRE_LIVE_STRIPE and not event.get("livemode", False):
            print("⚠️ Ignoring testmode payment because SLANT_REQUIRE_LIVE_STRIPE=True")
            return "ok", 200

        # Mark paid
        def _mark_paid(data):
            o = data.setdefault("orders", {}).setdefault(order_id, {})
            o["paid"] = True
            o["status"] = "paid"
            o["paidAt"] = utc_iso()
            o["stripe_session_id"] = session.get("id")
            o["payment_intent"] = session.get("payment_intent")
            o["updatedAt"] = utc_iso()
            return True
        store_update(_mark_paid)

        print(f"✅ Payment confirmed for order_id: {order_id}")

        if SLANT_ENABLED and SLANT_AUTO_SUBMIT:
            print(f"➡️ Queueing Slant submit: order_id={order_id}")
            _start_slant_async(order_id)

    return "ok", 200

@app.get("/order-data/<order_id>")
def order_data(order_id: str):
    o = store_get_order(order_id)
    if not o:
        return jsonify({"error": "not found"}), 404
    return jsonify(o)

@app.get("/order-status/<order_id>")
def order_status(order_id: str):
    o = store_get_order(order_id) or {}
    return jsonify({
        "order_id": order_id,
        "status": o.get("status", "unknown"),
        "paid": bool(o.get("paid", False)),
        "slant": o.get("slant", {}),
        "updatedAt": o.get("updatedAt")
    })

@app.get("/success")
def success():
    session_id = request.args.get("session_id", "")
    order_id = request.args.get("order_id", "")

    deep = APP_DEEPLINK_BASE
    qs = []
    if order_id:
        qs.append(f"order_id={order_id}")
    if session_id:
        qs.append(f"session_id={session_id}")
    deep_link = deep + ("?" + "&".join(qs) if qs else "")

    # Page tries to open app (may be blocked), but always shows a button.
    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Payment successful</title>
  <style>
    body {{ font-family: -apple-system, system-ui, Arial; padding: 24px; }}
    .btn {{ display:inline-block; padding:12px 16px; border-radius:10px; background:#000; color:#fff; text-decoration:none; }}
    .muted {{ color:#555; }}
    code {{ background:#f3f3f3; padding:2px 6px; border-radius:6px; }}
  </style>
</head>
<body>
  <h2>✅ Payment successful</h2>
  <p class="muted">You can close this window and return to the app.</p>
  <p><a class="btn" href="{deep_link}">Open Krezz App</a></p>

  <p class="muted">Order: <code>{order_id or "unknown"}</code></p>
  <p class="muted">Session: <code>{session_id or "unknown"}</code></p>

  <script>
    // Soft attempt to open the app (often blocked unless user taps)
    setTimeout(() => {{
      try {{ window.location.href = "{deep_link}"; }} catch (e) {{}}
    }}, 900);
  </script>
</body>
</html>
"""
    return make_response(html, 200)

@app.get("/cancel")
def cancel():
    return "Payment cancelled.", 200

# ----------------------------
# Slant integration
# ----------------------------

class SlantError(Exception):
    def __init__(self, status: int, body: str, where: str):
        super().__init__(f"{where}: status={status} body={body}")
        self.status = status
        self.body = body
        self.where = where

def _slant_headers() -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if SLANT_SEND_BEARER and SLANT_API_KEY:
        h["Authorization"] = f"Bearer {SLANT_API_KEY}"
    else:
        # If Slant ever supports other auth headers, you can add them here.
        # Leaving it empty will reproduce the 401 you saw.
        pass
    return h

def _slant_post_json(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(url, json=payload, headers=_slant_headers(), timeout=SLANT_TIMEOUT_SEC)
    if SLANT_DEBUG:
        print(f"🧪 SLANT_HTTP {{'where':'POST {url}','status':{r.status_code},'body_snippet':{json.dumps(r.text[:220])}}}")
    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, f"Slant POST {url}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}

def slant_create_file_by_url(job_id: str, stl_url: str) -> str:
    if not SLANT_PLATFORM_ID:
        raise SlantError(400, "Missing SLANT_PLATFORM_ID", "slant_create_file_by_url")

    # Try a few possible url field names. (Your logs show Slant is picky / unclear.)
    url_fields = []
    if SLANT_FILE_URL_FIELD:
        url_fields.append(SLANT_FILE_URL_FIELD)
    url_fields += ["url", "URL", "fileURL", "fileUrl"]

    last_err: Optional[Exception] = None

    for url_field in url_fields:
        payload = {
            "platformId": SLANT_PLATFORM_ID,
            "name": f"{job_id}.stl",
            "filename": f"{job_id}.stl",
            url_field: stl_url,
        }

        if SLANT_DEBUG:
            print(f"🧪 Slant create file request {{'endpoint':{json.dumps(SLANT_FILES_ENDPOINT)},'payload_keys':{list(payload.keys())},'url_field':{json.dumps(url_field)},'stl_url':{json.dumps(stl_url)} }}")

        try:
            data = _slant_post_json(SLANT_FILES_ENDPOINT, payload)

            # Heuristic: look for an ID-ish field in response
            for k in ("id", "publicFileServiceId", "fileId", "publicFileId"):
                if k in data and data[k]:
                    return str(data[k])

            # Some APIs return success + nested object
            if isinstance(data.get("data"), dict):
                d = data["data"]
                for k in ("id", "publicFileServiceId", "fileId", "publicFileId"):
                    if k in d and d[k]:
                        return str(d[k])

            # If we get here, response was 200 but no recognizable ID
            raise SlantError(500, json.dumps(data)[:500], "Slant create file: unknown response shape")
        except Exception as e:
            last_err = e
            # If 401, no point trying other fields
            if isinstance(e, SlantError) and e.status == 401:
                raise
            continue

    raise last_err or SlantError(500, "Unknown error", "slant_create_file_by_url")

def slant_create_file_multipart(job_id: str, stl_path: str) -> str:
    """
    Only works if Slant supports multipart form uploads on /files.
    If it doesn't, keep SLANT_UPLOAD_MODE=url.
    """
    if not SLANT_PLATFORM_ID:
        raise SlantError(400, "Missing SLANT_PLATFORM_ID", "slant_create_file_multipart")

    with open(stl_path, "rb") as f:
        files = {
            "file": (f"{job_id}.stl", f, STL_MIMETYPE),
        }
        data = {
            "platformId": SLANT_PLATFORM_ID,
            "name": f"{job_id}.stl",
            "filename": f"{job_id}.stl",
        }
        r = requests.post(SLANT_FILES_ENDPOINT, data=data, files=files, headers=_slant_headers(), timeout=SLANT_TIMEOUT_SEC)

    if SLANT_DEBUG:
        print(f"🧪 SLANT_HTTP {{'where':'MULTIPART POST /files','status':{r.status_code},'body_snippet':{json.dumps(r.text[:220])}}}")

    if r.status_code >= 400:
        raise SlantError(r.status_code, r.text, "Slant MULTIPART POST /files")

    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}

    for k in ("id", "publicFileServiceId", "fileId", "publicFileId"):
        if k in j and j[k]:
            return str(j[k])
    if isinstance(j.get("data"), dict):
        d = j["data"]
        for k in ("id", "publicFileServiceId", "fileId", "publicFileId"):
            if k in d and d[k]:
                return str(d[k])

    raise SlantError(500, json.dumps(j)[:500], "Slant multipart: unknown response shape")

def _start_slant_async(order_id: str) -> None:
    def _run():
        try:
            submit_paid_order_to_slant(order_id)
        except Exception as e:
            print(f"❌ Slant async exception: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()

def submit_paid_order_to_slant(order_id: str) -> None:
    o = store_get_order(order_id)
    if not o:
        return

    items = o.get("items") or []
    if not items:
        return

    def _set_status(status: str, extra: Dict[str, Any]):
        def _upd(data):
            ordx = data.setdefault("orders", {}).setdefault(order_id, {})
            ordx["status"] = status
            ordx.setdefault("slant", {}).update(extra)
            ordx["updatedAt"] = utc_iso()
            return True
        store_update(_upd)

    _set_status("slant_processing", {"startedAt": utc_iso(), "error": None})

    # For each item, upload / register STL with Slant if job_id exists
    for it in items:
        job_id = it.get("job_id") or it.get("jobId") or it.get("jobID") or ""
        if not job_id or not is_uuid_like(job_id):
            continue

        stl_filename = f"{safe_job_id(job_id)}.stl"
        stl_path = os.path.join(UPLOAD_DIR, stl_filename)
        stl_url = f"{PUBLIC_BASE_URL}/stl-raw/{stl_filename}"

        if not os.path.exists(stl_path):
            continue

        # URL mode vs multipart mode
        if SLANT_UPLOAD_MODE == "multipart":
            file_id = slant_create_file_multipart(job_id, stl_path)
        else:
            file_id = slant_create_file_by_url(job_id, stl_url)

        # Store back on item for downstream Slant order submission
        it["publicFileServiceId"] = file_id

    # Save updated items back into store
    def _save_items(data):
        ordx = data.setdefault("orders", {}).setdefault(order_id, {})
        ordx["items"] = items
        ordx["updatedAt"] = utc_iso()
        return True
    store_update(_save_items)

    # NOTE:
    # At this point you have file IDs on items.
    # If your Slant flow requires creating an order, add that step here once you confirm the endpoint/payload.
    _set_status("slant_ready", {"finishedAt": utc_iso()})

