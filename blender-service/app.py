import subprocess
from flask import Flask, jsonify, request, send_file
import tempfile
import uuid
import os
import json

app = Flask(__name__)

# ✅ Health check for Render
@app.route("/")
def health():
    return "OK", 200

@app.route("/blender-version")
def blender_version():
    try:
        out = subprocess.check_output(["blender", "-v"], text=True).strip()
        return jsonify({"blender_version": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/generate-stl", methods=["POST"])
def generate_stl():
    try:
        data = request.get_json()
        print("🛬 Received JSON:", data)

        vertices = data.get("vertices", [])
        neckline = data.get("neckline", [])
        overlay = data.get("overlay", "default")
        job_id = data.get("job_id", uuid.uuid4().hex[:8])  # fallback UUID if not provided

        if not vertices:
            return jsonify({"error": "No vertices provided"}), 400

        temp_id = uuid.uuid4().hex[:8]
        input_path = f"/tmp/input_{temp_id}.json"
        output_path = f"/tmp/output_{temp_id}.stl"

        # Write full payload with overlay & job_id
        with open(input_path, "w") as f:
            json.dump({
                "vertices": vertices,
                "neckline": neckline,
                "overlay": overlay,
                "job_id": job_id
            }, f)

        print(f"📦 Calling Blender with input: {input_path}, output: {output_path}")

        result = subprocess.run([
            "blender", "--background", "--python", "generate_stl.py", "--",
            input_path, output_path
        ], capture_output=True, text=True, timeout=60)

        print("✅ Blender STDOUT:\n", result.stdout)
        print("⚠️ Blender STDERR:\n", result.stderr)

        if result.returncode != 0:
            return jsonify({
                "error": "Blender failed",
                "stderr": result.stderr,
                "stdout": result.stdout
            }), 500

        if not os.path.exists(output_path):
            return jsonify({"error": "STL not created", "stderr": result.stderr}), 500

        return send_file(output_path, mimetype="application/octet-stream", as_attachment=True, download_name="mold.stl")

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Blender timed out"}), 504
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"Blender crashed", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
argv = sys.argv

if "--" not in argv:
    raise RuntimeError("Missing Blender script arguments.")

args = argv[argv.index("--") + 1:]

if len(args) != 4:
    raise RuntimeError(
        "Expected input_stl, output_stl, holes_json, radius_mm."
    )

input_path = args[0]
output_path = args[1]
holes_path = args[2]
radius_mm = float(args[3])


print("============================================================")
print("KREZZCUT BOOLEAN HOLE PROCESSOR")
print("============================================================")
print("Input STL:", input_path)
print("Output STL:", output_path)
print("Holes file:", holes_path)
print("Radius:", radius_mm, "mm")


# ============================================================
# HELPERS
# ============================================================

def select_only(obj):
    bpy.ops.object.select_all(action='DESELECT')

    obj.select_set(True)

    bpy.context.view_layer.objects.active = obj


def clean_mesh(
    obj,
    weld_distance=0.02,
    degenerate_distance=0.001
):
    """
    Very conservative cleanup.

    Units here are millimeters because the Swift STL has already
    been exported at meter -> millimeter scale.

    We intentionally DO NOT remesh or smooth the object because
    doing so could alter the customer's facial contour or the
    shaving edges.
    """

    mesh = obj.data

    bm = bmesh.new()
    bm.from_mesh(mesh)

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    if len(bm.verts) > 0:
        bmesh.ops.remove_doubles(
            bm,
            verts=bm.verts[:],
            dist=weld_distance
        )

    if len(bm.edges) > 0:
        bmesh.ops.dissolve_degenerate(
            bm,
            edges=bm.edges[:],
            dist=degenerate_distance
        )

    if len(bm.faces) > 0:
        bmesh.ops.recalc_face_normals(
            bm,
            faces=bm.faces[:]
        )

    bm.to_mesh(mesh)
    bm.free()

    mesh.validate(verbose=False)
    mesh.update()


def triangulate_mesh(obj):
    mesh = obj.data

    bm = bmesh.new()
    bm.from_mesh(mesh)

    bm.faces.ensure_lookup_table()

    if len(bm.faces) > 0:
        bmesh.ops.triangulate(
            bm,
            faces=bm.faces[:]
        )

        bmesh.ops.recalc_face_normals(
            bm,
            faces=bm.faces[:]
        )

    bm.to_mesh(mesh)
    bm.free()

    mesh.validate(verbose=False)
    mesh.update()


def mesh_report(obj, label):
    mesh = obj.data

    bm = bmesh.new()
    bm.from_mesh(mesh)

    boundary_edges = [
        e for e in bm.edges
        if len(e.link_faces) == 1
    ]

    non_manifold_edges = [
        e for e in bm.edges
        if len(e.link_faces) not in (1, 2)
    ]

    print(
        f"{label}: "
        f"verts={len(bm.verts)}, "
        f"edges={len(bm.edges)}, "
        f"faces={len(bm.faces)}, "
        f"boundary={len(boundary_edges)}, "
        f"non_manifold={len(non_manifold_edges)}"
    )

    bm.free()


# ============================================================
# LOAD HOLE DATA
# ============================================================

with open(holes_path, "r") as f:
    holes = json.load(f)

if not isinstance(holes, list):
    raise RuntimeError("Hole data must be a JSON array.")

print("Hole count:", len(holes))


# ============================================================
# RESET BLENDER
# ============================================================

bpy.ops.object.select_all(action='SELECT')

bpy.ops.object.delete(
    use_global=False
)


# ============================================================
# IMPORT COMPLETED KREZZCUT MOLD
# ============================================================

bpy.ops.import_mesh.stl(
    filepath=input_path
)

mold = bpy.context.active_object

if mold is None:
    raise RuntimeError("Blender could not import the raw mold STL.")

mold.name = "KrezzCutMold"

select_only(mold)

# Make absolutely sure object transforms are baked.
bpy.ops.object.transform_apply(
    location=False,
    rotation=False,
    scale=True
)

print("✅ Raw mold imported.")

mesh_report(
    mold,
    "Before boolean"
)


# ============================================================
# PRE-BOOLEAN MICRO CLEAN
# ============================================================

# 0.02 mm is intentionally tiny.
#
# This may merge floating-point duplicates but will NOT perform
# the aggressive 0.4 - 0.6 mm welding/remeshing that could alter
# the shaving edge or face fit.
clean_mesh(
    mold,
    weld_distance=0.02,
    degenerate_distance=0.001
)

print("✅ Pre-boolean micro cleanup complete.")


# ============================================================
# TRUE CYLINDRICAL BOOLEAN CUTS
# ============================================================

for index, hole in enumerate(holes):

    if not isinstance(hole, dict):
        raise RuntimeError(
            f"Hole {index} is not an object."
        )

    if (
        "x" not in hole or
        "y" not in hole or
        "z" not in hole
    ):
        raise RuntimeError(
            f"Hole {index} is missing x/y/z."
        )

    cx = float(hole["x"])
    cy = float(hole["y"])
    cz = float(hole["z"])

    print(
        f"🔩 Hole {index}: "
        f"x={cx:.4f}, "
        f"y={cy:.4f}, "
        f"z={cz:.4f}, "
        f"r={radius_mm:.4f}"
    )

    # --------------------------------------------------------
    # CUTTER DIMENSIONS
    # --------------------------------------------------------
    #
    # KrezzCut's current main mold thickness is 10 mm.
    #
    # Use a 30 mm cutter so BOTH ends extend far outside the
    # finished mold.
    #
    # This is important because a cutter terminating exactly on
    # a mold surface can create coincident/coplanar geometry,
    # which is exactly the kind of thing that can produce
    # hairline gaps in a slicer.
    #
    cutter_depth = 30.0

    # Hole center supplied by Swift is on/near the facial surface.
    #
    # Position cutter:
    #
    #     top ≈ cz + 10 mm
    #     bottom ≈ cz - 20 mm
    #
    # This completely crosses the 10 mm mold.
    cutter_center_z = cz - 5.0

    bpy.ops.mesh.primitive_cylinder_add(
        vertices=64,
        radius=radius_mm,
        depth=cutter_depth,
        end_fill_type='NGON',
        location=(
            cx,
            cy,
            cutter_center_z
        )
    )

    cutter = bpy.context.active_object

    if cutter is None:
        raise RuntimeError(
            f"Could not create cutter {index}."
        )

    cutter.name = f"KrezzCutHoleCutter_{index}"

    # Bake cutter transform as well.
    select_only(cutter)

    bpy.ops.object.transform_apply(
        location=False,
        rotation=False,
        scale=True
    )

    # --------------------------------------------------------
    # BOOLEAN DIFFERENCE
    # --------------------------------------------------------

    select_only(mold)

    modifier = mold.modifiers.new(
        name=f"KrezzCutHoleBoolean_{index}",
        type='BOOLEAN'
    )

    modifier.operation = 'DIFFERENCE'
    modifier.object = cutter

    # Blender's EXACT solver is preferable here because the
    # facial surface and cradle contain much more complicated
    # geometry than a simple flat plate.
    if hasattr(modifier, "solver"):
        modifier.solver = 'EXACT'

    bpy.context.view_layer.objects.active = mold

    try:
        bpy.ops.object.modifier_apply(
            modifier=modifier.name
        )
    except Exception as boolean_error:
        raise RuntimeError(
            f"Boolean failed for hole {index}: "
            f"{boolean_error}"
        )

    # Remove cutter after successful boolean.
    if cutter.name in bpy.data.objects:
        bpy.data.objects.remove(
            cutter,
            do_unlink=True
        )

    print(
        f"✅ Hole {index} boolean difference complete."
    )


# ============================================================
# POST-BOOLEAN CLEAN
# ============================================================

select_only(mold)

# Again, microscopic cleanup only.
#
# NO voxel remesh.
# NO large remove-doubles value.
# NO smoothing.
#
# We want to preserve:
# - AR facial contour
# - cheek shaving edge
# - chin cradle
# - neckline
# - lip geometry
#
clean_mesh(
    mold,
    weld_distance=0.01,
    degenerate_distance=0.001
)

triangulate_mesh(mold)

mesh_report(
    mold,
    "After boolean"
)

print("✅ Final boolean cleanup complete.")


# ============================================================
# EXPORT
# ============================================================

select_only(mold)

bpy.ops.export_mesh.stl(
    filepath=output_path,
    use_selection=True
)

if not os.path.exists(output_path):
    raise RuntimeError(
        "Blender did not create the finished STL."
    )

file_size = os.path.getsize(output_path)

if file_size <= 0:
    raise RuntimeError(
        "Blender created an empty STL."
    )

print(
    "✅ FINAL BOOLEAN STL EXPORTED:",
    output_path
)

print(
    "✅ Final STL bytes:",
    file_size
)

print(
    "✅ Boolean hole count:",
    len(holes)
)

print("============================================================")
'''


# ============================================================
# HELPERS
# ============================================================

def run_blender(command, timeout=BLENDER_TIMEOUT_SECONDS):
    """
    Runs Blender and captures logs so Render shows us exactly
    what happened if mold generation fails.
    """

    print("🚀 Blender command:")
    print(" ".join(command))

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout
    )

    print(
        "================ BLENDER STDOUT ================"
    )

    print(result.stdout)

    print(
        "================ BLENDER STDERR ================"
    )

    print(result.stderr)

    print(
        "================================================"
    )

    return result


def remove_if_exists(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(
            f"⚠️ Could not remove temporary file {path}:",
            e
        )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/")
def health():
    return "OK", 200


# ============================================================
# BLENDER VERSION
# ============================================================

@app.route("/blender-version")
def blender_version():
    try:

        out = subprocess.check_output(
            ["blender", "-v"],
            text=True
        ).strip()

        return jsonify({
            "blender_version": out
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


# ============================================================
# EXISTING GENERATE STL ROUTE
# ============================================================
#
# I left your original generation route operational.
#
# This is useful for any older app flow still calling
# /generate-stl.
#
# The NEW ViewController's local mold builder does not depend
# on this route for the cradle geometry.
#
@app.route("/generate-stl", methods=["POST"])
def generate_stl():

    input_path = None
    output_path = None

    try:

        data = request.get_json(
            silent=True
        )

        if not isinstance(data, dict):

            return jsonify({
                "error": "Expected JSON request body"
            }), 400

        print(
            "🛬 Received /generate-stl JSON"
        )

        # Support both the current and older field names.
        vertices = (
            data.get("vertices")
            or data.get("beardline")
            or []
        )

        neckline = data.get(
            "neckline",
            []
        )

        overlay = data.get(
            "overlay",
            "default"
        )

        job_id = data.get(
            "job_id",
            data.get(
                "jobID",
                uuid.uuid4().hex[:8]
            )
        )

        if not vertices:

            return jsonify({
                "error": "No vertices provided"
            }), 400

        temp_id = uuid.uuid4().hex[:8]

        input_path = (
            f"/tmp/input_{temp_id}.json"
        )

        output_path = (
            f"/tmp/output_{temp_id}.stl"
        )

        # IMPORTANT:
        #
        # Preserve the complete incoming payload rather than
        # throwing away params/hole data that generate_stl.py
        # may understand.
        blender_payload = dict(data)

        blender_payload["vertices"] = vertices
        blender_payload["neckline"] = neckline
        blender_payload["overlay"] = overlay
        blender_payload["job_id"] = job_id

        with open(
            input_path,
            "w"
        ) as f:

            json.dump(
                blender_payload,
                f
            )

        print(
            f"📦 Calling generate_stl.py "
            f"input={input_path} "
            f"output={output_path}"
        )

        result = run_blender(
            [
                "blender",
                "--background",
                "--python",
                "generate_stl.py",
                "--",
                input_path,
                output_path
            ],
            timeout=BLENDER_TIMEOUT_SECONDS
        )

        if result.returncode != 0:

            remove_if_exists(
                input_path
            )

            remove_if_exists(
                output_path
            )

            return jsonify({
                "error": "Blender failed",
                "stderr": result.stderr,
                "stdout": result.stdout
            }), 500

        if not os.path.exists(
            output_path
        ):

            remove_if_exists(
                input_path
            )

            return jsonify({
                "error": "STL not created",
                "stderr": result.stderr,
                "stdout": result.stdout
            }), 500

        if os.path.getsize(
            output_path
        ) <= 0:

            remove_if_exists(
                input_path
            )

            remove_if_exists(
                output_path
            )

            return jsonify({
                "error": "STL was empty"
            }), 500

        print(
            "✅ /generate-stl finished:",
            output_path
        )

        response = send_file(
            output_path,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=f"{job_id}.stl"
        )

        # Delete temp files after Flask finishes sending them.
        @response.call_on_close
        def cleanup_generate_files():

            remove_if_exists(
                input_path
            )

            remove_if_exists(
                output_path
            )

        return response

    except subprocess.TimeoutExpired:

        remove_if_exists(
            input_path
        )

        remove_if_exists(
            output_path
        )

        return jsonify({
            "error": "Blender timed out"
        }), 504

    except subprocess.CalledProcessError as e:

        remove_if_exists(
            input_path
        )

        remove_if_exists(
            output_path
        )

        return jsonify({
            "error": "Blender crashed",
            "details": str(e)
        }), 500

    except Exception as e:

        print(
            "❌ /generate-stl error:",
            repr(e)
        )

        remove_if_exists(
            input_path
        )

        remove_if_exists(
            output_path
        )

        return jsonify({
            "error": str(e)
        }), 500


# ============================================================
# NEW — TRUE BOOLEAN HOLE ROUTE
# ============================================================
#
# Expected multipart/form-data fields:
#
# file:
#     Raw STL generated locally by ViewController
#
# job_id:
#     UUID/string for the customer's mold
#
# holes:
#     JSON array:
#
#     [
#       {"x": ..., "y": ..., "z": ...},
#       {"x": ..., "y": ..., "z": ...}
#     ]
#
# radius_mm:
#     Usually 1.5875
#
@app.route(
    "/boolean-holes",
    methods=["POST"]
)
def boolean_holes():

    input_path = None
    output_path = None
    holes_path = None
    script_path = None

    try:

        print(
            "🛬 Received /boolean-holes request"
        )

        uploaded_file = request.files.get(
            "file"
        )

        if uploaded_file is None:

            return jsonify({
                "error": "Missing STL file"
            }), 400

        job_id = request.form.get(
            "job_id"
        )

        if not job_id:
            job_id = uuid.uuid4().hex

        holes_raw = request.form.get(
            "holes"
        )

        if not holes_raw:

            return jsonify({
                "error": "Missing holes field"
            }), 400

        try:

            holes = json.loads(
                holes_raw
            )

        except json.JSONDecodeError:

            return jsonify({
                "error": "Invalid holes JSON"
            }), 400

        if not isinstance(
            holes,
            list
        ):

            return jsonify({
                "error": "holes must be a JSON array"
            }), 400

        if len(holes) == 0:

            return jsonify({
                "error": "No holes supplied"
            }), 400

        # KrezzCut currently expects two attachment holes.
        #
        # Don't hard-fail if this changes later, but log it.
        if len(holes) != 2:

            print(
                "⚠️ Expected 2 holes but received:",
                len(holes)
            )

        for i, hole in enumerate(
            holes
        ):

            if not isinstance(
                hole,
                dict
            ):

                return jsonify({
                    "error": f"Hole {i} is invalid"
                }), 400

            for axis in (
                "x",
                "y",
                "z"
            ):

                if axis not in hole:

                    return jsonify({
                        "error":
                        f"Hole {i} missing {axis}"
                    }), 400

                try:

                    float(
                        hole[axis]
                    )

                except (
                    TypeError,
                    ValueError
                ):

                    return jsonify({
                        "error":
                        f"Hole {i} has invalid {axis}"
                    }), 400

        try:

            radius_mm = float(
                request.form.get(
                    "radius_mm",
                    "1.5875"
                )
            )

        except ValueError:

            return jsonify({
                "error": "Invalid radius_mm"
            }), 400

        # Basic sanity range:
        #
        # Prevent an accidentally malformed request from
        # cutting giant cylinders through the customer's mold.
        if (
            radius_mm <= 0.25
            or
            radius_mm >= 10.0
        ):

            return jsonify({
                "error":
                "radius_mm is outside expected range"
            }), 400

        temp_id = uuid.uuid4().hex

        input_path = (
            f"/tmp/"
            f"{job_id}_"
            f"{temp_id}_raw.stl"
        )

        output_path = (
            f"/tmp/"
            f"{job_id}_"
            f"{temp_id}_boolean.stl"
        )

        holes_path = (
            f"/tmp/"
            f"{job_id}_"
            f"{temp_id}_holes.json"
        )

        script_path = (
            f"/tmp/"
            f"{job_id}_"
            f"{temp_id}_boolean.py"
        )

        # ----------------------------------------------------
        # SAVE RAW STL
        # ----------------------------------------------------

        uploaded_file.save(
            input_path
        )

        if not os.path.exists(
            input_path
        ):

            raise RuntimeError(
                "Uploaded STL was not saved."
            )

        input_size = os.path.getsize(
            input_path
        )

        if input_size <= 0:

            raise RuntimeError(
                "Uploaded STL was empty."
            )

        print(
            "✅ Raw STL saved:",
            input_path,
            "bytes=",
            input_size
        )

        # ----------------------------------------------------
        # SAVE HOLE COORDINATES
        # ----------------------------------------------------

        with open(
            holes_path,
            "w"
        ) as f:

            json.dump(
                holes,
                f
            )

        print(
            "✅ Hole coordinates saved:",
            holes
        )

        # ----------------------------------------------------
        # SAVE BLENDER SCRIPT
        # ----------------------------------------------------

        with open(
            script_path,
            "w"
        ) as f:

            f.write(
                BOOLEAN_HOLES_BLENDER_SCRIPT
            )

        # ----------------------------------------------------
        # RUN TRUE BLENDER BOOLEAN
        # ----------------------------------------------------

        result = run_blender(
            [
                "blender",
                "--background",
                "--python",
                script_path,
                "--",
                input_path,
                output_path,
                holes_path,
                str(radius_mm)
            ],
            timeout=BLENDER_TIMEOUT_SECONDS
        )

        if result.returncode != 0:

            return jsonify({
                "error":
                    "Blender boolean operation failed",
                "stdout":
                    result.stdout,
                "stderr":
                    result.stderr
            }), 500

        # ----------------------------------------------------
        # VALIDATE RESULT
        # ----------------------------------------------------

        if not os.path.exists(
            output_path
        ):

            return jsonify({
                "error":
                    "Boolean STL was not created",
                "stdout":
                    result.stdout,
                "stderr":
                    result.stderr
            }), 500

        output_size = os.path.getsize(
            output_path
        )

        if output_size <= 0:

            return jsonify({
                "error":
                    "Boolean STL was empty"
            }), 500

        print(
            "✅ Boolean mold complete."
        )

        print(
            "✅ Input bytes:",
            input_size
        )

        print(
            "✅ Output bytes:",
            output_size
        )

        print(
            "✅ Holes:",
            len(holes)
        )

        print(
            "✅ Hole radius:",
            radius_mm,
            "mm"
        )

        # ----------------------------------------------------
        # RETURN FINAL STL
        # ----------------------------------------------------

        response = send_file(
            output_path,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=f"{job_id}.stl"
        )

        @response.call_on_close
        def cleanup_boolean_files():

            remove_if_exists(
                input_path
            )

            remove_if_exists(
                output_path
            )

            remove_if_exists(
                holes_path
            )

            remove_if_exists(
                script_path
            )

        return response

    except subprocess.TimeoutExpired:

        print(
            "❌ Boolean Blender operation timed out."
        )

        remove_if_exists(
            input_path
        )

        remove_if_exists(
            output_path
        )

        remove_if_exists(
            holes_path
        )

        remove_if_exists(
            script_path
        )

        return jsonify({
            "error":
                "Blender boolean operation timed out"
        }), 504

    except Exception as e:

        print(
            "❌ /boolean-holes error:",
            repr(e)
        )

        remove_if_exists(
            input_path
        )

        remove_if_exists(
            output_path
        )

        remove_if_exists(
            holes_path
        )

        remove_if_exists(
            script_path
        )

        return jsonify({
            "error": str(e)
        }), 500


# ============================================================
# OPTIONAL ROUTE — VERIFY BOOLEAN SERVICE
# ============================================================

@app.route(
    "/boolean-holes-status"
)
def boolean_holes_status():

    return jsonify({
        "service": "KrezzCut boolean holes",
        "status": "ready",
        "method": "Blender EXACT boolean difference",
        "units": "millimeters"
    }), 200


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            8000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
