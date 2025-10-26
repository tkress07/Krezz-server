# -*- coding: utf-8 -*-
# Beard Mold Generator (Swift-parity edition)
# - Base points sampled "closest-by-X keep original (x,y,z)"
# - Strap triangles use nearest-neck points per beardline segment
# - Conservative consolidation (no dissolve_limit), avoids slivers
#
# Usage (same as before):
# blender -b -P this_file.py -- /path/in.json /path/out.stl

import bpy
import bmesh
import json
import sys
import math
from mathutils import Vector

# ========= Tunables (good defaults for ~0.4 mm nozzle) =========
WELD_EPS_DEFAULT  = 0.00025       # shared-vertex tolerance (meters)
AREA_MIN          = 5e-13         # cull razor-thin triangles
VOXEL_DEFAULT     = 0.0           # OFF by default (server param can enable)
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


# ---------------------------
# Swift-parity sampling / strap
# ---------------------------

def base_points_swift_style(beardline, lip_segments):
    """
    Match the Swift path:
    - The beardline provided is already ordered the way the UI drew it.
    - Build a uniform X grid on [minX,maxX]; for each X pick the original
      beardline point whose X is closest; keep its original (x,y,z).
    - No resorting or interpolation that can re-order the path.
    """
    if not beardline or lip_segments <= 1:
        xs = [p[0] for p in (beardline or [])]
        minX = min(xs) if xs else 0.0
        maxX = max(xs) if xs else 0.0
        return beardline[:], minX, maxX

    xs_all = [p[0] for p in beardline]
    minX, maxX = min(xs_all), max(xs_all)
    step = (maxX - minX) / max(1, lip_segments - 1)

    out = []
    for i in range(lip_segments):
        x = minX + i * step
        p = min(beardline, key=lambda q: abs(q[0] - x))
        out.append(p)  # keep original
    return out, minX, maxX


def strap_tris_nearest(beardline, neckline):
    """
    Match Swift's nearest-neck strap:
    For each consecutive beardline pair b[i], b[i+1]:
      n0 = nearest(neckline, b[i])
      n1 = nearest(neckline, b[i+1])
      faces += [b0, n0, b1], [n0, n1, b1]
    """
    faces = []
    if not beardline or not neckline:
        return faces

    def nearest(p):
        px, py, pz = p
        best = None
        bd = 1e30
        for q in neckline:
            dx = q[0] - px; dy = q[1] - py; dz = q[2] - pz
            d2 = dx*dx + dy*dy + dz*dz
            if d2 < bd:
                bd = d2; best = q
        return best

    for i in range(len(beardline) - 1):
        b0, b1 = beardline[i], beardline[i+1]
        n0 = nearest(b0)
        n1 = nearest(b1)
        faces.append([b0, n0, b1])
        faces.append([n0, n1, b1])
    return faces


# ---------------------------
# Lip profile / ring mesh
# ---------------------------

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


# ---------------------------
# Build Blender mesh from triangles
# ---------------------------

def make_mesh_from_tris(tris, name="MoldMesh", weld_eps=WELD_EPS_DEFAULT):
    """Build an object from triangle coordinate tuples, removing duplicate faces."""
    v2i, verts, faces_idx = {}, [], []

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
            faces_idx.append(tuple(ids))

    # remove duplicate faces (same vertex set)
    uniq, out_faces = set(), []
    for (i, j, k) in faces_idx:
        fkey = tuple(sorted((i, j, k)))
        if fkey in uniq:
            continue
        uniq.add(fkey)
        out_faces.append((i, j, k))

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts], [], out_faces)
    mesh.validate(verbose=True); mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


# ---------------------------
# Mesh cleaning / boolean / diagnostics
# ---------------------------

def _do_clean(bm, weld_dist, degenerate_dist):
    # micro + main weld & dissolve
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_dist * 0.25)
    bmesh.ops.dissolve_degenerate(bm, dist=max(degenerate_dist * 0.25, 1e-7))
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_dist)
    bmesh.ops.dissolve_degenerate(bm, dist=max(degenerate_dist * 0.5, 1e-7))

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
    bm.free()
    print(f"[diag] boundary={len(boundary_edges)} nonmanifold={len(nonman_edges)} minEdge={shortest:.6f} m")
    return len(boundary_edges), len(nonman_edges), shortest

def count_duplicate_faces(obj):
    me = obj.data
    bm = bmesh.new(); bm.from_mesh(me)
    bm.verts.ensure_lookup_table(); bm.faces.ensure_lookup_table()
    seen, dup = set(), 0
    for f in bm.faces:
        key = tuple(sorted(v.index for v in f.verts))
        if key in seen: dup += 1
        else: seen.add(key)
    bm.free()
    print(f"[diag] duplicate_faces={dup}")
    return dup

def report_all(obj):
    mesh_diagnostics(obj)
    count_duplicate_faces(obj)
    z_top = max(v.co.z for v in obj.data.vertices)
    print(f"[diag] z_top={z_top:.6f}")


# ---------------------------
# Conservative consolidation (pre-extrusion)
# ---------------------------

def consolidate_front_sheet(faces, weld_eps, min_feature):
    """
    Conservative: weld + dissolve_degenerate + triangulate.
    Avoid dissolve_limit to prevent collapsing strap facets.
    """
    bm = bmesh.new()
    vmap = {}

    def v_for(p):
        k = _rounded_key(p, weld_eps)
        v = vmap.get(k)
        if v is None:
            v = bm.verts.new(Vector(k))
            vmap[k] = v
        return v

    for (a, b, c) in faces:
        va, vb, vc = v_for(a), v_for(b), v_for(c)
        try:
            bm.faces.new([va, vb, vc])
        except ValueError:
            pass

    bm.verts.ensure_lookup_table(); bm.edges.ensure_lookup_table(); bm.faces.ensure_lookup_table()

    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_eps)
    bmesh.ops.dissolve_degenerate(bm, dist=max(min_feature * 0.25, 1e-7))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.triangulate(bm, faces=bm.faces)

    tris = []
    for f in bm.faces:
        if len(f.verts) == 3:
            a, b, c = f.verts
            tris.append((tuple(a.co), tuple(b.co), tuple(c.co)))
    bm.free()
    return tris


# ---------------------------
# Build triangles (Swift parity)
# ---------------------------

def build_triangles(beardline, neckline, params):
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    lip_segments    = int(params.get("lipSegments", 220))
    arc_steps       = int(params.get("arcSteps", 48))
    max_lip_radius  = float(params.get("maxLipRadius", 0.010))
    min_lip_radius  = float(params.get("minLipRadius", 0.0045))
    taper_mult      = float(params.get("taperMult", 20.0))
    extrusion_depth = float(params.get("extrusionDepth", -0.010))
    weld_eps        = float(params.get("weldEps", WELD_EPS_DEFAULT))
    min_feature     = float(params.get("minFeature", 0.0012))

    # 1) Base points like Swift (no resorting, keep originals)
    base_points, minX, maxX = base_points_swift_style(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    # 2) Lip rings + quads
    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult
    )
    faces = []
    faces += quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    # 2b) Cap basePoints ↔ ring0
    for i in range(len(base_points) - 1):
        a = base_points[i]
        b = base_points[i + 1]
        c = lip_vertices[i * ring_count + 0]
        d = lip_vertices[(i + 1) * ring_count + 0]
        faces.append([a, c, b])
        faces.append([b, c, d])

    # 3) Strap to neckline using nearest-neighbor (Swift behavior)
    if neckline:
        faces += strap_tris_nearest(beardline, neckline)

    # 4) Consolidate and extrude
    faces = [tri for tri in faces if area2(tri[0], tri[1], tri[2]) > AREA_MIN]
    front = consolidate_front_sheet(faces, weld_eps=weld_eps, min_feature=min_feature)
    extruded = extrude_surface_z_solid(front, extrusion_depth, weld_eps=weld_eps)

    return extruded, abs(extrusion_depth), weld_eps


# ---------------------------
# IO / params helpers
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
    if "voxelSize" in out and "voxelRemesh" not in out:
        out["voxelRemesh"] = out["voxelSize"]
    elif "voxelsize" in params_lc and "voxelRemesh" not in out:
        out["voxelRemesh"] = params_lc["voxelsize"]
    use("embedOffset",    "embedoffset")
    use("holeRadius",     "holeradius")
    use("autoRemesh",     "autoremesh")
    use("neckSmoothPasses","necksmoothpasses")
    return out


# ---------------------------
# Main
# ---------------------------

def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    if len(argv) != 2:
        raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

    with open(input_path, 'r') as f:
        data = json.load(f)

    data_lc = _lower_keys(data)

    # Prefer 'beardline' but accept legacy 'vertices'
    beardline_in = data.get("beardline") or data.get("vertices") \
                   or data_lc.get("beardline") or data_lc.get("vertices")
    if not beardline_in:
        raise ValueError("No vertices provided (missing 'beardline'/'vertices').")
    beardline = [to_vec3(v) for v in beardline_in]  # keep provided order

    # Neckline optional (already computed in-app from shared connections)
    neckline_in = data.get("neckline") or data_lc.get("neckline") or []
    neckline = [to_vec3(v) for v in neckline_in]

    # Optional smoothing on neckline only (mirrors your Swift smoothing)
    params = _unify_params(data.get("params") or data_lc.get("params") or {})
    neck_passes = int(params.get("neckSmoothPasses", 3))
    if neckline and neck_passes > 0:
        neckline = smooth_vertices_open(neckline, passes=neck_passes)

    # Holes
    holes_in = data.get("holeCenters") or data.get("holes") \
               or data_lc.get("holecenters") or data_lc.get("holes") or []

    # Triangles
    tris, thickness, weld_eps = build_triangles(beardline, neckline, params)

    # Build object & clean
    mold_obj = make_mesh_from_tris(tris, name="BeardMold", weld_eps=weld_eps)
    clean_mesh(mold_obj, weld_eps, min_feature=params.get("minFeature", 0.0012), strong=False)
    clean_mesh(mold_obj, weld_eps, min_feature=params.get("minFeature", 0.0012), strong=True)

    # Optional remesh
    voxel_size = float(params.get("voxelRemesh", VOXEL_DEFAULT))
    voxel_remesh_if_requested(mold_obj, voxel_size)
    if voxel_size > 0:
        clean_mesh(mold_obj, weld_eps, min_feature=params.get("minFeature", 0.0012), strong=True)

    # Holes → boolean → clean
    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference(mold_obj, cutters)
        clean_mesh(mold_obj, weld_eps, min_feature=params.get("minFeature", 0.0012), strong=True)

    # Diagnostics
    report_all(mold_obj)

    # Export selected
    for obj in bpy.data.objects:
        obj.select_set(False)
    mold_obj.select_set(True)
    bpy.context.view_layer.objects.active = mold_obj
    export_stl_selected(output_path)

    print(
        f"STL export complete for job ID: {data.get('job_id', data.get('jobID','N/A'))} "
        f"overlay: {data.get('overlay','N/A')} "
        f"beardline_pts={len(beardline)} neckline_pts={len(neckline)} "
        f"holes={len(holes_in)} weld_eps={weld_eps} voxel={voxel_size}"
    )


if __name__ == "__main__":
    main()
