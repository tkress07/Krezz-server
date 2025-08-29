# generate_stl.py (spike-proofed)
import bpy
import bmesh
import json
import sys
import math
from mathutils import Vector

# ===========================
# Utils
# ===========================

def finite3(p):
    return (math.isfinite(p[0]) and math.isfinite(p[1]) and math.isfinite(p[2]))

def to_vec3(p):
    v = (float(p['x']), float(p['y']), float(p['z']))
    if not finite3(v):
        raise ValueError("Non-finite vertex in payload")
    return v

def dist2(a, b):
    dx = a[0]-b[0]; dy = a[1]-b[1]; dz = a[2]-b[2]
    return dx*dx + dy*dy + dz*dz

def tri_area(a, b, c):
    ab = (b[0]-a[0], b[1]-a[1], b[2]-a[2])
    ac = (c[0]-a[0], c[1]-a[1], c[2]-a[2])
    cx = ab[1]*ac[2]-ab[2]*ac[1]
    cy = ab[2]*ac[0]-ab[0]*ac[2]
    cz = ab[0]*ac[1]-ab[1]*ac[0]
    return 0.5 * math.sqrt(cx*cx + cy*cy + cz*cz)

def bbox(points):
    xs = [p[0] for p in points]; ys = [p[1] for p in points]; zs = [p[2] for p in points]
    return (min(xs), max(xs)), (min(ys), max(ys)), (min(zs), max(zs))

def smooth_vertices_open(vertices, passes=1):
    if len(vertices) < 3 or passes <= 0:
        return vertices[:]
    V = vertices[:]
    for _ in range(passes):
        NV = [V[0]]
        for i in range(1, len(V)-1):
            px,py,pz = V[i-1]; cx,cy,cz = V[i]; nx,ny,nz = V[i+1]
            NV.append(((px+cx+nx)/3.0, (py+cy+ny)/3.0, (pz+cz+nz)/3.0))
        NV.append(V[-1])
        V = NV
    return V

# ===========================
# Lip scaffold
# ===========================

def sample_base_points_along_x(beardline, lip_segments):
    xs = [p[0] for p in beardline]
    ys = [p[1] for p in beardline]
    zs = [p[2] for p in beardline]
    minX = min(xs); maxX = max(xs)
    seg_w = (maxX - minX) / max(1, (lip_segments - 1))
    base = []
    for i in range(lip_segments):
        x = minX + i*seg_w
        top = min(beardline, key=lambda p: abs(p[0]-x))
        base.append((x, top[1], top[2]))
    return base, minX, maxX

def tapered_radius(x, centerX, min_r, max_r, taper_mult):
    taper = max(0.0, 1.0 - abs(x - centerX) * taper_mult)
    return min_r + taper * (max_r - min_r)

def generate_lip_rings(base_points, arc_steps, min_r, max_r, centerX, taper_mult):
    ring_count = arc_steps + 1
    verts = []
    for (bx, by, bz) in base_points:
        r = tapered_radius(bx, centerX, min_r, max_r, taper_mult)
        for j in range(ring_count):
            angle = math.pi * (j / float(arc_steps))
            y = by - r * (1.0 - math.sin(angle))
            z = bz + r * math.cos(angle)
            verts.append((bx, y, z))
    return verts, ring_count

def quads_to_tris_between_rings(lip_vertices, base_count, ring_count):
    faces = []
    for i in range(base_count - 1):
        row0 = i * ring_count
        row1 = (i + 1) * ring_count
        for j in range(ring_count - 1):
            a = lip_vertices[row0 + j]
            b = lip_vertices[row0 + j + 1]
            c = lip_vertices[row1 + j]
            d = lip_vertices[row1 + j + 1]
            # CCW winding (viewed roughly from +X)
            faces.append([a, c, b])
            faces.append([b, c, d])
    return faces

# ===========================
# Safe beardline -> neckline skin (monotone + distance cap)
# ===========================

def skin_beardline_to_neckline_safe(beardline, neckline, max_pair_dist=0.025):
    faces = []
    if len(beardline) < 2 or not neckline:
        return faces
    prev = 0
    max_d2 = max_pair_dist * max_pair_dist
    for i in range(len(beardline)-1):
        b0, b1 = beardline[i], beardline[i+1]
        n0 = min(range(prev, len(neckline)), key=lambda k: dist2(neckline[k], b0))
        n1 = min(range(n0, len(neckline)), key=lambda k: dist2(neckline[k], b1))
        v0, v1 = neckline[n0], neckline[n1]
        # distance cap to avoid crazy long triangles
        if dist2(b0, v0) <= max_d2 and dist2(b1, v1) <= max_d2:
            faces.append([b0, v0, b1])
            faces.append([v0, v1, b1])
            prev = n0
        else:
            # skip this span; better a gap that Solidify rims than a spike
            prev = n0
    return faces

# ===========================
# Mesh build / cleanup
# ===========================

def make_mesh_from_tris(tris, name="BeardMoldSurface"):
    v2i = {}
    verts = []
    faces = []
    def key(p): return (round(p[0], 6), round(p[1], 6), round(p[2], 6))
    for (a,b,c) in tris:
        ids = []
        for p in (a,b,c):
            k = key(p)
            if k not in v2i:
                v2i[k] = len(verts)
                verts.append(k)
            ids.append(v2i[k])
        # avoid degenerate indices
        if ids[0] != ids[1] and ids[1] != ids[2] and ids[0] != ids[2]:
            faces.append(tuple(ids))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts], [], faces)
    mesh.validate(verbose=False)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj

def cull_bad_tris(tris, area_eps, max_edge):
    """Remove near-zero area and overlong-edge triangles."""
    kept = []
    emax2 = max_edge*max_edge
    for a,b,c in tris:
        A = tri_area(a,b,c)
        if A <= area_eps:
            continue
        if (dist2(a,b) > emax2) or (dist2(b,c) > emax2) or (dist2(c,a) > emax2):
            continue
        kept.append((a,b,c))
    return kept

def make_manifold_and_solidify(obj, thickness, merge_epsilon=1e-6, center=True):
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')

    try:
        bpy.ops.mesh.merge_by_distance(distance=merge_epsilon)
    except Exception:
        bpy.ops.mesh.remove_doubles(threshold=merge_epsilon)

    try:
        bpy.ops.mesh.dissolve_degenerate(threshold=1e-7)
    except Exception:
        pass

    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')

    solid = obj.modifiers.new(name="Solidify", type='SOLIDIFY')
    solid.thickness = float(thickness)
    solid.offset    = 1.0
    solid.use_even_offset = True
    solid.use_rim   = True
    bpy.ops.object.modifier_apply(modifier=solid.name)

    obj.data.validate(verbose=False)
    obj.data.update()

    if center:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        try:
            bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')
        except Exception:
            pass
        obj.location = (0.0, 0.0, 0.0)

def is_watertight(obj):
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.normal_update()
    boundary_edges = [e for e in bm.edges if len(e.link_faces) == 1]
    bm.free()
    return len(boundary_edges) == 0

# ===========================
# Cutters & boolean
# ===========================

def create_cylinders_z_aligned(holes, thickness, radius=0.0015875, embed_offset=0.0025):
    cylinders = []
    for h in holes:
        x,y,z = to_vec3(h)
        depth = float(thickness)
        center_z = z - (embed_offset + depth / 2.0)
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=(x, y, center_z))
        cylinders.append(bpy.context.active_object)
    return cylinders

def apply_boolean_difference_exact(target_obj, cutters):
    if not cutters:
        return
    bpy.ops.object.select_all(action='DESELECT')
    for c in cutters:
        c.select_set(True)
    bpy.context.view_layer.objects.active = cutters[0]
    bpy.ops.object.join()
    cutter = cutters[0]

    bpy.ops.object.select_all(action='DESELECT')
    target_obj.select_set(True)
    bpy.context.view_layer.objects.active = target_obj
    mod = target_obj.modifiers.new(name="HolesExact", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.solver    = 'EXACT'
    mod.object    = cutter
    bpy.ops.object.modifier_apply(modifier=mod.name)

    bpy.data.objects.remove(cutter, do_unlink=True)
    target_obj.data.validate(verbose=False)
    target_obj.data.update()

# ===========================
# Build (surface-first)
# ===========================

def build_triangles(beardline, neckline, params):
    if not beardline:
        raise ValueError("Empty beardline")

    lip_segments    = int(params.get("lipSegments", 100))
    arc_steps       = int(params.get("arcSteps", 24))
    max_lip_radius  = float(params.get("maxLipRadius", 0.008))
    min_lip_radius  = float(params.get("minLipRadius", 0.003))
    taper_mult      = float(params.get("taperMult", 25.0))
    extrusion_depth = float(params.get("extrusionDepth", -0.008))
    thickness       = abs(extrusion_depth)
    skin_max_dist   = float(params.get("skinMaxDist", 0.025))  # NEW

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult
    )

    faces = quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    if neckline:
        faces += skin_beardline_to_neckline_safe(beardline, neckline, max_pair_dist=skin_max_dist)

    return faces, thickness, lip_vertices

def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)

# ===========================
# Main
# ===========================

def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    if len(argv) != 2:
        raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

    bpy.ops.wm.read_factory_settings(use_empty=True)

    with open(input_path, 'r') as f:
        data = json.load(f)

    beardline_in = data.get("beardline") or data.get("vertices")
    if beardline_in is None:
        raise ValueError("Missing 'beardline' (or legacy 'vertices')")
    beardline = [to_vec3(v) for v in beardline_in]

    neckline_in = data.get("neckline")
    neckline = [to_vec3(v) for v in neckline_in] if neckline_in else []
    if neckline:
        neckline = smooth_vertices_open(neckline, passes=3)

    holes_in = data.get("holeCenters") or data.get("holes") or []
    params = data.get("params", {})

    # Build surface tris
    faces, thickness, lip_vertices = build_triangles(beardline, neckline, params)

    # Guardrails: cull degenerates & spikes before meshing
    # Set max edge as 4x the largest ring radius span or, if unknown, 0.08m
    _, (y0,y1), (z0,z1) = bbox(lip_vertices)
    approx_span = max(abs(y1-y0), abs(z1-z0))
    max_edge = max(0.04, 4.0 * approx_span)  # generous but finite
    faces = cull_bad_tris(faces, area_eps=1e-10, max_edge=max_edge)

    mold_obj = make_mesh_from_tris(faces, name="BeardMoldSurface")

    # Clean + Solidify (true thickness) + center
    make_manifold_and_solidify(mold_obj, thickness, merge_epsilon=1e-6, center=True)

    wt = is_watertight(mold_obj)

    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference_exact(mold_obj, cutters)
        wt = is_watertight(mold_obj)

    bpy.ops.object.select_all(action='DESELECT')
    mold_obj.select_set(True)
    bpy.context.view_layer.objects.active = mold_obj

    export_stl_selected(output_path)

    print(
        f"STL export complete | jobID={data.get('jobID','N/A')} "
        f"overlay={data.get('overlay','N/A')} "
        f"beardline={len(beardline)} neckline={len(neckline)} "
        f"holes={len(holes_in)} watertight={wt}"
    )

if __name__ == "__main__":
    main()
