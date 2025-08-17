# generate_stl.py
import bpy
import sys
import json

argv = sys.argv
argv = argv[argv.index("--") + 1:]

input_path = argv[0]
output_path = argv[1]

with open(input_path, "r") as f:
    points = json.load(f)

# Clean scene
bpy.ops.wm.read_factory_settings(use_empty=True)

# Create mesh from points (simple point cloud for now)
mesh = bpy.data.meshes.new("Mold")
obj = bpy.data.objects.new("MoldObject", mesh)
bpy.context.collection.objects.link(obj)

verts = [(v["x"], v["y"], v["z"]) for v in points]
faces = []

mesh.from_pydata(verts, [], faces)
mesh.update()

# Export STL
bpy.ops.export_mesh.stl(filepath=output_path, use_selection=False)
