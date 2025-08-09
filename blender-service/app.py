# Krezz-server/blender-service/app.py
import subprocess
from flask import Flask, jsonify

app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

@app.get("/blender-version")
def blender_version():
    try:
        p = subprocess.run(
            ["blender", "-v"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30
        )
        return jsonify({"ok": p.returncode == 0, "output": p.stdout})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
