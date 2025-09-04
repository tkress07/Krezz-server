import bpy
import bmesh
import json
import sys
import math
from mathutils import Vector

# ---------------------------
# Helpers (geometry & math)
# ---------------------------

def to_vec3(p):
    return (float(p['x']), float(p['y']), float(p['z']))

def dist2(a, b):
    dx = a[0]-b[0]; dy = a[1]-b[1]; dz = a[2]-b[2]
    return dx*dx + dy*dy + dz*dz

def smooth_vertices_open(vertices, passes=1):
    """Moving-average smoothing for an open polyline (preserve endpoints)."""
    if len(vertices) < 3 or passes <= 0:
        return vertices[:]
    V = vertices[:]
    for _ in range(passes):
        NV = [V[0]]
        for i in range(1, len(V)-1):
            px,py,pz = V[i-1]
            cx,cy,cz = V[i]
            nx,ny,nz = V[i+1]
            NV.append(((px+cx+nx)/3.0, (py+cy+ny)/3.0, (pz+cz+nz)/3.0))
        NV.append(V[-1])
        V = NV
    return V

def sample_base_points_along_x(beardline, lip_segments):
    xs = [p[0] for p in beardline]
    ys = [p[1] for p in beardline]
    zs = [p[2] for p in beardline]
    minX = min(xs) if xs else -0.05
    maxX = max(xs) if xs else  0.05
    seg_w = (maxX - minX) / max(1, (lip_segments - 1))
    fallbackY = max(ys) if ys else 0.03
    fallbackZ = (sum(zs)/len(zs)) if zs else 0.0

    base = []
    for i in range(lip_segments):
        x = minX + i*seg_w
        top = min(beardline, key=lambda p: abs(p[0]-x)) if beardline else None
        if top is not None:
            base.append((x, top[1], top[2]))
        else:
            base.append((x, fallbackY, fallbackZ))
    return base, minX, maxX

def tapered_radius(x, centerX, min_r, max_r, taper_mult):
    taper = max(0.0, 1.0 - abs(x - centerX) * taper_mult)
    return min_r + taper * (max_r - min_r)

def generate_lip_rings(base_points, arc_steps, min_r, max_r, centerX, taper_mult):
    """Return (lip_vertices, ring_count). Each base point gets a semicircle ring in YZ plane."""
    ring_count = arc_steps + 1
    verts = []
    n = len(base_points)
    for idx, (bx, by, bz) in enumerate(base_points):
        r = tapered_radius(bx, centerX, min_r, max_r, taper_mult)
        # soften the extreme ends to avoid needle-thin slivers
        if idx == 0 or idx == n-1:
            r *= 0.85
        for j in range(ring_count):
            angle = math.pi * (j / float(arc_steps))
            y = by - r * (1.0 - math.sin(angle))
            z = bz + r * math.cos(angle)
            verts.append((bx, y, z))
    return verts, ring_count

def quads_to_tris_between_rings(lip_vertices, base_count, ring_count):
    faces = []
    for i in range(base_count - 1):
        for j in range(ring_count - 1):
            a = lip_vertices[i * ring_count + j]
            b = lip_vertices[i * ring_count + j + 1]
            c = lip_vertices[(i + 1) * ring_count + j]
            d = lip_vertices[(i + 1) * ring_count + j + 1]
            faces.append([a, c, b])
            faces.append([b, c, d])
    return faces

def skin_beardline_to_neckline(beardline, neckline):
    faces = []
    if len(beardline) < 2 or not neckline:
        return faces
    for i in range(len(beardline) - 1):
        b0 = beardline[i]
        b1 = beardline[i+1]
        n0 = min(range(len(neckline)), key=lambda k: dist2(neckline[k], b0))
        n1 = min(range(len(neckline)), key=lambda k: dist2(neckline[k], b1))
        v0 = neckline[n0]
        v1 = neckline[n1]
        faces.append([b0, v0, b1])
        faces.append([v0, v1, b1])
    return faces

def stitch_first_column_to_base(base_points, lip_vertices, ring_count):
    faces = []
    for i in range(len(base_points) - 1):
        a = base_points[i]
        b = base_points[i+1]
        c = lip_vertices[i * ring_count + 0]
        d = lip_vertices[(i+1) * ring_count + 0]
        faces.append([a, c, b])
        faces.append([b, c, d])
    return faces

def make_mesh_from_tris(tris, name="MoldSurface"):
    # Deduplicate verts with rounding for stability
    v2i, verts, faces = {}, [], []
    def key(p): return (round(p[0], 6), round(p[1], 6), round(p[2], 6))
    for (a,b,c) in tris:
        ids = []
        for p in (a,b,c):
            k = key(p)
            if k not in v2i:
                v2i[k] = len(verts)
                verts.append(k)
            ids.append(v2i[k])
        faces.append(tuple(ids))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts], [], faces)
    mesh.validate(verbose=False)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj

def clean_topology(obj, merge_dist=1e-6):
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.remove_doubles(threshold=merge_dist)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    # Triangulate (keeps booleans predictable; SceneKit likes tris)
    bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY', ngon_method='BEAUTY')
    bpy.ops.object.mode_set(mode='OBJECT')
    try:
        bpy.ops.object.shade_smooth()
    except Exception:
        pass

def apply_solidify(obj, thickness, min_lip_radius=0.003, use_even_offset=True):
    """Centered, capped solid; clamp wall to avoid self-intersections on tight radii."""
    bpy.context.view_layer.objects.active = obj
    # Clamp to ~60% of the tightest lip radius (fallback to thickness if larger)
    t = max(1e-5, min(0.6 * float(min_lip_radius), float(thickness)))

    mod = obj.modifiers.new(name="Solidify", type='SOLIDIFY')
    mod.thickness = t
    mod.offset = 0.0                 # centered around surface -> fewer foldovers
    mod.use_even_offset = use_even_offset
    mod.use_quality_normals = True
    mod.use_rim = True               # cap open borders
    mod.use_rim_only = False
    # Guard newer fields for Blender 3.0.1
    if hasattr(mod, "nonmanifold_thickness_mode"):
        mod.nonmanifold_thickness_mode = 'EVEN'
    if hasattr(mod, "thickness_clamp"):
        mod.thickness_clamp = 1.0

    bpy.ops.object.modifier_apply(modifier=mod.name)

def create_cylinders_z_aligned(holes, thickness, radius=0.0015875, embed_offset=0.0025):
    cylinders = []
    for h in holes:
        x, y, z = to_vec3(h)
        depth = float(thickness) + embed_offset * 4.0  # overrun both sides
        center_z = z - depth * 0.5
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=(x, y, center_z))
        cyl = bpy.context.active_object
        # Ensure sane cutter normals
        bpy.context.view_layer.objects.active = cyl
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode='OBJECT')
        cylinders.append(cyl)
    return cylinders

def apply_boolean_difference(target_obj, cutters):
    bpy.context.view_layer.objects.active = target_obj
    for cutter in cutters:
        mod = target_obj.modifiers.new(name="Boolean", type='BOOLEAN')
        mod.operation = 'DIFFERENCE'
        mod.solver = 'EXACT'
        # overlap_threshold may not exist on 3.0.1
        if hasattr(mod, "overlap_threshold"):
            mod.overlap_threshold = 1e-6
        mod.object = cutter
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(cutter, do_unlink=True)

# ---------------------------
# Main pipeline (robust solid)
# ---------------------------

def build_front_surface_tris(beardline, neckline, params):
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    lip_segments   = int(params.get("lipSegments", 100))
    arc_steps      = int(params.get("arcSteps", 24))
    max_lip_radius = float(params.get("maxLipRadius", 0.008))
    min_lip_radius = float(params.get("minLipRadius", 0.003))
    taper_mult     = float(params.get("taperMult", 25.0))
    extrusion_depth= float(params.get("extrusionDepth", -0.008))  # used for target thickness

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult
    )

    faces = []
    faces += quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)
    if neckline:
        faces += skin_beardline_to_neckline(beardline, neckline)
    faces += stitch_first_column_to_base(base_points, lip_vertices, ring_count)

    thickness = abs(extrusion_depth)
    return faces, thickness, min_lip_radius

def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)

def main():
    # Parse CLI
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    if len(argv) != 2:
        raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

    # Reset scene
    bpy.ops.wm.read_factory_settings(use_empty=True)

    with open(input_path, 'r') as f:
        data = json.load(f)

    # Backward-compat for old payloads
    beardline_in = data.get("beardline") or data.get("vertices")
    if beardline_in is None:
        raise ValueError("Missing 'beardline' (or legacy 'vertices') in payload.")
    beardline = [to_vec3(v) for v in beardline_in]

    neckline_in = data.get("neckline")
    neckline = [to_vec3(v) for v in neckline_in] if neckline_in else []
    if neckline:
        neckline = smooth_vertices_open(neckline, passes=3)

    holes_in = data.get("holeCenters") or data.get("holes") or []
    params = data.get("params", {})

    # 1) Build front surface only
    tris, thickness, min_lip_radius = build_front_surface_tris(beardline, neckline, params)

    # 2) Create mesh object
    mold_surface = make_mesh_from_tris(tris, name="BeardMoldSurface")

    # 3) Clean topology before solidifying
    clean_topology(mold_surface, merge_dist=1e-6)

    # 4) Solidify (centered, capped, clamped)  <<< FIXED SIGNATURE
    apply_solidify(mold_surface, thickness=thickness, min_lip_radius=min_lip_radius, use_even_offset=True)

    # 5) Clean again post-solidify
    clean_topology(mold_surface, merge_dist=1e-6)

    # 6) Optional holes via boolean DIFFERENCE
    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference(mold_surface, cutters)
        clean_topology(mold_surface, merge_dist=1e-6)

    # 7) Export
    for obj in bpy.data.objects:
        obj.select_set(False)
    mold_surface.select_set(True)
    export_stl_selected(output_path)

    print(
        f"STL export complete for job ID: {data.get('jobID','N/A')} "
        f"overlay: {data.get('overlay','N/A')} "
        f"verts(beardline)={len(beardline)} "
        f"neckline={len(neckline)} "
        f"holes={len(holes_in)}  "
        f"thickness={thickness:.6f}  "
        f"minLipRadius={min_lip_radius:.6f}"
    )

if __name__ == "__main__":
    main()
