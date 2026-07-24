from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from flask import Flask, after_this_request, jsonify, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024

JOBS_DIR = Path(os.environ.get("JOBS_DIR", "/data/jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)

BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
APP_DIR = Path(__file__).resolve().parent
GENERATE_SCRIPT = APP_DIR / "generate_stl.py"
REPAIR_SCRIPT = APP_DIR / "repair_stl.py"


def run_blender(script: Path, args: list[str], timeout: int = 240) -> subprocess.CompletedProcess[str]:
    command = [
        BLENDER_BIN,
        "--background",
        "--python",
        str(script),
        "--",
        *args,
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@app.get("/")
def health():
    return jsonify(
        ok=True,
        service="krezzcut-blender",
        repair_endpoint="/repair-stl",
        jobs_dir=str(JOBS_DIR),
    )


# Keeps the existing JSON endpoint working for older app builds.
@app.post("/generate-stl")
def generate_stl():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="Expected a JSON object."), 400

    job_id = str(payload.get("job_id") or payload.get("jobID") or uuid.uuid4())
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    input_path = job_dir / "input.json"
    output_path = job_dir / f"{job_id}.stl"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        result = run_blender(
            GENERATE_SCRIPT,
            [str(input_path), str(output_path)],
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error="Blender timed out while generating the mold."), 504

    if result.returncode != 0 or not output_path.exists():
        message = (result.stderr or result.stdout or "Blender generation failed.")[-6000:]
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=message), 500

    @after_this_request
    def cleanup(response):
        shutil.rmtree(job_dir, ignore_errors=True)
        return response

    return send_file(
        output_path,
        mimetype="application/sla",
        as_attachment=True,
        download_name=f"mold_{job_id}.stl",
    )


# New endpoint used by ViewController_ManifoldServer.swift.
@app.post("/repair-stl")
def repair_stl():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify(error="Missing STL file field named 'file'."), 400

    job_id = str(request.form.get("job_id") or uuid.uuid4())
    params_raw = request.form.get("params") or "{}"

    try:
        params = json.loads(params_raw)
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        return jsonify(error=f"Invalid params: {exc}"), 400

    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    input_path = job_dir / "raw.stl"
    output_path = job_dir / f"{job_id}.stl"
    params_path = job_dir / "repair_params.json"

    uploaded.save(input_path)
    params_path.write_text(json.dumps(params), encoding="utf-8")

    try:
        result = run_blender(
            REPAIR_SCRIPT,
            [str(input_path), str(output_path), str(params_path)],
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error="Blender timed out while fusing the mold."), 504

    if result.returncode != 0 or not output_path.exists():
        message = (result.stderr or result.stdout or "Blender repair failed.")[-6000:]
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=message), 500

    @after_this_request
    def cleanup(response):
        shutil.rmtree(job_dir, ignore_errors=True)
        return response

    return send_file(
        output_path,
        mimetype="application/sla",
        as_attachment=True,
        download_name=f"mold_{job_id}.stl",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
