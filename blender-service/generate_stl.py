import bpy
import bmesh
import json
import sys
import math
from mathutils import Vector

# ---------------------------
# Helpers
# ---------------------------

def to_vec3(p):
    return (float(p['x']), float(p['y']), float(p['z']))

def smooth_vertices_open(vertices, passes=1):
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
    xs = [p[0] for p in beardline]; ys = [p[1] for p in beardline]; zs = [p[2] for p in beardline]
    minX = min(xs) if xs else -0.05
    maxX = max(xs) if xs else  0.05
    seg_w = (maxX - minX) / max(1, (lip_segments - 1))
    fallbackY = max(ys) if ys else 0.03
    fallbackZ = (sum(zs)/len(zs)) if zs else 0.0

    base = []
    for i in range(lip_segments):
        x = minX + i*seg_w
        # closest by |Δx|
        top = min(beardline, key=lambda p: abs(p[0]-x)) if beardline else None
        base.append((x, top[1], top[2]) if top else (x, fallbackY, fallbackZ))
    return base, minX, maxX

def tapered_radius(x, centerX, min_r, max_r, taper_mult):
    taper = max(0.0, 1.0 - abs(x - centerX) * taper_mult)
    return min_r + taper * (max_r - min_r)

def generate_lip_rings(base_points, arc_steps, min_r, max_r, centerX, taper_mult):
    ring_count = arc_steps + 1
    verts = []
    n = len(base_points)
    for idx, (bx, by, bz) in enumerate(base_points):
        r = tapered_radius(bx, centerX, min_r, max_r, taper_mult)
        if idx == 0 or idx == n-1:
            r *= 0.85  # soften ends to avoid slivers
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

def tri_area(a, b, c):
    # area of triangle using cross product
    ax,ay,az = a; bx,by,bz = b; cx,cy,cz = c
    ux,uy,uz = (bx-ax, by-ay, bz-az)
    vx,vy,vz = (cx-ax, cy-ay, cz-az)
    cx_, cy_, cz_ = (uy*vz - uz*vy, uz*vx - ux*vz, ux*vy - uy*vx)
    return 0.5 * math.sqrt(cx_*cx_ + cy_*cy_ + cz_*cz_)

def skin_with_shared_connections(beardline, neckline, shared_pairs, min_area=1e-10):
    """
    Bridge between beardline and neckline using explicit index pairs.
    shared_pairs may be:
      - [{'beardIndex': i, 'neckIndex': j}, ...]  OR
      - [{'b': i, 'n': j}, ...]                   OR
      - [[i, j], ...]
    We sort by beardIndex to keep strips ordered and build quads → two tris.
    """
    faces = []
    if not beardline or not neckline or not shared_pairs:
        return faces

    norm_pairs = []
    for p in shared_pairs:
        if isinstance(p, dict):
            bi = p.get('beardIndex', p.get('b'))
            ni = p.get('neckIndex',  p.get('n'))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            bi, ni = p[0], p[1]
        else:
            continue
        if isinstance(bi, int) and isinstance(ni, int):
            if 0 <= bi < len(beardline) and 0 <= ni < len(neckline):
                norm_pairs.append((bi, ni))

    if len(norm_pairs) < 2:
        return faces

    # sort by beard index to maintain order
    norm_pairs.sort(key=lambda t: t[0])

    for k in range(len(norm_pairs)-1):
        b0, n0 = norm_pairs[k]
        b1, n1 = norm_pairs[k+1]
        A = beardline[b0]; B = neckline[n0]; C = beardline[b1]; D = neckline[n1]

        # two tris: (A, B, C) and (B, D, C)
        if tri_area(A, B, C) >= min_area:
            faces.append([A, B, C])
        if tri_area(B, D, C) >= min_area:
            faces.append([B, D, C])

    return faces

def make_mesh_from_tris(tris, name="MoldSurface"):
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
    mesh.validate(verbose=False); mesh.update()
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
    bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY', ngon_method='BEAUTY')
    bpy.ops.object.mode_set(mode='OBJECT')
    try:
        bpy.ops.object.shade_smooth()
    except Exception:
        pass

def apply_solidify(obj, thickness, min_lip_radius=0.003, use_even_offset=True):
    bpy.context.view_layer.objects.active = obj
    # Clamp wall to avoid self-intersections on tight curvature
    t = max(1e-5, min(0.6 * float(min_lip_radius), float(thickness)))
    mod = obj.modifiers.new(name="Solidify", type='SOLIDIFY')
    mod.thickness = t
    mod.offset = 0.0                 # centered around surface
    mod.use_even_offset = use_even_offset
    mod.use_quality_normals = True
    mod.use_rim = True               # cap open borders
    mod.use_rim_only = False
    if hasattr(mod, "nonmanifold_thickness_mode"):
        mod.nonmanifold_thickness_mode = 'EVEN'
    if hasattr(mod, "thickness_clamp"):
        mod.thickness_clamp = 1.0
    bpy.ops.object.modifier_apply(modifier=mod.name)

def create_cylinders_z_aligned(holes, thickness, radius=0.0015875, embed_offset=0.0025):
    cylinders = []
    for h in holes:
        x, y, z = to_vec3(h)
        depth = float(thickness) + embed_offset * 4.0
        center_z = z - depth * 0.5
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=(x, y, center_z))
        cyl = bpy.context.active_object
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
        if hasattr(mod, "overlap_threshold"):
            mod.overlap_threshold = 1e-6
        mod.object = cutter
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(cutter, do_unlink=True)

# ---------------------------
# Main pipeline
# ---------------------------

def build_front_surface_tris(beardline, neckline, params, shared_pairs):
    if not beardline:
        raise ValueError("Empty beardline supplied.")
    lip_segments   = int(params.get("lipSegments", 100))
    arc_steps      = int(params.get("arcSteps", 24))
    max_lip_radius = float(params.get("maxLipRadius", 0.008))
    min_lip_radius = float(params.get("minLipRadius", 0.003))
    taper_mult     = float(params.get("taperMult", 25.0))
    extrusion_depth= float(params.get("extrusionDepth", -0.008))

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult
    )

    # Lofted lip sheet
    faces = quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    # If explicit shared connections are provided, bridge beardline↔neckline using them
    if neckline and shared_pairs:
        faces += skin_with_shared_connections(beardline, neckline, shared_pairs, min_area=1e-12)

    thickness = abs(extrusion_depth)
    return faces, thickness, min_lip_radius

def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)

def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    if len(argv) != 2:
        raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

    bpy.ops.wm.read_factory_settings(use_empty=True)

    with open(input_path, 'r') as f:
        data = json.load(f)

    # Inputs
    beardline_in = data.get("beardline") or data.get("vertices")
    if beardline_in is None:
        raise ValueError("Missing 'beardline' (or legacy 'vertices') in payload.")
    beardline = [to_vec3(v) for v in beardline_in]

    neckline_in = data.get("neckline") or []
    neckline = [to_vec3(v) for v in neckline_in] if neckline_in else []
    if neckline:
        neckline = smooth_vertices_open(neckline, passes=3)

    # Shared connections may come as 'sharedConnections' or 'shared_connections'
    shared_pairs = data.get("sharedConnections") or data.get("shared_connections") or []

    holes_in = data.get("holeCenters") or data.get("holes") or []
    params = data.get("params", {})

    # 1) Build surface (loft + optional shared-connection skin)
    tris, thickness, min_lip_radius = build_front_surface_tris(beardline, neckline, params, shared_pairs)

    # 2) Mesh object
    mold_surface = make_mesh_from_tris(tris, name="BeardMoldSurface")

    # 3) Clean pre-solidify
    clean_topology(mold_surface, merge_dist=1e-6)

    # 4) Solidify (centered, capped, clamped)
    apply_solidify(mold_surface, thickness=thickness, min_lip_radius=min_lip_radius, use_even_offset=True)

    # 5) Clean post-solidify
    clean_topology(mold_surface, merge_dist=1e-6)

    # 6) Holes
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
        f"STL export complete | overlay={data.get('overlay','N/A')} "
        f"verts(beardline)={len(beardline)} neckline={len(neckline)} "
        f"sharedPairs={len(shared_pairs)} holes={len(holes_in)} "
        f"thickness={thickness:.6f} minLipRadius={min_lip_radius:.6f}"
    )

if __name__ == "__main__":
    main()
