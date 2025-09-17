# path: app.py
# Flask API to run Blender with beard_mold_fix and accept shaping params.

import subprocess
from flask import Flask, jsonify, request, send_file
import tempfile
import uuid
import os
import json

app = Flask(__name__)

# Why: keep one source of truth for defaults so older callers work.
DEFAULT_PARAMS = {
    "profileBias": 1.5,
    "prelift": 0.0002,
    "enableAnchorRibs": True,
    "ribSpacing": 0.006,
    "ribThickness": 0.0006,
    "ribDepth": 0.0012,
    "ribZOffset": 0.0004,
    "ribBandY": 0.004,
    # Legacy knobs
    "lipSegments": 120,
    "arcSteps": 28,
    "voxelRemesh": 0.0006,
    "maxLipRadius": 0.008,
    "minLipRadius": 0.003,
    "taperMult": 25.0,
    "extrusionDepth": -0.008,
}

BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
# Why: point to our new generator with ribs/steeper profile.
BLENDER_SCRIPT = os.environ.get("BLENDER_SCRIPT", "tools/beard_mold_fix.py")
BLENDER_TIMEOUT = int(os.environ.get("BLENDER_TIMEOUT", "180"))


@app.route("/")
def health():
    return "OK", 200


@app.route("/blender-version")
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
        print("🛬 Received JSON:", src)

        # Inputs (support both 'beardline' and legacy 'vertices').
        beardline = src.get("beardline") or src.get("vertices") or []
        neckline = src.get("neckline", [])
        hole_centers = src.get("holeCenters") or src.get("holes") or []
        overlay = src.get("overlay", "default")
        job_id = src.get("job_id") or src.get("jobID") or uuid.uuid4().hex[:8]

        if not beardline:
            return jsonify({"error": "Missing 'beardline' or 'vertices'"}), 400

        # Merge shaping params.
        params = {**DEFAULT_PARAMS, **(src.get("params") or {})}

        # Write full payload Blender expects.
        temp_id = uuid.uuid4().hex[:8]
        input_path = f"/tmp/input_{temp_id}.json"
        output_path = f"/tmp/output_{temp_id}.stl"

        with open(input_path, "w") as f:
            json.dump(
                {
                    "beardline": beardline,
                    "neckline": neckline,
                    "holeCenters": hole_centers,
                    "overlay": overlay,
                    "jobID": job_id,   # keep both keys for downstream logs/tools
                    "job_id": job_id,
                    "params": params,
                },
                f,
                separators=(",", ":"),
            )

        print(f"📦 Calling Blender: {BLENDER_BIN} -b -P {BLENDER_SCRIPT} -- {input_path} {output_path}")

        result = subprocess.run(
            [BLENDER_BIN, "-b", "-P", BLENDER_SCRIPT, "--", input_path, output_path],
            capture_output=True,
            text=True,
            timeout=BLENDER_TIMEOUT,
        )

        print("✅ Blender STDOUT:
", result.stdout)
        print("⚠️ Blender STDERR:
", result.stderr)

        if result.returncode != 0:
            return (
                jsonify(
                    {
                        "error": "Blender failed",
                        "stderr": result.stderr,
                        "stdout": result.stdout,
                        "script": BLENDER_SCRIPT,
                    }
                ),
                500,
            )

        if not os.path.exists(output_path):
            return jsonify({"error": "STL not created", "stderr": result.stderr}), 500

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
    app.run(host="0.0.0.0", port=8000, debug=True)
