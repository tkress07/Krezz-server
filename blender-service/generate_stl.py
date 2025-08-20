import bpy
import sys
import json
import math

# Parse CLI args
argv = sys.argv
argv = argv[argv.index("--") + 1:]
input_path = argv[0]
output_path = argv[1]

# Load input JSON
with open(input_path, "r") as f:
    payload = json.load(f)

beardline = payload.get("vertices", [])
neckline = payload.get("neckline", [])

if not beardline:
    raise Exception("No beardline provided")

beardline = [(v["x"], v["y"], v["z"]) for v in beardline]
neckline = [(v["x"], v["y"], v["z"]) for v in neckline] if neckline else []

# Scene setup
bpy.ops.wm.read_factory_settings(use_empty=True)

hole_indices = [927, 1004]
hole_radius = 0.0015875
hole_depth = 0.01
extrude_depth = 0.008
smooth_passes = 3
arc_steps = 24
ring_count = arc_steps + 1

# --- Smooth beardline ---
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

# --- Lip arc ---
min_x = min(p[0] for p in beardline)
max_x = max(p[0] for p in beardline)
center_x = (min_x + max_x) / 2

def tapered_radius(x):
    taper = max(0.0, 1.0 - abs(x - center_x) * 25)
    return 0.003 + taper * (0.008 - 0.003)

base_points = []
for i in range(100):
    x = min_x + (max_x - min_x) * i / 99
    closest = min(beardline, key=lambda p: abs(p[0] - x))
    base_points.append(closest)

lip_vertices = []
for base in base_points:
    r = tapered_radius(base[0])
    for j in range(ring_count):
        angle = math.pi * j / arc_steps
        y = base[1] - r * (1 - math.sin(angle))
        z = base[2] + r * math.cos(angle)
        lip_vertices.append((base[0], y, z))

# --- Combine verts ---
verts = beardline + lip_vertices
faces = []

# --- Add lip faces ---
for i in range(len(base_points) - 1):
    for j in range(arc_steps):
        a = len(beardline) + i * ring_count + j
        b = len(beardline) + i * ring_count + j + 1
        c = len(beardline) + (i + 1) * ring_count + j
        d = len(beardline) + (i + 1) * ring_count + j + 1
        faces.append([a, c, b])
        faces.append([b, c, d])

# --- Face wrapping: beardline ↔ neckline ---
def find_closest(point, candidates):
    return min(candidates, key=lambda c: (c[0] - point[0])**2 + (c[1] - point[1])**2 + (c[2] - point[2])**2)

if neckline:
    neckline_offset = len(verts)
    verts.extend(neckline)

    for i in range(len(beardline) - 1):
        b0 = beardline[i]
        b1 = beardline[i + 1]
        n0 = find_closest(b0, neckline)
        n1 = find_closest(b1, neckline)

        b0_idx = i
        b1_idx = i + 1
        n0_idx = neckline_offset + neckline.index(n0)
        n1_idx = neckline_offset + neckline.index(n1)

        faces.append([b0_idx, n0_idx, b1_idx])
        faces.append([n0_idx, n1_idx, b1_idx])

# --- Create Blender mesh ---
mesh = bpy.data.meshes.new("Mold")
obj = bpy.data.objects.new("MoldObject", mesh)
bpy.context.collection.objects.link(obj)
mesh.from_pydata(verts, [], faces)
mesh.update()

# --- Extrude
bpy.context.view_layer.objects.active = obj
bpy.ops.object.select_all(action='DESELECT')
obj.select_set(True)
bpy.ops.object.convert(target='MESH')
bpy.ops.object.editmode_toggle()
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0, 0, -extrude_depth)})
bpy.ops.object.editmode_toggle()

# --- Hole cylinders
for idx in hole_indices:
    if idx < len(beardline):
        x, y, z = beardline[idx]
        bpy.ops.mesh.primitive_cylinder_add(
            radius=hole_radius,
            depth=hole_depth,
            location=(x, y, z - hole_depth / 2),
            rotation=(math.pi / 2, 0, 0)
        )

# ✅ Filter & join only valid mesh objects
mesh_objects = [
    obj for obj in bpy.context.scene.objects
    if obj.type == 'MESH' and len(obj.data.vertices) > 0
]

if not mesh_objects:
    print("⚠️ No valid mesh objects to join — skipping join/export")
    exit()

for o in bpy.context.selected_objects:
    o.select_set(False)
for o in mesh_objects:
    o.select_set(True)
bpy.context.view_layer.objects.active = mesh_objects[0]
bpy.ops.object.join()

# --- Export STL
bpy.ops.export_mesh.stl(filepath=output_path, use_selection=False)
