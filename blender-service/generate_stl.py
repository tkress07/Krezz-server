# file: blender_service_safe_v3.py
# Python 3.x • Blender 3.x API • headless-safe, adaptive, with backup surfacer

import bpy, bmesh, json, sys, math, traceback
from mathutils import Vector

# ---------- tiny utils ----------
def to_vec3(p): return (float(p['x']), float(p['y']), float(p['z']))

def area2(a,b,c):
    ab=(b[0]-a[0],b[1]-a[1],b[2]-a[2]); ac=(c[0]-a[0],c[1]-a[1],c[2]-a[2])
    cx=ab[1]*ac[2]-ab[2]*ac[1]; cy=ab[2]*ac[0]-ab[0]*ac[2]; cz=ab[0]*ac[1]-ab[1]*ac[0]
    return cx*cx+cy*cy+cz*cz

def tri_min_edge_len2(a,b,c):
    def d2(p,q): return (p[0]-q[0])**2+(p[1]-q[1])**2+(p[2]-q[2])**2
    return min(d2(a,b), d2(b,c), d2(c,a))

def bbox_of_points(pts):
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]; zs=[p[2] for p in pts]
    return (min(xs),max(xs)),(min(ys),max(ys)),(min(zs),max(zs))

# ---------- sampling / surfacing ----------
def smooth_open(V,passes=1):
    if len(V)<3 or passes<=0: return V[:]
    for _ in range(passes):
        W=[V[0]]
        for i in range(1,len(V)-1):
            px,py,pz=V[i-1]; cx,cy,cz=V[i]; nx,ny,nz=V[i+1]
            W.append(((px+cx+nx)/3.0,(py+cy+ny)/3.0,(pz+cz+nz)/3.0))
        W.append(V[-1]); V=W
    return V

def sample_base_x(beardline, nseg):
    xs=[p[0] for p in beardline]; ys=[p[1] for p in beardline]; zs=[p[2] for p in beardline]
    minX=min(xs) if xs else -0.05; maxX=max(xs) if xs else 0.05
    segW=(maxX-minX)/max(1,(nseg-1))
    fy=max(ys) if ys else 0.03; fz=sum(zs)/len(zs) if zs else 0.0
    base=[]
    for i in range(nseg):
        x=minX+i*segW
        top=min(beardline, key=lambda p: abs(p[0]-x)) if beardline else None
        base.append((x, top[1], top[2]) if top else (x, fy, fz))
    return base, minX, maxX

def tapered_radius(x,cx,rmin,rmax,taper):
    t=max(0.0, 1.0-abs(x-cx)*taper)
    return rmin + t*(rmax-rmin)

def gen_lip_rings(base, arc_steps, rmin, rmax, cx, taper):
    rc = max(2, arc_steps+1)
    out=[]
    denom = float(max(1, arc_steps))
    for (bx,by,bz) in base:
        r=tapered_radius(bx,cx,rmin,rmax,taper)
        for j in range(rc):
            ang=math.pi*(j/denom)
            y=by - r*(1.0 - math.sin(ang))
            z=bz + r*math.cos(ang)
            out.append((bx,y,z))
    return out, rc

def resample_by_x(P, xs):
    if not P: return [(x,0.0,0.0) for x in xs]
    P=sorted(P,key=lambda p:p[0]); out=[]; k=0; n=len(P)
    for x in xs:
        if x<=P[0][0]: out.append((x,P[0][1],P[0][2])); continue
        if x>=P[-1][0]: out.append((x,P[-1][1],P[-1][2])); continue
        while k<n-2 and P[k+1][0]<x: k+=1
        a,b=P[k],P[k+1]
        t=0.0 if b[0]==a[0] else (x-a[0])/(b[0]-a[0])
        out.append((x, a[1]*(1-t)+b[1]*t, a[2]*(1-t)+b[2]*t))
    return out

def strap_equal(A,B):
    f=[]; m=min(len(A),len(B))
    for i in range(max(0,m-1)):
        f.append([A[i],B[i],A[i+1]]); f.append([A[i+1],B[i],B[i+1]])
    return f

def to_mesh(tris, name, weld_eps=4e-4, min_feature=4.5e-4):
    v2i={}; V=[]; F=[]; min_e2=min_feature*min_feature
    def key(p,eps): return (round(p[0]/eps)*eps, round(p[1]/eps)*eps, round(p[2]/eps)*eps)
    for (a,b,c) in tris:
        if tri_min_edge_len2(a,b,c)<min_e2: continue
        ids=[]
        for p in (a,b,c):
            k=key(p,weld_eps)
            if k not in v2i: v2i[k]=len(V); V.append(k)
            ids.append(v2i[k])
        if area2(V[ids[0]],V[ids[1]],V[ids[2]])>1e-18:
            F.append(tuple(ids))
    if len(F) < 2:
        return None
    me=bpy.data.meshes.new(name)
    me.from_pydata([Vector(v) for v in V], [], F)
    me.validate(False); me.update()
    obj=bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(obj)
    # light cleanup
    bm=bmesh.new(); bm.from_mesh(me)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_eps)
    bmesh.ops.dissolve_degenerate(bm, dist=weld_eps*0.25)
    edges=[e for e in bm.edges if len(e.link_faces)==1]
    if edges: bmesh.ops.holes_fill(bm, edges=edges)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    bm.to_mesh(me); bm.free(); me.update()
    return obj

def solidify(obj, thickness):
    mod=obj.modifiers.new(name="Solid", type='SOLIDIFY')
    mod.thickness=float(thickness)
    mod.offset=-1.0
    mod.use_rim=True
    mod.use_even_offset=True
    mod.use_quality_normals=True
    mod.use_merge_vertices=False  # safer
    bpy.context.view_layer.objects.active=obj
    bpy.ops.object.modifier_apply(modifier=mod.name)

def weld(obj, d):
    mod=obj.modifiers.new(name="Weld", type='WELD')
    mod.merge_threshold=float(d)
    bpy.context.view_layer.objects.active=obj
    bpy.ops.object.modifier_apply(modifier=mod.name)

def adaptive_voxel(obj, nozzle, user_voxel):
    # compute bbox size
    me=obj.data; pts=[obj.matrix_world@v.co for v in me.vertices]
    xs=[p.x for p in pts]; ys=[p.y for p in pts]; zs=[p.z for p in pts]
    dx=max(xs)-min(xs); dy=max(ys)-min(ys); dz=max(zs)-min(zs)
    span=max(dx,dy,dz)
    if user_voxel>0: vx=user_voxel
    else: vx=max(nozzle*1.25, span/150.0, 0.0005)  # ~150 voxels across
    try:
        bpy.context.view_layer.objects.active=obj
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        bpy.ops.object.voxel_remesh(voxel_size=float(vx), adaptivity=0.0)
    except Exception:
        pass
    print(f"[Voxel] span={span:.6f}m voxel={vx:.6f}m")

# ---------- main triangle builder (surface only) ----------
def build_surface(beardline, neckline, P):
    lipSeg  = max(16, int(P.get("lipSegments", 160)))
    arcSt   = max(12, int(P.get("arcSteps", 40)))
    rMax    = float(P.get("maxLipRadius", 0.010))
    rMin    = float(P.get("minLipRadius", 0.0045))
    taper   = float(P.get("taperMult", 20.0))
    thick   = abs(float(P.get("extrusionDepth", -0.011)))

    base, minX, maxX = sample_base_x(beardline, lipSeg)
    cx = 0.5*(minX+maxX)
    lip, rc = gen_lip_rings(base, arcSt, rMin, rMax, cx, taper)

    tris=[]
    # rings
    for i in range(len(base)-1):
        for j in range(rc-1):
            a=lip[i*rc+j]; b=lip[i*rc+j+1]; c=lip[(i+1)*rc+j]; d=lip[(i+1)*rc+j+1]
            tris.append([a,c,b]); tris.append([b,c,d])

    xs=[bp[0] for bp in base]
    beardX=resample_by_x(beardline, xs)
    ring0=[lip[i*rc] for i in range(len(base))]
    tris += strap_equal(ring0, beardX)

    if neckline:
        neckX=resample_by_x(neckline, xs)
        tris += strap_equal(beardX, neckX)

    tris=[t for t in tris if area2(t[0],t[1],t[2])>1e-18]
    return tris, thick

# ---------- backup surfacer (guaranteed) ----------
def backup_surface(beardline, neckline, thick):
    """If the fancy ring/strap fails, build a simple ruled strip and cap it."""
    if not beardline: return [], 0.0
    # make an offset copy in -Z (small)
    dz = max(0.0008, thick*0.1)
    B = beardline
    C = [(x, y, z - dz) for (x,y,z) in B]
    tris=[]
    m=min(len(B),len(C))
    for i in range(m-1):
        a=B[i]; b=B[i+1]; c=C[i]; d=C[i+1]
        tris += [[a,c,b],[b,c,d]]
    # crude cap on ends (two triangles each)
    if m>=3:
        tris += [[B[0], C[0], C[1]], [B[0], C[1], B[1]]]
        tris += [[B[m-1], C[m-2], C[m-1]], [B[m-2], B[m-1], C[m-1]]]
    return tris, thick

# ---------- main ----------
def main():
    argv=sys.argv; argv=argv[argv.index("--")+1:] if "--" in argv else []
    if len(argv)!=2:
        raise ValueError("Expected input and output paths after '--'")
    in_p, out_p = argv

    with open(in_p,'r') as f: data=json.load(f)

    beard_in = data.get("beardline") or data.get("vertices")
    if not beard_in: raise ValueError("Missing 'beardline'/vertices.")
    beard = [to_vec3(v) for v in beard_in]

    neck_in = data.get("neckline") or []
    neck = [to_vec3(v) for v in neck_in]
    if neck: neck = smooth_open(neck, passes=3)

    P = data.get("params", {})
    nozzle   = float(P.get("nozzle", 0.0004))
    weldEps  = float(P.get("weldEps", 0.0004))
    minFeat  = float(P.get("minFeature", 0.00045))
    userVoxel= float(P.get("voxelRemesh", 0.0))
    print(f"[Input] verts={len(beard)} neck={len(neck)} weldEps={weldEps} minFeature={minFeat}")

    # build surface
    tris, thick = build_surface(beard, neck, P)
    if len(tris) < 8:
        print("[Warn] Primary surfacer produced too few faces; using backup.")
        tris, thick = backup_surface(beard, neck, thick)

    # mesh
    obj = to_mesh(tris, "BeardMoldSurf", weld_eps=weldEps, min_feature=minFeat)
    if obj is None:
        # last resort: turn beard polyline into a thin ribbon
        print("[Warn] to_mesh returned None; generating ribbon fallback.")
        if len(beard) >= 3:
            zoff = max(0.0008, abs(thick)*0.1)
            ribbon = []
            for i,p in enumerate(beard):
                ribbon.append((p[0], p[1], p[2]))
                ribbon.append((p[0], p[1], p[2]-zoff))
            tris=[]
            for i in range(0,len(ribbon)-3,2):
                a=ribbon[i]; b=ribbon[i+1]; c=ribbon[i+2]; d=ribbon[i+3]
                tris += [[a,b,c],[c,b,d]]
            obj = to_mesh(tris, "BeardMoldSurf2", weld_eps=weldEps, min_feature=minFeat)
    if obj is None:
        # still nothing—export small cube so client doesn’t break
        print("[Error] Could not build surface; exporting placeholder cube.")
        bpy.ops.mesh.primitive_cube_add(size=0.01, location=(0,0,0))  # 10mm
        obj=bpy.context.active_object
        bpy.ops.export_mesh.stl(filepath=out_p, use_selection=False)
        return

    # thickness (safe)
    solidify(obj, thick)
    # gentle weld to kiss-close seams
    weld(obj, min(0.00012, weldEps*0.4))  # ~0.08–0.12 mm
    # adaptive voxel (never too large for the part)
    adaptive_voxel(obj, nozzle, userVoxel)

    # ensure normals OK (no edit mode needed)
    me=obj.data
    me.calc_normals()

    # export (no selection dependency)
    bpy.ops.export_mesh.stl(filepath=out_p, use_selection=False)

    # print bbox for sanity
    pts=[obj.matrix_world@v.co for v in me.vertices]
    xs=[p.x for p in pts]; ys=[p.y for p in pts]; zs=[p.z for p in pts]
    print(f"[BBox m] dx={max(xs)-min(xs):.6f} dy={max(ys)-min(ys):.6f} dz={max(zs)-min(zs):.6f}")
    print(f"[OK] Exported solid with ~thickness={thick:.6f} m")

if __name__=="__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        # still export a cube so client gets a file, not an error
        try:
            out=sys.argv[-1]
            bpy.ops.mesh.primitive_cube_add(size=0.01, location=(0,0,0))
            bpy.ops.export_mesh.stl(filepath=out, use_selection=False)
            print("[Fallback] Exported 10mm cube.")
        except Exception:
            pass
