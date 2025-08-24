
import json
import math
import os
import tempfile
import uuid
from stl import mesh
import numpy as np
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

def generate_lip_arc(p1, p2, radius=0.01, smoothness=10):
    arc_points = []
    mid = (np.array(p1) + np.array(p2)) / 2
    direction = np.array(p2) - np.array(p1)
    perpendicular = np.cross(direction, [0, 1, 0])
    perpendicular = perpendicular / np.linalg.norm(perpendicular)
    for i in range(smoothness + 1):
        angle = math.pi * (i / smoothness)
        offset = radius * math.sin(angle)
        interp = np.array(p1) + direction * (i / smoothness)
        arc_point = interp + perpendicular * offset
        arc_points.append(arc_point.tolist())
    return arc_points

def make_face_strip(top_points, bottom_points):
    faces = []
    for i in range(len(top_points) - 1):
        t0, t1 = top_points[i], top_points[i + 1]
        b0, b1 = bottom_points[i], bottom_points[i + 1]
        faces.append([t0, b0, b1])
        faces.append([t0, b1, t1])
    return faces

def extrude(points, height=0.005):
    top = [(x, y + height, z) for (x, y, z) in points]
    return top

def insert_arc(beardline, shared_connections, radius=0.01, smoothness=10):
    if not shared_connections or len(shared_connections) < 2:
        raise ValueError("sharedConnections must include two indices")
    start_idx, end_idx = shared_connections[0], shared_connections[1]
    arc = generate_lip_arc(beardline[start_idx], beardline[end_idx], radius, smoothness)
    return beardline[:start_idx+1] + arc + beardline[end_idx:]

@app.route("/generate-stl", methods=["POST"])
def generate_stl():
    try:
        data = request.get_json()
        vertices = data.get("vertices", [])
        beardline_indices = data.get("beardline", [])
        shared_connections = data.get("sharedConnections", [])
        hole_indices = data.get("holeIndices", [])

        if not vertices or not beardline_indices:
            return jsonify({"error": "Missing vertices or beardline"}), 400

        # Build base path
        beardline = [tuple(vertices[i].values()) for i in beardline_indices]
        if shared_connections and len(shared_connections) >= 2:
            beardline = insert_arc(beardline, shared_connections)

        top = extrude(beardline)
        faces = make_face_strip(top, beardline)

        # Optional: add holes
        hole_radius = 0.002
        for i in hole_indices:
            if 0 <= i < len(vertices):
                center = np.array(tuple(vertices[i].values()))
                num_segments = 8
                for j in range(num_segments):
                    theta1 = 2 * math.pi * j / num_segments
                    theta2 = 2 * math.pi * (j + 1) / num_segments
                    p1 = center + hole_radius * np.array([math.cos(theta1), 0, math.sin(theta1)])
                    p2 = center + hole_radius * np.array([math.cos(theta2), 0, math.sin(theta2)])
                    faces.append([center.tolist(), p1.tolist(), p2.tolist()])

        all_faces = np.array(faces)
        flat_faces = all_faces.reshape(-1, 3, 3)
        mold_mesh = mesh.Mesh(np.zeros(flat_faces.shape[0], dtype=mesh.Mesh.dtype))
        for i, f in enumerate(flat_faces):
            mold_mesh.vectors[i] = f

        file_id = str(uuid.uuid4())
        temp_dir = tempfile.gettempdir()
        file_path = os.path.join(temp_dir, f"{file_id}.stl")
        mold_mesh.save(file_path)

        return jsonify({"fileId": file_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
