# Rewritten generate_stl.py
# Converts SCNVector3-based Swift logic to Blender-compatible Python

import bpy
import sys
import json
import math

# --- CLI Args ---
argv = sys.argv
argv = argv[argv.index("--") + 1:]
input_path = argv[0]
output_path = argv[1]

# --- Load JSON Payload ---
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

# --- Constants ---
arc_steps = 24
ring_count = arc_steps + 1
extrude_depth = -0.008
hole_indices = [927, 1004]
hole_radius = 0.0015875
hole_depth = 0.01

# --- Utils ---
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

def tapered_radius(x, min_x, max_x):
    center_x = (min_x + max_x) / 2
    taper = max(0.0, 1.0 - abs(x - center_x) * 25)
    return 0.003 + taper * (0.008 - 0.003)

# --- Base Points from Beardline ---
beardline = smooth(beardline, 3)
min_x = min(v[0] for v in beardline)
max_x = max(v[0] for v in beardline)

base_points = []
for i in range(100):
    x = min_x + (max_x - min_x) * i / 99
    closest = min(beardline, key=lambda p: abs(p[0] - x))
    base_points.append(closest)

# --- Lip Arc ---
lip_vertices = []
for base in base_points:
    r = tapered_radius(base[0], min_x, max_x)
    for j in range(ring_count):
        angle = math.pi * j / arc_steps
        y = base[1] - r * (1 - math.sin(angle))
        z = base[2] + r * math.cos(angle)
        lip_vertices.append((base[0], y, z))

# --- Combine Mesh Geometry ---
verts = beardline + lip_vertices + neckline
faces = []

# Lip arc faces
for i in range(len(base_points) - 1):
    for j in range(arc_steps):
        a = len(beardline) + i * ring_count + j
        b = a + 1
        c = a + ring_count
        d = c + 1
        faces.append([a, c, b])
        faces.append([b, c, d])

# Stitch neckline
if neckline:
    neckline = smooth(neckline, 3)
    offset = len(beardline) + len(lip_vertices)
    for i in range(len(beardline) - 1):
        b0 = beardline[i]
        b1 = beardline[i + 1]
        n0 = min(neckline, key=lambda n: sum((n[k] - b0[k])**2 for k in range(3)))
        n1 = min(neckline, key=lambda n: sum((n[k] - b1[k])**2 for k in range(3)))
        n0_idx = offset + neckline.index(n0)
        n1_idx = offset + neckline.index(n1)
        faces.append([i, n0_idx, i+1])
        faces.append([n0_idx, n1_idx, i+1])

# --- Blender Setup ---
bpy.ops.wm.read_factory_settings(use_empty=True)
mesh = bpy.data.meshes.new("Guard")
obj = bpy.data.objects.new("GuardObj", mesh)
bpy.context.collection.objects.link(obj)
mesh.from_pydata(verts, [], faces)
mesh.update()

# --- Extrude ---
bpy.context.view_layer.objects.active = obj
bpy.ops.object.select_all(action='DESELECT')
obj.select_set(True)
bpy.ops.object.convert(target='MESH')
bpy.ops.object.editmode_toggle()
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0, 0, extrude_depth)})
bpy.ops.object.editmode_toggle()

# --- Drill Holes ---
cutter_objs = []
for idx in hole_indices:
    if idx < len(beardline):
        x, y, z = beardline[idx]
        bpy.ops.mesh.primitive_cylinder_add(radius=hole_radius, depth=hole_depth, location=(x, y, z - hole_depth/2), rotation=(math.pi / 2, 0, 0))
        cutter = bpy.context.object
        cutter_objs.append(cutter)

bpy.context.view_layer.objects.active = obj
for cutter in cutter_objs:
    mod = obj.modifiers.new(name="bool", type='BOOLEAN')
    mod.object = cutter
    mod.operation = 'DIFFERENCE'
    bpy.ops.object.modifier_apply(modifier=mod.name)
    cutter.select_set(True)
    bpy.ops.object.delete()

# --- Export ---
bpy.ops.object.select_all(action='SELECT')
bpy.context.view_layer.objects.active = obj
bpy.ops.object.join()
bpy.ops.export_mesh.stl(filepath=output_path, use_selection=True)
print(f"✅ STL saved for overlay: {overlay_name}, job: {job_id}")
