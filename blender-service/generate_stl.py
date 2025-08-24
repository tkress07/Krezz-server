# Rewritten generate_stl.py with enhanced shared connection bridging and better mold closure

import bpy
import sys
import json
import math

# --- CLI args ---
argv = sys.argv
argv = argv[argv.index("--") + 1:]
input_path = argv[0]
output_path = argv[1]

# --- Load JSON payload ---
with open(input_path, "r") as f:
    payload = json.load(f)

beardline = payload.get("vertices", [])
neckline = payload.get("neckline", [])
overlay_name = payload.get("overlay", "unknown")
job_id = payload.get("job_id", "mold")

if not beardline:
    raise Exception("No beardline provided")

beardline = [(v["x"], v["y"], v["z"]) for v in beardline]
neckline = [(v["x"], v["y"], v["z"]) for v in neckline] if neckline else []

bpy.ops.wm.read_factory_settings(use_empty=True)

hole_indices = [927, 1004]
hole_radius = 0.0015875
hole_depth = 0.01
extrude_depth = 0.008
smooth_passes = 3
arc_steps = 24
ring_count = arc_steps + 1

# --- Smooth helper ---
def smooth(verts, passes):
    for _ in range(passes):
        new = []
        for i in range(len(verts)):
            if i == 0 or i == len(verts) - 1:
                new.append(verts[i])
            else:
                x = (verts[i-1][0] + verts[i][0] + verts[i+1][0]) / 3
                y = (verts[i-1][1] + verts[i][1] + verts[i+1][1]) / 3
                z = (verts[i-1][2] + verts[i][2] + verts[i+1][2]) / 3
                new.append((x, y, z))
        verts = new
    return verts

beardline = smooth(beardline, smooth_passes)
if neckline:
    neckline = smooth(neckline, smooth_passes)

# --- Combine with extrusion ---
verts = beardline[:]
faces = []

if neckline:
    offset = len(verts)
    verts.extend(neckline)
    for i in range(len(beardline) - 1):
        bi0 = i
        bi1 = i + 1
        ni0 = offset + i if i < len(neckline) else offset + len(neckline) - 1
        ni1 = offset + i + 1 if i + 1 < len(neckline) else offset + len(neckline) - 1
        faces.append([bi0, ni0, bi1])
        faces.append([bi1, ni0, ni1])
else:
    # Fake bottom ring if neckline is missing
    bottom_ring = [(x, y, z - extrude_depth) for (x, y, z) in beardline]
    offset = len(verts)
    verts.extend(bottom_ring)
    for i in range(len(beardline) - 1):
        a, b = i, i + 1
        c, d = offset + i, offset + i + 1
        faces.append([a, b, c])
        faces.append([b, d, c])

# Cap the end if open
if len(beardline) >= 3:
    center = tuple(sum(c[i] for c in beardline) / len(beardline) for i in range(3))
    center_idx = len(verts)
    verts.append(center)
    for i in range(len(beardline) - 1):
        faces.append([i, i + 1, center_idx])

mesh = bpy.data.meshes.new("Mold")
obj = bpy.data.objects.new("MoldObject", mesh)
bpy.context.collection.objects.link(obj)
mesh.from_pydata(verts, [], faces)
mesh.update()

# Extrude
bpy.context.view_layer.objects.active = obj
bpy.ops.object.select_all(action='DESELECT')
obj.select_set(True)
bpy.ops.object.convert(target='MESH')
bpy.ops.object.editmode_toggle()
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0, 0, -extrude_depth)})
bpy.ops.object.editmode_toggle()

# Export
bpy.ops.object.select_all(action='SELECT')
bpy.context.view_layer.objects.active = obj
bpy.ops.object.join()
bpy.ops.export_mesh.stl(filepath=output_path, use_selection=True)
print(f"\u2705 STL file saved for overlay: {overlay_name}, job ID: {job_id}")
