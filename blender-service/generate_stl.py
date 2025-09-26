# File: blender_mold_maker.py
import bpy, bmesh, json, sys, math
from mathutils import Vector

ROUND = 6  # aligns with client rounding; helps welding

def vkey(p): return (round(p[0], ROUND), round(p[1], ROUND), round(p[2], ROUND))
def to_vec3(p): return (float(p["x"]), float(p["y"]), float(p["z"]))

def map_from_coord_frame(p, frame):
    # Optional: map ARKit face frame -> Blender (Z-up). If you kept both sides consistent,
    # leave as identity. Enable if your VM expects Blender axes.
    if frame == "arkit_face_to_blender":
        x, y, z = p
        return (x, z, -y)
    return p  # identity

def area2(a,b,c):
    ab=(b[0]-a[0],b[1]-a[1],b[2]-a[2]); ac=(c[0]-a[0],c[1]-a[1],c[2]-a[2])
    cx=ab[1]*ac[2]-ab[2]*ac[1]; cy=ab[2]*ac[0]-ab[0]*ac[2]; cz=ab[0]*ac[1]-ab[1]*ac[0]
    return cx*cx+cy*cy+cz*cz

def smooth_vertices_open(verts, passes=1):
    if len(verts)<3 or passes<=0: return verts[:]
    V=verts[:]
    for _ in range(passes):
        NV=[V[0]]
        for i in range(1,len(V)-1):
            px,py,pz=V[i-1]; cx,cy,cz=V[i]; nx,ny,nz=V[i+1]
            NV.append(((px+cx+nx)/3.0,(py+cy+ny)/3.0,(pz+cz+nz)/3.0))
        NV.append(V[-1]); V=NV
    return V

def sample_base_points_along_x(beardline, lip_segments):
    xs=[p[0] for p in beardline]; ys=[p[1] for p in beardline]; zs=[p[2] for p in beardline]
    min_x=min(xs) if xs else -0.05; max_x=max(xs) if xs else 0.05
    seg_w=(max_x-min_x)/max(1,(lip_segments-1))
    fallback_y=max(ys) if ys else 0.03; fallback_z=(sum(zs)/len(zs)) if zs else 0.0
    base=[]
    for i in range(lip_segments):
        x=min_x+i*seg_w
        if beardline:
            # linear interp for continuity, not nearest-only
            # find neighbors
            j = 0
            while j < len(beardline)-2 and beardline[j+1][0] < x: j += 1
            a, b = beardline[j], beardline[j+1]
            t = 0.0 if b[0]==a[0] else (x - a[0])/(b[0]-a[0])
            y = a[1]*(1-t)+b[1]*t; z = a[2]*(1-t)+b[2]*t
            base.append((x,y,z))
        else:
            base.append((x,fallback_y,fallback_z))
    return base, min_x, max_x

def tapered_radius(x, cx, min_r, max_r, taper_mult):
    taper=max(0.0, 1.0-abs(x-cx)*taper_mult)
    return min_r + taper*(max_r-min_r)

def generate_lip_rings(base_points, arc_steps, min_r, max_r, center_x, taper_mult, profile_bias, prelift):
    ring_count = arc_steps + 1
    verts=[]
    for bx,by,bz in base_points:
        r=tapered_radius(bx,center_x,min_r,max_r,taper_mult)
        r=max(r, min_r)  # guard
        for j in range(ring_count):
            angle=math.pi*(j/float(arc_steps))
            angle_b=angle**profile_bias
            y=by - r*(1.0 - math.sin(angle_b)) + prelift
            z=bz + r*math.cos(angle_b)
            verts.append((bx,y,z))
    return verts, ring_count

def quads_to_tris_between_rings(lip_vertices, base_count, ring_count):
    faces=[]
    for i in range(base_count-1):
        for j in range(ring_count-1):
            a=lip_vertices[i*ring_count + j]
            b=lip_vertices[i*ring_count + j + 1]
            c=lip_vertices[(i+1)*ring_count + j]
            d=lip_vertices[(i+1)*ring_count + j + 1]
            faces.append([a,c,b]); faces.append([b,c,d])
    return faces

def first_ring_column(lip_vertices, base_count, ring_count):
    return [lip_vertices[i*ring_count] for i in range(base_count)]

def resample_polyline_by_x(points, xs):
    if not points: return [(x,0.0,0.0) for x in xs]
    P=sorted(points,key=lambda p:p[0])
    out=[]; k=0; n=len(P)
    for x in xs:
        if x<=P[0][0]: out.append((x,P[0][1],P[0][2])); continue
        if x>=P[-1][0]: out.append((x,P[-1][1],P[-1][2])); continue
        while k < n-2 and P[k+1][0] < x: k += 1
        a,b=P[k],P[k+1]
        t=0.0 if b[0]==a[0] else (x-a[0])/(b[0]-a[0])
        y=a[1]*(1-t)+b[1]*t; z=a[2]*(1-t)+b[2]*t
        out.append((x,y,z))
    return out

def strap_tris_equal_counts(A,B):
    faces=[]; m=min(len(A),len(B))
    for i in range(m-1):
        faces.append([A[i],B[i],A[i+1]]); faces.append([A[i+1],B[i],B[i+1]])
    return faces

def extrude_surface_z_solid(tri_faces, depth):
    # depth may be negative; we want positive thickness and consistent back direction (+Z here)
    th = abs(float(depth))
    v2i={}; verts=[]; tris_idx=[]
    def idx_of(p):
        k=vkey(p); i=v2i.get(k)
        if i is None: i=len(verts); v2i[k]=i; verts.append(k)
        return i
    for a,b,c in tri_faces:
        ia,ib,ic=idx_of(a),idx_of(b),idx_of(c); tris_idx.append((ia,ib,ic))
    edge_count={}; edge_dir={}
    for ia,ib,ic in tris_idx:
        for (u,v) in ((ia,ib),(ib,ic),(ic,ia)):
            ue=(min(u,v),max(u,v)); edge_count[ue]=edge_count.get(ue,0)+1
            edge_dir.setdefault(ue,(u,v))
    boundary=[ue for ue,c in edge_count.items() if c==1]
    back_offset=len(verts)
    back_verts=[(x,y,z+th) for (x,y,z) in verts]  # back = +Z
    out=[]
    for ia,ib,ic in tris_idx:
        out.append((verts[ia],verts[ib],verts[ic]))  # front
        ja,jb,jc = ia+back_offset, ib+back_offset, ic+back_offset
        out.append((back_verts[jc-back_offset], back_verts[jb-back_offset], back_verts[ja-back_offset]))  # back (reversed)
    for ue in boundary:
        u,v = edge_dir[ue]; ju,jv = u+back_offset, v+back_offset
        out.append((verts[u],verts[v],back_verts[jv-back_offset]))
        out.append((verts[u],back_verts[jv-back_offset],back_verts[ju-back_offset]))
    return out, th

def make_mesh_from_tris(tris, name="MoldMesh"):
    v2i={}; verts=[]; faces=[]
    for (a,b,c) in tris:
        ids=[]
        for p in (a,b,c):
            k=vkey(p)
            if k not in v2i: v2i[k]=len(verts); verts.append(k)
            ids.append(v2i[k])
        if area2(verts[ids[0]], verts[ids[1]], verts[ids[2]]) > 1e-18:
            faces.append(tuple(ids))
    mesh=bpy.data.meshes.new(name)
    mesh.from_pydata([Vector(v) for v in verts], [], faces)
    mesh.validate(verbose=False); mesh.update()
    obj=bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)

    bm=bmesh.new(); bm.from_mesh(mesh)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bmesh.ops.dissolve_degenerate(bm, dist=1e-7)
    boundary_edges=[e for e in bm.edges if len(e.link_faces)==1]
    if boundary_edges: bmesh.ops.holes_fill(bm, edges=boundary_edges)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    bm.to_mesh(mesh); bm.free()
    mesh.validate(verbose=False); mesh.update()

    bpy.context.view_layer.objects.active = obj
    for o in bpy.data.objects: o.select_set(False)
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT"); bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj

def voxel_remesh_if_requested(obj, voxel_size):
    if voxel_size <= 0: return
    for o in bpy.data.objects: o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    bpy.ops.object.voxel_remesh(voxel_size=float(voxel_size), adaptivity=0.0)

def create_cylinders_z_aligned(holes, thickness, radius=0.0015875, embed_offset=0.0025):
    cutters=[]
    depth=float(thickness)+float(embed_offset)*2.0  # ensure full pass-through
    for h in holes:
        x,y,z = to_vec3(h)
        center_z = z - (embed_offset + thickness/2.0)
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=(x,y,center_z))
        cutters.append(bpy.context.active_object)
    return cutters

def apply_boolean_difference(target_obj, cutters):
    bpy.context.view_layer.objects.active = target_obj
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    for cutter in cutters:
        mod=target_obj.modifiers.new(name="Boolean", type="BOOLEAN")
        mod.object=cutter; mod.operation="DIFFERENCE"; mod.solver="EXACT"
        try: bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception:
            mod.solver="FAST"
            try: bpy.ops.object.modifier_apply(modifier=mod.name)
            except Exception: pass
        try: bpy.data.objects.remove(cutter, do_unlink=True)
        except Exception: pass

def export_stl_selected(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)

def build_triangles(beardline, neckline, params):
    if not beardline: raise ValueError("Empty beardline supplied.")
    lip_segments   = max(8, int(params.get("lipSegments", 100)))
    arc_steps      = max(6, int(params.get("arcSteps", 24)))
    max_lip_radius = max(0.0005, float(params.get("maxLipRadius", 0.008)))
    min_lip_radius = max(0.0002, float(params.get("minLipRadius", 0.003)))
    taper_mult     = float(params.get("taperMult", 25.0))
    extrusion_depth= float(params.get("extrusionDepth", -0.008))
    profile_bias   = float(params.get("profileBias", 1.0))
    prelift        = float(params.get("prelift", 0.0))

    base_points, min_x, max_x = sample_base_points_along_x(beardline, lip_segments)
    center_x = 0.5*(min_x+max_x)
    lip_vertices, ring_count = generate_lip_rings(
        base_points, arc_steps, min_lip_radius, max_lip_radius, center_x, taper_mult, profile_bias, prelift
    )

    faces=[]
    faces += quads_to_tris_between_rings(lip_vertices, len(base_points), ring_count)

    xs = [bp[0] for bp in base_points]
    beard_X = resample_polyline_by_x(beardline, xs)
    ring0 = first_ring_column(lip_vertices, len(base_points), ring_count)
    faces += strap_tris_equal_counts(ring0, beard_X)

    if neckline:
        neck_X = resample_polyline_by_x(neckline, xs)
        faces += strap_tris_equal_counts(beard_X, neck_X)

    faces = [tri for tri in faces if area2(tri[0],tri[1],tri[2]) > 1e-18]
    extruded, thickness = extrude_surface_z_solid(faces, extrusion_depth)
    return extruded, thickness

def main():
    argv=sys.argv; argv = argv[argv.index("--")+1:] if "--" in argv else []
    if len(argv)!=2: raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

    with open(input_path,"r") as f: data=json.load(f)

    # IDs (compat)
    job_id = data.get("job_id") or data.get("jobID") or "N/A"

    # Coordinate frame hint
    coord_frame = data.get("coord_frame", "arkit_face")  # or "arkit_face_to_blender" to swap axes

    # Ingest curves
    beardline_in = data.get("beardline") or data.get("vertices")
    if beardline_in is None: raise ValueError("Missing 'beardline'/'vertices' in payload.")
    beard_raw = [to_vec3(v) for v in beardline_in]
    neck_raw  = [to_vec3(v) for v in (data.get("neckline") or [])]

    # Optional axis map
    beardline = [map_from_coord_frame(p, coord_frame) for p in beard_raw]
    neckline  = [map_from_coord_frame(p, coord_frame) for p in neck_raw]

    # Smooth (match client)
    params = data.get("params", {})
    neck_passes = int(params.get("neckSmoothPasses", data.get("neckSmoothPasses", 3)))
    if neckline: neckline = smooth_vertices_open(neckline, passes=neck_passes)

    # Build shell
    tris, thickness = build_triangles(beardline, neckline, params)
    mold_obj = make_mesh_from_tris(tris, name=f"Mold_{job_id}")

    # Remesh (smaller voxel to avoid pinholes)
    voxel_size = float(params.get("voxelRemesh", 0.0003))
    voxel_remesh_if_requested(mold_obj, voxel_size)

    # Holes
    holes_in = data.get("holeCenters") or data.get("holes") or []
    if holes_in:
        cutters = create_cylinders_z_aligned(holes_in, thickness,
                    radius=float(params.get("holeRadius", 0.0015875)),
                    embed_offset=float(params.get("embedOffset", 0.0025)))
        apply_boolean_difference(mold_obj, cutters)

    # Export
    for o in bpy.data.objects: o.select_set(False)
    mold_obj.select_set(True); bpy.context.view_layer.objects.active = mold_obj
    export_stl_selected(output_path)
    print(f"STL export complete | job_id={job_id} | beard={len(beardline)} neck={len(neckline)} holes={len(holes_in)}")

if __name__ == "__main__":
    main()
