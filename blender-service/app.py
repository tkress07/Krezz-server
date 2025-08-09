import subprocess
from flask import Flask, jsonify

app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

@app.get("/blender-version")
def blender_version():
    try:
        out = subprocess.check_output(["blender", "-v"], text=True).strip()
        return jsonify({"blender_version": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
