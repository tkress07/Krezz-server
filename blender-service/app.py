# path: app.py
# Flask API to run Blender with beard_mold_fix and accept shaping params.

import os
import json
import uuid
import subprocess
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

# Why: default knobs so older callers keep working without sending new params.
DEFAULT_PARAMS = {
    "profileBias": 1.5,
    "prelift": 0.0002,
    "enableAnchorRibs": True,
    "ribSpacing": 0.006,
    "ribThickness": 0.0006,
    "ribDepth": 0.0012,
    "ribZOffset": 0.0004,
    "ribBandY": 0.004,
    # Legacy/geometry knobs
    "lipSegments": 120,
    "arcSteps": 28,
    "voxelRemesh": 0.0006,
    "maxLipRadius": 0.008,
    "minLipRadius": 0.003,
    "taperMult": 25.0,
    "extrusionDepth": -0.008,
}

# Configurable via env for Render/Docker
BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
# Use the updated generator with profile/ribs:
BLENDER_SCRIPT = os.environ.get("BLENDER_SCRIPT", "tools/beard_mold_fix.py")
BLENDER_TIMEOUT = int(os.environ.get("BLENDER_TIMEOUT", "180"))


@app.get("/")
def health():
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
    try:
        src = request.get_json(force=True, silent=False) or {}
        print("Received JSON:", src)

        # Inputs (support both 'beardline' and legacy 'vertices').
        beardline = src.get("beardline") or src.get("vertices") or []
        neckline = src.get("neckline", [])
        hole_centers = src.get("holeCenters") or src.get("holes") or []
        overlay = src.get("overlay", "default")
        job_id = src.get("job_id") or src.get("jobID") or uuid.uuid4().hex[:8]

        if not beardline:
            return jsonify({"error": "Missing 'beardline' or 'vertices'"}), 400

        # Merge shaping params (caller overrides defaults).
        params = {**DEFAULT_PARAMS, **(src.get("params") or {})}

        # Temp files
        temp_id = uuid.uuid4().hex[:8]
        input_path = f"/tmp/input_{temp_id}.json"
        output_path = f"/tmp/output_{temp_id}.stl"

        # Write the payload Blender expects.
        payload = {
            "beardline": beardline,
            "neckline": neckline,
            "holeCenters": hole_centers,
            "overlay": overlay,
            "jobID": job_id,   # keep both keys for downstream logs/tools
            "job_id": job_id,
            "params": params,
        }
        with open(input_path, "w") as f:
            json.dump(payload, f, separators=(",", ":"))

        cmd = [BLENDER_BIN, "-b", "-P", BLENDER_SCRIPT, "--", input_path, output_path]
        print("Calling Blender:", " ".join(cmd))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=BLENDER_TIMEOUT,
        )

        stdout = (result.stdout or "").rstrip()
        stderr = (result.stderr or "").rstrip()
        print("Blender STDOUT:\n" + stdout)
        print("Blender STDERR:\n" + stderr)

        if result.returncode != 0:
            return (
                jsonify(
                    {
                        "error": "Blender failed",
                        "stderr": stderr,
                        "stdout": stdout,
                        "script": BLENDER_SCRIPT,
                    }
                ),
                500,
            )

        if not os.path.exists(output_path):
            return jsonify({"error": "STL not created", "stderr": stderr}), 500

        return send_file(
            output_path,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name="mold.stl",
        )

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Blender timed out"}), 504
    except subprocess.CalledProcessError as e:
        return jsonify({"error": "Blender crashed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # For local dev; Render will use gunicorn: gunicorn app:app --bind 0.0.0.0:$PORT --timeout 180
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=True)
