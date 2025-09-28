# file: blender_service_watertight_swiftparity.py
import bpy, bmesh, json, sys, math
from mathutils import Vector

# ===== Good defaults for 0.4 mm nozzle =====
WELD_EPS_DEFAULT = 0.00025   # 0.25 mm shared-vertex tolerance
AREA_MIN         = 1e-14     # drop ultra-skinny sliver tris early
VOXEL_DEFAULT    = 0.0       # OFF by default; if needed use 0.0008–0.0010
# ===========================================

# ----------------- helpers -----------------
def to_vec3(p): return (float(p['x']), float(p['y']), float(p['z']))

def area2(a, b, c):
    ab = (b[0]-a[0], b[1]-a[1], b[2]-a[2])
    ac = (c[0]-a[0], c[1]-a[1], c[2]-a[2])
    cx = ab[1]*ac[2] - ab[2]*ac[1]
    cy = ab[2]*ac[0] - ab[0]*ac[2]
    cz = ab[0]*ac[1] - ab[1]*ac[0]
    return cx*cx + cy*cy + cz*cz

def smooth_vertices_open(vertices, passes=1):
    if len(vertices) < 3 or passes <= 0: return vertices[:]
    V = vertices[:]
    for _ in range(passes):
        NV = [V[0]]
        for i in range(1, len(V)-1):
            px, py, pz = V[i-1]; cx, cy, cz = V[i]; nx, ny, nz = V[i+1]
            NV.append(((px+cx+nx)/3.0, (py+cy+ny)/3.0, (pz+cz+nz)/3.0))
        NV.append(V[-1]); V = NV
    return V

def sample_base_points_along_x(beardline, lip_segments):
    xs = [p[0] for p in beardline]; ys = [p[1] for p in beardline]; zs = [p[2] for p in beardline]
    minX = min(xs) if xs else -0.05; maxX = max(xs) if xs else 0.05
    seg_w = (maxX - minX) / max(1, (lip_segments - 1))
    fallbackY = max(ys) if ys else 0.03
    fallbackZ = (sum(zs)/len(zs)) if zs else 0.0
    base = []
    for i in range(lip_segments):
        x = minX + i*seg_w
        top = min(beardline, key=lambda p: abs(p[0]-x)) if beardline else None
        base.append((x, top[1], top[2]) if top else (x, fallbackY, fallbackZ))
    return base, minX, maxX

def tapered_radius(x, centerX, min_r, max_r, taper_mult):
    taper = max(0.0, 1.0 - abs(x-centerX)*taper_mult)
    return min_r + taper*(max_r-min_r)

def generate_lip_rings(base_points, arc_steps, min_r, max_r, centerX, taper_mult, prelift=0.0):
    ring_count = arc_steps + 1
    verts = []
    for (bx, by, bz) in base_points:
        r = tapered_radius(bx, centerX, min_r, max_r, taper_mult)
        for j in range(ring_count):
            angle = math.pi * (j/float(arc_steps))
            y = by - r*(1.0 - math.sin(angle))
            z = bz + r*math.cos(angle) + prelift  # <— tiny lift to avoid coplanar z
            verts.append((bx, y, z))
    return verts, ring_count

def quads_to_tris_between_rings(lip_vertices, base_count, ring_count):
    faces = []
    for i in range(base_count-1):
        for j in range(ring_count-1):
            a = lip_vertices[i*ring_count + j]
            b = lip_vertices[i*ring_count + j + 1]
            c = lip_vertices[(i+1)*ring_count + j]
            d = lip_vertices[(i+1)*ring_count + j + 1]
            faces.append([a, c, b]); faces.append([b, c, d])
    return faces

def _rounded_key(p, eps):
    return (round(p[0]/eps)*eps, round(p[1]/eps)*eps, round(p[2]/eps)*eps)

# ---------- Swift-style nearest-neighbor strap ----------
def strap_beardline_to_neckline_swift(beard, neck):
    """Mirror Swift: for each consecutive pair in beardline, find nearest
    indices in neckline for each endpoint and make two tris."""
    if len(beard) < 2 or len(neck) < 2: return []
    faces = []
    # precompute for speed
    import math
    def dist2(a,b): dx=a[0]-b[0]; dy=a[1]-b[1]; dz=a[2]-b[2]; return dx*dx+dy*dy+dz*dz
    for i in range(len(beard)-1):
        b0, b1 = beard[i], beard[i+1]
        n0i = min(range(len(neck)), key=lambda k: dist2(neck[k], b0))
        n1i = min(range(len(neck)), key=lambda k: dist2(neck[k], b1))
        n0, n1 = neck[n0i], neck[n1i]
        faces.append([b0, n0, b1])
        faces.append([n0, n1, b1])
    return faces

# --------------- extrusion & mesh build ---------------
def extrude_surface_z_solid(tri_faces, depth, weld_eps):
    v2i, verts, tris_idx = {}, [], []
    def idx_of(p):
        k = _rounded_key(p, weld_eps)
        i = v2i.get(k)
        if i is None: i=len(verts); v2i[k]=i; verts.append(k)
        return i
    for a,b,c in tri_faces:
        ia=idx_of(a); ib=idx_of(b); ic=idx_of(c); tris_idx.append((ia,ib,ic))

    edge_count, edge_dir = {}, {}
    for ia,ib,ic in tris_idx:
        for (u,v) in ((ia,ib),(ib,ic),(ic,ia)):
            ue=(min(u,v),max(u,v)); edge_count[ue]=edge_count.get(ue,0)+1
            if ue not in edge_dir: edge_dir[ue]=(u,v)
    boundary=[ue for ue,c in edge_count.items() if c==1]

    back_offset=len(verts)
    back_verts=[(x,y,z+depth) for (x,y,z) in verts]
    out=[]
    for ia,ib,ic in tris_idx:
        out.append((verts[ia],verts[ib],verts[ic]))
        ja,jb,jc = ia+back_offset, ib+back_offset, ic+back_offset
        out.append((back_verts[jc-back_offset], back_verts[jb-back_offset], back_verts[ja-back_offset]))
    for ue in boundary:
        u,v = edge_dir[ue]; ju, jv = u+back_offset, v+back_offset
        out.append((verts[u], verts[v], back_verts[jv-back_offset]))
        out.append((verts[u], back_verts[jv-back_offset], back_verts[ju-back_offset]))
    return out

def make_mesh_from_tris(tris, name="MoldMesh", weld_eps=WELD_EPS_DEFAULT):
    v2i, verts, faces = {}, [], []
    def key(p): return _rounded_key(p, weld_eps)
    for (a,b,c) in tris:
        ids=[]
        for p in (a,b,c):
            k=key(p)
            if k not in v2i: v2i[k]=len(verts); verts.append(k)
            ids.append(v2i[k])
        if area2(verts[ids[0]], verts[ids[1]], verts[ids[2]]) > AREA_MIN:
            faces.append(tuple(ids))
    mesh=bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts], [], faces)
    mesh.validate(verbose=True); mesh.update()
    obj=bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj

def clean_mesh(obj, weld_eps):
    mesh=obj.data
    bm=bmesh.new(); bm.from_mesh(mesh)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_eps*0.25)
    bmesh.ops.dissolve_degenerate(bm, dist=weld_eps*0.20)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_eps)
    bmesh.ops.dissolve_degenerate(bm, dist=weld_eps*0.20)
    boundary=[e for e in bm.edges if len(e.link_faces)==1]
    if boundary: bmesh.ops.holes_fill(bm, edges=boundary)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    bm.to_mesh(mesh); bm.free()
    mesh.validate(verbose=True); mesh.update()

def create_cylinders_z_aligned(holes, thickness, radius=0.0015875, embed_offset=0.0025):
    cylinders=[]
    for h in holes:
        x,y,z = to_vec3(h); depth=float(thickness)
        center_z = z - (embed_offset + depth/2.0)
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=(x,y,center_z))
        cylinders.append(bpy.context.active_object)
    return cylinders

def apply_boolean_difference(target_obj, cutters):
    bpy.context.view_layer.objects.active = target_obj
    for cutter in cutters:
        mod = target_obj.modifiers.new(name="Boolean", type='BOOLEAN')
        mod.operation='DIFFERENCE'; mod.object=cutter
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(cutter, do_unlink=True)

# --------------- build triangles (Swift parity) ---------------
def build_triangles(beardline, neckline, params):
    if not beardline: raise ValueError("Empty beardline supplied.")
    lip_segments    = int(params.get("lipSegments", 100))
    arc_steps       = int(params.get("arcSteps", 24))
    max_lip_radius  = float(params.get("maxLipRadius", 0.008))
    min_lip_radius  = float(params.get("minLipRadius", 0.003))
    taper_mult      = float(params.get("taperMult", 25.0))
    extrusion_depth = float(params.get("extrusionDepth", -0.008))
    prelift         = float(params.get("prelift", 0.0003))  # tiny raise to avoid coplanar z

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5*(minX+maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult, prelift=prelift
    )

    faces=[]
    # lip ring quads
    faces += quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    # basePoints ↔ first ring cap (Swift: faces.append([a,c,b]); faces.append([b,c,d]))
    for i in range(len(base_points)-1):
        a = base_points[i]; b = base_points[i+1]
        c = lip_vertices[i*ring_count + 0]
        d = lip_vertices[(i+1)*ring_count + 0]
        faces.append([a, c, b]); faces.append([b, c, d])

    # end fans (Swift end-caps)
    if base_points:
        first_base = base_points[0]
        for j in range(ring_count-1):
            a = lip_vertices[j]; b = lip_vertices[j+1]
            faces.append([a, b, first_base])
        last_base = base_points[-1]
        start_idx = (len(base_points)-1)*ring_count
        for j in range(ring_count-1):
            a = lip_vertices[start_idx + j]; b = lip_vertices[start_idx + j + 1]
            faces.append([a, b, last_base])

    # beardline ↔ neckline (Swift nearest-neighbor method)
    if neckline:
        faces += strap_beardline_to_neckline_swift(beardline, neckline)

    faces = [tri for tri in faces if area2(tri[0], tri[1], tri[2]) > AREA_MIN]
    weld_eps = float(params.get("weldEps", WELD_EPS_DEFAULT))
    extruded = extrude_surface_z_solid(faces, extrusion_depth, weld_eps=weld_eps)
    return extruded, abs(extrusion_depth), weld_eps

# -------------------- main pipeline --------------------
def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)

def voxel_remesh_if_requested(obj, voxel_size):
    if voxel_size <= 0: return
    try:
        for o in bpy.data.objects: o.select_set(False)
        obj.select_set(True); bpy.context.view_layer.objects.active = obj
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        bpy.ops.object.voxel_remesh(voxel_size=float(voxel_size), adaptivity=0.0)
    except Exception: pass

def report_non_manifold(obj):
    try:
        mesh=obj.data; bm=bmesh.new(); bm.from_mesh(mesh)
        nonman = [e for e in bm.edges if len(e.link_faces) not in (1,2)]
        bound  = [e for e in bm.edges if len(e.link_faces)==1]
        print(f"Non-manifold edges: {len(nonman)} | Boundary edges: {len(bound)}")
        bm.free()
    except Exception: pass

def main():
    argv = sys.argv; argv = argv[argv.index("--")+1:] if "--" in argv else []
    if len(argv)!=2: raise ValueError("Expected input and output paths after '--'")
    input_path, output_path = argv
    with open(input_path,'r') as f: data=json.load(f)

    beardline_in = data.get("beardline") or data.get("vertices")
    if beardline_in is None: raise ValueError("Missing 'beardline' (or legacy 'vertices').")
    beardline = [to_vec3(v) for v in beardline_in]

    neckline_in = data.get("neckline")
    neckline = [to_vec3(v) for v in neckline_in] if neckline_in else []
    if neckline: neckline = smooth_vertices_open(neckline, passes=3)

    holes_in = data.get("holeCenters") or data.get("holes") or []
    params = data.get("params", {})

    tris, thickness, weld_eps = build_triangles(beardline, neckline, params)

    mold_obj = make_mesh_from_tris(tris, name="BeardMold", weld_eps=weld_eps)
    clean_mesh(mold_obj, weld_eps)

    voxel_size = float(params.get("voxelRemesh", VOXEL_DEFAULT))
    voxel_remesh_if_requested(mold_obj, voxel_size)
    if voxel_size > 0: clean_mesh(mold_obj, weld_eps)

    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference(mold_obj, cutters)
        clean_mesh(mold_obj, weld_eps)

    report_non_manifold(mold_obj)
    for o in bpy.data.objects: o.select_set(False)
    mold_obj.select_set(True); bpy.context.view_layer.objects.active = mold_obj
    export_stl_selected(output_path)

    print(
        f"STL export complete for job ID: {data.get('job_id', data.get('jobID','N/A'))} "
        f"overlay: {data.get('overlay','N/A')} "
        f"verts(beardline)={len(beardline)} neckline={len(neckline)} "
        f"holes={len(holes_in)} weld_eps={weld_eps} voxel={voxel_size}"
    )

if __name__ == "__main__":
    main()
