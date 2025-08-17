import subprocess
import tempfile
import uuid
import os
import json
from flask import Flask, request, send_file, jsonify

app = Flask(__name__)

@app.route("/generate-stl", methods=["POST"])
def generate_stl():
    try:
        data = request.get_json()
        vertices = data.get("vertices", [])

        if not vertices:
            return jsonify({"error": "No vertices provided"}), 400

        # Create temp files
        temp_id = uuid.uuid4().hex[:8]
        input_path = f"/tmp/vertices_{temp_id}.json"
        output_path = f"/tmp/mold_{temp_id}.stl"

        # Write vertices to JSON
        with open(input_path, "w") as f:
            json.dump(vertices, f)

        # Run Blender CLI script
        blender_cmd = [
            "blender", "--background", "--python", "generate_stl.py", "--",
            input_path, output_path
        ]
        subprocess.run(blender_cmd, check=True)

        if not os.path.exists(output_path):
            return jsonify({"error": "STL generation failed"}), 500

        # Return STL file
        return send_file(output_path, mimetype="application/octet-stream", as_attachment=True, download_name="mold.stl")

    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"Blender failed: {e}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
