# --- change 1: booleans use FAST solver (silence GMP warnings) ----------------
def apply_boolean_union(target_obj, cutters):
    bpy.context.view_layer.objects.active = target_obj
    for cutter in cutters:
        mod = target_obj.modifiers.new(name="Union", type='BOOLEAN')
        mod.operation = 'UNION'
        mod.solver = 'FAST'          # was 'EXACT'
        mod.object = cutter
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception:
            pass
        try:
            bpy.data.objects.remove(cutter, do_unlink=True)
        except Exception:
            pass

def apply_boolean_difference(target_obj, cutters):
    bpy.context.view_layer.objects.active = target_obj
    for cutter in cutters:
        mod = target_obj.modifiers.new(name="Boolean", type='BOOLEAN')
        mod.operation = 'DIFFERENCE'
        mod.solver = 'FAST'          # explicitly FAST
        mod.object = cutter
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception:
            pass
        try:
            bpy.data.objects.remove(cutter, do_unlink=True)
        except Exception:
            pass

# --- change 2: export helper that applies m→mm (or custom) scale --------------
def export_stl_selected(filepath, *, global_scale: float = 1000.0):
    """Export selection to STL. global_scale=1000 converts meters→millimeters."""
    bpy.ops.export_mesh.stl(
        filepath=filepath,
        use_selection=True,
        global_scale=float(global_scale),
    )

# --- change 3: in main(), compute scale from params and pass it ----------------
def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
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

    # NEW: decide STL scale. Default to mm since slicers assume mm.
    stl_units = (params.get("stlUnits") or "mm").lower()  # "mm" or "m"
    stl_scale = float(params.get("stlScale")) if "stlScale" in params else (1000.0 if stl_units == "mm" else 1.0)

    tris, thickness, xs, lip_band_y = build_triangles(beardline, neckline, params)
    mold_obj = make_mesh_from_tris(tris, name="BeardMold")

    add_anchor_ribs(mold_obj, xs, params, thickness, lip_band_y)

    voxel_size = float(params.get("voxelRemesh", 0.0006))
    voxel_remesh_if_requested(mold_obj, voxel_size)

    report_non_manifold(mold_obj)

    for obj in bpy.data.objects:
        obj.select_set(False)
    mold_obj.select_set(True)

    if holes_in:
        radius = float(params.get("holeRadius", 0.0015875))
        embed_offset = float(params.get("embedOffset", 0.0025))
        cutters = create_cylinders_z_aligned(holes_in, thickness, radius=radius, embed_offset=embed_offset)
        apply_boolean_difference(mold_obj, cutters)

    # NEW: export with scale so the model is the right physical size in the slicer
    export_stl_selected(output_path, global_scale=stl_scale)

    print(
        f"STL export complete | scale={stl_scale} | jobID={data.get('jobID','N/A')} "
        f"overlay={data.get('overlay','N/A')} "
        f"verts(beardline)={len(beardline)} neckline={len(neckline)} holes={len(holes_in)}"
    )

if __name__ == "__main__":
    main()
