# file: scripts/build_mold_outline.py
# Blender 3.x — generates solid mold then optional outline (wireframe or boundary-only)

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

def end_caps(lip_vertices, base_points, ring_count):
    faces = []
    if not base_points:
        return faces
    # first end
    first_base = base_points[0]
    for i in range(ring_count - 1):
        a = lip_vertices[i]
        b = lip_vertices[i+1]
        faces.append([a, b, first_base])
    # last end
    last_base = base_points[-1]
    start_idx = (len(base_points) - 1) * ring_count
    for i in range(ring_count - 1):
        a = lip_vertices[start_idx + i]
        b = lip_vertices[start_idx + i + 1]
        faces.append([a, b, last_base])
    return faces

def extrude_faces_z(tri_faces, depth):
    """Replicates the Swift extrusion: front, flipped back, and 3 side quads per triangle."""
    out = []
    for tri in tri_faces:
        f0, f1, f2 = tri
        front = [f0, f1, f2]
        back  = [(f0[0], f0[1], f0[2] + depth),
                 (f1[0], f1[1], f1[2] + depth),
                 (f2[0], f2[1], f2[2] + depth)]
        out.append(front)
        out.append([back[0], back[2], back[1]])
        for i in range(3):
            j = (i + 1) % 3
            out.append([front[i], front[j], back[j]])
            out.append([back[j], back[i], front[i]])
    return out

def make_mesh_from_tris(tris, name="MoldMesh"):
    """Create a Blender mesh object from triangle vertex positions."""
    v2i = {}
    verts = []
    faces = []

    def key(p):
        return (round(p[0], 6), round(p[1], 6), round(p[2], 6))

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

def create_cylinders_z_aligned(holes, thickness, radius=0.0015875, embed_offset=0.0025):
    """Z-aligned cylinders, centered at (x,y), spanning down along -Z through the mold thickness."""
    cylinders = []
    for h in holes:
        x,y,z = to_vec3(h)
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
# Outline generators (B & C)
# ---------------------------

def duplicate_object(obj, new_name):
    """Full object + mesh copy to allow non-destructive variants."""
    dup = obj.copy()
    dup.data = obj.data.copy()
    dup.name = new_name
    dup.data.name = new_name + "_Mesh"
    bpy.context.collection.objects.link(dup)
    return dup

def apply_wireframe_modifier(obj, thickness=0.0015, even=True, boundary=True, replace=True, relative=False):
    """Use Blender's Wireframe modifier to create struts along all edges."""
    bpy.context.view_layer.objects.active = obj
    mod = obj.modifiers.new(name="Wireframe", type='WIREFRAME')
    mod.thickness = float(thickness)
    mod.use_even_offset = bool(even)
    mod.use_boundary = bool(boundary)  # keep open edges connected
    mod.use_replace = bool(replace)
    mod.use_relative_offset = bool(relative)
    bpy.ops.object.modifier_apply(modifier=mod.name)
    return obj

def build_boundary_outline(obj, thickness=0.0015, bevel_res=2, curve_res=12):
    """Keep only boundary edges, convert to curve, bevel to tubes, convert back to mesh.
    Why: Wireframe mod can't target only boundary; curve bevel gives even round tubes.
    """
    outline = duplicate_object(obj, obj.name + "_OutlineBoundary")

    # Compute boundary edges while faces still present
    me = outline.data
    bm = bmesh.new()
    bm.from_mesh(me)

    # delete non-boundary edges
    for e in list(bm.edges):
        if not e.is_boundary:
            bm.edges.remove(e)

    # delete all faces but keep boundary edges
    for f in list(bm.faces):
        bm.faces.remove(f)

    bm.verts.ensure_lookup_table()
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.to_mesh(me)
    bm.free()

    # Convert to curve and bevel
    for o in bpy.context.selected_objects:
        o.select_set(False)
    outline.select_set(True)
    bpy.context.view_layer.objects.active = outline
    bpy.ops.object.convert(target='CURVE')
    curve_obj = bpy.context.active_object
    cu = curve_obj.data
    cu.dimensions = '3D'
    cu.bevel_depth = float(thickness) * 0.5  # radius
    cu.bevel_resolution = int(bevel_res)
    cu.resolution_u = int(curve_res)

    # Back to mesh for export
    bpy.ops.object.convert(target='MESH')
    mesh_obj = bpy.context.active_object
    mesh_obj.name = outline.name  # keep name
    return mesh_obj

# ---------------------------
# Main pipeline (Swift parity + outline)
# ---------------------------

def build_triangles(beardline, neckline, params):
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    lip_segments   = int(params.get("lipSegments", 100))
    arc_steps      = int(params.get("arcSteps", 24))
    max_lip_radius = float(params.get("maxLipRadius", 0.008))
    min_lip_radius = float(params.get("minLipRadius", 0.003))
    taper_mult     = float(params.get("taperMult", 25.0))
    extrusion_depth= float(params.get("extrusionDepth", -0.008))  # Swift uses negative to go back in +Z

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult
    )

    faces = quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    if neckline:
        faces += skin_beardline_to_neckline(beardline, neckline)

    faces += stitch_first_column_to_base(base_points, lip_vertices, ring_count)
    faces += end_caps(lip_vertices, base_points, ring_count)

    extruded = extrude_faces_z(faces, extrusion_depth)

    return extruded, abs(extrusion_depth)

def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)


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

    # Outline params (B default). Disabled if mode == 'off'.
    outline = params.get("outline", {})
    outline_mode = (outline.get("mode") or params.get("outlineMode") or "wireframe").lower()
    outline_enabled = outline_mode in {"wireframe", "boundary"}
    thickness = float(outline.get("thickness", 0.0015))
    keep_solid = bool(outline.get("keepSolid", True))
    even = bool(outline.get("evenThickness", True))
    export_which = (outline.get("export") or "outline").lower()  # outline|solid|both

    # Build triangles
    tris, thickness_z = build_triangles(beardline, neckline, params)

    # Create mesh object
    mold_obj = make_mesh_from_tris(tris, name="BeardMold")

    # Deselect all
    for obj in bpy.data.objects:
        obj.select_set(False)

    # Optional holes on the base solid
    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness_z, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference(mold_obj, cutters)

    outline_obj = None

    if outline_enabled:
        if outline_mode == "wireframe":
            # Duplicate if keeping original solid
            target = duplicate_object(mold_obj, "BeardMold_OutlineWire") if keep_solid else mold_obj
            outline_obj = apply_wireframe_modifier(
                target,
                thickness=thickness,
                even=even,
                boundary=True,
                replace=True,
                relative=False,
            )
        elif outline_mode == "boundary":
            # Always duplicate for clarity; boundary tubes only
            outline_obj = build_boundary_outline(
                mold_obj,
                thickness=thickness,
                bevel_res=int(outline.get("bevelRes", 2)),
                curve_res=int(outline.get("curveRes", 12)),
            )

    # Select for export
    for obj in bpy.data.objects:
        obj.select_set(False)

    if export_which == "solid":
        mold_obj.select_set(True)
    elif export_which == "both" and outline_obj is not None:
        mold_obj.select_set(True)
        outline_obj.select_set(True)
    elif outline_obj is not None:
        outline_obj.select_set(True)
    else:
        mold_obj.select_set(True)

    export_stl_selected(output_path)

    print(
        f"STL export complete for job ID: {data.get('jobID','N/A')} "
        f"outline={outline_mode if outline_enabled else 'off'} thickness={thickness} keepSolid={keep_solid} "
        f"verts(beardline)={len(beardline)} neckline={len(neckline)} holes={len(holes_in)}"
    )


if __name__ == "__main__":
    main()
