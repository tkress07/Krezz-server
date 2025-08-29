# generate_stl.py
import bpy
import bmesh
import json
import sys
import math
from mathutils import Vector

# =========================
# Helpers (geometry & math)
# =========================

def to_vec3(p):
    return (float(p['x']), float(p['y']), float(p['z']))

def is_finite3(v):
    return all(math.isfinite(c) for c in v)

def dist2(a, b):
    dx = a[0]-b[0]; dy = a[1]-b[1]; dz = a[2]-b[2]
    return dx*dx + dy*dy + dz*dz

def tri_area(a, b, c):
    # 0.5 * |(b-a) x (c-a)|
    ax,ay,az = a; bx,by,bz = b; cx,cy,cz = c
    ux,uy,uz = (bx-ax, by-ay, bz-az)
    vx,vy,vz = (cx-ax, cy-ay, cz-az)
    cxp = (uy*vz - uz*vy, uz*vx - ux*vz, ux*vy - uy*vx)
    return 0.5 * math.sqrt(cxp[0]**2 + cxp[1]**2 + cxp[2]**2)

def bb_diag(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]; zs = [p[2] for p in pts]
    if not xs: return 1.0
    minx,maxx = min(xs),max(xs); miny,maxy = min(ys),max(ys); minz,maxz = min(zs),max(zs)
    dx = maxx-minx; dy = maxy-miny; dz = maxz-minz
    return max(1e-6, math.sqrt(dx*dx + dy*dy + dz*dz))

def smooth_vertices_open(vertices, passes=1):
    """Simple moving-average smoothing for an open polyline (preserve endpoints)."""
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
        # closest by |Δx|
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
    """
    Build a semicircle ring at each base point in the YZ plane.
    Returns (verts, ring_count). verts is list of 3-tuples.
    """
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
    # Triangulate the strips between consecutive rings (no end-caps here)
    for i in range(base_count - 1):
        for j in range(ring_count - 1):
            a = lip_vertices[i * ring_count + j]
            b = lip_vertices[i * ring_count + j + 1]
            c = lip_vertices[(i + 1) * ring_count + j]
            d = lip_vertices[(i + 1) * ring_count + j + 1]
            faces.append((a, c, b))
            faces.append((b, c, d))
    return faces

def skin_beardline_to_neckline_monotone(beardline, neckline, skin_max_dist):
    """
    Safer skinning: enforce monotone mapping along neckline indices to avoid crossings.
    Skip spans that exceed skin_max_dist to avoid long kites.
    Returns list of triangle faces.
    """
    faces = []
    if len(beardline) < 2 or len(neckline) < 2:
        return faces

    # Precompute order on neckline by X to get a consistent index progression.
    # (You can switch to arc-length order if needed.)
    neck_sorted_idx = sorted(range(len(neckline)), key=lambda k: neckline[k][0])
    neck_pos_to_rank = {idx: rank for rank, idx in enumerate(neck_sorted_idx)}

    # Map each beard pt to nearest neckline point (by distance), then enforce monotonicity by rank
    nearest_idx = []
    for b in beardline:
        j = min(range(len(neckline)), key=lambda k: dist2(neckline[k], b))
        nearest_idx.append(j)

    # Enforce non-decreasing rank (monotone)
    fixed_idx = [nearest_idx[0]]
    for j in nearest_idx[1:]:
        prev = fixed_idx[-1]
        # convert to ranks
        prev_r = neck_pos_to_rank[prev]
        cur_r  = neck_pos_to_rank[j]
        if cur_r < prev_r:
            # bump forward to prev rank (no backward crossings)
            # find the neck index that has rank == prev_r
            j = neck_sorted_idx[prev_r]
        fixed_idx.append(j)

    # Now triangulate consecutive quads with distance and degeneracy checks
    for i in range(len(beardline) - 1):
        b0, b1 = beardline[i], beardline[i+1]
        n0, n1 = neckline[fixed_idx[i]], neckline[fixed_idx[i+1]]

        # Distance cap (avoid long stretched kites)
        if math.sqrt(dist2(b0, n0)) > skin_max_dist or math.sqrt(dist2(b1, n1)) > skin_max_dist:
            continue

        f1 = (b0, n0, b1)
        f2 = (n0, n1, b1)
        faces.extend([f1, f2])

    return faces

def filter_tris(tris, scale_diag):
    """Remove degenerate/near-zero area or over-long-edge triangles."""
    out = []
    min_area = (1e-6 * scale_diag) ** 2     # scale-aware tiny
    max_edge = 0.6 * scale_diag             # forbid crazy long edges
    for a,b,c in tris:
        area = tri_area(a,b,c)
        if area < min_area:
            continue
        # check max edge
        emax = max(math.sqrt(dist2(a,b)), math.sqrt(dist2(b,c)), math.sqrt(dist2(c,a)))
        if emax > max_edge:
            continue
        out.append((a,b,c))
    return out

def make_object_from_tris(name, tris):
    """Create a Blender object from list of triangle vertices (with dedup)."""
    v2i = {}
    verts = []
    faces = []
    def key(p):
        # stable rounding to avoid duplicates
        return (round(p[0], 7), round(p[1], 7), round(p[2], 7))
    for (a,b,c) in tris:
        ids = []
        for p in (a,b,c):
            k = key(p)
            if k not in v2i:
                v2i[k] = len(verts)
                verts.append(k)
            ids.append(v2i[k])
        faces.append(tuple(ids))
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([Vector(v) for v in verts], [], faces)
    mesh.validate(clean_customdata=True)
    mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj

def clean_mesh_bmesh(obj, merge_eps=1e-5, laplace_iters=0, laplace_lambda=0.2):
    """Do cleanup in BMesh: merge doubles, dissolve degenerate, recalc normals, optional smoothing."""
    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)

    # Remove doubles
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=merge_eps)

    # Dissolve degenerate faces/edges
    bmesh.ops.dissolve_degenerate(bm, dist=merge_eps)

    # Optional Laplacian smooth (very mild to relax tiny kinks)
    if laplace_iters > 0 and 0.0 < laplace_lambda < 1.0:
        try:
            bmesh.ops.smooth_laplacian(
                bm,
                verts=bm.verts,
                lambda_factor=laplace_lambda,
                repeat=laplace_iters,
                preserve_volume=False,
                use_x=True, use_y=True, use_z=True
            )
        except Exception:
            pass

    # Triangulate to be explicit (optional; booleans fine with quads too)
    bmesh.ops.triangulate(bm, faces=bm.faces)

    # Recalc normals consistently
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    bm.to_mesh(me)
    me.validate(clean_customdata=True)
    me.update()
    bm.free()

def solidify_apply(obj, thickness):
    bpy.context.view_layer.objects.active = obj
    mod = obj.modifiers.new(name="Solidify", type='SOLIDIFY')
    mod.thickness = float(thickness)
    mod.offset = -1.0                 # push thickness inward from the original surface
    mod.use_rim = True                # close borders
    mod.use_even_offset = True
    mod.use_quality_normals = True
    bpy.ops.object.modifier_apply(modifier=mod.name)

def create_joined_cylinders_z(holes, shell_thickness, radius=0.0015875, embed_offset=0.0025):
    """Create Z-aligned cylinders that fully traverse the shell; join into one object."""
    cutters = []
    depth = float(shell_thickness) + 2.0 * float(embed_offset)
    for h in holes:
        x,y,z = to_vec3(h)
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            continue
        center_z = z - (depth * 0.5)  # center so it spans above and below surface
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=(x, y, center_z))
        cutters.append(bpy.context.active_object)

    if not cutters:
        return None

    # Join into one
    for obj in bpy.data.objects:
        obj.select_set(False)
    cutters[0].select_set(True)
    for c in cutters[1:]:
        c.select_set(True)
    bpy.context.view_layer.objects.active = cutters[0]
    bpy.ops.object.join()
    cutters[0].name = "CuttersJoined"
    return cutters[0]

def boolean_difference_exact(target, cutter):
    bpy.context.view_layer.objects.active = target
    mod = target.modifiers.new(name="Boolean", type='BOOLEAN')
    mod.operation = 'DIFFERENCE'
    mod.solver = 'EXACT'
    mod.object = cutter
    mod.use_hole_tolerant = True
    bpy.ops.object.modifier_apply(modifier=mod.name)

def recenter_to_origin(obj):
    # Set origin to geometry, then move object to world origin
    bpy.context.view_layer.objects.active = obj
    for o in bpy.data.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')
    loc = obj.location.copy()
    obj.location = (0.0, 0.0, 0.0)

def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)

# =========================
# Main build (clean surface)
# =========================

def build_surface_tris(beardline, neckline, params):
    if not beardline:
        raise ValueError("Empty beardline supplied.")

    # Params (safe defaults; tuneable from payload)
    lip_segments   = int(params.get("lipSegments", 100))
    arc_steps      = int(params.get("arcSteps", 24))
    max_lip_radius = float(params.get("maxLipRadius", 0.008))
    min_lip_radius = float(params.get("minLipRadius", 0.003))
    taper_mult     = float(params.get("taperMult", 25.0))
    skin_max_dist  = float(params.get("skinMaxDist", 0.02))  # tighten to 0.015/0.01 to skip risky spans
    smooth_neck    = int(params.get("neckSmoothingPasses", 3))

    # Smoothing neckline only (to avoid jagged kites)
    if neckline and smooth_neck > 0:
        neckline = smooth_vertices_open(neckline, passes=smooth_neck)

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5 * (minX + maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult
    )

    # Lip strips (no end-caps; rims will be closed by Solidify)
    tris = quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    # Optional beard->neck skin (monotone + capped)
    if neckline:
        tris.extend(skin_beardline_to_neckline_monotone(beardline, neckline, skin_max_dist))

    # Cull degenerates/overlong
    scale = bb_diag(beardline + neckline + lip_vertices)
    tris = filter_tris(tris, scale)

    return tris

def main():
    # Parse CLI
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    if len(argv) != 2:
        raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

    # Reset scene
    bpy.ops.wm.read_factory_settings(use_empty=True)

    with open(input_path, 'r') as f:
        data = json.load(f)

    # Input guards / backward-compat
    beardline_in = data.get("beardline") or data.get("vertices")
    if beardline_in is None or len(beardline_in) == 0:
        raise ValueError("Missing 'beardline' (or legacy 'vertices') in payload.")

    beardline = [to_vec3(v) for v in beardline_in if is_finite3(to_vec3(v))]
    if not beardline:
        raise ValueError("No valid beardline vertices (non-finite?).")

    neckline_in = data.get("neckline") or []
    neckline = [to_vec3(v) for v in neckline_in if is_finite3(to_vec3(v))]

    holes_in = data.get("holeCenters") or data.get("holes") or []
    params = data.get("params", {})

    # Thickness for solidify
    shell_thickness = float(params.get("shellThickness", 0.008))  # ~8mm
    laplace_iters = int(params.get("laplaceIters", 0))            # try 1-3 if you want subtle smoothing
    laplace_lambda = float(params.get("laplaceLambda", 0.2))

    # 1) Build a single clean surface (no per-tri extrusion!)
    surf_tris = build_surface_tris(beardline, neckline, params)

    # 2) Make object & clean
    mold_obj = make_object_from_tris("MoldSurface", surf_tris)
    clean_mesh_bmesh(mold_obj, merge_eps=1e-5, laplace_iters=laplace_iters, laplace_lambda=laplace_lambda)

    # 3) Solidify once (closes rims → watertight shell)
    solidify_apply(mold_obj, thickness=shell_thickness)

    # 4) Optional: add holes via a single EXACT boolean (join cutters first)
    cutters = None
    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_joined_cylinders_z(holes_in, shell_thickness, radius=radius, embed_offset=embed_offset)
        if cutters:
            boolean_difference_exact(mold_obj, cutters)
            # cleanup cutters object
            try:
                bpy.data.objects.remove(cutters, do_unlink=True)
            except Exception:
                pass

    # 5) Final tiny cleanup & recenter (nice bounds for SceneKit)
    clean_mesh_bmesh(mold_obj, merge_eps=1e-5, laplace_iters=0, laplace_lambda=0.2)
    recenter_to_origin(mold_obj)

    # 6) Export selected
    for o in bpy.data.objects:
        o.select_set(False)
    mold_obj.select_set(True)
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
