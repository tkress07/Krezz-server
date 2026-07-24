from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy
import bmesh


def argv_after_separator() -> list[str]:
    argv = sys.argv
    return argv[argv.index("--") + 1 :] if "--" in argv else []


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def active_mesh_object():
    objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not objects:
        raise RuntimeError("The uploaded STL did not contain a mesh.")

    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]

    if len(objects) > 1:
        bpy.ops.object.join()

    obj = bpy.context.view_layer.objects.active
    if obj is None or obj.type != "MESH":
        raise RuntimeError("Could not activate the imported mesh.")
    return obj


def clean_mesh(obj, weld_distance: float, fill_boundaries: bool) -> tuple[int, int]:
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)

    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_distance)
    bmesh.ops.dissolve_degenerate(bm, dist=max(weld_distance * 0.25, 1e-8))

    boundary_edges = [edge for edge in bm.edges if len(edge.link_faces) == 1]
    if fill_boundaries and boundary_edges:
        try:
            bmesh.ops.holes_fill(bm, edges=boundary_edges, sides=0)
        except Exception:
            pass

    if bm.faces:
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bmesh.ops.triangulate(bm, faces=bm.faces)

    bm.to_mesh(mesh)
    bm.free()
    mesh.validate(verbose=True)
    mesh.update()

    bm_check = bmesh.new()
    bm_check.from_mesh(mesh)
    boundary_count = sum(1 for edge in bm_check.edges if len(edge.link_faces) == 1)
    nonmanifold_count = sum(1 for edge in bm_check.edges if len(edge.link_faces) not in (1, 2))
    bm_check.free()
    return boundary_count, nonmanifold_count


def voxel_remesh(obj, voxel_size: float) -> None:
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    try:
        obj.data.remesh_voxel_size = voxel_size
        obj.data.remesh_voxel_adaptivity = 0.0
        bpy.ops.object.voxel_remesh()
        return
    except Exception as first_error:
        print(f"voxel_remesh operator fallback: {first_error}")

    modifier = obj.modifiers.new(name="VoxelUnion", type="REMESH")
    modifier.mode = "VOXEL"
    modifier.voxel_size = voxel_size
    modifier.use_smooth_shade = False
    bpy.ops.object.modifier_apply(modifier=modifier.name)


def apply_volume_preserving_smooth(obj, iterations: int, factor: float) -> None:
    if iterations <= 0 or factor <= 0:
        return

    try:
        modifier = obj.modifiers.new(name="LightSurfaceSmooth", type="LAPLACIANSMOOTH")
        modifier.lambda_factor = factor
        modifier.lambda_border = factor * 0.5
        modifier.iterations = iterations
        modifier.use_volume_preserve = True
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    except Exception as error:
        print(f"Laplacian smoothing skipped: {error}")


def apply_small_bevel(obj, width: float, segments: int) -> None:
    if width <= 0 or segments <= 0:
        return

    try:
        modifier = obj.modifiers.new(name="ComfortEdge", type="BEVEL")
        modifier.width = width
        modifier.segments = segments
        modifier.limit_method = "ANGLE"
        modifier.angle_limit = math.radians(38.0)
        modifier.harden_normals = True
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    except Exception as error:
        print(f"Bevel skipped: {error}")


def main() -> None:
    args = argv_after_separator()
    if len(args) != 3:
        raise RuntimeError("Expected input STL, output STL, and params JSON paths.")

    input_path = Path(args[0])
    output_path = Path(args[1])
    params_path = Path(args[2])

    params = json.loads(params_path.read_text(encoding="utf-8"))
    voxel_size = float(params.get("voxelSize", 0.00035))
    weld_distance = float(params.get("weldDistance", 0.00020))
    smooth_iterations = int(params.get("smoothIterations", 2))
    smooth_factor = float(params.get("smoothFactor", 0.16))
    bevel_width = float(params.get("bevelWidth", 0.00020))
    bevel_segments = int(params.get("bevelSegments", 3))
    output_scale = float(params.get("outputScale", 1000.0))

    clear_scene()
    bpy.ops.import_mesh.stl(filepath=str(input_path))
    obj = active_mesh_object()

    clean_mesh(obj, weld_distance=weld_distance, fill_boundaries=False)

    # This is the critical operation: every overlapping cheek, center, and lip
    # component becomes one continuous watertight volume. Sub-millimeter seams
    # disappear instead of remaining as separate triangle soups.
    voxel_remesh(obj, voxel_size=voxel_size)
    clean_mesh(obj, weld_distance=weld_distance, fill_boundaries=True)

    apply_volume_preserving_smooth(
        obj,
        iterations=smooth_iterations,
        factor=smooth_factor,
    )
    apply_small_bevel(obj, width=bevel_width, segments=bevel_segments)

    boundary_count, nonmanifold_count = clean_mesh(
        obj,
        weld_distance=max(weld_distance * 0.5, 1e-7),
        fill_boundaries=True,
    )

    # A second remesh is a safety net only when the first cleanup still reports
    # open or non-manifold edges.
    if boundary_count > 0 or nonmanifold_count > 0:
        voxel_remesh(obj, voxel_size=voxel_size)
        boundary_count, nonmanifold_count = clean_mesh(
            obj,
            weld_distance=weld_distance,
            fill_boundaries=True,
        )

    if boundary_count > 0 or nonmanifold_count > 0:
        raise RuntimeError(
            f"Repair did not produce a closed mesh: boundary={boundary_count}, "
            f"nonmanifold={nonmanifold_count}"
        )

    obj.scale = (output_scale, output_scale, output_scale)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    for scene_obj in bpy.context.scene.objects:
        scene_obj.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_mesh.stl(
        filepath=str(output_path),
        use_selection=True,
        ascii=False,
    )

    print(
        "repair complete",
        {
            "output": str(output_path),
            "voxel_size": voxel_size,
            "boundary_edges": boundary_count,
            "nonmanifold_edges": nonmanifold_count,
            "vertices": len(obj.data.vertices),
            "polygons": len(obj.data.polygons),
        },
    )


if __name__ == "__main__":
    main()
