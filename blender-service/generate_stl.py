# generate_stl.py
import bpy
import bmesh
import json
import sys
import math
from mathutils import Vector

# ===========================
# Geometry & math helpers
# ===========================

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
            # Consistent winding (CCW when seen from +X side)
            faces.append([a, c, b])
            faces.append([b, c, d])
    return faces

def skin_beardline_to_neckline_monotone(beardline, neckline):
    """Greedy monotone pairing to reduce crossings/degenerates."""
    faces = []
    if len(beardline) < 2 or not neckline:
        return faces
    prev = 0
    for i in range(len(beardline)-1):
        b0, b1 = beardline[i], beardline[i+1]
        n0 = min(range(prev, len(neckline)), key=lambda k: dist2(neckline[k], b0))
        n1 = min(range(n0, len(neckline)), key=lambda k: dist2(neckline[k], b1))
        v0, v1 = neckline[n0], neckline[n1]
        faces.append([b0, v0, b1])
        faces.append([v0, v1, b1])
        prev = n0
    return faces

def stitch_first_column_to_base(base_points, lip_vertices, ring_count):
    """Close the boundary at j=0 to the base polyline."""
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
    """Cap the ends across base index (i=0 and i=end)."""
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

# ===========================
# Mesh build / cleanup
# ===========================

def make_mesh_from_tris(tris, name="BeardMoldSurface"):
    """Create a Blender mesh object from triangle vertex positions (deduped)."""
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

def make_manifold_and_solidify(obj, thickness, merge_epsilon=1e-6, center=True):
    """
    Clean single-surface mesh and add true thickness without hole-filling fans.
    - No fill_holes (Solidify with use_rim closes the border cleanly)
    - Merge tiny cracks, dissolve degenerates, recalc normals
    - Apply Solidify once (watertight), then optionally center to origin
    """
    # Ensure object mode -> edit
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')

    # Merge tiny gaps
    try:
        bpy.ops.mesh.merge_by_distance(distance=merge_epsilon)
    except Exception:
        bpy.ops.mesh.remove_doubles(threshold=merge_epsilon)

    # Remove zero-area/needle faces that can explode triangulation
    try:
        bpy.ops.mesh.dissolve_degenerate(threshold=1e-7)
    except Exception:
        pass

    # Consistent outward normals
    bpy.ops.mesh.normals_make_consistent(inside=False)

    # Back to object mode
    bpy.ops.object.mode_set(mode='OBJECT')

    # Add real thickness; cap rim instead of fill_holes
    solid = obj.modifiers.new(name="Solidify", type='SOLIDIFY')
    solid.thickness = float(thickness)
    solid.offset    = 1.0
    solid.use_even_offset = True
    solid.use_rim   = True  # closes the open border as proper side walls
    bpy.ops.object.modifier_apply(modifier=solid.name)

    # Final validate
    obj.data.validate(verbose=False)
    obj.data.update()

    # Optional: recenter so preview bounds aren't huge from any past spikes
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
    """Quick boundary-edge check (True = watertight)."""
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
    """Z-aligned cylinders centered at (x,y), extending down along -Z through the mold thickness."""
    cylinders = []
    for h in holes:
        x,y,z = to_vec3(h)
        depth = float(thickness)
        # top slightly above the surface, extending downward
        center_z = z - (embed_offset + depth / 2.0)
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=(x, y, center_z))
        cyl = bpy.context.active_object
        cylinders.append(cyl)
    return cylinders

def apply_boolean_difference_exact(target_obj, cutters):
    """Join cutters and apply a single EXACT boolean for robustness."""
    if not cutters:
        return
    # Join cutters into one mesh
    bpy.ops.object.select_all(action='DESELECT')
    for c in cutters:
        c.select_set(True)
    bpy.context.view_layer.objects.active = cutters[0]
    bpy.ops.object.join()
    cutter = cutters[0]

    # Apply EXACT boolean
    bpy.ops.object.select_all(action='DESELECT')
    target_obj.select_set(True)
    bpy.context.view_layer.objects.active = target_obj
    mod = target_obj.modifiers.new(name="HolesExact", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.solver    = 'EXACT'
    mod.object    = cutter
    bpy.ops.object.modifier_apply(modifier=mod.name)

    # Cleanup
    bpy.data.objects.remove(cutter, do_unlink=True)
    target_obj.data.validate(verbose=False)
    target_obj.data.update()

# ===========================
# Build pipeline (surface-first)
# ===========================

def build_triangles(beardline, neckline, params):
    """
    Build a single-sided surface (no per-triangle thickening).
    Thickness is added later via Solidify for watertightness.
    """
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    # Params (Swift-ish defaults)
    lip_segments    = int(params.get("lipSegments", 100))
    arc_steps       = int(params.get("arcSteps", 24))
    max_lip_radius  = float(params.get("maxLipRadius", 0.008))
    min_lip_radius  = float(params.get("minLipRadius", 0.003))
    taper_mult      = float(params.get("taperMult", 25.0))
    extrusion_depth = float(params.get("extrusionDepth", -0.008))  # Swift negative => back in +Z
    thickness       = abs(extrusion_depth)

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult
    )

    # Lip surface between rings
    faces = quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    # Skin beardline -> neckline (monotone to avoid crossings)
    if neckline:
        faces += skin_beardline_to_neckline_monotone(beardline, neckline)

    # Stitch one side of the ring to base (other edge will be closed by Solidify rim)
    faces += stitch_first_column_to_base(base_points, lip_vertices, ring_count)

    # End caps across base index
    faces += end_caps(lip_vertices, base_points, ring_count)

    # Return surface triangles only; thickness handled later
    return faces, thickness

def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)

# ===========================
# Main
# ===========================

def main():
    # CLI
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    if len(argv) != 2:
        raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

    # Reset scene for clean context
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # Load payload
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

    # Build single-surface tris
    tris, thickness = build_triangles(beardline, neckline, params)

    # Create mesh object
    mold_obj = make_mesh_from_tris(tris, name="BeardMoldSurface")

    # Cleanup + Solidify (one manifold shell), then center to origin
    make_manifold_and_solidify(mold_obj, thickness, merge_epsilon=1e-6, center=True)

    # Optional quick watertight check (post-solidify)
    wt = is_watertight(mold_obj)

    # Holes via a single EXACT boolean
    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference_exact(mold_obj, cutters)
        wt = is_watertight(mold_obj)  # recheck after booleans

    # Select for export
    bpy.ops.object.select_all(action='DESELECT')
    mold_obj.select_set(True)
    bpy.context.view_layer.objects.active = mold_obj

    # Export STL
    export_stl_selected(output_path)

    print(
        f"STL export complete for jobID={data.get('jobID','N/A')} "
        f"overlay={data.get('overlay','N/A')} "
        f"beardline={len(beardline)} "
        f"neckline={len(neckline)} "
        f"holes={len(holes_in)} "
        f"watertight={wt}"
    )

if __name__ == "__main__":
    main()
