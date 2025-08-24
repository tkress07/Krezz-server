# generate_stl.py
# Full Blender logic to replicate Swift's generateSTLFile mold generation pipeline

import bpy
import bmesh
import math
import json
import sys
import os
from mathutils import Vector
from math import cos, sin, pi

# --- Helpers ---
def clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)

def create_mesh(name, vertices, faces):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj

def extrude_mesh(obj, depth, taper_radius, lip_radius):
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)

    bpy.ops.object.convert(target='MESH')
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0, 0, -depth)})
    bpy.ops.object.mode_set(mode='OBJECT')

    # Apply taper and lip (basic implementation)
    bpy.ops.object.modifier_add(type='BEVEL')
    obj.modifiers['Bevel'].width = lip_radius
    bpy.ops.object.modifier_add(type='SIMPLE_DEFORM')
    obj.modifiers['SimpleDeform'].deform_method = 'TAPER'
    obj.modifiers['SimpleDeform'].factor = taper_radius
    bpy.ops.object.convert(target='MESH')

def add_cylinder_hole(obj, position, radius=0.003, depth=0.02):
    bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=radius, depth=depth, location=position)
    cyl = bpy.context.object
    cyl.location.z -= depth / 2
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    cyl.select_set(True)
    bpy.ops.object.boolean_modifier_add(type='DIFFERENCE')
    obj.modifiers[-1].object = cyl
    bpy.ops.object.modifier_apply(modifier=obj.modifiers[-1].name)
    bpy.data.objects.remove(cyl)

def smooth_bezier(vertices, connections):
    # Optional: implement Bézier smoothing if required
    return vertices, connections

# --- Main ---
def main():
    data = json.load(sys.stdin)

    vertices_data = data['vertices']
    shared_connections = data.get('sharedConnections', [])
    hole_indices = data.get('holeIndices', [])
    overlay_config = data.get('overlayConfig', {})
    job_id = data.get('jobID', 'mold')

    extrude_depth = overlay_config.get('extrudeDepth', 0.01)
    taper_radius = overlay_config.get('taperRadius', 0.0)
    lip_radius = overlay_config.get('addLipRadius', 0.0)

    vertices = [Vector((v['x'], v['y'], v['z'])) for v in vertices_data]
    faces = [conn for conn in shared_connections if len(conn) == 3]

    clear_scene()
    mold = create_mesh("MoldMesh", vertices, faces)
    extrude_mesh(mold, extrude_depth, taper_radius, lip_radius)

    for index in hole_indices:
        if index < len(vertices):
            hole_pos = vertices[index]
            add_cylinder_hole(mold, hole_pos)

    # Export
    export_path = f"/mnt/data/{job_id}.stl"
    bpy.ops.export_mesh.stl(filepath=export_path, use_selection=True)
    print(json.dumps({"status": "success", "path": export_path}))

if __name__ == "__main__":
    main()
