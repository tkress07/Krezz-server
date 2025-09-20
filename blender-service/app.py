# path: app.py
# -*- coding: utf-8 -*-

import os
import re
import io
import json
import uuid
import tempfile
import subprocess
from typing import Any, Dict, Optional, Tuple, List

from flask import Flask, jsonify, request, send_file, after_this_request
from flask_cors import CORS

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)  # Enable for all origins; adjust if you need stricter CORS

# -----------------------------------------------------------------------------
# Environment / defaults
# -----------------------------------------------------------------------------
BLENDER_BIN = os.getenv("BLENDER_BIN", "blender")
BLENDER_SCRIPT = os.getenv("BLENDER_SCRIPT", "/app/tools/beard_mold_fix.py")
BLENDER_TIMEOUT = int(os.getenv("BLENDER_TIMEOUT", "180"))  # seconds

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def bool_query(name: str) -> bool:
    v = request.args.get(name)
    if v is None:
        return False
    return v.lower() in ("1", "true", "yes", "y", "on")

def tail(text: str, n: int = 120) -> str:
    # keep last n lines for easier debugging
    lines = text.splitlines()
    return "\n".join(lines[-n:])

def parse_stats(stdout: str) -> Dict[str, Any]:
    """
    Pulls a few useful bits from your Blender script's logs, e.g.:
      "STL export complete | scale=1000.0 | jobID=e34e1372 overlay=default ... dim_mm=[151.25, 98.13, 98.55]"
    Returns whatever we can safely parse; missing fields are omitted.
    """
    out: Dict[str, Any] = {}
    try:
        m_scale = re.search(r"scale\s*=\s*([0-9.]+)", stdout)
        if m_scale:
            out["scale"] = float(m_scale.group(1))
        m_job = re.search(r"jobID\s*=\s*([A-Za-z0-9_-]+)", stdout)
        if m_job:
            out["jobID"] = m_job.group(1)
        m_overlay = re.search(r"overlay\s*=\s*([^\s|]+)", stdout)
        if m_overlay:
            out["overlay"] = m_overlay.group(1)
        m_dims = re.search(r"dim_mm\s*=\s*\[([0-9eE+.,\s-]+)\]", stdout)
        if m_dims:
            dims = [float(x.strip()) for x in m_dims.group(1).split(",")]
            out["dim_mm"] = dims
        m_counts = re.search(
            r"verts\(beardline\)\s*=\s*(\d+)\s+neckline\s*=\s*(\d+)\s+holes\s*=\s*(\d+)",
            stdout,
        )
        if m_counts:
            out["counts"] = {
                "beardline": int(m_counts.group(1)),
                "neckline": int(m_counts.group(2)),
                "holes": int(m_counts.group(3)),
            }
    except Exception:
        pass
    return out

def pick_beardline(payload: Dict[str, Any]) -> Optional[List[Dict[str, float]]]:
    # accept either "beardline" (preferred) or legacy "vertices"
    bl = payload.get("beardline")
    if bl is None:
        bl = payload.get("vertices")
    return bl

def save_payload_to_tmp(payload: Dict[str, Any], tmpdir: str) -> str:
    in_path = os.path.join(tmpdir, "input.json")
    # normalize keys: your generator already handles both beardline/vertices
    with open(in_path, "w") as f:
        json.dump(payload, f)
    return in_path

def run_blender(input_path: str, output_path: str) -> Tuple[int, str, str]:
    """
    Launch Blender headless with your script and the two file args.
    Returns (returncode, stdout, stderr)
    """
    if not os.path.exists(BLENDER_SCRIPT):
        return (
            127,
            "",
            f"Blender script not found at {BLENDER_SCRIPT}. Ensure it is copied into the image.",
        )
    cmd = [
        BLENDER_BIN, "-b", "-P", BLENDER_SCRIPT, "--",
        input_path, output_path
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=BLENDER_TIMEOUT,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", f"Timeout after {BLENDER_TIMEOUT}s\n{e.stderr or ''}"
    except Exception as e:
        return 125, "", f"{type(e).__name__}: {e}"

def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except Exception:
        return 0

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/")
def health() -> Tuple[str, int]:
    return "OK", 200

@app.get("/blender-version")
def blender_version():
    try:
        out = subprocess.check_output([BLENDER_BIN, "-v"], text=True).strip()
        return jsonify({"blender_version": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/generate-stl")
def generate_stl():
    debug = bool_query("debug")

    # 1) Parse input JSON
    try:
        data = request.get_json(force=True, silent=False)  # better errors
    except Exception as e:
        return jsonify({"error": f"Invalid JSON: {e}"}), 400

    # Validate required geometry
    beardline = pick_beardline(data or {})
    if not beardline:
        return jsonify({"error": "Missing 'beardline' (or legacy 'vertices')"}), 400

    # Optional extras
    neckline = data.get("neckline", [])
    holes = data.get("holeCenters") or data.get("holes") or []
    params = data.get("params", {})
    overlay = data.get("overlay", "default")
    job_id = data.get("job_id") or data.get("jobID") or uuid.uuid4().hex[:8]

    # 2) Compose payload for Blender exactly as your script expects
    blender_payload = {
        "beardline": beardline,
        "neckline": neckline,
        "holeCenters": holes,
        "params": params,
        "overlay": overlay,
        "jobID": job_id,
    }

    # 3) Temp workspace
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = save_payload_to_tmp(blender_payload, tmpdir)
        out_path = os.path.join(tmpdir, "mold.stl")

        # 4) Run Blender
        rc, stdout, stderr = run_blender(in_path, out_path)

        # 5) Handle failures
        if rc != 0 or not os.path.exists(out_path) or file_size(out_path) == 0:
            return jsonify({
                "error": "Blender failed to produce STL",
                "return_code": rc,
                "stdout": tail(stdout),
                "stderr": tail(stderr),
                "hint": "Check BLENDER_SCRIPT path and that the script prints 'STL export complete'."
            }), 500

        # 6) Stats for debug / UI
        stats = parse_stats(stdout)
        stats["size_bytes"] = file_size(out_path)

        if debug:
            # JSON-only mode (no file transfer)
            return jsonify({
                "ok": True,
                "jobID": job_id,
                "overlay": overlay,
                "stats": stats,
                "stdout_tail": tail(stdout),
                "stderr_tail": tail(stderr),
            })

        # 7) Stream STL to client; schedule cleanup after response
        @after_this_request
        def _cleanup(response):
            # tempdir will auto-clean; nothing to do
            return response

        # Serve as STL; many viewers recognize application/sla or model/stl
        return send_file(
            out_path,
            mimetype="application/sla",
            as_attachment=True,
            download_name=f"mold_{job_id}.stl",
            max_age=0,
            etag=False,
            conditional=False,
        )

# -----------------------------------------------------------------------------
# Entrypoint for local runs (Render uses Gunicorn via Docker CMD)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
