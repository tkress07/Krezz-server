# path: tools/beard_mold_fix.py
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


def dist2(a, b):
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return dx * dx + dy * dy + dz * dz


def area2(a, b, c):
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cx = ab[1] * ac[2] - ab[2] * ac[1]
    cy = ab[2] * ac[0] - ab[0] * ac[2]
    cz = ab[0] * ac[1] - ab[1] * ac[0]
    return cx * cx + cy * cy + cz * cz


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
    xs = [p[0] for p in beardline]
    ys = [p[1] for p in beardline]
    zs = [p[2] for p in beardline]
    minX = min(xs) if xs else -0.05
    maxX = max(xs) if xs else 0.05
    seg_w = (maxX - minX) / max(1, (lip_segments - 1))
    fallbackY = max(ys) if ys else 0.03
    fallbackZ = (sum(zs) / len(zs)) if zs else 0.0

    base = []
    for i in range(lip_segments):
        x = minX + i * seg_w
        top = min(beardline, key=lambda p: abs(p[0] - x)) if beardline else None
        if top is not None:
            base.append((x, top[1], top[2]))
        else:
            base.append((x, fallbackY, fallbackZ))
    return base, minX, maxX


def tapered_radius(x, centerX, min_r, max_r, taper_mult):
    taper = max(0.0, 1.0 - abs(x - centerX) * taper_mult)
    return min_r + taper * (max_r - min_r)


# ---------------------------
# KEY CHANGE 1: steeper lip profile near start using bias + optional pre-lift
# ---------------------------

def generate_lip_rings(base_points, arc_steps, min_r, max_r, centerX, taper_mult, *, profile_bias=1.0, prelift=0.0):
    """Return (lip_vertices, ring_count).

    "profile_bias" > 1.0 concentrates angles near 0, making the initial slope
    steeper (reduces long shallow overhangs that cause slicer gaps).
    "prelift" (meters) lifts the ring start slightly in +Z before the arc
    begins, forcing a short near-vertical segment.
    """
    ring_count = arc_steps + 1
    verts = []
    for (bx, by, bz) in base_points:
        r = tapered_radius(bx, centerX, min_r, max_r, taper_mult)
        for j in range(ring_count):
            t = j / float(arc_steps)
            t = pow(t, max(1.0, float(profile_bias)))
            angle = math.pi * t
            y = by - r * (1.0 - math.sin(angle))
            z = bz + prelift + r * math.cos(angle)
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
    """Column 0 of semicircle rings along X."""
    return [lip_vertices[i * ring_count + 0] for i in range(base_count)]

# ----------------------------------------------------------------------
# NEW: robust strap: resample both beardline *and* neckline to shared Xs
# ----------------------------------------------------------------------

def resample_polyline_by_x(points, xs):
    if not points:
        return [(x, 0.0, 0.0) for x in xs]
    P = sorted(points, key=lambda p: p[0])
    out = []
    k = 0
    n = len(P)
    for x in xs:
        if x <= P[0][0]:
            out.append((x, P[0][1], P[0][2]))
            continue
        if x >= P[-1][0]:
            out.append((x, P[-1][1], P[-1][2]))
            continue
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

# ----------------------------------------------------------------------
# Solid, manifold extrusion
# ----------------------------------------------------------------------

def extrude_surface_z_solid(tri_faces, depth):
    """Extrude a triangle surface by `depth` along +Z and close only boundary edges.
    Keeps a single welded vertex set up front to avoid T-junction pinholes."""
    key = lambda p: (round(p[0], 6), round(p[1], 6), round(p[2], 6))
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
        ia = idx_of(a)
        ib = idx_of(b)
        ic = idx_of(c)
        tris_idx.append((ia, ib, ic))

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
    for ia, ib, ic in tris_idx:
        out.append((verts[ia], verts[ib], verts[ic]))
        ja, jb, jc = ia + back_offset, ib + back_offset, ic + back_offset
        out.append((back_verts[jc - back_offset], back_verts[jb - back_offset], back_verts[ja - back_offset]))

    for ue in boundary:
        u, v = edge_dir[ue]
        ju, jv = u + back_offset, v + back_offset
        out.append((verts[u], verts[v], back_verts[jv - back_offset]))
        out.append((verts[u], back_verts[jv - back_offset], back_verts[ju - back_offset]))

    return out


def make_mesh_from_tris(tris, name="MoldMesh"):
    """Create mesh, then clean to guarantee watertightness for slicing."""
    v2i = {}
    verts = []
    faces = []

    def key(p):
        return (round(p[0], 6), round(p[1], 6), round(p[2], 6))

    for (a, b, c) in tris:
        ids = []
        for p in (a, b, c):
            k = key(p)
            if k not in v2i:
                v2i[k] = len(verts)
                verts.append(k)
            ids.append(v2i[k])
        if area2(verts[ids[0]], verts[ids[1]], verts[ids[2]]) > 1e-18:
            faces.append(tuple(ids))

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts], [], faces)
    mesh.validate(verbose=False)
    mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)

    try:
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
        bmesh.ops.dissolve_degenerate(bm, dist=1e-7)
        boundary_edges = [e for e in bm.edges if len(e.link_faces) == 1]
        if boundary_edges:
            bmesh.ops.holes_fill(bm, edges=boundary_edges)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bmesh.ops.triangulate(bm, faces=bm.faces)
        bm.to_mesh(mesh)
        bm.free()
        mesh.validate(verbose=False)
        mesh.update()
    except Exception:
        pass

    try:
        view = bpy.context.view_layer
        view.objects.active = obj
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


# ---------------------------
# NEW: anchor ribs to support shallow overhang skin from inside
# ---------------------------

def apply_boolean_union(target_obj, cutters):
    bpy.context.view_layer.objects.active = target_obj
    for cutter in cutters:
        mod = target_obj.modifiers.new(name="Union", type='BOOLEAN')
        mod.operation = 'UNION'
        mod.solver = 'EXACT'
        mod.object = cutter
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception:
            # Fallback: leave modifier unapplied if apply fails
            pass
        try:
            bpy.data.objects.remove(cutter, do_unlink=True)
        except Exception:
            pass


def add_anchor_ribs(mold_obj, xs, params, thickness, lip_band_y):
    """Add thin internal ribs just under the top skin along X to anchor top lines.
    "why": anchors eliminate slicer gap-fill misses over long shallow overhangs.
    """
    if not bool(params.get("enableAnchorRibs", False)):
        return

    rib_spacing = float(params.get("ribSpacing", 0.006))          # 6 mm
    rib_thickness = float(params.get("ribThickness", 0.0006))      # 0.6 mm
    rib_depth = float(params.get("ribDepth", 0.0012))              # 1.2 mm
    rib_z_offset = float(params.get("ribZOffset", 0.0004))         # 0.4 mm below top
    rib_band_y = float(params.get("ribBandY", 0.004))              # band width in Y (4 mm)
    rib_y_shift = float(params.get("ribYShift", 0.0))              # shift band center

    # Bounds and top Z
    co = [mold_obj.matrix_world @ v.co for v in mold_obj.data.vertices]
    minx = min(c.x for c in co)
    maxx = max(c.x for c in co)
    miny = min(c.y for c in co)
    maxy = max(c.y for c in co)
    topz = max(c.z for c in co)

    # Center band near the lip's first column
    band_y_center = lip_band_y + rib_y_shift

    # Create ribs as thin cubes and UNION them into the mold
    cutters = []
    x = minx + rib_spacing * 0.5
    while x < maxx:
        cx = x
        cy = band_y_center
        cz = topz - rib_z_offset - rib_depth * 0.5
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(cx, cy, cz))
        rib = bpy.context.active_object
        rib.scale = (rib_thickness * 0.5, rib_band_y * 0.5, rib_depth * 0.5)
        cutters.append(rib)
        x += rib_spacing

    apply_boolean_union(mold_obj, cutters)


# ---------------------------
# Main pipeline
# ---------------------------

def build_triangles(beardline, neckline, params):
    """Build a single seamless sheet: lip → beardline(X-resampled) → neckline(X-resampled).
    Using the *same* X samples avoids T-junction micro gaps seen in slicers."""
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    lip_segments = int(params.get("lipSegments", 100))
    arc_steps = int(params.get("arcSteps", 24))
    max_lip_radius = float(params.get("maxLipRadius", 0.008))
    min_lip_radius = float(params.get("minLipRadius", 0.003))
    taper_mult = float(params.get("taperMult", 25.0))
    extrusion_depth = float(params.get("extrusionDepth", -0.008))

    # NEW controllables for overhang behavior
    profile_bias = float(params.get("profileBias", 1.35))  # >1 ⇒ steeper start
    prelift = float(params.get("prelift", 0.0))            # meters

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult,
        profile_bias=profile_bias, prelift=prelift
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

    extruded = extrude_surface_z_solid(faces, extrusion_depth)

    # For ribs placement we want a representative Y of the lip start column
    lip_band_y = sum(p[1] for p in ring0) / max(1, len(ring0))

    return extruded, abs(extrusion_depth), xs, lip_band_y


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


def create_cylinders_z_aligned(holes, thickness, radius=0.0015875, embed_offset=0.0025):
    """Z-aligned cylinders centered at (x,y), extending through thickness."""
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
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception:
            pass
        try:
            bpy.data.objects.remove(cutter, do_unlink=True)
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

    tris, thickness, xs, lip_band_y = build_triangles(beardline, neckline, params)

    mold_obj = make_mesh_from_tris(tris, name="BeardMold")

    # NEW: optional internal anchor ribs before remesh (then remesh bakes them in)
    add_anchor_ribs(mold_obj, xs, params, thickness, lip_band_y)

    voxel_size = float(params.get("voxelRemesh", 0.0006))
    voxel_remesh_if_requested(mold_obj, voxel_size)

    report_non_manifold(mold_obj)

    for obj in bpy.data.objects:
        obj.select_set(False)
    mold_obj.select_set(True)

    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference(mold_obj, cutters)

    export_stl_selected(output_path)

    print(
        f"STL export complete for job ID: {data.get('jobID','N/A')} "
        f"overlay: {data.get('overlay','N/A')} "
        f"verts(beardline)={len(beardline)} "
        f"neckline={len(neckline)} "
        f"holes={len(holes_in)}"
    )


if __name__ == "__main__":
    main()
