import bpy
import sys
import os
import json
import traceback

print("\U0001F4CD Blender script started")
print(f"sys.argv = {sys.argv}")

job_id = sys.argv[-1]
INPUT_DIR = os.path.join("input", job_id)
OUTPUT_DIR = os.path.join("output", job_id)
os.makedirs(OUTPUT_DIR, exist_ok=True)

input_stl = os.path.join(INPUT_DIR, "mold.stl")
payload_json = os.path.join(INPUT_DIR, "payload.json")
output_stl = os.path.join(OUTPUT_DIR, f"{job_id}.stl")

try:
    print(f"\U0001F4C2 Input STL: {input_stl}")
    print(f"\U0001F4C4 Payload JSON: {payload_json}")

    with open(payload_json) as f:
        data = json.load(f)

    hole_positions = data.get("hole_positions", [])
    hole_radius = 0.0025
    hole_segments = 16

    def create_cylinder(location, radius, depth, segments):
        z_back_offset = -0.01
        adjusted_location = (
            location[0], location[1] + 0.003, location[2] + z_back_offset
        )
        bpy.ops.mesh.primitive_cylinder_add(
            radius=radius,
            depth=depth,
            vertices=segments,
            location=adjusted_location
        )
        cyl = bpy.context.object
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        return cyl

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="io_mesh_stl")

    bpy.ops.import_mesh.stl(filepath=input_stl)
    if not bpy.context.selected_objects:
        raise Exception("❌ STL import failed: No object selected.")

    mold_obj = bpy.context.selected_objects[0]
    bpy.context.view_layer.objects.active = mold_obj
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    
        # Get bounding box for the mold
    min_bound = mold_obj.bound_box[0]
    max_bound = mold_obj.bound_box[6]
    bounds = [(min_bound[i], max_bound[i]) for i in range(3)]

    def is_valid_position(pos, bounds, margin=0.01):
        return all(
            bounds[i][0] - margin <= pos[i] <= bounds[i][1] + margin
            for i in range(3)
        )


    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.remove_doubles(threshold=0.0005)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')
    
    # Enter edit mode to clean and seal geometry
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')

    # Merge close verts
    bpy.ops.mesh.remove_doubles(threshold=0.0001)

    # Fill open edges if any (optional safety)
    bpy.ops.mesh.fill_holes(sides=6)

    # Recalculate normals
    bpy.ops.mesh.normals_make_consistent(inside=False)

    # Return to object mode
    bpy.ops.object.mode_set(mode='OBJECT')


    print(f"\U0001F529 Adding {len(hole_positions)} holes")
    mold_height = mold_obj.dimensions.z
    hole_depth = .03
    cylinders = []

    for pos in hole_positions:
        if len(pos) == 3:
            cyl = create_cylinder(pos, hole_radius, hole_depth, hole_segments)
            cylinders.append(cyl)

    for i, cyl in enumerate(cylinders):
        mod = mold_obj.modifiers.new(name=f"Hole_{i}", type='BOOLEAN')
        mod.object = cyl
        mod.operation = 'DIFFERENCE'
        mod.solver = 'EXACT'
        bpy.context.view_layer.objects.active = mold_obj
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(cyl, do_unlink=True)

    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.shade_smooth()

    print("📋 Exporting STL...")
    bpy.ops.export_mesh.stl(filepath=output_stl, ascii=False)

    if os.path.exists(output_stl):
        size = os.path.getsize(output_stl) / 1024 / 1024
        print(f"✅ STL export complete: {output_stl} ({size:.2f} MB)")
    else:
        raise Exception("❌ STL file not written.")

    # 🖼️ Set up camera, lighting, and render to PNG
    preview_png = os.path.join(OUTPUT_DIR, "preview.png")

    # Create a camera
    bpy.ops.object.camera_add(location=(0, -0.15, 0.07), rotation=(1.2, 0, 0))
    camera = bpy.context.object
    bpy.context.scene.camera = camera

    # Create a light
    bpy.ops.object.light_add(type='AREA', location=(0, -0.1, 0.1))
    light = bpy.context.object
    light.data.energy = 500

    # Set render engine and resolution
    bpy.context.scene.render.engine = 'CYCLES'
    bpy.context.scene.cycles.device = 'CPU'
    bpy.context.scene.render.resolution_x = 800
    bpy.context.scene.render.resolution_y = 800
    bpy.context.scene.render.filepath = preview_png

    bpy.context.view_layer.update()

    # Render the image
    bpy.ops.render.render(write_still=True)

    print(f"🖼️ Rendered preview image saved to {preview_png}")


except Exception as e:
    print("\U0001F6A8 ERROR DURING SCRIPT EXECUTION:")
    traceback.print_exc()
