# file: app.py
import os, json, tempfile, subprocess, uuid, sys
from flask import Flask, request, send_file, abort, Response

BLENDER = os.environ.get("BLENDER_BIN", "/usr/bin/blender")  # path to blender
SCRIPT  = os.environ.get("BLENDER_SCRIPT", "/app/blender_mold_maker.py")

app = Flask(__name__)

def run_blender(input_json_path: str, output_stl_path: str) -> None:
    """
    Launch Blender headless to build STL. All logs go to server logs; never to response.
    """
    cmd = [
        BLENDER, "-b", "--python", SCRIPT, "--",
        input_json_path, output_stl_path
    ]
    # Capture logs; DO NOT forward to client
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    # Log to server (stdout/stderr). Keep responses binary-only.
    sys.stdout.write(proc.stdout.decode("utf-8", errors="ignore"))
    sys.stderr.write(proc.stderr.decode("utf-8", errors="ignore"))
    if proc.returncode != 0 or not os.path.exists(output_stl_path) or os.path.getsize(output_stl_path) == 0:
        raise RuntimeError(f"Blender failed (code={proc.returncode}).")

@app.post("/generate-stl")
def generate_stl():
    # Parse JSON
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return abort(400, "Invalid JSON")

    # Optional: normalize keys for logging/compat only
    job_id = (payload.get("job_id") or payload.get("jobID") or str(uuid.uuid4())[:8])

    with tempfile.TemporaryDirectory() as td:
        in_path  = os.path.join(td, f"input_{job_id}.json")
        out_path = os.path.join(td, f"output_{job_id}.stl")

        # Write exact JSON we received (no reformatting that could reorder keys)
        with open(in_path, "w") as f:
            json.dump(payload, f, separators=(",", ":"))  # compact

        # Run Blender headless
        try:
            run_blender(in_path, out_path)
        except Exception as e:
            return abort(500, f"Blender error: {e}")

        # Stream STL with correct headers
        return send_file(
            out_path,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=f"mold_{job_id}.stl",
            max_age=0
        )

@app.get("/healthz")
def health():
    return Response("ok", mimetype="text/plain")

if __name__ == "__main__":
    # For local testing only; in prod run via gunicorn/uvicorn.
    app.run(host="0.0.0.0", port=8000)
