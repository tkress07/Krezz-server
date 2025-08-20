import subprocess
from flask import Flask, jsonify, request, send_file
import tempfile
import uuid
import os
import json

app = Flask(__name__)

# ✅ Health check for Render
@app.route("/")
def health():
    return "OK", 200

@app.route("/blender-version")
def blender_version():
    try:
        out = subprocess.check_output(["blender", "-v"], text=True).strip()
        return jsonify({"blender_version": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/generate-stl", methods=["POST"])
def generate_stl():
    try:
        data = request.get_json()
        vertices = data.get("vertices", [])
        neckline = data.get("neckline", [])

        if not vertices:
            return jsonify({"error": "No vertices provided"}), 400

        temp_id = uuid.uuid4().hex[:8]
        input_path = f"/tmp/input_{temp_id}.json"
        output_path = f"/tmp/output_{temp_id}.stl"

        with open(input_path, "w") as f:
            json.dump({"vertices": vertices, "neckline": neckline}, f)

        subprocess.run([
            "blender", "--background", "--python", "generate_stl.py", "--",
            input_path, output_path
        ], check=True)

        if not os.path.exists(output_path):
            return jsonify({"error": "STL generation failed"}), 500

        return send_file(output_path, mimetype="application/octet-stream", as_attachment=True, download_name="mold.stl")

    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"Blender failed: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
