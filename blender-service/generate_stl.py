# file: blender_service_safe_v2.py
# Python 3.x • Blender 3.x API • Headless-friendly

import bpy, bmesh, json, sys, math, traceback
from mathutils import Vector

# ---------------------------
# Small utilities
# ---------------------------

def to_vec3(p): return (float(p['x']), float(p['y']), float(p['z']))

def area2(a,b,c):
    ab=(b[0]-a[0],b[1]-a[1],b[2]-a[2])
    ac=(c[0]-a[0],c[1]-a[1],c[2]-a[2])
    cx=ab[1]*ac[2]-ab[2]*ac[1]; cy=ab[2]*ac[0]-ab[0]*ac[2]; cz=ab[0]*ac[1]-ab[1]*ac[0]
    return cx*cx+cy*cy+cz*cz

def tri_min_edge_len2(a,b,c):
    def d2(p,q): return (p[0]-q[0])**2+(p[1]-q[1])**2+(p[2]-q[2])**2
    return min(d2(a,b), d2(b,c), d2(c,a))

def safe_min(a,b): return a if a<b else b
def safe_max(a,b): return a if a>b else b

# ---------------------------
# Sampling & surfacing
# ---------------------------

def smooth_vertices_open(V,passes=1):
    if len(V)<3 or passes<=0: return V[:]
    for _ in range(passes):
        NV=[V[0]]
        for i in range(1,len(V)-1):
            px,py,pz=V[i-1]; cx,cy,cz=V[i]; nx,ny,nz=V[i+1]
            NV.append(((px+cx+nx)/3.0,(py+cy+ny)/3.0,(pz+cz+nz)/3.0))
        NV.append(V[-1]); V=NV
    return V

def sample_base_points_along_x(beardline,lip_segments):
    xs=[p[0] for p in beardline]; ys=[p[1] for p in beardline]; zs=[p[2] for p in beardline]
    minX=min(xs) if xs else -0.05; maxX=max(xs) if xs else 0.05
    seg_w=(maxX-minX)/max(1,(lip_segments-1))
    fallbackY=max(ys) if ys else 0.03; fallbackZ=(sum(zs)/len(zs)) if zs else 0.0
    base=[]
    for i in range(lip_segments):
        x=minX+i*seg_w
        top=min(beardline,key=lambda p: abs(p[0]-x)) if beardline else None
        base.append((x, top[1], top[2]) if top else (x, fallbackY, fallbackZ))
    return base, minX, maxX

def tapered_radius(x,cx,min_r,max_r,taper_mult):
    t=max(0.0,1.0-abs(x-cx)*taper_mult)
    return min_r+t*(max_r-min_r)

def generate_lip_rings(base,arc_steps,min_r,max_r,cx,taper_mult):
    ring_count=max(2, arc_steps+1)
    verts=[]
    for (bx,by,bz) in base:
        r=tapered_radius(bx,cx,min_r,max_r,taper_mult)
        for j in range(ring_count):
            ang=math.pi*(j/float(max(1,arc_steps)))
            y=by - r*(1.0 - math.sin(ang))
            z=bz + r*math.cos(ang)
            verts.append((bx,y,z))
    return verts, ring_count

def quads_to_tris_between_rings(lip,base_count,ring_count):
    f=[]
    for i in range(max(0,base_count-1)):
        for j in range(max(0,ring_count-1)):
            a=lip[i*ring_count+j]; b=lip[i*ring_count+j+1]
            c=lip[(i+1)*ring_count+j]; d=lip[(i+1)*ring_count+j+1]
            f.append([a,c,b]); f.append([b,c,d])
    return f

def first_ring_column(lip,base_count,ring_count):
    return [lip[i*ring_count+0] for i in range(base_count)]

def resample_polyline_by_x(P,xs):
    if not P: return [(x,0.0,0.0) for x in xs]
    P=sorted(P,key=lambda p:p[0]); out=[]; k=0; n=len(P)
    for x in xs:
        if x<=P[0][0]: out.append((x,P[0][1],P[0][2])); continue
        if x>=P[-1][0]: out.append((x,P[-1][1],P[-1][2])); continue
        while k<n-2 and P[k+1][0]<x: k+=1
        a,b=P[k],P[k+1]; t=0.0 if b[0]==a[0] else (x-a[0])/(b[0]-a[0])
        y=a[1]*(1-t)+b[1]*t; z=a[2]*(1-t)+b[2]*t
        out.append((x,y,z))
    return out

def strap_tris_equal_counts(A,B):
    f=[]; m=min(len(A),len(B))
    for i in range(max(0,m-1)):
        f.append([A[i],B[i],A[i+1]]); f.append([A[i+1],B[i],B[i+1]])
    return f

# ---------------------------
# Mesh build / cleanup
# ---------------------------

def _rounded_key(p,eps): return (round(p[0]/eps)*eps, round(p[1]/eps)*eps, round(p[2]/eps)*eps)

def make_mesh_from_tris(tris,name="BeardMoldSurf",weld_eps=4e-4,min_feature=4.5e-4):
    v2i={}; V=[]; F=[]; min_e2=min_feature*min_feature
    def key(p): return _rounded_key(p,weld_eps)
    for (a,b,c) in tris:
        if tri_min_edge_len2(a,b,c)<min_e2: continue
        ids=[]
        for p in (a,b,c):
            k=key(p)
            if k not in v2i: v2i[k]=len(V); V.append(k)
            ids.append(v2i[k])
        if area2(V[ids[0]],V[ids[1]],V[ids[2]])>1e-18:
            F.append(tuple(ids))

    if len(F) < 2:
        raise ValueError(f"Surface too small: faces={len(F)} (nothing to solidify)")

    me=bpy.data.meshes.new(name)
    me.from_pydata([Vector(v) for v in V],[],F)
    me.validate(False); me.update()
    obj=bpy.data.objects.new(name,me)
    bpy.context.collection.objects.link(obj)

    # Light cleanup only
    bm=bmesh.new(); bm.from_mesh(me)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_eps)
    bmesh.ops.dissolve_degenerate(bm, dist=weld_eps*0.25)
    edges=[e for e in bm.edges if len(e.link_faces)==1]
    if edges: bmesh.ops.holes_fill(bm, edges=edges)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    bm.to_mesh(me); bm.free()
    me.validate(False); me.update()
    return obj

def solidify_no_merge(obj, thickness):
    mod=obj.modifiers.new(name="Solid", type='SOLIDIFY')
    mod.thickness=float(thickness)
    mod.offset=-1.0
    mod.use_rim=True
    mod.use_quality_normals=True
    mod.use_even_offset=True
    mod.use_merge_vertices=False
    bpy.context.view_layer.objects.active=obj
    bpy.ops.object.modifier_apply(modifier=mod.name)

def apply_weld(obj, merge_dist):
    mod=obj.modifiers.new(name="Weld", type='WELD')
    mod.merge_threshold=float(merge_dist)
    bpy.context.view_layer.objects.active=obj
    bpy.ops.object.modifier_apply(modifier=mod.name)

def voxel_remesh(obj, voxel_size):
    if voxel_size<=0: return
    bpy.context.view_layer.objects.active=obj
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    bpy.ops.object.voxel_remesh(voxel_size=float(voxel_size), adaptivity=0.0)

def report_bbox(obj):
    me=obj.data
    pts=[obj.matrix_world@v.co for v in me.vertices]
    xs=[p.x for p in pts]; ys=[p.y for p in pts]; zs=[p.z for p in pts]
    print(f"[BBox m] dx={max(xs)-min(xs):.6f} dy={max(ys)-min(ys):.6f} dz={max(zs)-min(zs):.6f}")

# ---------------------------
# Surfacing (no custom Z-extrusion)
# ---------------------------

def build_triangles(beardline, neckline, params):
    if not beardline: raise ValueError("Empty beardline.")
    lip_segments = max(8, int(params.get("lipSegments", 100)))
    arc_steps    = max(8, int(params.get("arcSteps", 24)))
    max_r        = float(params.get("maxLipRadius", 0.008))
    min_r        = float(params.get("minLipRadius", 0.003))
    taper        = float(params.get("taperMult", 25.0))
    thickness    = abs(float(params.get("extrusionDepth", -0.010)))  # meters

    base,minX,maxX = sample_base_points_along_x(beardline, lip_segments)
    cx = 0.5*(minX+maxX)

    lip,rc = generate_lip_rings(base, arc_steps, min_r, max_r, cx, taper)

    faces=[]
    faces += quads_to_tris_between_rings(lip, len(base), rc)

    xs = [bp[0] for bp in base]
    beard_X = resample_polyline_by_x(beardline, xs)
    ring0   = first_ring_column(lip, len(base), rc)
    faces += strap_tris_equal_counts(ring0, beard_X)

    if neckline:
        neck_X = resample_polyline_by_x(neckline, xs)
        faces += strap_tris_equal_counts(beard_X, neck_X)

    faces = [tri for tri in faces if area2(tri[0],tri[1],tri[2]) > 1e-18]
    return faces, thickness

# ---------------------------
# Main (defensive)
# ---------------------------

def main():
    try:
        argv=sys.argv; argv=argv[argv.index("--")+1:] if "--" in argv else []
        if len(argv)!=2: raise ValueError("Expected input and output paths after '--'")
        in_p, out_p = argv

        with open(in_p,'r') as f: data=json.load(f)

        beardline_in = data.get("beardline") or data.get("vertices")
        if not beardline_in: raise ValueError("Missing 'beardline'/vertices.")
        beardline = [to_vec3(v) for v in beardline_in]

        neckline_in = data.get("neckline") or []
        neckline = [to_vec3(v) for v in neckline_in]
        if neckline: neckline = smooth_vertices_open(neckline, passes=3)

        _holes_ignored = data.get("holeCenters") or data.get("holes") or []
        params = data.get("params", {})

        nozzle      = float(params.get("nozzle", 0.0004))
        weld_eps    = float(params.get("weldEps", 0.0004))
        min_feature = float(params.get("minFeature", 0.00045))
        voxel       = float(params.get("voxelRemesh", max(nozzle*1.5, 0.0006)))

        # Build surface triangles
        tris, thickness = build_triangles(beardline, neckline, params)
        print(f"[Info] tris={len(tris)} thickness={thickness:.6f} voxel={voxel:.6f}")

        # Create mesh → solidify → tiny weld → voxel
        obj = make_mesh_from_tris(tris, "BeardMoldSurf", weld_eps=weld_eps, min_feature=min_feature)
        solidify_no_merge(obj, thickness)
        apply_weld(obj, merge_dist=min(0.00015, weld_eps*0.5))   # ~0.10–0.15 mm
        voxel_remesh(obj, voxel)
        report_bbox(obj)

        # Consistent normals
        bpy.context.view_layer.objects.active=obj
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode='OBJECT')

        # Export WITHOUT relying on selection
        bpy.ops.export_mesh.stl(filepath=out_p, use_selection=False)
        print(
            f"[OK] STL export | job={data.get('job_id','N/A')} overlay={data.get('overlay','N/A')} "
            f"verts={len(beardline)} neck={len(neckline)} holes_ignored={len(_holes_ignored)} "
            f"weld_eps={weld_eps} min_feature={min_feature} voxel={voxel} thick={thickness}"
        )
    except Exception as e:
        print("[ERROR]", repr(e))
        traceback.print_exc()
        # Create a tiny placeholder cube so the client never gets an empty file.
        try:
            bpy.ops.mesh.primitive_cube_add(size=0.001, location=(0,0,0))
            cube=bpy.context.active_object
            bpy.ops.export_mesh.stl(filepath=out_p, use_selection=False)
            print("[Fallback] Exported placeholder cube to avoid empty response.")
        except Exception:
            pass
        # Re-raise so your server still logs 500 if desired
        raise

if __name__=="__main__":
    main()
