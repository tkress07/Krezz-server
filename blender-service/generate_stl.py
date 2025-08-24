import bpy
import bmesh
import json
import sys
import math
from mathutils import Vector

def smooth_vertices(vertices, iterations=2):
    for _ in range(iterations):
        new_vertices = []
        for i in range(len(vertices)):
            prev_vertex = vertices[i - 1]
            next_vertex = vertices[(i + 1) % len(vertices)]
            current_vertex = vertices[i]
            new_vertex = (
                (prev_vertex[0] + current_vertex[0] + next_vertex[0]) / 3,
                (prev_vertex[1] + current_vertex[1] + next_vertex[1]) / 3,
                (prev_vertex[2] + current_vertex[2] + next_vertex[2]) / 3,
            )
            new_vertices.append(new_vertex)
        vertices = new_vertices
    return vertices

def create_mold_mesh(vertices, lip_radius=0.002, extrude_depth=-0.006):
    mesh = bpy.data.meshes.new(name="BeardMold")
    obj = bpy.data.objects.new("BeardMold", mesh)
    bpy.context.collection.objects.link(obj)

    bm = bmesh.new()
    top_verts = [bm.verts.new(Vector(v)) for v in vertices]
    bmesh.ops.contextual_create(bm, geom=top_verts)

    bm.verts.index_update()
    bm.faces.index_update()

    # Extrude downward
    ret = bmesh.ops.extrude_face_region(bm, geom=bm.faces[:])
    extruded_geom = ret['geom']
    verts_extruded = [ele for ele in extruded_geom if isinstance(ele, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, verts=verts_extruded, vec=Vector((0, extrude_depth, 0)))

    # Recalculate normals
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)

    # Finish
    bm.to_mesh(mesh)
    bm.free()

    return obj

def create_cylinders(holes, radius=0.0015, depth=0.01):
    cylinders = []
    for hole in holes:
        loc = Vector((hole['x'], hole['y'], hole['z']))
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=loc)
        cyl = bpy.context.active_object
        cyl.rotation_euler[0] = math.radians(90)
        cyl.location.z -= depth / 2
        cylinders.append(cyl)
    return cylinders

def apply_boolean_cut(mold_obj, cutters):
    for cutter in cutters:
        mod = mold_obj.modifiers.new(name="Boolean", type='BOOLEAN')
        mod.object = cutter
        mod.operation = 'DIFFERENCE'
        bpy.context.view_layer.objects.active = mold_obj
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(cutter, do_unlink=True)

def export_stl(filepath):
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)

def main():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    if len(argv) != 2:
        raise ValueError("Expected input and output file paths after '--'")
    input_path, output_path = argv

    with open(input_path, 'r') as f:
        data = json.load(f)

    vertices = [(v['x'], v['y'], v['z']) for v in data['vertices']]
    holes = data.get('holes', [])

    smooth_verts = smooth_vertices(vertices)
    mold_obj = create_mold_mesh(smooth_verts)
    for obj in bpy.data.objects:
        obj.select_set(False)
    mold_obj.select_set(True)

    if holes:
        hole_objs = create_cylinders(holes)
        apply_boolean_cut(mold_obj, hole_objs)

    export_stl(output_path)
    print(f"STL export complete for job ID: {data.get('jobID', 'N/A')} with overlay: {data.get('overlay', 'N/A')}")

if __name__ == '__main__':
    main()
