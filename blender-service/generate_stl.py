# === Beard Mold (tiny-sliver hardening) ===
import bpy, bmesh, json, sys, math, statistics
from mathutils import Vector

# -------- Tunables (safe defaults) --------
WELD_EPS_DEFAULT = 0.0002          # vertex weld tolerance (m)
AREA_MIN         = 5e-12           # was 5e-13; raise to cull razor-thin tris early
VOXEL_DEFAULT    = 0.0             # off unless requested
# ------------------------------------------

# ---------------------------
# Helpers (geometry & math)
# ---------------------------
def to_vec3(p): return (float(p['x']), float(p['y']), float(p['z']))

def area2(a,b,c):
    ab=(b[0]-a[0],b[1]-a[1],b[2]-a[2]); ac=(c[0]-a[0],c[1]-a[1],c[2]-a[2])
    cx=ab[1]*ac[2]-ab[2]*ac[1]; cy=ab[2]*ac[0]-ab[0]*ac[2]; cz=ab[0]*ac[1]-ab[1]*ac[0]
    return cx*cx+cy*cy+cz*cz

def _dist(u,v):
    dx=v[0]-u[0]; dy=v[1]-u[1]; dz=v[2]-u[2]
    return (dx*dx+dy*dy+dz*dz)**0.5

def smooth_vertices_open(vertices, passes=1):
    if len(vertices)<3 or passes<=0: return vertices[:]
    V=vertices[:]
    for _ in range(passes):
        NV=[V[0]]
        for i in range(1,len(V)-1):
            px,py,pz=V[i-1]; cx,cy,cz=V[i]; nx,ny,nz=V[i+1]
            NV.append(((px+cx+nx)/3.0,(py+cy+ny)/3.0,(pz+cz+nz)/3.0))
        NV.append(V[-1]); V=NV
    return V

def smooth_row_keep_x(row, passes=2):
    if len(row)<3 or passes<=0: return row
    R=row[:]
    for _ in range(passes):
        N=[R[0]]
        for i in range(1,len(R)-1):
            x=R[i][0]
            y=(R[i-1][1]+R[i][1]+R[i+1][1])/3.0
            z=(R[i-1][2]+R[i][2]+R[i+1][2])/3.0
            N.append((x,y,z))
        N.append(R[-1]); R=N
    return R

def resample_polyline_by_x(points,xs):
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

def sample_base_points_along_x(beardline, lip_segments, eps=1e-6):
    if not beardline:
        cols=[(-0.008+0.016*i/max(1,lip_segments-1),0.03,0.0) for i in range(lip_segments)]
        return cols,-0.008,0.008
    P=sorted(beardline,key=lambda p:p[0])
    minX,maxX=P[0][0],P[-1][0]
    xs=[minX+i*(maxX-minX)/max(1,lip_segments-1) for i in range(lip_segments)]
    cols=resample_polyline_by_x(P,xs)
    out=[]; last=None
    for x,y,z in cols:
        if last is not None and abs(x-last)<eps: x=last+eps
        out.append((x,y,z)); last=x
    return out,minX,maxX

def tapered_radius(x, centerX, min_r, max_r, taper_mult):
    return min_r + max(0.0,1.0-abs(x-centerX)*taper_mult)*(max_r-min_r)

def generate_lip_rings(base_points, arc_steps, min_r, max_r, centerX, taper_mult):
    ring_count=arc_steps+1; verts=[]
    for (bx,by,bz) in base_points:
        r=tapered_radius(bx,centerX,min_r,max_r,taper_mult)
        for j in range(ring_count):
            ang=math.pi*(j/float(arc_steps))
            y=by - r*(1.0-math.sin(ang))
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
            faces.append([a,c,b])
            faces.append([b,c,d])
    return faces

def strap_tris_equal_counts(A,B):
    faces=[]; m=min(len(A),len(B))
    for i in range(m-1):
        faces.append([A[i],B[i],A[i+1]])
        faces.append([A[i+1],B[i],B[i+1]])
    return faces

# ---------------------------
# Solid, manifold extrusion
# ---------------------------
def _rounded_key(p,eps): return (round(p[0]/eps)*eps,round(p[1]/eps)*eps,round(p[2]/eps)*eps)

def extrude_surface_z_solid(tri_faces, depth, weld_eps):
    v2i={}; verts=[]; tris_idx=[]
    def idx_of(p):
        k=_rounded_key(p,weld_eps); i=v2i.get(k)
        if i is None: i=len(verts); v2i[k]=i; verts.append(k)
        return i
    for a,b,c in tri_faces:
        ia=idx_of(a); ib=idx_of(b); ic=idx_of(c); tris_idx.append((ia,ib,ic))
    edge_count={}; edge_dir={}
    for ia,ib,ic in tris_idx:
        for (u,v) in ((ia,ib),(ib,ic),(ic,ia)):
            ue=(min(u,v),max(u,v)); edge_count[ue]=edge_count.get(ue,0)+1
            if ue not in edge_dir: edge_dir[ue]=(u,v)
    boundary=[ue for ue,c in edge_count.items() if c==1]
    back_offset=len(verts); back_verts=[(x,y,z+depth) for (x,y,z) in verts]
    out=[]
    for ia,ib,ic in tris_idx:
        out.append((verts[ia],verts[ib],verts[ic]))
        ja,jb,jc=ia+back_offset,ib+back_offset,ic+back_offset
        out.append((back_verts[jc-back_offset],back_verts[jb-back_offset],back_verts[ja-back_offset]))
    for ue in boundary:
        u,v=edge_dir[ue]; ju,jv=u+back_offset,v+back_offset
        out.append((verts[u],verts[v],back_verts[jv-back_offset]))
        out.append((verts[u],back_verts[jv-back_offset],back_verts[ju-back_offset]))
    return out

def make_mesh_from_tris(tris,name="MoldMesh",weld_eps=WELD_EPS_DEFAULT):
    v2i,verts,faces_idx={},[],[]
    def key(p): return _rounded_key(p,weld_eps)
    for (a,b,c) in tris:
        ids=[]
        for p in (a,b,c):
            k=key(p)
            if k not in v2i: v2i[k]=len(verts); verts.append(k)
            ids.append(v2i[k])
        if area2(verts[ids[0]],verts[ids[1]],verts[ids[2]])>AREA_MIN:
            faces_idx.append(tuple(ids))
    uniq,setfaces=set(),[]
    for (i,j,k) in faces_idx:
        fkey=tuple(sorted((i,j,k)))
        if fkey in uniq: continue
        uniq.add(fkey); setfaces.append((i,j,k))
    mesh=bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts],[],setfaces)
    mesh.validate(verbose=True); mesh.update()
    obj=bpy.data.objects.new(name,mesh); bpy.context.collection.objects.link(obj); return obj

# ---------------------------
# Mesh cleanup / diagnostics
# ---------------------------
def _do_clean(bm,weld_dist,degenerate_dist):
    bmesh.ops.remove_doubles(bm,verts=bm.verts,dist=weld_dist*0.25)
    bmesh.ops.dissolve_degenerate(bm,dist=degenerate_dist*0.25)
    bmesh.ops.remove_doubles(bm,verts=bm.verts,dist=weld_dist)
    bmesh.ops.dissolve_degenerate(bm,dist=degenerate_dist*0.5)
    boundary_edges=[e for e in bm.edges if len(e.link_faces)==1]
    if boundary_edges: bmesh.ops.holes_fill(bm,edges=boundary_edges)

def clean_mesh(obj,weld_eps,min_feature=None,strong=False):
    mesh=obj.data; bm=bmesh.new(); bm.from_mesh(mesh)
    mf=float(min_feature) if (min_feature is not None) else weld_eps*0.8
    weld_dist=max(weld_eps,0.8*mf); weld_dist*=1.25 if strong else 1.0
    _do_clean(bm,weld_dist,mf)
    bmesh.ops.recalc_face_normals(bm,faces=bm.faces)
    bmesh.ops.triangulate(bm,faces=bm.faces,quad_method='BEAUTY',ngon_method='BEAUTY')
    bm.to_mesh(mesh); bm.free(); mesh.validate(verbose=True); mesh.update()

def mesh_diagnostics(obj):
    mesh=obj.data; bm=bmesh.new(); bm.from_mesh(mesh)
    nonman=[e for e in bm.edges if len(e.link_faces) not in (1,2)]
    boundary=[e for e in bm.edges if len(e.link_faces)==1]
    shortest=1e9
    for e in bm.edges:
        try: shortest=min(shortest,float(e.calc_length()))
        except Exception: pass
    bm.free()
    print(f"[diag] boundary={len(boundary)} nonmanifold={len(nonman)} minEdge={shortest:.6f} m")
    return len(boundary),len(nonman),shortest

def count_duplicate_faces(obj):
    me=obj.data; bm=bmesh.new(); bm.from_mesh(me)
    bm.verts.ensure_lookup_table(); bm.faces.ensure_lookup_table()
    seen,dup=set(),0
    for f in bm.faces:
        key=tuple(sorted(v.index for v in f.verts))
        if key in seen: dup+=1
        else: seen.add(key)
    bm.free(); print(f"[diag] duplicate_faces={dup}"); return dup

def slice_islands(obj,z,tol=1e-5):
    me=obj.data; bm=bmesh.new(); bm.from_mesh(me); edges=0
    for e in bm.edges:
        z0=e.verts[0].co.z; z1=e.verts[1].co.z
        if (z0-z)*(z1-z)<=0 and abs(z0-z)>tol and abs(z1-z)>tol: edges+=1
    bm.free(); print(f"[diag] cross_edges@z={z:.4f} -> {edges}"); return edges

def report_all(obj):
    mesh_diagnostics(obj); count_duplicate_faces(obj)
    z_top=max(v.co.z for v in obj.data.vertices); slice_islands(obj,z_top-0.0005)

# ---------------------------
# Payload helpers / compose
# ---------------------------
def _lower_keys(obj):
    if isinstance(obj,dict): return {(k.lower() if isinstance(k,str) else k):_lower_keys(v) for k,v in obj.items()}
    if isinstance(obj,list): return [_lower_keys(v) for v in obj]
    return obj

def _unify_params(params_any):
    params_any=params_any or {}
    params_lc={(k.lower() if isinstance(k,str) else k):v for k,v in params_any.items()}
    out=dict(params_any)
    def use(cam,lc):
        if cam not in out and lc in params_lc: out[cam]=params_lc[lc]
    for cam in ["lipSegments","arcSteps","maxLipRadius","minLipRadius","taperMult",
                "extrusionDepth","weldEps","minFeature","voxelRemesh","embedOffset",
                "holeRadius","autoRemesh","minColDx","baseSmoothPasses","neckSmoothPasses",
                "sliverHeight"]:
        use(cam,cam.lower())
    if "voxelSize" in out and "voxelRemesh" not in out: out["voxelRemesh"]=out["voxelSize"]
    if "voxelsize" in params_lc and "voxelRemesh" not in out: out["voxelRemesh"]=params_lc["voxelsize"]
    return out

def _snap_close_endpoints(sorted_pts,tol=1e-4):
    if len(sorted_pts)>2:
        a,b=sorted_pts[0],sorted_pts[-1]
        dx=a[0]-b[0]; dy=a[1]-b[1]; dz=a[2]-b[2]
        if (dx*dx+dy*dy+dz*dz)**0.5<tol: sorted_pts[-1]=(a[0],a[1],a[2])
    return sorted_pts

def _dedupe_exact(points,eps=0.0):
    out,last=[],None
    for p in points:
        if last is None or abs(p[0]-last[0])>eps or abs(p[1]-last[1])>eps or abs(p[2]-last[2])>eps:
            out.append(p); last=p
    return out

def _split_by_discontinuity(points,mult=8.0,floor=0.002):
    if len(points)<3: return [points]
    P=sorted(points,key=lambda p:p[0])
    steps=[math.dist(P[i+1],P[i]) for i in range(len(P)-1)]
    med=max(1e-9,statistics.median(steps)); thr=max(floor,mult*med)
    segs=[[P[0]]]
    for i in range(1,len(P)):
        if math.dist(P[i],P[i-1])>thr: segs.append([P[i]])
        else: segs[-1].append(P[i])
    return segs

def _split_segments(points):
    P=_dedupe_exact(points); P=sorted(P,key=lambda p:p[0])
    segs=_split_by_discontinuity(P,mult=8.0,floor=0.002)
    return [s for s in segs if len(s)>=3]

def _interp_on_segment(seg,x):
    if not seg: return None
    if x<seg[0][0] or x>seg[-1][0]: return None
    for k in range(len(seg)-1):
        a,b=seg[k],seg[k+1]
        if a[0]<=x<=b[0]:
            t=0.0 if b[0]==a[0] else (x-a[0])/(b[0]-a[0])
            return (x, a[1]*(1-t)+b[1]*t, a[2]*(1-t)+b[2]*t)
    return (x, seg[-1][1], seg[-1][2])

def compose_beardline_uniform(segments,sample_count=400,eps=1e-6):
    if not segments: return []
    xs_min=min(s[0][0] for s in segments if s); xs_max=max(s[-1][0] for s in segments if s)
    if xs_max<=xs_min: return segments[0][:]
    xs=[xs_min+i*(xs_max-xs_min)/max(1,sample_count-1) for i in range(sample_count)]
    endpoints=[seg[0] for seg in segments]+[seg[-1] for seg in segments]
    out=[]; last=None
    for x in xs:
        cands=[_interp_on_segment(seg,x) for seg in segments]
        cands=[p for p in cands if p is not None]
        if cands: px,py,pz=max(cands,key=lambda q:q[1])
        else:
            nearest=min(endpoints,key=lambda e:abs(e[0]-x)); px,py,pz=(x,nearest[1],nearest[2])
        if last is not None and abs(px-last)<eps: px=last+eps
        out.append((px,py,pz)); last=px
    print(f"[compose:beard] segments={len(segments)} X[{xs_min:.4f},{xs_max:.4f}] samples={len(out)}")
    return out

# ---------------------------
# Front sheet consolidation & sliver filtering
# ---------------------------
def _is_skinny(a,b,c,min_h):
    L=max(_dist(a,b),_dist(b,c),_dist(c,a))
    A=(area2(a,b,c)**0.5)/2.0
    h=0.0 if L<1e-12 else (2*A)/L
    return h<min_h

def _filter_skinny(tris,min_h):
    return [t for t in tris if not _is_skinny(t[0],t[1],t[2],min_h)]

def consolidate_front_sheet(faces,weld_eps,min_feature):
    bm=bmesh.new(); vmap={}
    def v_for(p):
        k=_rounded_key(p,weld_eps); v=vmap.get(k)
        if v is None: v=bm.verts.new(Vector(k)); vmap[k]=v
        return v
    for (a,b,c) in faces:
        va,vb,vc=v_for(a),v_for(b),v_for(c)
        try: bm.faces.new([va,vb,vc])
        except ValueError: pass
    bm.verts.ensure_lookup_table(); bm.edges.ensure_lookup_table(); bm.faces.ensure_lookup_table()
    bmesh.ops.remove_doubles(bm,verts=bm.verts,dist=weld_eps)
    bmesh.ops.dissolve_degenerate(bm,dist=max(min_feature*0.25,1e-7))
    bmesh.ops.recalc_face_normals(bm,faces=bm.faces)
    # re-diagonalize skinny quads before triangulating
    bmesh.ops.beautify_fill(bm,faces=bm.faces,edges=bm.edges)
    bmesh.ops.triangulate(bm,faces=bm.faces,quad_method='BEAUTY',ngon_method='BEAUTY')
    tris=[]
    for f in bm.faces:
        if len(f.verts)==3:
            a,b,c=f.verts; tris.append((tuple(a.co),tuple(b.co),tuple(c.co)))
    bm.free(); return tris

# ---------------------------
# Build triangles
# ---------------------------
def build_triangles(beardline,neckline,params):
    if not beardline: raise ValueError("Empty beardline supplied.")
    lip_segments   = int(params.get("lipSegments",100))
    arc_steps      = int(params.get("arcSteps",24))
    max_r          = float(params.get("maxLipRadius",0.008))
    min_r          = float(params.get("minLipRadius",0.003))
    taper_mult     = float(params.get("taperMult",25.0))
    extrusion_depth= float(params.get("extrusionDepth",-0.008))
    weld_eps       = float(params.get("weldEps",WELD_EPS_DEFAULT))
    min_feature    = float(params.get("minFeature",max(0.0012,weld_eps*2.0)))
    base_smooth    = int(params.get("baseSmoothPasses",2))
    neck_smooth    = int(params.get("neckSmoothPasses",3))
    min_col_dx     = float(params.get("minColDx",5e-5))
    sliver_h       = float(params.get("sliverHeight",min_feature*0.25))

    # 1) Base points (strictly increasing X), then light smoothing
    base_points,minX,maxX = sample_base_points_along_x(beardline, lip_segments)
    if base_smooth>0: base_points = smooth_row_keep_x(base_points, passes=base_smooth)
    centerX=0.5*(minX+maxX)

    # 2) Lip rings
    lip_vertices, ring_count = generate_lip_rings(base_points, arc_steps, min_r, max_r, centerX, taper_mult)

    faces=[]
    # 2a) Ring quads
    faces += quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    # 2b) Cap base ↔ ring0, skip almost-zero columns (prevents ribbon slivers)
    for i in range(len(base_points)-1):
        a=base_points[i]; b=base_points[i+1]
        if abs(b[0]-a[0])<min_col_dx: continue
        c=lip_vertices[i*ring_count+0]; d=lip_vertices[(i+1)*ring_count+0]
        faces.append([a,c,b]); faces.append([b,c,d])

    # 3) Strap to neckline (optional)
    if neckline:
        xs=[bp[0] for bp in base_points]
        neck_X = resample_polyline_by_x(neckline, xs)
        if neck_smooth>0: neck_X = smooth_row_keep_x(neck_X, passes=neck_smooth)
        faces += strap_tris_equal_counts(base_points, neck_X)

    # 4) Cull razor-thin triangles early + quality filter
    faces = [tri for tri in faces if area2(tri[0],tri[1],tri[2])>AREA_MIN]
    faces = _filter_skinny(faces, sliver_h)

    # 5) Consolidate the sheet (weld/beautify/triangulate)
    faces = consolidate_front_sheet(faces, weld_eps=weld_eps, min_feature=min_feature)
    faces = _filter_skinny(faces, sliver_h)   # post-pass, just in case

    # 6) Solid extrusion with welded sides
    extruded = extrude_surface_z_solid(faces, extrusion_depth, weld_eps=weld_eps)
    return extruded, abs(extrusion_depth), weld_eps

# ---------------------------
# IO / main pipeline
# ---------------------------
def export_stl_selected(filepath): bpy.ops.export_mesh.stl(filepath=filepath,use_selection=True)

def voxel_remesh_if_requested(obj,voxel_size):
    if voxel_size<=0: return
    try:
        for o in bpy.data.objects: o.select_set(False)
        obj.select_set(True); bpy.context.view_layer.objects.active=obj
        bpy.ops.object.transform_apply(location=False,rotation=False,scale=True)
        bpy.ops.object.voxel_remesh(voxel_size=float(voxel_size),adaptivity=0.0)
    except Exception: pass

def report_non_manifold(obj):
    try:
        mesh=obj.data; bm=bmesh.new(); bm.from_mesh(mesh)
        nonman=[e for e in bm.edges if len(e.link_faces) not in (1,2)]
        boundary=[e for e in bm.edges if len(e.link_faces)==1]
        print(f"Non-manifold edges: {len(nonman)} | Boundary edges: {len(boundary)}")
        bm.free()
    except Exception: pass

def ensure_watertight(obj,params):
    weld_eps=float(params.get("weldEps",WELD_EPS_DEFAULT))
    min_feature=float(params.get("minFeature",0.0012))
    voxel_size=float(params.get("voxelRemesh",0.0))
    allow_auto=bool(params.get("autoRemesh",False))
    b,n,shortest=mesh_diagnostics(obj); count_duplicate_faces(obj)
    if not allow_auto:
        print("[fix] auto-remesh disabled (autoRemesh=false)."); return
    needs=(b>0 or n>0 or shortest<min_feature*0.25)
    if needs:
        suggested=max(voxel_size, min_feature*0.75)
        print(f"[fix] auto-remesh → voxel={suggested:.6f} (was {voxel_size:.6f})")
        voxel_remesh_if_requested(obj, suggested)
        clean_mesh(obj, weld_eps, min_feature=min_feature, strong=True)
        mesh_diagnostics(obj); count_duplicate_faces(obj)

def main():
    argv=sys.argv; argv=argv[argv.index("--")+1:] if "--" in argv else []
    if len(argv)!=2: raise ValueError("Expected input and output file paths after '--'")
    input_path,output_path=argv
    with open(input_path,'r') as f: data=json.load(f)
    data_lc=_lower_keys(data)

    beardline_in = data.get("beardline") or data.get("vertices") or data_lc.get("beardline") or data_lc.get("vertices")
    if beardline_in is None: raise ValueError("Missing 'beardline' (or legacy 'vertices').")

    beardline_raw=[to_vec3(v) for v in beardline_in]
    beard_segments=_split_segments(beardline_raw)
    print(f"[clean:beard] segments={len(beard_segments)} sizes={[len(s) for s in beard_segments]}")
    beardline = compose_beardline_uniform(beard_segments, sample_count=max(400, int((data.get('params') or {}).get('lipSegments',160))*2))
    beardline = _snap_close_endpoints(sorted(beardline,key=lambda p:p[0]), tol=1e-4)

    neckline_in = data.get("neckline") or data_lc.get("neckline")
    if neckline_in:
        neckline_pts=[to_vec3(v) for v in neckline_in]
        neck_segs=_split_segments(neckline_pts)
        neck=max(neck_segs, key=len) if neck_segs else neckline_pts
        neckline = smooth_vertices_open(sorted(neck,key=lambda p:p[0]), passes=3)
    else:
        neckline=[]

    holes_in = data.get("holeCenters") or data.get("holes") or data_lc.get("holecenters") or data_lc.get("holes") or []
    params_any = data.get("params") or data_lc.get("params") or {}
    params = _unify_params(params_any)

    tris, thickness, weld_eps = build_triangles(beardline, neckline, params)

    mold_obj = make_mesh_from_tris(tris, name="BeardMold", weld_eps=weld_eps)
    mf_param = params.get("minFeature")
    clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=False)
    clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=True)

    voxel_size=float(params.get("voxelRemesh",VOXEL_DEFAULT))
    voxel_remesh_if_requested(mold_obj, voxel_size)
    if voxel_size>0: clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=True)

    if holes_in:
        radius=float(params.get("holeRadius",0.0015875)); embed_offset=float(params.get("embedOffset",0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference(mold_obj, cutters)
        clean_mesh(mold_obj, weld_eps, min_feature=mf_param, strong=True)

    ensure_watertight(mold_obj, params); report_non_manifold(mold_obj)

    for obj in bpy.data.objects: obj.select_set(False)
    mold_obj.select_set(True); bpy.context.view_layer.objects.active=mold_obj
    export_stl_selected(output_path)

    print(f"STL export complete for job ID: {data.get('job_id', data.get('jobID','N/A'))} overlay: {data.get('overlay','N/A')} "
          f"verts(beardline)={len(beardline)} neckline={len(neckline)} holes={len(holes_in)} "
          f"weld_eps={weld_eps} voxel={voxel_size}")

if __name__=="__main__": main()
