# file: app.py
import os, json, tempfile, subprocess, uuid, sys
from flask import Flask, request, send_file, abort, Response

BLENDER = os.environ.get("BLENDER_BIN", "/usr/bin/blender")
SCRIPT  = os.environ.get("BLENDER_SCRIPT", "/app/blender_mold_maker.py")
TIMEOUT_SEC = int(os.environ.get("BLENDER_TIMEOUT", "300"))

app = Flask(__name__)

@app.get("/")
def root():
    # Render's default health probe usually hits "/"
    return Response("ok", mimetype="text/plain")

@app.get("/healthz")
def healthz():
    return Response("ok", mimetype="text/plain")

def run_blender(input_json_path: str, output_stl_path: str) -> None:
    # why: capture logs to server, never to client; enforce timeout so worker won't hang
    cmd = [BLENDER, "-b", "--python", SCRIPT, "--", input_json_path, output_stl_path]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=TIMEOUT_SEC,
    )
    # Log to server only
    sys.stdout.write(proc.stdout.decode("utf-8", errors="ignore"))
    sys.stderr.write(proc.stderr.decode("utf-8", errors="ignore"))
    if proc.returncode != 0:
        raise RuntimeError(f"Blender exit code={proc.returncode}")
    if not os.path.exists(output_stl_path) or os.path.getsize(output_stl_path) == 0:
        raise RuntimeError("Blender produced no STL")

@app.post("/generate-stl")
def generate_stl():
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return abort(400, "Invalid JSON")

    job_id = (payload.get("job_id") or payload.get("jobID") or str(uuid.uuid4())[:8])

    with tempfile.TemporaryDirectory() as td:
        in_path  = os.path.join(td, f"input_{job_id}.json")
        out_path = os.path.join(td, f"output_{job_id}.stl")

        # Compact JSON to reduce IO; no reordering side effects for our use
        with open(in_path, "w") as f:
            json.dump(payload, f, separators=(",", ":"))

        try:
            run_blender(in_path, out_path)
        except subprocess.TimeoutExpired:
            return abort(504, "Blender timed out")
        except Exception as e:
            return abort(500, f"Blender error: {e}")

        return send_file(
            out_path,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=f"mold_{job_id}.stl",
            max_age=0,
        )

# Local dev only: on Render use Gunicorn (see Procfile)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
