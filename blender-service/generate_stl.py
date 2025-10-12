# ============================================
# Beard Mold Generator (gap-proof v2)
# Drop-in Blender script
# ============================================

import bpy
import bmesh
import json
import sys
import math
from mathutils import Vector

# ========= Tunables (safe defaults for ~0.4 mm nozzle) =========
WELD_EPS_DEFAULT  = 0.0002       # shared-vertex tolerance (meters)
AREA_MIN          = 1e-14        # drop ultra-skinny sliver tris early
VOXEL_DEFAULT     = 0.0          # OFF by default; try 0.0005–0.001 if needed
# ===============================================================


# ---------------------------
# Helpers (geometry & math)
# ---------------------------

def to_vec3(p):
    return (float(p['x']), float(p['y']), float(p['z']))

def area2(a, b, c):
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cx = ab[1] * ac[2] - ab[2] * ac[1]
    cy = ab[2] * ac[0] - ab[0] * ac[2]
    cz = ab[0] * ac[1] - ab[1] * ac[0]
    return cx * cx + cy * cy + cz * cz

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

def sample_base_points_along_x(beardline, lip_segments, eps=1e-6):
    """Sample along X with strictly increasing X (no duplicate columns)."""
    if not beardline:
        return [(-0.008 + 0.016 * i / max(1, lip_segments - 1), 0.03, 0.0)
                for i in range(lip_segments)], -0.008, 0.008

    xs = [p[0] for p in beardline]
    ys = [p[1] for p in beardline]
    zs = [p[2] for p in beardline]

    minX, maxX = min(xs), max(xs)
    seg_w = (maxX - minX) / max(1, (lip_segments - 1))
    fallbackY = max(ys)
    fallbackZ = sum(zs) / len(zs)

    cols = []
    last_x = None
    for i in range(lip_segments):
        x = minX + i * seg_w
        if last_x is not None and abs(x - last_x) < eps:
            x = last_x + eps
        last_x = x
        top = min(beardline, key=lambda p: abs(p[0] - x)) if beardline else None
        cols.append((x, top[1], top[2]) if top else (x, fallbackY, fallbackZ))

    return cols, minX, maxX

def tapered_radius(x, centerX, min_r, max_r, taper_mult):
    taper = max(0.0, 1.0 - abs(x - centerX) * taper_mult)
    return min_r + taper * (max_r - min_r)

def _bias_t(t, profile_bias):
    """Bias the semi-circular profile to pack samples near crest.
       profile_bias=1 → uniform. >1 → more samples near 90° apex."""
    if profile_bias <= 0.0:
        return t
    return pow(t, 1.0 / profile_bias)

def generate_lip_rings(base_points, arc_steps, min_r, max_r, centerX, taper_mult,
                       prelift=0.0, profile_bias=1.0):
    """Create a half-round 'lip' around the beardline samples."""
    ring_count = arc_steps + 1
    verts = []
    for (bx, by, bz) in base_points:
        r = tapered_radius(bx, centerX, min_r, max_r, taper_mult)
        for j in range(ring_count):
            tj = _bias_t(j / float(arc_steps), profile_bias)
            angle = math.pi * tj
            y = (by + prelift) - r * (1.0 - math.sin(angle))
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
# Solid, manifold extrusion (shared welding)
# ---------------------------

def _rounded_key(p, eps):
    return (round(p[0] / eps) * eps, round(p[1] / eps) * eps, round(p[2] / eps) * eps)

def extrude_surface_z_solid(tri_faces, depth, weld_eps):
    """Extrude in +Z and close side walls using a shared vertex map."""
    v2i = {}
    verts = []
    tris_idx = []

    def idx_of(p):
        k = _rounded_key(p, weld_eps)
        i = v2i.get(k)
        if i is None:
            i = len(verts)
            v2i[k] = i
            verts.append(k)
        return i

    for a, b, c in tri_faces:
        ia = idx_of(a); ib = idx_of(b); ic = idx_of(c)
        tris_idx.append((ia, ib, ic))

    # boundary edges on the front sheet
    edge_count = {}
    edge_dir = {}
    for ia, ib, ic in tris_idx:
        for (u, v) in ((ia, ib), (ib, ic), (ic, ia)):
            ue = (min(u, v), max(u, v))
            edge_count[ue] = edge_count.get(ue, 0) + 1
            if ue not in edge_dir:
                edge_dir[ue] = (u, v)
    boundary = [ue for ue, c in edge_count.items() if c == 1]

    back_offset = len(verts)
    back_verts = [(x, y, z + depth) for (x, y, z) in verts]

    out = []
    # front + back (flip back winding)
    for ia, ib, ic in tris_idx:
        out.append((verts[ia], verts[ib], verts[ic]))
        ja, jb, jc = ia + back_offset, ib + back_offset, ic + back_offset
        out.append((back_verts[jc - back_offset], back_verts[jb - back_offset], back_verts[ja - back_offset]))

    # sides
    for ue in boundary:
        u, v = edge_dir[ue]
        ju, jv = u + back_offset, v + back_offset
        out.append((verts[u], verts[v], back_verts[jv - back_offset]))
        out.append((verts[u], back_verts[jv - back_offset], back_verts[ju - back_offset]))

    return out

def make_mesh_from_tris(tris, name="MoldMesh", weld_eps=WELD_EPS_DEFAULT):
    v2i, verts, faces = {}, [], []

    def key(p): return _rounded_key(p, weld_eps)

    for (a, b, c) in tris:
        ids = []
        for p in (a, b, c):
            k = key(p)
            if k not in v2i:
                v2i[k] = len(verts)
                verts.append(k)
            ids.append(v2i[k])
        if area2(verts[ids[0]], verts[ids[1]], verts[ids[2]]) > AREA_MIN:
            faces.append(tuple(ids))

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts], [], faces)
    mesh.validate(verbose=True); mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


# ---------------------------
# Mesh cleaning & manifold repair
# ---------------------------

def _do_clean(bm, weld_dist, degenerate_dist):
    # micro + main weld & dissolve
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_dist * 0.25)
    bmesh.ops.dissolve_degenerate(bm, dist=degenerate_dist * 0.25)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_dist)
    bmesh.ops.dissolve_degenerate(bm, dist=degenerate_dist * 0.5)

    # remove loose verts/edges
    loose_verts = [v for v in bm.verts if not v.link_edges]
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context='VERTS')

    # tiny angle slivers
    try:
        bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(1.0), use_dissolve_boundaries=True, verts=bm.verts, edges=bm.edges)
    except Exception:
        pass

    # fill any open perimeters
    boundary_edges = [e for e in bm.edges if len(e.link_faces) == 1]
    if boundary_edges:
        bmesh.ops.holes_fill(bm, edges=boundary_edges)

def clean_mesh(obj, weld_eps, min_feature=None, strong=False):
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)

    mf = float(min_feature) if (min_feature is not None) else weld_eps * 0.8
    weld_dist = max(weld_eps, 0.8 * mf)
    if strong:
        weld_dist *= 1.25

    _do_clean(bm, weld_dist, mf)

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.triangulate(bm, faces=bm.faces)

    bm.to_mesh(mesh); bm.free()
    mesh.validate(verbose=True); mesh.update()

def ensure_closed_manifold(obj, weld_eps, min_feature=None, passes=3):
    """Multi-pass boundary close + weld to kill pinholes after booleans."""
    mesh = obj.data
    for _ in range(max(1, passes)):
        bm = bmesh.new()
        bm.from_mesh(mesh)
        boundary_edges = [e for e in bm.edges if len(e.link_faces) == 1]
        if not boundary_edges:
            bm.free(); break
        bmesh.ops.holes_fill(bm, edges=boundary_edges)
        bmesh.ops.remove_doubles(bm, verts=bm.verts,
                                 dist=max(weld_eps * 0.9, (min_feature or weld_eps) * 0.6))
        bmesh.ops.dissolve_degenerate(bm, dist=(min_feature or weld_eps) * 0.6)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(mesh); bm.free()
        mesh.validate(verbose=False); mesh.update()

def create_cylinders_z_aligned(holes, thickness, radius=0.0015875,
                               embed_offset=0.0025, through_margin=0.0008):
    """Make cylinders that cut completely through the body with a margin."""
    cylinders = []
    full_depth = float(thickness) + 2.0 * float(through_margin)
    for h in holes:
        x, y, z = to_vec3(h)
        center_z = z - (embed_offset + full_depth / 2.0)
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=full_depth,
                                            location=(x, y, center_z))
        cyl = bpy.context.active_object
        cylinders.append(cyl)
    return cylinders

def apply_boolean(op, target_obj, cutters, solver_exact=True, weld_eps=0.0002):
    bpy.context.view_layer.objects.active = target_obj
    for c in cutters:
        mod = target_obj.modifiers.new(name="Boolean", type='BOOLEAN')
        mod.operation = op  # 'UNION' / 'DIFFERENCE'
        try:
            mod.solver = 'EXACT' if solver_exact else 'FAST'
            if hasattr(mod, "double_threshold"):
                mod.double_threshold = weld_eps * 0.5
        except Exception:
            pass
        mod.object = c
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(c, do_unlink=True)


# ---------------------------
# Build triangles (Swift parity)
# ---------------------------

def build_triangles(beardline, neckline, params):
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    lip_segments    = int(params.get("lipSegments", 160))
    arc_steps       = int(params.get("arcSteps", 40))
    max_lip_radius  = float(params.get("maxLipRadius", 0.010))
    min_lip_radius  = float(params.get("minLipRadius", 0.0045))
    taper_mult      = float(params.get("taperMult", 20.0))
    extrusion_depth = float(params.get("extrusionDepth", -0.011))
    prelift         = float(params.get("prelift", 0.0))
    profile_bias    = float(params.get("profileBias", 1.0))

    # 1) Base points sampled by X (strictly increasing columns)
    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    # 2) Lip rings from base points (with prelift + profile bias)
    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius,
        centerX, taper_mult, prelift=prelift, profile_bias=profile_bias
    )

    faces = []
    # 2a) Quads between lip rings (ring0↔ring1, ring1↔ring2, ...)
    faces += quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    # 2b) Cap basePoints ↔ ring0, skipping near-zero-width columns
    for i in range(len(base_points) - 1):
        a = base_points[i]
        b = base_points[i + 1]
        if abs(b[0] - a[0]) < 1e-7:
            continue  # avoid slot down the middle
        c = lip_vertices[i * ring_count + 0]
        d = lip_vertices[(i + 1) * ring_count + 0]
        faces.append([a, c, b])
        faces.append([b, c, d])

    # 3) Separate strap: make TOP of strap exactly our base grid to guarantee weld
    if neckline:
        xs = [bp[0] for bp in base_points]
        beard_X = base_points[:]  # share exact columns with lip/base
        neck_X  = resample_polyline_by_x(neckline, xs)
        faces += strap_tris_equal_counts(beard_X, neck_X)

    # Drop razor-thin slivers early
    faces = [tri for tri in faces if area2(tri[0], tri[1], tri[2]) > AREA_MIN]

    # Consistent weld for the whole sheet prior to extrusion
    weld_eps = float(params.get("weldEps", WELD_EPS_DEFAULT))
    extruded = extrude_surface_z_solid(faces, extrusion_depth, weld_eps=weld_eps)

    return extruded, abs(extrusion_depth), weld_eps


# ---------------------------
# Ribs (optional)
# ---------------------------

def create_anchor_ribs_along_x(beardline, params, centerY=None):
    """Create thin rectangular ribs at interval across X. Returns rib objects (for UNION)."""
    if not params.get("enableAnchorRibs", False):
        return []

    spacing   = float(params.get("ribSpacing",   0.004))
    thick     = float(params.get("ribThickness", 0.0009))
    depth     = float(params.get("ribDepth",     0.0018))
    z_offset  = float(params.get("ribZOffset",   0.0003))
    band_y    = float(params.get("ribBandY",     0.004))

    if not beardline:
        return []

    xs = [p[0] for p in beardline]
    ys = [p[1] for p in beardline]
    zs = [p[2] for p in beardline]
    minX, maxX = min(xs), max(xs)
    midY = centerY if centerY is not None else sum(ys) / len(ys)
    baseZ = sum(zs) / len(zs)

    ribs = []
    x = minX
    while x <= maxX:
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(x, midY, baseZ + z_offset))
        rib = bpy.context.active_object
        rib.scale = (thick * 0.5, band_y * 0.5, depth * 0.5)  # unit cube → scaled rib
        ribs.append(rib)
        x += spacing

    return ribs


# ---------------------------
# IO / main pipeline
# ---------------------------

def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)

def voxel_remesh_if_requested(obj, voxel_size):
    if voxel_size <= 0:
        return
    try:
        for o in bpy.data.objects:
            o.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        bpy.ops.object.voxel_remesh(voxel_size=float(voxel_size), adaptivity=0.0)
    except Exception:
        pass

def report_non_manifold(obj):
    try:
        mesh = obj.data
        bm = bmesh.new()
        bm.from_mesh(mesh)
        nonman_edges = [e for e in bm.edges if len(e.link_faces) not in (1, 2)]
        boundary_edges = [e for e in bm.edges if len(e.link_faces) == 1]
        print(f"Non-manifold edges: {len(nonman_edges)} | Boundary edges: {len(boundary_edges)}")
        bm.free()
    except Exception:
        pass

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

    holes_in = data.get("holeCenters") or data.get("holes") or []
    params = data.get("params", {})

    # Build main sheet (gap-proof) → extrude
    tris, thickness, weld_eps = build_triangles(beardline, neckline, params)

    # Create mesh from triangles
    mf_param = params.get("minFeature")
    mold_obj = make_mesh_from_tris(tris, name="BeardMold", weld_eps=weld_eps)

    # First clean
    clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=False)

    # Optional ribs (union)
    ribs = create_anchor_ribs_along_x(beardline, params)
    if ribs:
        apply_boolean('UNION', mold_obj, ribs,
                      solver_exact=bool(params.get("booleanExact", True)),
                      weld_eps=weld_eps)
        clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=True)

    # Optional remesh + clean again
    voxel_size = float(params.get("voxelRemesh", VOXEL_DEFAULT))
    voxel_remesh_if_requested(mold_obj, voxel_size)
    if voxel_size > 0:
        clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=True)

    # Holes → boolean → clean again
    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        thru = float(params.get("holeThroughMargin", 0.0008))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius,
                                             embed_offset=embed_offset, through_margin=thru)
        apply_boolean('DIFFERENCE', mold_obj, cutters,
                      solver_exact=bool(params.get("booleanExact", True)),
                      weld_eps=weld_eps)
        ensure_closed_manifold(mold_obj, weld_eps, min_feature=mf_param, passes=3)
        clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=True)

    # Final “fuse” pass to kill micro gaps
    final_fuse = float(params.get("finalFuseEps", 0.00035))
    ensure_closed_manifold(mold_obj, final_fuse, min_feature=mf_param, passes=2)
    clean_mesh(mold_obj, final_fuse, min_feature=mf_param, strong=True)

    report_non_manifold(mold_obj)

    for obj in bpy.data.objects:
        obj.select_set(False)
    mold_obj.select_set(True)
    bpy.context.view_layer.objects.active = mold_obj

    export_stl_selected(output_path)

    print(
        f"STL export complete for job ID: {data.get('job_id', data.get('jobID','N/A'))} "
        f"overlay: {data.get('overlay','N/A')} "
        f"verts(beardline)={len(beardline)} "
        f"neckline={len(neckline)} "
        f"holes={len(holes_in)} "
        f"weld_eps={weld_eps} voxel={voxel_size} "
        f"finalFuse={final_fuse} ribs={'on' if ribs else 'off'}"
    )

if __name__ == "__main__":
    main()
