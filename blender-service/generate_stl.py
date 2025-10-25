# krezz_mold.py — continuous top-edge lip + arc-length sweep + watertight solid
# Python 3 / Blender 3.x

import bpy
import bmesh
import json
import sys
import math
from mathutils import Vector

# ========= Tunables (safe defaults; override via "params") ====================
WELD_EPS_DEFAULT  = 0.0002     # shared-vertex tolerance (meters)
AREA_MIN          = 1e-14      # drop ultra-skinny sliver tris early
VOXEL_DEFAULT     = 0.0        # OFF by default; use autoRemesh or set voxelRemesh
# ============================================================================


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
    """Average only Y/Z (leave X as-is)."""
    if len(vertices) < 3 or passes <= 0:
        return vertices[:]
    V = vertices[:]
    for _ in range(passes):
        NV = [V[0]]
        for i in range(1, len(V) - 1):
            x = V[i][0]
            y = (V[i - 1][1] + V[i][1] + V[i + 1][1]) / 3.0
            z = (V[i - 1][2] + V[i][2] + V[i + 1][2]) / 3.0
            NV.append((x, y, z))
        NV.append(V[-1])
        V = NV
    return V

# --------- Arc-length parameterization (top edge becomes intrinsically smooth)
def _cumlen(points):
    L = [0.0]
    for i in range(1, len(points)):
        a, b = points[i-1], points[i]
        dx = b[0]-a[0]; dy = b[1]-a[1]; dz = b[2]-a[2]
        L.append(L[-1] + (dx*dx+dy*dy+dz*dz)**0.5)
    return L, (L[-1] if L else 0.0)

def _resample_by_t(points, ts):
    """Linear interpolate by arc-length t in [0,1]."""
    if not points:
        return [(0.0, 0.0, 0.0) for _ in ts]
    P = points[:]
    L, total = _cumlen(P)
    if total <= 1e-12:
        x = [p[0] for p in P]; y = sum(p[1] for p in P)/len(P); z = sum(p[2] for p in P)/len(P)
        return [(xi, y, z) for xi in x]
    out = []
    i = 0
    for t in ts:
        s = t * total
        while i < len(L)-2 and L[i+1] < s:
            i += 1
        a, b = P[i], P[i+1]
        s0, s1 = L[i], L[i+1]
        u = 0.0 if s1 == s0 else (s - s0) / (s1 - s0)
        out.append((
            a[0]*(1-u)+b[0]*u,
            a[1]*(1*u)+b[1]*u - 0.0*(1-u),  # standard LERP
            a[2]*(1-u)+b[2]*u
        ))
    return out

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

# --- optional X-densifier (kept for compatibility)
def densify_by_dx(points, max_dx):
    P = sorted(points, key=lambda p: p[0])
    if len(P) < 2: return P
    out = [P[0]]
    for i in range(len(P)-1):
        a, b = P[i], P[i+1]
        dx = max(1e-12, b[0]-a[0])
        n  = max(1, int(math.ceil(dx / max_dx)))
        for k in range(1, n+1):
            t = k / n
            out.append((a[0] + t*dx,
                        a[1]*(1-t) + b[1]*t,
                        a[2]*(1-t) + b[2]*t))
    return out

def smooth_row_keep_x(row, passes=2):
    if len(row) < 3 or passes <= 0: return row
    R = row[:]
    for _ in range(passes):
        N = [R[0]]
        for i in range(1, len(R)-1):
            x = R[i][0]
            y = (R[i-1][1] + R[i][1] + R[i+1][1]) / 3.0
            z = (R[i-1][2] + R[i][2] + R[i+1][2]) / 3.0
            N.append((x, y, z))
        N.append(R[-1])
        R = N
    return R


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
    # front + back
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

def _do_clean(bm, weld_dist, degenerate_dist):
    # micro + main weld & dissolve
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_dist * 0.25)
    bmesh.ops.dissolve_degenerate(bm, dist=degenerate_dist * 0.25)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_dist)
    bmesh.ops.dissolve_degenerate(bm, dist=degenerate_dist * 0.5)

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

def create_cylinders_z_aligned(holes, thickness, radius=0.0015875, embed_offset=0.0025):
    cylinders = []
    for h in holes:
        x, y, z = to_vec3(h)
        depth = float(thickness)
        center_z = z - (embed_offset + depth / 2.0)
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=(x, y, center_z))
        cyl = bpy.context.active_object
        cylinders.append(cyl)
    return cylinders

def apply_boolean_difference(target_obj, cutters):
    bpy.context.view_layer.objects.active = target_obj
    for cutter in cutters:
        mod = target_obj.modifiers.new(name="Boolean", type='BOOLEAN')
        mod.operation = 'DIFFERENCE'
        mod.object = cutter
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(cutter, do_unlink=True)


# ---------------------------
# Grid construction
# ---------------------------

def grid_to_tris(grid):
    """Convert a [rows][cols] vertex grid to triangle list (row-wise strips)."""
    if not grid: return []
    rows = len(grid)
    cols = min(len(r) for r in grid)
    faces = []
    for r in range(rows - 1):
        A = grid[r]
        B = grid[r + 1]
        for c in range(cols - 1):
            a = A[c]; b = A[c + 1]; c0 = B[c]; d = B[c + 1]
            faces.append([a, c0, b])
            faces.append([b, c0, d])
    return faces


# ---------------------------
# Build triangles (smooth top + edge roll + quarter-arc)
# ---------------------------

def build_triangles(beardline, neckline, params):
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    # ---- Params
    lip_segments    = int(params.get("lipSegments", 200))
    arc_steps       = int(params.get("arcSteps", 48))
    max_lip_radius  = float(params.get("maxLipRadius", 0.010))
    min_lip_radius  = float(params.get("minLipRadius", 0.0045))
    taper_mult      = float(params.get("taperMult", 20.0))
    extrusion_depth = float(params.get("extrusionDepth", -0.011))
    profile_bias    = float(params.get("profileBias", 2.2))   # speeds initial curvature
    prelift         = float(params.get("prelift", 0.0008))
    weld_eps        = float(params.get("weldEps", WELD_EPS_DEFAULT))

    # sampling & smoothing
    sample_mode     = str(params.get("sampleMode", "arclen")).lower()  # "arclen" | "x"
    base_smooth     = int(params.get("baseSmoothPasses", 3))
    lip_smooth_pass = int(params.get("lipSmoothPasses", 3))
    target_dx       = float(params.get("targetDx", 0.00045))  # only used for "x" mode

    # top-edge roll (continuous lip all around the top edge)
    edgeR           = float(params.get("edgeLipRadius", 0.0015))   # ~1.5 mm
    edgeAngDeg      = float(params.get("edgeLipAngleDeg", 35.0))   # roll angle
    edgeSteps       = int(params.get("edgeLipSteps", 4))           # rows for the roll

    # ---- 1) Build base row with smooth, arc-length sampling
    P = [tuple(p) for p in sorted(beardline, key=lambda p: p[0])]
    if sample_mode == "arclen":
        ts = [i / max(1, lip_segments - 1) for i in range(lip_segments)]
        base = _resample_by_t(P, ts)
        if base_smooth > 0:
            base = smooth_row_keep_x(base, passes=base_smooth)
        xs = [b[0] for b in base]  # for neckline resample
    else:
        # legacy X-regular grid
        base, minX, maxX = resample_polyline_by_x(P, [P[0][0] + (P[-1][0]-P[0][0])*i/max(1, lip_segments-1) for i in range(lip_segments)]), P[0][0], P[-1][0]
        if (maxX - minX) / max(1, len(base)-1) > target_dx:
            base = densify_by_dx(base, target_dx)
        if base_smooth > 0:
            base = smooth_row_keep_x(base, passes=base_smooth)
        xs = [b[0] for b in base]

    minX = min(b[0] for b in base); maxX = max(b[0] for b in base)
    centerX = 0.5 * (minX + maxX)

    # ---- 2) Optional neckline strap (resampled to the same parameter)
    neck_row = []
    if neckline:
        if sample_mode == "arclen":
            neck_row = _resample_by_t(sorted(neckline, key=lambda p: p[0]), [i/max(1, len(base)-1) for i in range(len(base))])
            neck_row = smooth_vertices_open(neck_row, passes=3)
        else:
            neck_row = resample_polyline_by_x(neckline, xs)
            neck_row = smooth_vertices_open(neck_row, passes=3)

    # ---- 3) Build grid rows:
    #   (a) edge roll ABOVE the base (small convex fillet)
    grid = []
    if edgeR > 0.0 and edgeSteps > 0 and edgeAngDeg > 0.0:
        phi = math.radians(max(0.0, min(85.0, edgeAngDeg)))  # clamp
        for k in range(edgeSteps, 0, -1):
            t = k / float(edgeSteps)
            ang = phi * t                                  # 0..phi
            row = []
            for (bx, by, bz) in base:
                y = by + edgeR * math.sin(ang)
                z = bz + edgeR * (1.0 - math.cos(ang))
                row.append((bx, y, z))
            grid.append(smooth_row_keep_x(row, passes=max(1, lip_smooth_pass//2)))

    #   (b) base row itself (top edge the slicer sees)
    grid.append(base)

    #   (c) main quarter-arc lip DOWN from base (smooth)
    for j in range(arc_steps):
        tt = (j + 1) / float(arc_steps + 1)               # (0,1]
        tt = max(1e-6, min(1.0, tt)) ** max(0.1, float(profile_bias))
        ang = 0.5 * math.pi * tt                          # 0 → π/2
        row = []
        for (bx, by, bz) in base:
            dx = abs(bx - centerX)
            taper = max(0.0, 1.0 - dx * taper_mult)
            r = min_lip_radius + taper * (max_lip_radius - min_lip_radius)
            y = by - r * math.sin(ang)                    # down the cheek
            z = bz + prelift * (1.0 - math.cos(ang))      # tiny prelift to avoid micro steps
            row.append((bx, y, z))
        grid.append(smooth_row_keep_x(row, passes=lip_smooth_pass))

    # ---- 4) Triangulate the grid
    faces = grid_to_tris(grid)

    # ---- 5) Optionally strap base↔neckline (shares the same base row vertices)
    if neck_row:
        A = base
        B = neck_row
        m = min(len(A), len(B))
        for i in range(m - 1):
            faces.append([A[i],   B[i],   A[i + 1]])
            faces.append([A[i+1], B[i],   B[i + 1]])

    # purge slivers
    faces = [tri for tri in faces if area2(tri[0], tri[1], tri[2]) > AREA_MIN]

    # ---- 6) Extrude to a watertight solid (front/back + side walls)
    extruded = extrude_surface_z_solid(faces, extrusion_depth, weld_eps=weld_eps)

    return extruded, abs(extrusion_depth), weld_eps


# ---------------------------
# IO / diagnostics / main pipeline
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

def mesh_diagnostics(obj):
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    nonman_edges = [e for e in bm.edges if len(e.link_faces) not in (1, 2)]
    boundary_edges = [e for e in bm.edges if len(e.link_faces) == 1]
    shortest = 1e9
    for e in bm.edges:
        try:
            shortest = min(shortest, float(e.calc_length()))
        except Exception:
            pass
    # duplicates quick check
    seen = set(); dup = 0
    for f in bm.faces:
        key = tuple(sorted(v.index for v in f.verts))
        if key in seen: dup += 1
        else: seen.add(key)
    bm.free()
    print(f"[diag] boundary={len(boundary_edges)} nonmanifold={len(nonman_edges)} "
          f"minEdge={shortest:.6f} m")
    print(f"[diag] duplicate_faces={dup}")
    return len(boundary_edges), len(nonman_edges), shortest

def ensure_watertight(obj, params):
    weld_eps = float(params.get("weldEps", WELD_EPS_DEFAULT))
    min_feature = float(params.get("minFeature", 0.0012))
    voxel_size  = float(params.get("voxelRemesh", 0.0))
    auto = bool(params.get("autoRemesh", True))

    b, n, shortest = mesh_diagnostics(obj)
    needs_fix = (b > 0 or n > 0 or shortest < min_feature * 0.25)

    if not auto:
        print("[fix] auto-remesh disabled (autoRemesh=false).")
        return

    if needs_fix:
        suggested = voxel_size if voxel_size > 0 else max(min_feature * 0.6, 0.0004)
        print(f"[fix] auto-remesh → voxel={suggested:.6f}")
        voxel_remesh_if_requested(obj, suggested)
        clean_mesh(obj, weld_eps, min_feature=min_feature, strong=True)
        mesh_diagnostics(obj)

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


# --- Param & input normalization ---------------------------------------------

def _lower_keys(obj):
    if isinstance(obj, dict):
        return { (k.lower() if isinstance(k, str) else k): _lower_keys(v) for k, v in obj.items() }
    if isinstance(obj, list):
        return [ _lower_keys(v) for v in obj ]
    return obj

def _unify_params(params_any):
    params_any = params_any or {}
    params_lc = { (k.lower() if isinstance(k, str) else k): v for k, v in params_any.items() }
    out = dict(params_any)
    def use(cam, lc):
        if cam not in out and lc in params_lc:
            out[cam] = params_lc[lc]
    use("lipSegments",    "lipsegments")
    use("arcSteps",       "arcsteps")
    use("maxLipRadius",   "maxlipradius")
    use("minLipRadius",   "minlipradius")
    use("taperMult",      "tapermult")
    use("extrusionDepth", "extrusiondepth")
    use("weldEps",        "weldeps")
    use("minFeature",     "minfeature")
    use("voxelRemesh",    "voxelsize")
    use("embedOffset",    "embedoffset")
    use("holeRadius",     "holeradius")
    use("profileBias",    "profilebias")
    use("prelift",        "prelift")
    use("targetDx",       "targetdx")
    use("lipSmoothPasses","lipsmoothpasses")
    use("baseSmoothPasses","basesmoothpasses")
    use("sampleMode",     "samplemode")
    use("edgeLipRadius",  "edgelipradius")
    use("edgeLipAngleDeg","edgelipangledeg")
    use("edgeLipSteps",   "edgelipsteps")
    use("autoRemesh",     "autoremessh")
    return out

def _snap_close_endpoints(sorted_pts, tol=1e-4):
    if len(sorted_pts) > 2:
        a, b = sorted_pts[0], sorted_pts[-1]
        dx = a[0]-b[0]; dy = a[1]-b[1]; dz = a[2]-b[2]
        if (dx*dx + dy*dy + dz*dz) ** 0.5 < tol:
            sorted_pts[-1] = (a[0], a[1], a[2])
    return sorted_pts


# --- Main --------------------------------------------------------------------

def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    if len(argv) != 2:
        raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

    with open(input_path, 'r') as f:
        data = json.load(f)

    data_lc = _lower_keys(data)

    # Accept both 'beardline' and legacy 'vertices', any casing
    beardline_in = data.get("beardline") or data.get("vertices") \
                   or data_lc.get("beardline") or data_lc.get("vertices")
    if beardline_in is None:
        raise ValueError("Missing 'beardline' (or legacy 'vertices') in payload.")
    beardline = sorted([to_vec3(v) for v in beardline_in], key=lambda p: p[0])
    beardline = _snap_close_endpoints(beardline, tol=1e-4)

    neckline_in = data.get("neckline") or data_lc.get("neckline")
    neckline = [to_vec3(v) for v in neckline_in] if neckline_in else []

    holes_in = data.get("holeCenters") or data.get("holes") \
               or data_lc.get("holecenters") or data_lc.get("holes") or []

    params_any = data.get("params") or data_lc.get("params") or {}
    params = _unify_params(params_any)

    tris, thickness, weld_eps = build_triangles(beardline, neckline, params)

    mf_param = params.get("minFeature")
    mold_obj = make_mesh_from_tris(tris, name="BeardMold", weld_eps=weld_eps)

    # First clean
    clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=False)

    # Optional direct remesh + clean again (if param set explicitly)
    voxel_size = float(params.get("voxelRemesh", VOXEL_DEFAULT))
    voxel_remesh_if_requested(mold_obj, voxel_size)
    if voxel_size > 0:
        clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=True)

    # Holes → boolean → clean again
    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference(mold_obj, cutters)
        clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=True)

    # Force watertight if needed (autoRemesh may escalate)
    ensure_watertight(mold_obj, params)
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
        f"weld_eps={weld_eps} "
        f"voxel={voxel_size}"
    )

if __name__ == "__main__":
    main()
