# path: tools/beard_mold_fix.py
# Python 3.x • Blender 3.x API

import bpy
import bmesh
import json
import sys
import math
from mathutils import Vector
import bisect

# ---------------------------
# Helpers (geometry & math)
# ---------------------------

def to_vec3(p):
    return (float(p['x']), float(p['y']), float(p['z']))


def dist2(a, b):
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return dx * dx + dy * dy + dz * dz


def smooth_vertices_open(vertices, passes=1):
    """Moving-average smoothing for an open polyline (preserve endpoints)."""
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
    """Monotonic-in-X resampling using linear interpolation to avoid jitter.
    If multiple beardline points share the same X, they are averaged.
    """
    if not beardline:
        return [], -0.05, 0.05

    # group by X and average Y,Z for duplicates
    by_x = {}
    for x, y, z in beardline:
        acc = by_x.get(x)
        if acc is None:
            by_x[x] = [y, z, 1]
        else:
            acc[0] += y
            acc[1] += z
            acc[2] += 1
    uniq = []
    for x, (sy, sz, c) in by_x.items():
        uniq.append((x, sy / c, sz / c))

    uniq.sort(key=lambda p: p[0])
    if len(uniq) == 1:
        x, y, z = uniq[0]
        return [(x, y, z)] * lip_segments, x, x

    xs = [p[0] for p in uniq]
    minX, maxX = xs[0], xs[-1]
    seg_w = (maxX - minX) / max(1, (lip_segments - 1))

    base = []
    for i in range(lip_segments):
        x = minX + i * seg_w
        j = bisect.bisect_left(xs, x)
        if j == 0:
            base.append(uniq[0])
            continue
        if j >= len(uniq):
            base.append(uniq[-1])
            continue
        x0, y0, z0 = uniq[j - 1]
        x1, y1, z1 = uniq[j]
        t = 0.0 if abs(x1 - x0) < 1e-9 else (x - x0) / (x1 - x0)
        y = y0 * (1 - t) + y1 * t
        z = z0 * (1 - t) + z1 * t
        base.append((x, y, z))

    return base, minX, maxX


def tapered_radius(x, centerX, min_r, max_r, taper_mult):
    taper = max(0.0, 1.0 - abs(x - centerX) * taper_mult)
    return min_r + taper * (max_r - min_r)


def generate_lip_rings(base_points, arc_steps, min_r, max_r, centerX, taper_mult):
    """Return (lip_vertices, ring_count). Each base point gets a semicircle ring in YZ plane."""
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


def skin_beardline_to_neckline(beardline, neckline):
    """Triangulate between consecutive beardline points and their nearest neckline points (Swift parity)."""
    faces = []
    if len(beardline) < 2 or not neckline:
        return faces
    for i in range(len(beardline) - 1):
        b0 = beardline[i]
        b1 = beardline[i + 1]
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
        b = base_points[i + 1]
        c = lip_vertices[i * ring_count + 0]
        d = lip_vertices[(i + 1) * ring_count + 0]
        faces.append([a, c, b])
        faces.append([b, c, d])
    return faces


def end_caps(lip_vertices, base_points, ring_count):
    faces = []
    if not base_points:
        return faces
    first_base = base_points[0]
    for i in range(ring_count - 1):
        a = lip_vertices[i]
        b = lip_vertices[i + 1]
        faces.append([a, b, first_base])
    last_base = base_points[-1]
    start_idx = (len(base_points) - 1) * ring_count
    for i in range(ring_count - 1):
        a = lip_vertices[start_idx + i]
        b = lip_vertices[start_idx + i + 1]
        faces.append([a, b, last_base])
    return faces

# ----------------------------------------------------------------------
# Solid, manifold extrusion (reworked)
# ----------------------------------------------------------------------

def _round_decimals_from_tol(tol: float) -> int:
    if tol <= 0:
        return 6
    # clamp to a reasonable range to avoid huge round() work
    dec = max(0, min(8, int(round(-math.log10(max(1e-9, tol))))))
    return dec


def extrude_surface_z_solid(tri_faces, depth, weld_tol=1e-5):
    """Build a closed shell by offsetting along Z and stitching boundary edges.

    * Welds input vertices within `weld_tol` during indexing.
    * Drops degenerate input triangles.
    * Adds front, back and side faces; side faces only on true boundary edges.
    """

    def area2(a, b, c):
        ax, ay, az = a
        bx, by, bz = b
        cx, cy, cz = c
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        cxp = (uy * vz - uz * vy)
        cyp = (uz * vx - ux * vz)
        czp = (ux * vy - uy * vx)
        return cxp * cxp + cyp * cyp + czp * czp

    decimals = _round_decimals_from_tol(weld_tol)

    def key(p):
        return (round(p[0], decimals), round(p[1], decimals), round(p[2], decimals))

    v2i = {}
    verts = []
    tris_idx = []

    def idx_of(p):
        k = key(p)
        i = v2i.get(k)
        if i is None:
            i = len(verts)
            v2i[k] = i
            verts.append(k)
        return i

    for a, b, c in tri_faces:
        if area2(a, b, c) <= 1e-18:
            continue
        ia, ib, ic = idx_of(a), idx_of(b), idx_of(c)
        if len({ia, ib, ic}) == 3:
            tris_idx.append((ia, ib, ic))

    if not tris_idx:
        return []

    edge_count = {}
    edge_dir = {}
    for ia, ib, ic in tris_idx:
        for (u, v) in ((ia, ib), (ib, ic), (ic, ia)):
            ue = (min(u, v), max(u, v))
            edge_count[ue] = edge_count.get(ue, 0) + 1
            if ue not in edge_dir:
                edge_dir[ue] = (u, v)  # store a consistent direction

    boundary = [ue for ue, c in edge_count.items() if c == 1]

    back_offset = len(verts)
    back_verts = [(x, y, z + depth) for (x, y, z) in verts]

    out = []
    # front faces as-is
    for ia, ib, ic in tris_idx:
        out.append((verts[ia], verts[ib], verts[ic]))
    # back faces reversed
    for ia, ib, ic in tris_idx:
        ja, jb, jc = ia + back_offset, ib + back_offset, ic + back_offset
        out.append((back_verts[jc - back_offset], back_verts[jb - back_offset], back_verts[ja - back_offset]))

    # side walls for each boundary edge
    for ue in boundary:
        u, v = edge_dir[ue]
        ju, jv = u + back_offset, v + back_offset
        # Two tris forming the quad; winding keeps outward normal consistent
        out.append((verts[u], verts[v], back_verts[jv - back_offset]))
        out.append((verts[u], back_verts[jv - back_offset], back_verts[ju - back_offset]))

    return out


def make_mesh_from_tris(tris, name="MoldMesh", weld_tol=1e-5, cap_holes=True):
    """Create a Blender mesh from triangles and enforce manifoldness via bmesh.

    `weld_tol` removes tiny gaps. `cap_holes` fills any remaining boundary loops.
    """
    if not tris:
        raise ValueError("No triangles to build mesh.")

    decimals = _round_decimals_from_tol(weld_tol)

    def key(p):
        return (round(p[0], decimals), round(p[1], decimals), round(p[2], decimals))

    v2i = {}
    verts = []
    faces = []

    for (a, b, c) in tris:
        ids = []
        for p in (a, b, c):
            k = key(p)
            if k not in v2i:
                v2i[k] = len(verts)
                verts.append(k)
            ids.append(v2i[k])
        if len({ids[0], ids[1], ids[2]}) == 3:
            faces.append(tuple(ids))

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts], [], faces)
    mesh.validate(verbose=False)
    mesh.update()

    # Manifold enforcement
    bm = bmesh.new()
    bm.from_mesh(mesh)
    try:
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_tol)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        if cap_holes:
            boundary_edges = [e for e in bm.edges if e.is_boundary]
            if boundary_edges:
                bmesh.ops.holes_fill(bm, edges=boundary_edges, sides=0)
        bm.normal_update()
        bm.to_mesh(mesh)
    finally:
        bm.free()

    mesh.validate(verbose=False)
    mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)

    try:
        bpy.context.view_layer.objects.active = obj
        for o in bpy.data.objects:
            o.select_set(False)
        obj.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        pass

    return obj


def create_cylinders_z_aligned(holes, thickness, radius=0.0015875, embed_offset=0.0025):
    cylinders = []
    for h in holes:
        x, y, z = to_vec3(h)
        depth = float(thickness)
        center_z = z - (embed_offset + depth / 2.0)
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=(x, y, center_z))
        cylinders.append(bpy.context.active_object)
    return cylinders


def apply_boolean_difference(target_obj, cutters):
    bpy.context.view_layer.objects.active = target_obj
    for cutter in cutters:
        try:
            mod = target_obj.modifiers.new(name="Boolean", type='BOOLEAN')
            mod.operation = 'DIFFERENCE'
            # Using EXACT avoids thin-gap artifacts from 'FAST' on complex shells
            try:
                mod.solver = 'EXACT'
            except Exception:
                pass
            mod.object = cutter
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception:
            pass
        finally:
            try:
                bpy.data.objects.remove(cutter, do_unlink=True)
            except Exception:
                pass

# ---------------------------
# Pipeline helpers
# ---------------------------

def sanitize_params(params):
    lip_segments = max(2, int(params.get("lipSegments", 100)))
    arc_steps = max(1, int(params.get("arcSteps", 24)))
    max_lip_radius = abs(float(params.get("maxLipRadius", 0.008)))
    min_lip_radius = abs(float(params.get("minLipRadius", 0.003)))
    if min_lip_radius > max_lip_radius:
        min_lip_radius, max_lip_radius = max_lip_radius, min_lip_radius
    taper_mult = max(0.0, float(params.get("taperMult", 25.0)))
    extrusion_depth = float(params.get("extrusionDepth", -0.008))
    if abs(extrusion_depth) < 1e-6:
        extrusion_depth = -0.002
    base_smooth = max(0, int(params.get("baseSmoothPasses", 1)))
    voxel = float(params.get("voxelSize", 0.0) or 0.0)
    weld_tol = float(params.get("weldTol", 1e-5))
    cap_holes = bool(params.get("capHoles", True))
    return {
        "lip_segments": lip_segments,
        "arc_steps": arc_steps,
        "max_lip_radius": max_lip_radius,
        "min_lip_radius": min_lip_radius,
        "taper_mult": taper_mult,
        "extrusion_depth": extrusion_depth,
        "base_smooth": base_smooth,
        "voxel": voxel,
        "weld_tol": weld_tol,
        "cap_holes": cap_holes,
    }


def triangle_area2(a, b, c):
    ax, ay, az = a
    bx, by, bz = b
    cx, cy, cz = c
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    cxp = (uy * vz - uz * vy)
    cyp = (uz * vx - ux * vz)
    czp = (ux * vy - uy * vx)
    return cxp * cxp + cyp * cyp + czp * czp


def remove_degenerate_tris(tris, eps=1e-18):
    out = []
    for a, b, c in tris:
        if triangle_area2(a, b, c) > eps:
            out.append((a, b, c))
    return out

# ---------------------------
# Main pipeline helpers (bridging)
# ---------------------------

def bridge_polylines_mono_x(poly_a, poly_b):
    """Triangulate a zipper strip between two open polylines sorted by X.
    Keeps surfaces connected to avoid T-junction gaps between base resample and original beardline.
    """
    if len(poly_a) < 2 or len(poly_b) < 2:
        return []
    A = sorted(poly_a, key=lambda p: p[0])
    B = sorted(poly_b, key=lambda p: p[0])
    i, j = 0, 0
    faces = []
    while i < len(A) - 1 and j < len(B) - 1:
        ax0, *_ = A[i]
        ax1, *_ = A[i + 1]
        bx0, *_ = B[j]
        bx1, *_ = B[j + 1]
        if ax1 <= bx1:
            faces.append([A[i], B[j], A[i + 1]])
            i += 1
        else:
            faces.append([A[i], B[j], B[j + 1]])
            j += 1
    # fan out remainder
    while i < len(A) - 1:
        faces.append([A[i], B[-1], A[i + 1]])
        i += 1
    while j < len(B) - 1:
        faces.append([A[-1], B[j], B[j + 1]])
        j += 1
    return faces

# ---------------------------
# Main pipeline
# ---------------------------

def build_triangles(beardline, neckline, params):
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    P = sanitize_params(params)

    base_points, minX, maxX = sample_base_points_along_x(beardline, P["lip_segments"])
    if P["base_smooth"] > 0:
        base_points = smooth_vertices_open(base_points, passes=P["base_smooth"])

    centerX = 0.5 * (minX + maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points,
        P["arc_steps"],
        P["min_lip_radius"],
        P["max_lip_radius"],
        centerX,
        P["taper_mult"],
    )

    faces = quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    # Bridge original beardline to resampled base to avoid gaps between strips
    faces += bridge_polylines_mono_x(beardline, base_points)

    if neckline:
        faces += skin_beardline_to_neckline(beardline, neckline)

    faces += stitch_first_column_to_base(base_points, lip_vertices, ring_count)
    faces += end_caps(lip_vertices, base_points, ring_count)

    faces = remove_degenerate_tris(faces)
    extruded = extrude_surface_z_solid(faces, P["extrusion_depth"], weld_tol=P["weld_tol"])  # closed shell

    return extruded, abs(P["extrusion_depth"]), P


def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)


def safe_print_summary(data, beardline, neckline, holes, mesh_stats):
    job = data.get('jobID', 'N/A')
    overlay = data.get('overlay', 'N/A')
    n_b = len(beardline)
    n_n = len(neckline)
    n_h = len(holes)
    n_verts, n_faces, n_boundary = mesh_stats
    msg = (
        f"STL export complete for job ID: {job} "
        f"overlay: {overlay} "
        f"verts(beardline)={n_b} neckline={n_n} holes={n_h} "
        f"meshVerts={n_verts} meshFaces={n_faces} boundaryEdges={n_boundary}"
    )
    print(msg)


def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
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

    tris, thickness, P = build_triangles(beardline, neckline, params)

    mold_obj = make_mesh_from_tris(tris, name="BeardMold", weld_tol=P["weld_tol"], cap_holes=P["cap_holes"])

    for obj in bpy.data.objects:
        obj.select_set(False)
    mold_obj.select_set(True)

    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference(mold_obj, cutters)

    # Optional voxel remesh to seal micro-gaps
    if P["voxel"] > 0.0:
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
            bpy.context.view_layer.objects.active = mold_obj
            mold_obj.select_set(True)
            bpy.ops.object.voxel_remesh(voxel_size=P["voxel"], adaptivity=0.0, half_res=False)
        except Exception:
            pass

    export_stl_selected(output_path)

    # Diagnostics
    n_verts = n_faces = n_boundary = -1
    try:
        mesh = mold_obj.data
        n_verts = len(mesh.vertices)
        n_faces = len(mesh.polygons)
        edge_face_counts = {tuple(sorted(e.vertices)): 0 for e in mesh.edges}
        for poly in mesh.polygons:
            idxs = list(poly.vertices)
            for i in range(len(idxs)):
                e = tuple(sorted((idxs[i], idxs[(i + 1) % len(idxs)])))
                if e in edge_face_counts:
                    edge_face_counts[e] += 1
        n_boundary = sum(1 for c in edge_face_counts.values() if c == 1)
    except Exception:
        pass

    safe_print_summary(data, beardline, neckline, holes_in, (n_verts, n_faces, n_boundary))


if __name__ == "__main__":
    main()
