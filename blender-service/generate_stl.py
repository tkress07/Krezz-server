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
    # first end
    first_base = base_points[0]
    for i in range(ring_count - 1):
        a = lip_vertices[i]
        b = lip_vertices[i + 1]
        faces.append([a, b, first_base])
    # last end
    last_base = base_points[-1]
    start_idx = (len(base_points) - 1) * ring_count
    for i in range(ring_count - 1):
        a = lip_vertices[start_idx + i]
        b = lip_vertices[start_idx + i + 1]
        faces.append([a, b, last_base])
    return faces

# ----------------------------------------------------------------------
# Solid, manifold extrusion (replaces per-triangle extrusion that caused gaps)
# ----------------------------------------------------------------------

def extrude_surface_z_solid(tri_faces, depth, weld_eps=1e-6):
    """Extrude a triangle *surface* by `depth` along +Z and close only boundary edges.
    Returns list of triangles (front, back, sides) forming a watertight solid.
    """
    # 1) Build a unique vertex pool for the front surface
    key = lambda p: (round(p[0], 6), round(p[1], 6), round(p[2], 6))
    v2i = {}
    verts = []  # front verts
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

    # 2) Count undirected edges to find boundary
    edge_count = {}
    edge_dir = {}
    for ia, ib, ic in tris_idx:
        edges = [(ia, ib), (ib, ic), (ic, ia)]
        for (u, v) in edges:
            e = (u, v)
            ue = (min(u, v), max(u, v))
            edge_count[ue] = edge_count.get(ue, 0) + 1
            if ue not in edge_dir:
                edge_dir[ue] = e  # remember one oriented copy

    boundary = [ue for ue, c in edge_count.items() if c == 1]

    # 3) Build back vertex pool (offset along Z)
    back_offset = len(verts)
    back_verts = [(x, y, z + depth) for (x, y, z) in verts]

    # 4) Assemble triangles: front + flipped back
    out = []
    for ia, ib, ic in tris_idx:
        out.append((verts[ia], verts[ib], verts[ic]))
        ja, jb, jc = ia + back_offset, ib + back_offset, ic + back_offset
        out.append((back_verts[jc - back_offset], back_verts[jb - back_offset], back_verts[ja - back_offset]))

    # 5) Side walls only along boundary edges
    for ue in boundary:
        u, v = edge_dir[ue]  # oriented
        ju, jv = u + back_offset, v + back_offset
        # two triangles forming quad (u->v) between front and back
        out.append((verts[u], verts[v], back_verts[jv - back_offset]))
        out.append((verts[u], back_verts[jv - back_offset], back_verts[ju - back_offset]))

    return out


def make_mesh_from_tris(tris, name="MoldMesh"):
    """Create a Blender mesh object from triangle vertex positions.
    Ensures welded verts and consistent normals (avoid slicer gaps)."""
    # Weld verts with rounding for stability
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
        faces.append(tuple(ids))

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts], [], faces)
    mesh.validate(verbose=False)
    mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)

    # Recalculate normals outside so all side faces are coherent
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
        pass  # keep going even if running from background

    return obj


def create_cylinders_z_aligned(holes, thickness, radius=0.0015875, embed_offset=0.0025):
    """Z-aligned cylinders, centered at (x,y), spanning down along -Z through the mold thickness."""
    cylinders = []
    for h in holes:
        x, y, z = to_vec3(h)
        depth = float(thickness)
        # top slightly above surface, extending downwards (boolean is more robust)
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
# Main pipeline (Swift parity)
# ---------------------------

def build_triangles(beardline, neckline, params):
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    # params with Swift defaults
    lip_segments = int(params.get("lipSegments", 100))
    arc_steps = int(params.get("arcSteps", 24))
    max_lip_radius = float(params.get("maxLipRadius", 0.008))
    min_lip_radius = float(params.get("minLipRadius", 0.003))
    taper_mult = float(params.get("taperMult", 25.0))
    extrusion_depth = float(params.get("extrusionDepth", -0.008))  # negative to go back in +Z

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult
    )

    # Lip surface between rings
    faces = quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    # Skin beardline -> neckline (if provided)
    if neckline:
        faces += skin_beardline_to_neckline(beardline, neckline)

    # Rim seam (first column of rings to base)
    faces += stitch_first_column_to_base(base_points, lip_vertices, ring_count)

    # End caps
    faces += end_caps(lip_vertices, base_points, ring_count)

    # Extrude the *surface* to a closed solid
    extruded = extrude_surface_z_solid(faces, extrusion_depth)

    return extruded, abs(extrusion_depth)


def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)


def main():
    # Parse CLI
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    if len(argv) != 2:
        raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

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

    # Build triangles with Swift-equivalent pipeline
    tris, thickness = build_triangles(beardline, neckline, params)

    # Create mesh object
    mold_obj = make_mesh_from_tris(tris, name="BeardMold")

    # Deselect all, then select the mold for export
    for obj in bpy.data.objects:
        obj.select_set(False)
    mold_obj.select_set(True)

    # Optional holes via boolean DIFFERENCE (now Z-aligned & positioned like Swift)
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
