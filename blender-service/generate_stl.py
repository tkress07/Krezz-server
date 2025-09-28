# file: blender_service_fixed_weld.py
# Python 3.x • Blender 3.x API

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

def area2(a, b, c):
    ab = (b[0]-a[0], b[1]-a[1], b[2]-a[2])
    ac = (c[0]-a[0], c[1]-a[1], c[2]-a[2])
    cx = ab[1]*ac[2] - ab[2]*ac[1]
    cy = ab[2]*ac[0] - ab[0]*ac[2]
    cz = ab[0]*ac[1] - ab[1]*ac[0]
    return cx*cx + cy*cy + cz*cz

def tri_min_edge_len2(a, b, c):
    def d2(p, q): return (p[0]-q[0])**2 + (p[1]-q[1])**2 + (p[2]-q[2])**2
    return min(d2(a,b), d2(b,c), d2(c,a))

def smooth_vertices_open(vertices, passes=1):
    if len(vertices) < 3 or passes <= 0:
        return vertices[:]
    V = vertices[:]
    for _ in range(passes):
        NV = [V[0]]
        for i in range(1, len(V) - 1):
            px, py, pz = V[i - 1]
            cx, cy, cz = V[i]
            nx, ny, nz = V[i + 1]
            NV.append(((px + cx + nx) / 3.0, (py + cy + ny) / 3.0, (pz + cz + nz) / 3.0))
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
    fallbackZ = (sum(zs) / len(zs)) if zs else 0.0

    base = []
    for i in range(lip_segments):
        x = minX + i * seg_w
        top = min(beardline, key=lambda p: abs(p[0] - x)) if beardline else None
        base.append((x, top[1], top[2]) if top is not None else (x, fallbackY, fallbackZ))
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
        for j in range(ring_count - 1):
            a = lip_vertices[i * ring_count + j]
            b = lip_vertices[i * ring_count + j + 1]
            c = lip_vertices[(i + 1) * ring_count + j]
            d = lip_vertices[(i + 1) * ring_count + j + 1]
            faces.append([a, c, b])
            faces.append([b, c, d])
    return faces

def first_ring_column(lip_vertices, base_count, ring_count):
    return [lip_vertices[i * ring_count + 0] for i in range(base_count)]

def resample_polyline_by_x(points, xs):
    if not points:
        return [(x, 0.0, 0.0) for x in xs]
    P = sorted(points, key=lambda p: p[0])
    out = []
    k = 0
    n = len(P)
    for x in xs:
        if x <= P[0][0]:
            out.append((x, P[0][1], P[0][2])); continue
        if x >= P[-1][0]:
            out.append((x, P[-1][1], P[-1][2])); continue
        while k < n - 2 and P[k + 1][0] < x:
            k += 1
        a, b = P[k], P[k + 1]
        t = 0.0 if b[0] == a[0] else (x - a[0]) / (b[0] - a[0])
        y = a[1] * (1 - t) + b[1] * t
        z = a[2] * (1 - t) + b[2] * t
        out.append((x, y, z))
    return out

def strap_tris_equal_counts(A, B):
    faces = []
    m = min(len(A), len(B))
    for i in range(m - 1):
        faces.append([A[i], B[i], A[i + 1]])
        faces.append([A[i + 1], B[i], B[i + 1]])
    return faces

# ---------------------------
# Mesh building & cleanup
# ---------------------------

def _rounded_key(p, eps):
    return (round(p[0] / eps) * eps, round(p[1] / eps) * eps, round(p[2] / eps) * eps)

def make_mesh_from_tris(tris, name="MoldMesh", weld_eps=2e-4, min_feature=3.0e-4):
    """Create a surface mesh and clean it to prep for Solidify.
    weld_eps ~0.2–0.5 mm; min_feature ~0.3–0.6 mm."""
    v2i = {}
    verts = []
    faces = []
    min_edge2 = min_feature * min_feature

    def key(p): return _rounded_key(p, weld_eps)

    for (a, b, c) in tris:
        if tri_min_edge_len2(a, b, c) < min_edge2:
            continue
        ids = []
        for p in (a, b, c):
            k = key(p)
            if k not in v2i:
                v2i[k] = len(verts); verts.append(k)
            ids.append(v2i[k])
        if area2(verts[ids[0]], verts[ids[1]], verts[ids[2]]) > 1e-18:
            faces.append(tuple(ids))

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts], [], faces)
    mesh.validate(verbose=False); mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)

    # Light geometry cleanup on the surface
    try:
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_eps)
        bmesh.ops.dissolve_degenerate(bm, dist=weld_eps * 0.25)
        boundary_edges = [e for e in bm.edges if len(e.link_faces) == 1]
        if boundary_edges:
            bmesh.ops.holes_fill(bm, edges=boundary_edges)
        # Optional: clean slivers
        bmesh.ops.dissolve_limit(bm, angle_limit=0.005, use_dissolve_boundaries=False, verts=bm.verts)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bmesh.ops.triangulate(bm, faces=bm.faces)
        bm.to_mesh(mesh); bm.free()
        mesh.validate(verbose=False); mesh.update()
    except Exception:
        pass

    # Ensure outward normals
    try:
        view = bpy.context.view_layer
        view.objects.active = obj
        for o in bpy.data.objects: o.select_set(False)
        obj.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        pass

    return obj

def apply_weld(obj, merge_dist):
    try:
        mod = obj.modifiers.new(name="Weld", type='WELD')
        mod.merge_threshold = float(merge_dist)
        bpy.context.view_layer.objects.active = obj
        for o in bpy.data.objects: o.select_set(False)
        obj.select_set(True)
        bpy.ops.object.modifier_apply(modifier=mod.name)
    except Exception:
        pass

def solidify_and_seal(obj, thickness, merge_thresh):
    try:
        mod = obj.modifiers.new(name="Solid", type='SOLIDIFY')
        mod.thickness = float(thickness)
        mod.offset = -1.0              # thicken “down” into the mold
        mod.use_rim = True             # close open borders
        mod.use_quality_normals = True
        mod.use_even_offset = True
        mod.use_merge_vertices = True  # MERGE while creating thickness
        mod.merge_threshold = float(merge_thresh)
        bpy.context.view_layer.objects.active = obj
        for o in bpy.data.objects: o.select_set(False)
        obj.select_set(True)
        bpy.ops.object.modifier_apply(modifier=mod.name)
    except Exception:
        pass

def voxel_remesh_if_requested(obj, voxel_size):
    if voxel_size <= 0:
        return
    try:
        for o in bpy.data.objects: o.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        bpy.ops.object.voxel_remesh(voxel_size=float(voxel_size), adaptivity=0.0)
    except Exception:
        pass  # older Blender versions

def report_non_manifold(obj):
    try:
        mesh = obj.data
        bm = bmesh.new(); bm.from_mesh(mesh)
        nonman_edges = [e for e in bm.edges if len(e.link_faces) not in (1, 2)]
        boundary_edges = [e for e in bm.edges if len(e.link_faces) == 1]
        print(f"Non-manifold edges: {len(nonman_edges)} | Boundary edges: {len(boundary_edges)}")
        bm.free()
    except Exception:
        pass

def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)

# ---------------------------
# Build triangles (SURFACE ONLY)
# ---------------------------

def build_triangles(beardline, neckline, params):
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    lip_segments     = int(params.get("lipSegments", 100))
    arc_steps        = int(params.get("arcSteps", 24))
    max_lip_radius   = float(params.get("maxLipRadius", 0.008))
    min_lip_radius   = float(params.get("minLipRadius", 0.003))
    taper_mult       = float(params.get("taperMult", 25.0))
    extrusion_depth  = float(params.get("extrusionDepth", -0.008))  # thickness magnitude

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult
    )

    faces = []
    faces += quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    xs = [bp[0] for bp in base_points]
    beard_X = resample_polyline_by_x(beardline, xs)
    ring0 = first_ring_column(lip_vertices, len(base_points), ring_count)
    faces += strap_tris_equal_counts(ring0, beard_X)

    if neckline:
        neck_X = resample_polyline_by_x(neckline, xs)
        faces += strap_tris_equal_counts(beard_X, neck_X)

    faces = [tri for tri in faces if area2(tri[0], tri[1], tri[2]) > 1e-18]
    thickness = abs(extrusion_depth)  # we’ll add thickness later via Solidify
    return faces, thickness

# ---------------------------
# Main pipeline
# ---------------------------

def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    if len(argv) != 2:
        raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

    with open(input_path, 'r') as f:
        data = json.load(f)

    beardline_in = data.get("beardline") or data.get("vertices")
    if beardline_in is None:
        raise ValueError("Missing 'beardline' (or legacy 'vertices') in payload.")
    beardline = [to_vec3(v) for v in beardline_in]

    neckline_in = data.get("neckline")
    neckline = [to_vec3(v) for v in neckline_in] if neckline_in else []
    if neckline:
        neckline = smooth_vertices_open(neckline, passes=3)

    # NOTE: holeCenters (if present) are intentionally ignored (no booleans)
    _holes_ignored = data.get("holeCenters") or data.get("holes") or []

    params = data.get("params", {})

    nozzle       = float(params.get("nozzle", 0.0004))          # meters
    weld_eps     = float(params.get("weldEps", 4e-4))           # 0.4 mm default here
    min_feature  = float(params.get("minFeature", 4.5e-4))      # ≥ ~nozzle size
    voxel_size   = float(params.get("voxelRemesh", max(nozzle*1.5, 0.0006)))

    tris, thickness = build_triangles(beardline, neckline, params)

    # 1) Make surface and clean
    mold_obj = make_mesh_from_tris(tris, name="BeardMoldSurf",
                                   weld_eps=weld_eps,
                                   min_feature=min_feature)

    # 2) Weld on surface to eliminate near-duplicate borders
    apply_weld(mold_obj, merge_dist=max(weld_eps, 0.00035))

    # 3) Solidify with vertex merge at rims (creates ONE closed shell)
    solidify_and_seal(mold_obj, thickness, merge_thresh=max(weld_eps, 0.0005))

    # 4) Voxel remesh at ≥ 1.5× nozzle to erase micro seams
    voxel_remesh_if_requested(mold_obj, voxel_size)

    report_non_manifold(mold_obj)

    for obj in bpy.data.objects: obj.select_set(False)
    mold_obj.select_set(True)
    export_stl_selected(output_path)

    print(
        f"STL export complete for job ID: {data.get('job_id','N/A')} "
        f"overlay: {data.get('overlay','N/A')} "
        f"verts(beardline)={len(beardline)} "
        f"neckline={len(neckline)} "
        f"holes_ignored={len(_holes_ignored)} "
        f"weld_eps={weld_eps} voxel={voxel_size} min_feature={min_feature} thickness={thickness}"
    )

if __name__ == "__main__":
    main()
