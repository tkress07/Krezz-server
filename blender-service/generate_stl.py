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

# --- Scene Setup ---
bpy.ops.wm.read_factory_settings(use_empty=True)

hole_indices = [927, 1004]
hole_radius = 0.0015875
hole_depth = 0.01
extrude_depth = 0.008
smooth_passes = 3
arc_steps = 24
ring_count = arc_steps + 1

# --- Helper: Smooth Vertices ---
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

# --- Mustache fallback (optional) ---
def bezier_curve(p0, p1, p2, steps=40):
    return [( (1 - t)**2 * p0[0] + 2*(1 - t)*t*p1[0] + t**2*p2[0],
              (1 - t)**2 * p0[1] + 2*(1 - t)*t*p1[1] + t**2*p2[1],
              (1 - t)**2 * p0[2] + 2*(1 - t)*t*p1[2] + t**2*p2[2] )
            for t in [i / steps for i in range(steps + 1)]]

mustache_start = (beardline[0][0], beardline[0][1], beardline[0][2])
mustache_end = (beardline[-1][0], beardline[-1][1], beardline[-1][2])
mustache_control = (
    (mustache_start[0] + mustache_end[0]) / 2,
    (mustache_start[1] + mustache_end[1]) / 2 + 0.005,
    (mustache_start[2] + mustache_end[2]) / 2
)
mustache_curve = bezier_curve(mustache_start, mustache_control, mustache_end)

# Only add if it's not already in beardline
if not any(p[1] > mustache_control[1] for p in beardline):
    beardline += mustache_curve

# --- Lip Arc ---
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

# --- Combine Verts and Faces ---
verts = beardline + lip_vertices
faces = []

for i in range(len(base_points) - 1):
    for j in range(arc_steps):
        a = len(beardline) + i * ring_count + j
        b = len(beardline) + i * ring_count + j + 1
        c = len(beardline) + (i + 1) * ring_count + j
        d = len(beardline) + (i + 1) * ring_count + j + 1
        faces.append([a, c, b])
        faces.append([b, c, d])

# --- Neckline bridging ---
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

# --- Create Blender Mesh ---
if len(faces) == 0 or len(verts) == 0:
    print("⚠️ No geometry — exporting fallback cube.")
    bpy.ops.mesh.primitive_cube_add(size=0.01, location=(0, 0, 0))
    bpy.ops.export_mesh.stl(filepath=output_path, use_selection=True)
    exit()

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

# --- Create and Boolean Hole Cylinders
cutter_objs = []

for idx in hole_indices:
    if idx < len(beardline):
        x, y, z = beardline[idx]
        bpy.ops.mesh.primitive_cylinder_add(
            radius=hole_radius,
            depth=hole_depth,
            location=(x, y, z - hole_depth / 2),
            rotation=(math.pi / 2, 0, 0)
        )
        cutter = bpy.context.object
        cutter.name = f"Hole_{idx}"
        cutter_objs.append(cutter)

# --- Apply Boolean Difference for each hole
bpy.context.view_layer.objects.active = obj
for cutter in cutter_objs:
    bool_mod = obj.modifiers.new(name=f"bool_{cutter.name}", type='BOOLEAN')
    bool_mod.object = cutter
    bool_mod.operation = 'DIFFERENCE'
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=bool_mod.name)
    cutter.select_set(True)
    bpy.ops.object.delete()

# --- Final Export
bpy.ops.object.select_all(action='SELECT')
bpy.context.view_layer.objects.active = obj
bpy.ops.object.join()
bpy.ops.export_mesh.stl(filepath=output_path, use_selection=True)

print(f"✅ STL file saved for overlay: {overlay_name}, job ID: {job_id}")
