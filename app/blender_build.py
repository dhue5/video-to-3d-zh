"""Blender-side model builder for VideoTo3D Studio.

This file intentionally contains no API configuration or network code. The
desktop app performs AI analysis, then passes only the generated JSON spec to
Blender.
"""

import argparse
import json
import math
import os

import bpy


def args_after_double_dash():
    argv = list(__import__("sys").argv)
    return argv[argv.index("--") + 1:] if "--" in argv else []


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--blend", required=True)
    parser.add_argument("--export", required=True)
    parser.add_argument("--format", choices=("glb", "gltf", "obj", "fbx"), default="glb")
    return parser.parse_args(args_after_double_dash())


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def vector(value, fallback):
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return tuple(fallback)
    return tuple(safe_float(item, fallback[index]) for index, item in enumerate(value[:3]))


def color(value, fallback):
    rgb = vector(value, fallback)
    return (*[max(0.0, min(1.0, item)) for item in rgb], 1.0)


def material(name, rgba):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = rgba
    mat.metallic = 0.15
    mat.roughness = 0.38
    return mat


def bevel(obj, width):
    modifier = obj.modifiers.new("轻微倒角", "BEVEL")
    modifier.width = max(0.003, width)
    modifier.segments = 3


def build_part(part, index, collection):
    kind = str(part.get("type", part.get("类型", "box"))).lower()
    name = str(part.get("name", part.get("名称", f"部件_{index}")))
    dimensions = vector(part.get("dimensions", part.get("尺寸")), (0.5, 0.5, 0.5))
    position = vector(part.get("position", part.get("位置")), (0.0, 0.0, 0.0))
    rotation = vector(part.get("rotation", part.get("旋转")), (0.0, 0.0, 0.0))
    radius = max(0.01, max(abs(dimensions[0]), abs(dimensions[1])) / 2.0)
    depth = max(0.01, abs(dimensions[2]))

    if kind in ("cylinder", "圆柱", "圆柱体"):
        bpy.ops.mesh.primitive_cylinder_add(vertices=64, radius=radius, depth=depth, location=position)
        obj = bpy.context.object
        bevel(obj, min(radius, depth) * 0.08)
    elif kind in ("sphere", "uv_sphere", "球", "球体"):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=radius, location=position)
        obj = bpy.context.object
        obj.scale = (max(0.01, abs(dimensions[0]) / (2 * radius)), max(0.01, abs(dimensions[1]) / (2 * radius)), max(0.01, abs(dimensions[2]) / (2 * radius)))
    elif kind in ("torus", "圆环", "环"):
        minor = max(0.005, safe_float(part.get("minor_radius", part.get("小半径", radius * 0.16)), radius * 0.16))
        major = max(minor * 1.2, safe_float(part.get("major_radius", part.get("大半径", radius)), radius))
        bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, major_segments=96, minor_segments=20, location=position)
        obj = bpy.context.object
    elif kind in ("cone", "圆锥", "圆锥体"):
        top_radius = max(0.0, safe_float(part.get("top_radius", radius * 0.55), radius * 0.55))
        bpy.ops.mesh.primitive_cone_add(vertices=64, radius1=radius, radius2=top_radius, depth=depth, location=position)
        obj = bpy.context.object
        bevel(obj, min(radius, depth) * 0.06)
    else:
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=position)
        obj = bpy.context.object
        obj.dimensions = dimensions
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        bevel(obj, min(abs(item) for item in dimensions if item) * 0.05)

    obj.name = f"视频转3D_{name}"
    obj.rotation_euler = tuple(math.radians(item) for item in rotation)
    obj.data.materials.append(material(f"材质_{index}", color(part.get("color", part.get("颜色")), (0.35, 0.5, 0.75))))
    obj["智能建模类型"] = kind
    obj["智能建模参数"] = json.dumps(part, ensure_ascii=False)
    collection.objects.link(obj)
    for old_collection in list(obj.users_collection):
        if old_collection != collection:
            old_collection.objects.unlink(obj)
    return obj


def main():
    options = parse_args()
    with open(options.spec, "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    parts = spec.get("parts") or spec.get("部件") or spec.get("components") or spec.get("组件")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("建模参数中没有部件列表")

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    collection = bpy.data.collections.new("视频转3D_智能模型")
    bpy.context.scene.collection.children.link(collection)
    root = bpy.data.objects.new("视频转3D_模型根节点", None)
    collection.objects.link(root)
    root["建模参数"] = json.dumps(spec, ensure_ascii=False)

    objects = []
    valid_parts = [part for part in parts if isinstance(part, dict)]
    total = len(valid_parts)
    for index, part in enumerate(valid_parts, start=1):
        obj = build_part(part, index, collection)
        obj.parent = root
        objects.append(obj)
        print(f"V3D_PROGRESS {round(index / max(1, total) * 100)} 生成模型部件：{index}/{total}", flush=True)

    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(options.blend))

    export_path = os.path.abspath(options.export)
    os.makedirs(os.path.dirname(export_path), exist_ok=True)
    if options.format == "glb":
        bpy.ops.export_scene.gltf(filepath=export_path, export_format="GLB", use_selection=True)
    elif options.format == "gltf":
        bpy.ops.export_scene.gltf(filepath=export_path, export_format="GLTF_SEPARATE", use_selection=True)
    elif options.format == "obj":
        bpy.ops.wm.obj_export(filepath=export_path, export_selected_objects=True)
    elif options.format == "fbx":
        bpy.ops.export_scene.fbx(filepath=export_path, use_selection=True)
    print(f"V3D_PROGRESS 100 输出完成：{export_path}", flush=True)


if __name__ == "__main__":
    main()

