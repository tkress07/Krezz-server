# file: blender_service_baseline_extrude.py
# Python 3.x • Blender 3.x API • “return to working shape” baseline

import bpy, bmesh, json, sys, math
from mathutils import Vector

# ---------------------------
# Geometry helpers
# ---------------------------

def to_vec3(p): return (float(p['x']), float(p['y']), float(p['z']))

def area2(a,b,c):
    ab=(b[0]-a[0],b[1]-a[1],b[2]-a[2]); ac=(c[0]-a[0],c[1]-a[1],c[2]-a[2])
    cx=ab[1]*ac[2]-ab[2]*ac[1]; cy=ab[2]*ac[0]-ab[0]*ac[2]; cz=ab[0]*ac[1]-ab[1]*ac[0]
    return cx*cx+cy*cy+cz*cz

def tri_min_edge_len2(a,b,c):
    def d2(p,q): return (p[0]-q[0])**2+(p[1]-q[1])**2+(p[2]-q[2])**2
    return min(d2(a,b), d2(b,c), d2(c,a))

def smooth_vertices_open(V, passes=1):
    if len(V)<3 or passes<=0: return V[:]
    for _ in range(passes):
        W=[V[0]]
        for i in range(1,len(V)-1):
            px,py,pz=V[i-1]; cx,cy,cz=V[i]; nx,ny,nz=V[i+1]
            W.append(((px+cx+nx)/3.0,(py+cy+ny)/3.0,(pz+cz+nz)/3.0))
        W.append(V[-1]); V=W
    return V

# ---------------------------
# Lip surface (your previous approach)
# ---------------------------

def sample_base_points_along_x(beardline, lip_segments):
    xs=[p[0] for p in beardline]; ys=[p[1] for p in beardline]; zs=[p[2] for p in beardline]
    minX=min(xs) if xs else -0.05; maxX=max(xs) if xs else 0.05
    seg_w=(maxX-minX)/max(1,(lip_segments-1))
    fallbackY=max(ys) if ys else 0.03
    fallbackZ=(sum(zs)/len(zs)) if zs else 0.0
    base=[]
    for i in range(lip_segments):
        x=minX+i*seg_w
        top=min(beardline, key=lambda p: abs(p[0]-x)) if beardline else None
        base.append((x, top[1], top[2]) if top else (x, fallbackY, fallbackZ))
    return base, minX, maxX

def tapered_radius(x,cx,min_r,max_r,taper_mult):
    t=max(0.0, 1.0-abs(x-cx)*taper_mult)
    return min_r + t*(max_r-min_r)

def generate_lip_rings(base_points, arc_steps, min_r, max_r, centerX, taper_mult):
    ring_count=arc_steps+1
    verts=[]
    denom=float(max(1,arc_steps))
    for (bx,by,bz) in base_points:
        r=tapered_radius(bx,centerX,min_r,max_r,taper_mult)
        for j in range(ring_count):
            ang=math.pi*(j/denom)
            y=by - r*(1.0 - math.sin(ang))
            z=bz + r*math.cos(ang)
            verts.append((bx,y,z))
    return verts, ring_count

def quads_to_tris_between_rings(lip_vertices, base_count, ring_count):
    faces=[]
    for i in range(base_count-1):
        for j in range(ring_count-1):
            a=lip_vertices[i*ring_count+j]
            b=lip_vertices[i*ring_count+j+1]
            c=lip_vertices[(i+1)*ring_count+j]
            d=lip_vertices[(i+1)*ring_count+j+1]
            faces.append([a,c,b]); faces.append([b,c,d])
    return faces

def first_ring_column(lip_vertices, base_count, ring_count):
    return [lip_vertices[i*ring_count+0] for i in range(base_count)]

def resample_polyline_by_x(points, xs):
    if not points: return [(x,0.0,0.0) for x in xs]
    P=sorted(points,key=lambda p:p[0]); out=[]; k=0; n=len(P)
    for x in xs:
        if x<=P[0][0]: out.append((x,P[0][1],P[0][2])); continue
        if x>=P[-1][0]: out.append((x,P[-1][1],P[-1][2])); continue
        while k<n-2 and P[k+1][0]<x: k+=1
        a,b=P[k],P[k+1]
        t=0.0 if b[0]==a[0] else (x-a[0])/(b[0]-a[0])
        out.append((x, a[1]*(1-t)+b[1]*t, a[2]*(1-t)+b[2]*t))
    return out

def strap_tris_equal_counts(A,B):
    faces=[]; m=min(len(A),len(B))
    for i in range(m-1):
        faces.append([A[i],B[i],A[i+1]])
        faces.append([A[i+1],B[i],B[i+1]])
    return faces

# ---------------------------
# Your original Z-extrusion (kept)
# ---------------------------

def _rounded_key(p, eps):
    return (round(p[0]/eps)*eps, round(p[1]/eps)*eps, round(p[2]/eps)*eps)

def extrude_surface_z_solid(tri_faces, depth, weld_eps):
    """Build a closed solid by duplicating verts in -Z and stitching sides."""
    v2i={}; verts=[]; tris_idx=[]
    def idx_of(p):
        k=_rounded_key(p, weld_eps)
        i=v2i.get(k)
        if i is None: i=len(verts); v2i[k]=i; verts.append(k)
        return i
    for a,b,c in tri_faces:
        ia=idx_of(a); ib=idx_of(b); ic=idx_of(c)
        tris_idx.append((ia,ib,ic))

    # boundary edges
    edge_count={}; edge_dir={}
    for ia,ib,ic in tris_idx:
        for (u,v) in ((ia,ib),(ib,ic),(ic,ia)):
            ue=(min(u,v),max(u,v))
            edge_count[ue]=edge_count.get(ue,0)+1
            if ue not in edge_dir: edge_dir[ue]=(u,v)
    boundary=[ue for ue,c in edge_count.items() if c==1]

    back_offset=len(verts)
    back_verts=[(x,y,z+depth) for (x,y,z) in verts]

    out=[]
    # front + back
    for ia,ib,ic in tris_idx:
        out.append((verts[ia], verts[ib], verts[ic]))
        ja, jb, jc = ia+back_offset, ib+back_offset, ic+back_offset
        out.append((back_verts[jc-back_offset], back_verts[jb-back_offset], back_verts[ja-back_offset]))
    # sides
    for ue in boundary:
        u,v=edge_dir[ue]; ju, jv = u+back_offset, v+back_offset
        out.append((verts[u], verts[v], back_verts[jv-back_offset]))
        out.append((verts[u], back_verts[jv-back_offset], back_verts[ju-back_offset]))
    return out

def make_mesh_from_tris(tris, name="MoldMesh", weld_eps=5e-4, min_feature=6e-4):
    """Create mesh and clean it to guarantee watertightness for slicing."""
    v2i={}; verts=[]; faces=[]
    min_edge2=min_feature*min_feature
    def key(p): return _rounded_key(p, weld_eps)
    for (a,b,c) in tris:
        if tri_min_edge_len2(a,b,c) < min_edge2: continue
        ids=[]
        for p in (a,b,c):
            k=key(p)
            if k not in v2i:
                v2i[k]=len(verts); verts.append(k)
            ids.append(v2i[k])
        if area2(verts[ids[0]],verts[ids[1]],verts[ids[2]]) > 1e-18:
            faces.append(tuple(ids))

    me=bpy.data.meshes.new(name)
    me.from_pydata([Vector(v) for v in verts], [], faces)
    me.validate(False); me.update()
    obj=bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(obj)

    # Clean (mesh-level only; no modifiers)
    bm=bmesh.new(); bm.from_mesh(me)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_eps)
    bmesh.ops.dissolve_degenerate(bm, dist=weld_eps*0.25)
    boundary_edges=[e for e in bm.edges if len(e.link_faces)==1]
    if boundary_edges:
        bmesh.ops.holes_fill(bm, edges=boundary_edges)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    bm.to_mesh(me); bm.free()
    me.validate(False); me.update()

    # Force consistent outward normals
    me.calc_normals()
    return obj

# ---------------------------
# Build triangles (surface), then Z-extrude to solid
# ---------------------------

def build_triangles(beardline, neckline, params):
    if not beardline: raise ValueError("Empty beardline supplied.")
    lip_segments     = int(params.get("lipSegments", 160))
    arc_steps        = int(params.get("arcSteps", 40))
    max_lip_radius   = float(params.get("maxLipRadius", 0.010))
    min_lip_radius   = float(params.get("minLipRadius", 0.0045))
    taper_mult       = float(params.get("taperMult", 20.0))
    extrusion_depth  = float(params.get("extrusionDepth", -0.011))  # meters; negative means “down”

    base_points, minX, maxX = sample_base_points_along_x(beardline, lip_segments)
    centerX = 0.5*(minX+maxX)

    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, centerX, taper_mult
    )

    faces=[]
    faces += quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    xs=[bp[0] for bp in base_points]
    beard_X = resample_polyline_by_x(beardline, xs)
    ring0   = first_ring_column(lip_vertices, len(base_points), ring_count)
    faces += strap_tris_equal_counts(ring0, beard_X)

    if neckline:
        neck_X = resample_polyline_by_x(neckline, xs)
        faces += strap_tris_equal_counts(beard_X, neck_X)

    faces = [tri for tri in faces if area2(tri[0],tri[1],tri[2]) > 1e-18]
    weld_eps = float(params.get("weldEps", 5e-4))      # ↑ slightly to guarantee fusing
    extruded = extrude_surface_z_solid(faces, extrusion_depth, weld_eps=weld_eps)
    return extruded

# ---------------------------
# Main
# ---------------------------

def main():
    argv=sys.argv; argv=argv[argv.index("--")+1:] if "--" in argv else []
    if len(argv)!=2: raise ValueError("Expected input and output paths after '--'")
    input_path, output_path = argv

    with open(input_path,'r') as f:
        data=json.load(f)

    beardline_in = data.get("beardline") or data.get("vertices")
    if not beardline_in: raise ValueError("Missing 'beardline'/vertices.")
    beardline = [to_vec3(v) for v in beardline_in]

    neckline_in = data.get("neckline") or []
    neckline = [to_vec3(v) for v in neckline_in]
    if neckline: neckline = smooth_vertices_open(neckline, passes=3)

    params = data.get("params", {})
    min_feature = float(params.get("minFeature", 6e-4))  # ↑ to ~0.6 mm
    weld_eps    = float(params.get("weldEps", 5e-4))     # ↑ to ~0.5 mm

    tris = build_triangles(beardline, neckline, params)

    obj = make_mesh_from_tris(tris, name="BeardMold",
                              weld_eps=weld_eps,
                              min_feature=min_feature)

    # Export without relying on selection
    bpy.ops.export_mesh.stl(filepath=output_path, use_selection=False)

    # Log bounding box for sanity
    me=obj.data
    pts=[obj.matrix_world@v.co for v in me.vertices]
    xs=[p.x for p in pts]; ys=[p.y for p in pts]; zs=[p.z for p in pts]
    dx=max(xs)-min(xs); dy=max(ys)-min(ys); dz=max(zs)-min(zs)
    print(f"[OK] Exported solid | bbox(m) dx={dx:.6f} dy={dy:.6f} dz={dz:.6f} | faces={len(me.polygons)}")

if __name__=="__main__":
    main()
