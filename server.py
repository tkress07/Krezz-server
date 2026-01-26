from __future__ import annotations

import os
import json
import uuid
import time
import logging
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import stripe
import requests
from flask import Flask, request, jsonify, send_file, abort

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("krezz-server")

# ------------------------------------------------------------
# Flask
# ------------------------------------------------------------
app = Flask(__name__)

# ------------------------------------------------------------
# Stripe config
# ------------------------------------------------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET")
if not stripe.api_key or not STRIPE_ENDPOINT_SECRET:
    raise ValueError("Stripe env vars missing: STRIPE_SECRET_KEY and/or STRIPE_ENDPOINT_SECRET")

# ------------------------------------------------------------
# Local storage (Render disk)
# ------------------------------------------------------------
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATA_PATH = os.getenv("ORDER_DATA_PATH", "/data/order_data.json")


class OrderStore:
    """
    Small JSON store with a process-local lock + atomic writes.
    Note: if you run multiple gunicorn workers, each worker is its own process.
    For true multi-worker safety, move this to a DB. For now keep workers=1.
    """
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                with open(self.path, "r") as f:
                    self.data = json.load(f)
                log.info("✅ Loaded ORDER_DATA (%d orders) from %s", len(self.data), self.path)
            except Exception:
                self.data = {}
                log.info("ℹ️ No prior ORDER_DATA found (starting fresh)")

    def save(self) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                # atomic write
                with tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(self.path)) as tf:
                    json.dump(self.data, tf)
                    tmp_name = tf.name
                os.replace(tmp_name, self.path)
            except Exception as e:
                log.exception("❌ Failed to persist ORDER_DATA: %s", e)

    def get(self, order_id: str) -> Optional[dict]:
        with self._lock:
            v = self.data.get(order_id)
            return json.loads(json.dumps(v)) if v is not None else None

    def set(self, order_id: str, value: dict) -> None:
        with self._lock:
            self.data[order_id] = value

    def update(self, order_id: str, patch: dict) -> None:
        with self._lock:
            base = self.data.get(order_id) or {}
            base.update(patch)
            self.data[order_id] = base


STORE = OrderStore(DATA_PATH)

# ------------------------------------------------------------
# Slant config  (IMPORTANT: use slant3dapi.com/v2/api + Bearer)
# ------------------------------------------------------------
SLANT_API_KEY = os.getenv("SLANT_API_KEY")
SLANT_PLATFORM_ID = os.getenv("SLANT_PLATFORM_ID")  # REQUIRED for file upload (per your 400 error)
SLANT_BASE_URL = (os.getenv("SLANT_BASE_URL") or "https://slant3dapi.com/v2/api").rstrip("/")

# Optional
SLANT_DEFAULT_FILAMENT_ID = os.getenv("SLANT_DEFAULT_FILAMENT_ID")  # publicId
SLANT_TIMEOUT_SEC = int(os.getenv("SLANT_TIMEOUT_SEC", "30"))
SLANT_UPLOAD_TIMEOUT_SEC = int(os.getenv("SLANT_UPLOAD_TIMEOUT_SEC", "90"))

# Endpoints (overrideable)
SLANT_FILAMENTS_ENDPOINT = os.getenv("SLANT_FILAMENTS_ENDPOINT") or f"{SLANT_BASE_URL}/filaments"
SLANT_FILES_ENDPOINT = os.getenv("SLANT_FILES_ENDPOINT") or f"{SLANT_BASE_URL}/files"
SLANT_ORDERS_ENDPOINT = os.getenv("SLANT_ORDERS_ENDPOINT") or f"{SLANT_BASE_URL}/orders"


@dataclass
class SlantError(Exception):
    status: int
    body: str
    where: str

    def __str__(self) -> str:
        return f"{self.where}: status={self.status} body={self.body[:1200]}"


class SlantClient:
    def __init__(self, api_key: str, platform_id: Optional[str]):
        self.api_key = api_key
        self.platform_id = platform_id
        self.sess = requests.Session()

    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "KrezzServer/1.0",
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
        timeout: int = SLANT_TIMEOUT_SEC,
        where: str = "slant",
        retry: int = 2,
    ) -> requests.Response:
        # Very small retry for transient failures
        for attempt in range(retry + 1):
            try:
                r = self.sess.request(
                    method=method,
                    url=url,
                    headers=self.headers(),
                    json=json_body,
                    data=data,
                    files=files,
                    timeout=timeout,
                )
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retry:
                    time.sleep(0.7 * (attempt + 1))
                    continue
                return r
            except requests.RequestException as e:
                if attempt >= retry:
                    raise SlantError(status=0, body=str(e), where=where)
                time.sleep(0.7 * (attempt + 1))
        raise SlantError(status=0, body="Unknown request failure", where=where)

    def get_filaments(self) -> List[dict]:
        r = self._request("GET", SLANT_FILAMENTS_ENDPOINT, where="Slant get_filaments")
        if r.status_code >= 400:
            raise SlantError(r.status_code, r.text, "Slant get_filaments")
        payload = r.json() if r.text else {}
        return payload.get("data") or []

    def resolve_filament_id(self, shipping: dict) -> str:
        material = (shipping.get("material") or "").upper()
        color = (shipping.get("color") or "").strip().lower()
        want_profile = "PETG" if "PETG" in material else "PLA"

        filaments = self.get_filaments()

        # exact match (profile + color)
        for f in filaments:
            if not f.get("available", True):
                continue
            if (f.get("profile") or "").upper() == want_profile and (f.get("color") or "").lower() == color:
                if f.get("publicId"):
                    return f["publicId"]

        # profile match
        for f in filaments:
            if not f.get("available", True):
                continue
            if (f.get("profile") or "").upper() == want_profile and f.get("publicId"):
                return f["publicId"]

        if SLANT_DEFAULT_FILAMENT_ID:
            return SLANT_DEFAULT_FILAMENT_ID

        if filaments and filaments[0].get("publicId"):
            return filaments[0]["publicId"]

        raise RuntimeError("No filament available and SLANT_DEFAULT_FILAMENT_ID not set.")

    def upload_stl(self, job_id: str, stl_path: str) -> str:
        if not self.platform_id:
            # This is the exact cause of your earlier 400.
            raise RuntimeError("SLANT_PLATFORM_ID is missing (required for /files upload).")

        if not os.path.exists(stl_path):
            raise RuntimeError(f"STL not found for job_id={job_id}: {stl_path}")

        with open(stl_path, "rb") as f:
            files = {
                # many APIs accept model/stl; your prior application/sla worked too.
                "file": (f"{job_id}.stl", f, "model/stl"),
            }
            form = {
                "platformId": self.platform_id,
                "name": f"{job_id}.stl",
                # keep optional hints; harmless if ignored
                "type": "STL",
            }

            log.info("➡️ Uploading STL to Slant Files: job_id=%s endpoint=%s", job_id, SLANT_FILES_ENDPOINT)
            r = self._request(
                "POST",
                SLANT_FILES_ENDPOINT,
                files=files,
                data=form,
                timeout=SLANT_UPLOAD_TIMEOUT_SEC,
                where="Slant upload_stl",
            )

        if r.status_code >= 400:
            raise SlantError(r.status_code, r.text, "Slant upload_stl")

        payload = r.json() if r.text else {}
        data_obj = payload.get("data") if isinstance(payload, dict) else None

        public_id = None
        if isinstance(data_obj, dict):
            public_id = data_obj.get("publicFileServiceId") or data_obj.get("publicId")
        if not public_id and isinstance(payload, dict):
            public_id = payload.get("publicFileServiceId") or payload.get("publicId")

        if not public_id:
            raise RuntimeError(f"Upload succeeded but no publicId returned: {str(payload)[:800]}")

        log.info("✅ Slant file uploaded: job_id=%s publicFileServiceId=%s", job_id, public_id)
        return public_id

    def create_order(self, internal_order_id: str, shipping: dict, items: List[dict]) -> str:
        """
        Creates (drafts) a Slant order. Payload includes redundant keys to be tolerant of schema variations.
        If Slant returns a schema error, the response is logged + stored so you can adjust quickly.
        """
        if not self.platform_id:
            raise RuntimeError("SLANT_PLATFORM_ID is missing (required to place orders).")

        # Normalize shipping
        def country_iso2(v: str) -> str:
            if not v:
                return "US"
            c = v.strip().lower()
            if c in ("us", "usa", "united states", "united states of america"):
                return "US"
            if len(v.strip()) == 2:
                return v.strip().upper()
            return "US"

        email = shipping.get("email") or "unknown@test.com"
        full_name = shipping.get("fullName") or shipping.get("name") or "Customer"
        phone = shipping.get("phone") or ""
        is_res = shipping.get("isResidential")
        if isinstance(is_res, str):
            is_res = is_res.strip().lower() in ("1", "true", "yes", "y")
        is_res = bool(is_res) if is_res is not None else True

        addr = {
            "name": full_name,
            "line1": shipping.get("addressLine") or shipping.get("line1") or "",
            "line2": shipping.get("addressLine2") or shipping.get("line2") or "",
            "city": shipping.get("city") or "",
            "state": shipping.get("state") or "",
            "zip": shipping.get("zipCode") or shipping.get("zip") or "",
            "country": country_iso2(shipping.get("country") or "US"),
        }

        filament_id = self.resolve_filament_id(shipping)

        slant_items = []
        for it in items:
            pfsid = it.get("publicFileServiceId")
            if not pfsid:
                raise RuntimeError(f"Missing publicFileServiceId for item job_id={it.get('job_id')}")
            slant_items.append({
                "type": "PRINT",
                "publicFileServiceId": pfsid,
                "filamentId": filament_id,
                "quantity": int(it.get("quantity", 1)),
                "name": it.get("name", "Krezz Mold"),
                "SKU": it.get("SKU") or it.get("sku") or it.get("job_id", ""),
            })

        payload = {
            # platform identifiers (often required)
            "platformId": self.platform_id,
            "customer": {
                "platformId": self.platform_id,
                "details": {
                    "email": email,
                    "phone": phone,
                    "isResidential": is_res,
                    "address": addr,
                },
            },
            "items": slant_items,
            "metadata": {
                "internalOrderId": internal_order_id,
                "jobIds": [it.get("job_id") for it in items],
                "source": "KREZZ_SERVER",
            },
        }

        log.info("➡️ Creating Slant order: endpoint=%s internalOrderId=%s", SLANT_ORDERS_ENDPOINT, internal_order_id)
        r = self._request(
            "POST",
            SLANT_ORDERS_ENDPOINT,
            json_body=payload,
            timeout=SLANT_TIMEOUT_SEC,
            where="Slant create_order",
        )
        if r.status_code >= 400:
            raise SlantError(r.status_code, r.text, "Slant create_order")

        resp = r.json() if r.text else {}
        data_obj = resp.get("data") if isinstance(resp, dict) else None
        public_order_id = None
        if isinstance(data_obj, dict):
            public_order_id = data_obj.get("publicId") or data_obj.get("publicOrderId")
        if not public_order_id and isinstance(resp, dict):
            public_order_id = resp.get("publicId") or resp.get("publicOrderId")

        if not public_order_id:
            raise RuntimeError(f"Create order succeeded but no publicId returned: {str(resp)[:1200]}")

        log.info("✅ Slant order created: publicOrderId=%s", public_order_id)
        return public_order_id

    def process_order(self, public_order_id: str) -> dict:
        """
        Some APIs use POST /orders/{id}/process, others POST /orders/{id}.
        We'll try /process first, then fallback.
        """
        url_process = f"{SLANT_ORDERS_ENDPOINT}/{public_order_id}/process"
        url_fallback = f"{SLANT_ORDERS_ENDPOINT}/{public_order_id}"

        log.info("➡️ Processing Slant order (try /process): %s", url_process)
        r = self._request("POST", url_process, timeout=SLANT_TIMEOUT_SEC, where="Slant process_order(/process)")
        if r.status_code == 404:
            log.info("ℹ️ /process not found, trying fallback: %s", url_fallback)
            r = self._request("POST", url_fallback, timeout=SLANT_TIMEOUT_SEC, where="Slant process_order(fallback)")

        if r.status_code >= 400:
            raise SlantError(r.status_code, r.text, "Slant process_order")

        return r.json() if r.text else {"success": True}


def slant_enabled() -> bool:
    return bool(SLANT_API_KEY)


def get_slant_client() -> SlantClient:
    if not SLANT_API_KEY:
        raise RuntimeError("SLANT_API_KEY not configured")
    return SlantClient(api_key=SLANT_API_KEY, platform_id=SLANT_PLATFORM_ID)


def submit_paid_order_to_slant(order_id: str) -> None:
    """
    Idempotent: only submit once. Stores errors on the order record.
    """
    order = STORE.get(order_id) or {}
    status = order.get("status")

    if status in ("submitted_to_slant", "slant_processing", "in_production"):
        log.info("ℹ️ Order already submitted: order_id=%s status=%s", order_id, status)
        return

    items = order.get("items", []) or []
    shipping = order.get("shipping", {}) or {}

    client = get_slant_client()

    # mark submitting
    order["status"] = "slant_submitting"
    order.pop("slant_error", None)
    STORE.set(order_id, order)
    STORE.save()

    # upload files if needed
    for it in items:
        job_id = it.get("job_id")
        if not job_id:
            raise RuntimeError("Item missing job_id")

        if not it.get("publicFileServiceId"):
            stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
            pfsid = client.upload_stl(job_id, stl_path)
            it["publicFileServiceId"] = pfsid

    # create + process order
    public_order_id = client.create_order(order_id, shipping, items)
    order.setdefault("slant", {})
    order["slant"]["publicOrderId"] = public_order_id
    order["status"] = "slant_created"
    STORE.set(order_id, order)
    STORE.save()

    process_resp = client.process_order(public_order_id)
    order["slant"]["processResponse"] = process_resp
    order["status"] = "submitted_to_slant"
    STORE.set(order_id, order)
    STORE.save()

    log.info("✅ Slant submission complete: order_id=%s publicOrderId=%s", order_id, public_order_id)


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.route("/")
def index():
    return "✅ Krezz server is live (Stripe + Slant)."


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "time": datetime.utcnow().isoformat() + "Z",
        "slant_enabled": slant_enabled(),
        "slant_base_url": SLANT_BASE_URL,
        "has_slant_platform_id": bool(SLANT_PLATFORM_ID),
        "upload_dir": UPLOAD_DIR,
        "order_data_path": DATA_PATH,
    })


@app.route("/upload", methods=["POST"])
def upload_stl():
    job_id = request.form.get("job_id")
    file = request.files.get("file")
    if not job_id or not file:
        return jsonify({"error": "Missing job_id or file"}), 400

    save_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    file.save(save_path)
    log.info("✅ Uploaded STL job_id=%s -> %s", job_id, save_path)
    return jsonify({"success": True, "path": save_path})


@app.route("/stl/<job_id>.stl", methods=["GET"])
def serve_stl(job_id):
    stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
    if not os.path.exists(stl_path):
        return abort(404)
    return send_file(
        stl_path,
        mimetype="model/stl",
        as_attachment=True,
        download_name=f"mold_{job_id}.stl",
    )


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    try:
        data = request.get_json(silent=True) or {}
        log.info("📥 /create-checkout-session payload: %s", data)

        items = data.get("items", []) or []
        shipping_info = data.get("shippingInfo", {}) or {}
        if not items:
            return jsonify({"error": "No items provided"}), 400

        order_id = data.get("order_id") or str(uuid.uuid4())

        # normalize items
        normalized_items = []
        for it in items:
            job_id = it.get("job_id") or it.get("jobId") or it.get("id")
            if not job_id:
                job_id = str(uuid.uuid4())
            it["job_id"] = job_id
            it["quantity"] = int(it.get("quantity", 1))
            normalized_items.append(it)

        STORE.set(order_id, {
            "items": normalized_items,
            "shipping": shipping_info,
            "status": "created",
            "created_at": datetime.utcnow().isoformat() + "Z",
        })
        STORE.save()

        line_items = [{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": it.get("name", "Beard Mold")},
                "unit_amount": int(it.get("price", 7500)),
            },
            "quantity": int(it.get("quantity", 1)),
        } for it in normalized_items]

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=line_items,
            success_url=f"krezzapp://order-confirmed?order_id={order_id}",
            cancel_url="https://krezzapp.com/cancel",
            metadata={"order_id": order_id},
        )

        log.info("✅ Created checkout session: %s order_id=%s", session.id, order_id)
        return jsonify({"url": session.url, "order_id": order_id})

    except Exception as e:
        log.exception("❌ Error in checkout session: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_ENDPOINT_SECRET)
    except Exception as e:
        log.exception("❌ Stripe webhook error: %s", e)
        return "Webhook error", 400

    log.info("📦 Stripe event: %s", event["type"])

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = (session.get("metadata") or {}).get("order_id")

        if not order_id:
            log.error("❌ Missing order_id in Stripe metadata")
            return jsonify(success=True)

        order = STORE.get(order_id) or {"items": [], "shipping": {}, "status": "created"}

        order["status"] = "paid"
        order["payment"] = {
            "stripe_session_id": session.get("id"),
            "amount_total": session.get("amount_total"),
            "currency": session.get("currency"),
            "created": datetime.utcfromtimestamp(session["created"]).isoformat() + "Z",
            "email": session.get("customer_email", "unknown"),
            "status": "paid",
        }

        STORE.set(order_id, order)
        STORE.save()
        log.info("✅ Payment confirmed for order_id=%s", order_id)

        # Submit to Slant (never fail the webhook)
        if slant_enabled():
            try:
                log.info("➡️ Submitting to Slant: order_id=%s", order_id)
                submit_paid_order_to_slant(order_id)
            except Exception as e:
                log.exception("❌ Slant submit exception: %s", e)
                order = STORE.get(order_id) or order
                order["slant_error"] = str(e)
                order["status"] = "slant_failed"
                STORE.set(order_id, order)
                STORE.save()
        else:
            log.info("ℹ️ Slant disabled (no SLANT_API_KEY). Skipping submit.")

    return jsonify(success=True)


@app.route("/order-data/<order_id>", methods=["GET"])
def get_order_data(order_id):
    data = STORE.get(order_id)
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
# Debug helpers
# -----------------------------
@app.route("/debug/slant/config", methods=["GET"])
def debug_slant_config():
    return jsonify({
        "slant_base_url": SLANT_BASE_URL,
        "filaments_endpoint": SLANT_FILAMENTS_ENDPOINT,
        "files_endpoint": SLANT_FILES_ENDPOINT,
        "orders_endpoint": SLANT_ORDERS_ENDPOINT,
        "has_api_key": bool(SLANT_API_KEY),
        "has_platform_id": bool(SLANT_PLATFORM_ID),
    })


@app.route("/debug/slant/filaments", methods=["GET"])
def debug_slant_filaments():
    try:
        client = get_slant_client()
        filaments = client.get_filaments()
        return jsonify({"ok": True, "count": len(filaments), "data": filaments})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/debug/slant/upload/<job_id>", methods=["POST"])
def debug_slant_upload(job_id):
    try:
        stl_path = os.path.join(UPLOAD_DIR, f"{job_id}.stl")
        client = get_slant_client()
        pfsid = client.upload_stl(job_id, stl_path)
        return jsonify({"ok": True, "job_id": job_id, "publicFileServiceId": pfsid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/debug/slant/submit/<order_id>", methods=["POST"])
def debug_slant_submit(order_id):
    try:
        submit_paid_order_to_slant(order_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
