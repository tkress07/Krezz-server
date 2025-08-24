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

# --- Blender Scene Reset ---
bpy.ops.wm.read_factory_settings(use_empty=True)

# --- Constants ---
hole_indices = [927, 1004]
hole_radius = 0.0015875
hole_depth = 0.01
extrude_depth = 0.008
smooth_passes = 3
arc_steps = 24
ring_count = arc_steps + 1

# --- Smoothing ---
def smooth(points, passes):
    for _ in range(passes):
        new = []
        for i in range(len(points)):
            if i == 0 or i == len(points) - 1:
                new.append(points[i])
            else:
                x = (points[i-1][0] + points[i][0] + points[i+1][0]) / 3
                y = (points[i-1][1] + points[i][1] + points[i+1][1]) / 3
                z = (points[i-1][2] + points[i][2] + points[i+1][2]) / 3
                new.append((x, y, z))
        points = new
    return points

beardline = smooth(beardline, smooth_passes)
if neckline:
    neckline = smooth(neckline, smooth_passes)

# --- Mustache Bezier Arc ---
def bezier_curve(p0, p1, p2, steps=40):
    return [
        (
            (1 - t)**2 * p0[0] + 2*(1 - t)*t*p1[0] + t**2*p2[0],
            (1 - t)**2 * p0[1] + 2*(1 - t)*t*p1[1] + t**2*p2[1],
            (1 - t)**2 * p0[2] + 2*(1 - t)*t*p1[2] + t**2*p2[2],
        )
        for t in [i / steps for i in range(steps + 1)]
    ]

mustache_start = beardline[0]
mustache_end = beardline[-1]
mustache_control = (
    (mustache_start[0] + mustache_end[0]) / 2,
    (mustache_start[1] + mustache_end[1]) / 2 + 0.005,
    (mustache_start[2] + mustache_end[2]) / 2,
)
mustache_curve = bezier_curve(mustache_start, mustache_control, mustache_end)

if not any(p[1] > mustache_control[1] for p in beardline):
    beardline += mustache_curve

# --- Lip Arc Geometry ---
min_x, max_x = min(p[0] for p in beardline), max(p[0] for p in beardline)
center_x = (min_x + max_x) / 2

def tapered_radius(x):
    taper = max(0.0, 1.0 - abs(x - center_x) * 25)
    return 0.003 + taper * (0.008 - 0.003)

base_points = [
    min(beardline, key=lambda p: abs(p[0] - (min_x + (max_x - min_x) * i / 99)))
    for i in range(100)
]

lip_vertices = []
for base in base_points:
    r = tapered_radius(base[0])
    for j in range(ring_count):
        angle = math.pi * j / arc_steps
        y = base[1] - r * (1 - math.sin(angle))
        z = base[2] + r * math.cos(angle)
        lip_vertices.append((base[0], y, z))

# --- Combine Geometry ---
verts = beardline + lip_vertices
faces = []

for i in range(len(base_points) - 1):
    for j in range(arc_steps):
        a = len(beardline) + i * ring_count + j
        b = a + 1
        c = a + ring_count
        d = c + 1
        faces.append([a, c, b])
        faces.append([b, c, d])

# --- Neckline Bridging ---
def find_closest(point, candidates):
    return min(candidates, key=lambda c: sum((c[k] - point[k])**2 for k in range(3)))

if neckline:
    offset = len(verts)
    verts.extend(neckline)
    for i in range(len(beardline) - 1):
        b0 = beardline[i]
        b1 = beardline[i + 1]
        n0 = find_closest(b0, neckline)
        n1 = find_closest(b1, neckline)

        b0_idx = i
        b1_idx = i + 1
        n0_idx = offset + neckline.index(n0)
        n1_idx = offset + neckline.index(n1)

        faces.append([b0_idx, n0_idx, b1_idx])
        faces.append([n0_idx, n1_idx, b1_idx])

# --- Mesh Creation ---
if not verts or not faces:
    print("⚠️ Empty mesh — creating fallback cube")
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
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.transform.translate(value=(0, 0, -extrude_depth))
bpy.ops.object.mode_set(mode='OBJECT')

# --- Hole Cylinders + Boolean ---
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
        cutter_objs.append(cutter)

for cutter in cutter_objs:
    mod = obj.modifiers.new(name=f"bool_{cutter.name}", type='BOOLEAN')
    mod.object = cutter
    mod.operation = 'DIFFERENCE'
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)
    cutter.select_set(True)
    bpy.ops.object.delete()

# --- Final Export ---
bpy.ops.object.select_all(action='DESELECT')
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.export_mesh.stl(filepath=output_path, use_selection=True)

print(f"✅ STL exported: {output_path} (Overlay: {overlay_name}, Job: {job_id})")
