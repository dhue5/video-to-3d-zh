bl_info = {
    'name': '视频转 3D 模型（中文）',
    'author': 'video_to_3d_zh contributors',
    'version': (0, 2, 0),
    'blender': (5, 0, 0),
    'location': '3D 视图 > 侧栏 > 视频转3D',
    'description': '参考项目化 3D 重建流程，导入视频、分析关键帧、生成并输出可编辑模型。',
    'category': '导入-导出',
}

import base64
import json
import math
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup, UIList


ADDON_PREFIX = '视频转3D_'

QUALITY_PRESETS = {
    'FAST': {'label': '快速', 'keyframes': 4, 'description': '少量关键帧，适合快速验证'},
    'BALANCED': {'label': '均衡', 'keyframes': 6, 'description': '速度和稳定性的推荐平衡'},
    'DETAILED': {'label': '精细', 'keyframes': 8, 'description': '更多关键帧，尽量保留细节'},
}

PIPELINE_STAGES = (
    ('preparing', '准备项目'),
    ('extracting', '抽取关键帧'),
    ('analyzing', 'AI 分析'),
    ('building', '生成模型'),
    ('saving', '保存项目'),
    ('exporting', '输出模型'),
)

DEFAULT_OUTPUT_DIR = os.path.join(os.path.expanduser('~'), 'Downloads')


def _safe_text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _find_first_value(value, keys):
    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key]:
                return value[key]
        for child in value.values():
            found = _find_first_value(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first_value(child, keys)
            if found:
                return found
    return None


def _request_json(url, payload=None, headers=None, timeout=120):
    request = urllib.request.Request(url, data=payload, headers=headers or {}, method='POST' if payload else 'GET')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or 'utf-8'
        text = raw.decode(charset, errors='replace')
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {'原始返回': text}


def _multipart(fields, file_field, file_path):
    boundary = '----VideoTo3DBlender' + uuid.uuid4().hex
    chunks = []
    for key, value in fields.items():
        chunks.extend([
            f'--{boundary}\r\n'.encode('utf-8'),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode('utf-8'),
            str(value).encode('utf-8'),
            b'\r\n',
        ])
    filename = os.path.basename(file_path)
    content_type = 'video/mp4' if filename.lower().endswith('.mp4') else 'application/octet-stream'
    chunks.extend([
        f'--{boundary}\r\n'.encode('utf-8'),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode('utf-8'),
        f'Content-Type: {content_type}\r\n\r\n'.encode('utf-8'),
    ])
    with open(file_path, 'rb') as handle:
        chunks.append(handle.read())
    chunks.extend([b'\r\n', f'--{boundary}--\r\n'.encode('utf-8')])
    return b''.join(chunks), f'multipart/form-data; boundary={boundary}'


def _headers(settings):
    result = {'User-Agent': 'Blender-VideoTo3D-ZH/0.1'}
    key = settings.api_key.strip()
    if settings.auth_type == 'BEARER' and key:
        result['Authorization'] = 'Bearer ' + key
    elif settings.auth_type == 'X_API_KEY' and key:
        result['x-api-key'] = key
    return result


def _addon_preferences():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def _load_saved_config(settings):
    prefs = _addon_preferences()
    if not prefs:
        return False
    settings.api_url = prefs.api_url
    settings.api_mode = prefs.api_mode
    settings.model_name = prefs.model_name
    settings.auth_type = prefs.auth_type
    settings.file_field = prefs.file_field
    settings.output_format = prefs.output_format
    settings.prompt = prefs.prompt
    settings.remember_key = prefs.remember_key
    if prefs.remember_key:
        settings.api_key = prefs.api_key
    settings.model_options.clear()
    try:
        saved_models = json.loads(prefs.model_list_json or '[]')
    except (TypeError, json.JSONDecodeError):
        saved_models = []
    for record in saved_models:
        if not isinstance(record, dict) or not record.get('id'):
            continue
        item = settings.model_options.add()
        item.model_id = str(record.get('id'))
        item.provider = str(record.get('provider') or '')
        item.context_length = int(record.get('context_length') or 0)
    return True


def _save_config(settings):
    prefs = _addon_preferences()
    if not prefs:
        raise RuntimeError('无法访问 Blender 用户偏好设置。')
    prefs.api_url = settings.api_url
    prefs.api_mode = settings.api_mode
    prefs.model_name = settings.model_name
    prefs.auth_type = settings.auth_type
    prefs.file_field = settings.file_field
    prefs.output_format = settings.output_format
    prefs.prompt = settings.prompt
    prefs.remember_key = settings.remember_key
    prefs.api_key = settings.api_key if settings.remember_key else ''
    prefs.model_list_json = json.dumps([
        {
            'id': item.model_id,
            'provider': item.provider,
            'context_length': item.context_length,
        }
        for item in settings.model_options
    ], ensure_ascii=False)
    bpy.ops.wm.save_userpref()


class V3D_AddonPreferences(AddonPreferences):
    bl_idname = __package__

    api_url: StringProperty(name='接口 URL（支持 /v1）')
    api_mode: EnumProperty(
        name='接口类型',
        items=[
            ('MODEL_UPLOAD', '视频上传接口', '直接上传视频并返回模型文件'),
            ('CHAT', '聊天兼容接口', '兼容 /v1/chat/completions 的文本接口'),
        ],
        default='MODEL_UPLOAD',
    )
    api_key: StringProperty(name='接口密钥', subtype='PASSWORD')
    model_name: StringProperty(name='模型名称')
    auth_type: EnumProperty(
        name='认证方式',
        items=[
            ('BEARER', 'Bearer', '在 Authorization 中使用 Bearer 密钥'),
            ('X_API_KEY', 'X-API-Key', '在 x-api-key 请求头中使用密钥'),
            ('NONE', '不使用密钥', '不附加认证请求头'),
        ],
        default='BEARER',
    )
    file_field: StringProperty(name='文件字段', default='video')
    prompt: StringProperty(name='重建说明', default='请根据视频生成物体的可编辑 3D 模型，保留主要外形和材质。')
    output_format: EnumProperty(
        name='输出格式',
        items=[
            ('glb', 'GLB', '推荐的通用模型格式'),
            ('gltf', 'glTF', '带外部资源的 glTF 模型'),
            ('obj', 'OBJ', '通用网格格式'),
            ('fbx', 'FBX', '适合部分 DCC 软件'),
        ],
        default='glb',
    )
    remember_key: BoolProperty(name='保存接口密钥', default=False)
    model_list_json: StringProperty(name='模型列表缓存', default='', options={'HIDDEN'})

    def draw(self, context):
        layout = self.layout
        layout.label(text='视频转3D接口配置保存在 Blender 用户偏好中。')
        layout.label(text='密钥不会写入项目文件，但用户配置文件仍应妥善保护。')


def _download(url, path, timeout=300):
    request = urllib.request.Request(url, headers={'User-Agent': 'Blender-VideoTo3D-ZH/0.1'})
    with urllib.request.urlopen(request, timeout=timeout) as response, open(path, 'wb') as output:
        shutil.copyfileobj(response, output)
    return path


def _guess_extension(url, response_data, preferred='glb'):
    lower = str(url).lower().split('?')[0]
    for ext in ('.glb', '.gltf', '.fbx', '.obj', '.ply', '.stl'):
        if lower.endswith(ext):
            return ext[1:]
    value = _find_first_value(response_data, ('格式', 'format', 'extension', '文件格式'))
    if value:
        return str(value).lower().lstrip('.')
    return preferred


def _output_directory(settings):
    directory = settings.output_dir.strip()
    if not directory:
        directory = DEFAULT_OUTPUT_DIR
    os.makedirs(directory, exist_ok=True)
    return os.path.abspath(directory)


def _safe_project_name(value):
    name = str(value or '未命名物体').strip()
    for char in '<>:"/\\|?*':
        name = name.replace(char, '_')
    return name[:80] or '未命名物体'


def _append_log(settings, message, stage=None, level='INFO'):
    line = settings.log_lines.add()
    line.timestamp = time.strftime('%H:%M:%S')
    line.stage = stage or settings.current_stage or 'system'
    line.level = level
    line.message = str(message)[:500]
    while len(settings.log_lines) > 200:
        settings.log_lines.remove(0)
    if settings.project_log_path:
        try:
            with open(bpy.path.abspath(settings.project_log_path), 'a', encoding='utf-8') as handle:
                handle.write(f'[{line.timestamp}] [{line.level}] [{line.stage}] {line.message}\n')
        except Exception:
            pass


def _write_project_state(settings, status, **extra):
    if not settings.project_state_path:
        return
    payload = {
        'status': status,
        'progress': int(settings.progress_percent),
        'stage': settings.current_stage,
        'stage_label': settings.progress_stage,
        'elapsed_seconds': round(float(settings.elapsed_seconds), 2),
        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    payload.update(extra)
    try:
        with open(bpy.path.abspath(settings.project_state_path), 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _write_project_metadata(settings, video, **extra):
    if not settings.project_path:
        return
    payload = {
        'project_name': os.path.basename(os.path.normpath(settings.project_path)),
        'source_video': os.path.abspath(video),
        'api_mode': settings.api_mode,
        'model_name': settings.model_name,
        'quality_preset': settings.quality_preset,
        'output_format': settings.output_format,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    payload.update(extra)
    path = os.path.join(bpy.path.abspath(settings.project_path), 'project.json')
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _create_project_workspace(settings, video):
    root = _output_directory(settings)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    stem = _safe_project_name(Path(video).stem)
    project_path = os.path.join(root, f'{stamp}_{stem}')
    suffix = 2
    while os.path.exists(project_path):
        project_path = os.path.join(root, f'{stamp}_{stem}-{suffix}')
        suffix += 1
    frames_dir = os.path.join(project_path, 'work', 'frames')
    logs_dir = os.path.join(project_path, 'logs')
    source_dir = os.path.join(project_path, 'source')
    for path in (frames_dir, logs_dir, source_dir):
        os.makedirs(path, exist_ok=True)
    settings.project_path = project_path
    settings.project_state_path = os.path.join(project_path, 'state.json')
    settings.project_log_path = os.path.join(logs_dir, 'pipeline.log')
    _write_project_metadata(settings, video)
    _write_project_state(settings, 'running')
    return project_path, frames_dir


def _set_stage(settings, stage, percent, message, current=None, total=None, level='INFO'):
    settings.current_stage = stage
    settings.stage_current = int(current or 0)
    settings.stage_total = int(total or 0)
    _progress_update(settings, percent, message)
    _append_log(settings, message, stage, level)
    _write_project_state(settings, 'running')


def _progress_update(settings, percent, stage):
    settings.progress_percent = max(0, min(100, int(percent)))
    settings.progress_stage = str(stage)[:240]
    if settings.task_started_at:
        settings.elapsed_seconds = max(0.0, time.monotonic() - settings.task_started_at)
    try:
        bpy.context.window_manager.progress_update(settings.progress_percent)
    except Exception:
        pass
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type in {'VIEW_3D', 'PROPERTIES'}:
                area.tag_redraw()
    try:
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    except Exception:
        pass


def _progress_begin(settings):
    settings.log_lines.clear()
    settings.project_path = ''
    settings.project_state_path = ''
    settings.project_log_path = ''
    settings.task_started_at = time.monotonic()
    settings.elapsed_seconds = 0.0
    settings.task_status = 'RUNNING'
    settings.current_stage = 'preparing'
    settings.progress_running = True
    _progress_update(settings, 0, '准备开始')
    _append_log(settings, '任务开始', 'preparing')
    try:
        bpy.context.window_manager.progress_begin(0, 100)
    except Exception:
        pass


def _progress_end(settings, success=True, message=''):
    if success:
        _progress_update(settings, 100, message or '全部完成')
        settings.task_status = 'COMPLETED'
        _append_log(settings, message or '任务完成', 'completed')
    else:
        settings.progress_stage = ('失败：' + str(message))[:240]
        settings.task_status = 'FAILED'
        _append_log(settings, message or '任务失败', settings.current_stage or 'failed', 'ERROR')
    if settings.task_started_at:
        settings.elapsed_seconds = max(0.0, time.monotonic() - settings.task_started_at)
    _write_project_state(settings, 'completed' if success else 'failed', error=None if success else str(message))
    try:
        bpy.context.window_manager.progress_end()
    except Exception:
        pass
    settings.progress_running = False


def _result_url(data):
    return _find_first_value(data, (
        'model_url', 'modelUrl', 'download_url', 'downloadUrl',
        'result_url', 'resultUrl', 'output_url', 'outputUrl', 'url',
    ))


def _poll_result(settings, data):
    direct = _result_url(data)
    if direct:
        return data, direct
    job_id = _find_first_value(data, ('job_id', 'jobId', 'task_id', 'taskId', 'id'))
    template = settings.status_url_template.strip()
    if not job_id or not template:
        return data, None
    status_url = template.replace('{job_id}', str(job_id)).replace('{task_id}', str(job_id))
    headers = _headers(settings)
    last = data
    for _ in range(max(1, settings.poll_count)):
        time.sleep(max(1, settings.poll_interval))
        last = _request_json(status_url, headers=headers, timeout=settings.timeout)
        result = _result_url(last)
        if result:
            return last, result
        status = str(_find_first_value(last, ('status', '状态')) or '').lower()
        if status in ('failed', 'error', '失败', '错误'):
            break
    return last, None


def _create_result_collection():
    name = ADDON_PREFIX + '导入结果'
    collection = bpy.data.collections.get(name)
    if not collection:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    return collection


def _move_to_collection(objects, collection):
    for obj in objects:
        for old_collection in list(obj.users_collection):
            old_collection.objects.unlink(obj)
        collection.objects.link(obj)


def _import_model(path):
    ext = Path(path).suffix.lower()
    before = set(bpy.data.objects)
    if ext in ('.glb', '.gltf'):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == '.fbx':
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == '.obj':
        if hasattr(bpy.ops.wm, 'obj_import'):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif ext == '.stl':
        if hasattr(bpy.ops.wm, 'stl_import'):
            bpy.ops.wm.stl_import(filepath=path)
        else:
            bpy.ops.import_mesh.stl(filepath=path)
    elif ext == '.ply':
        if hasattr(bpy.ops.wm, 'ply_import'):
            bpy.ops.wm.ply_import(filepath=path)
        else:
            raise RuntimeError('当前 Blender 没有可用的 PLY 导入器。')
    else:
        raise RuntimeError('暂不支持该模型格式：' + ext)
    imported = [obj for obj in bpy.data.objects if obj not in before]
    collection = _create_result_collection()
    _move_to_collection(imported, collection)
    parent = bpy.data.objects.new(ADDON_PREFIX + '模型根节点', None)
    collection.objects.link(parent)
    for obj in imported:
        if obj != parent and obj.parent is None:
            obj.parent = parent
    parent['来源'] = '视频转3D插件'
    parent['模型文件'] = os.path.abspath(path)
    return imported, parent


def _export_selected(filepath, output_format):
    if output_format == 'glb':
        bpy.ops.export_scene.gltf(
            filepath=filepath,
            export_format='GLB',
            use_selection=True,
            export_apply=True,
        )
    elif output_format == 'gltf':
        bpy.ops.export_scene.gltf(
            filepath=filepath,
            export_format='GLTF_SEPARATE',
            use_selection=True,
            export_apply=True,
        )
    elif output_format == 'fbx':
        bpy.ops.export_scene.fbx(
            filepath=filepath,
            use_selection=True,
            add_leaf_bones=False,
        )
    elif output_format == 'obj':
        if hasattr(bpy.ops.wm, 'obj_export'):
            bpy.ops.wm.obj_export(
                filepath=filepath,
                export_selected_objects=True,
                export_materials=True,
            )
        else:
            bpy.ops.export_scene.obj(
                filepath=filepath,
                use_selection=True,
                use_materials=True,
            )
    else:
        raise RuntimeError('暂不支持该输出格式：' + output_format)


class V3D_OT_export_model(Operator):
    bl_idname = 'v3d.export_model'
    bl_label = '导出模型'
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.v3d_settings
        output_dir = _output_directory(settings)
        ext = settings.output_format
        filename = '视频转3D导出_' + time.strftime('%Y%m%d_%H%M%S') + '.' + ext
        path = os.path.join(output_dir, filename)

        original_selected = list(bpy.context.selected_objects)
        original_active = bpy.context.view_layer.objects.active
        try:
            bpy.ops.object.select_all(action='DESELECT')
            collection = bpy.data.collections.get(ADDON_PREFIX + '导入结果')
            candidates = list(collection.objects) if collection else []
            candidates = [obj for obj in candidates if obj.type in {'MESH', 'CURVE', 'FONT', 'EMPTY'}]
            if not candidates:
                candidates = [obj for obj in original_selected if obj.name in bpy.data.objects]
            if not candidates:
                self.report({'ERROR'}, '没有找到可导出的模型，请先导入或选择模型。')
                return {'CANCELLED'}
            for obj in candidates:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = candidates[0]
            _export_selected(path, ext)
            settings.last_model_path = path
            settings.last_message = '模型已输出：' + path
            self.report({'INFO'}, settings.last_message)
            return {'FINISHED'}
        except Exception as exc:
            settings.last_message = '输出失败：' + str(exc)
            self.report({'ERROR'}, settings.last_message)
            return {'CANCELLED'}
        finally:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in original_selected:
                if obj.name in bpy.data.objects:
                    obj.select_set(True)
            if original_active and original_active.name in bpy.data.objects:
                bpy.context.view_layer.objects.active = original_active


def _extract_keyframes(video_path, output_dir, count=12, progress_callback=None):
    window = bpy.context.window
    old_scene = window.scene if window else bpy.context.scene
    temp_scene = bpy.data.scenes.new(ADDON_PREFIX + '临时抽帧')
    try:
        if window:
            window.scene = temp_scene
        sequence_editor = temp_scene.sequence_editor_create()
        strip = sequence_editor.strips.new_movie(
            name='输入视频', filepath=video_path, channel=1, frame_start=1
        )
        duration = max(1, strip.frame_final_duration)
        width = getattr(strip.elements[0], 'orig_width', 1920)
        height = getattr(strip.elements[0], 'orig_height', 1080)
        temp_scene.frame_start = 1
        temp_scene.frame_end = duration
        temp_scene.render.engine = 'BLENDER_WORKBENCH'
        temp_scene.render.use_sequencer = True
        temp_scene.render.resolution_x = width
        temp_scene.render.resolution_y = height
        temp_scene.render.resolution_percentage = 50
        temp_scene.render.image_settings.file_format = 'PNG'
        key_dir = os.path.join(output_dir, '关键帧')
        os.makedirs(key_dir, exist_ok=True)
        frames = sorted(set(round(1 + i * (duration - 1) / max(1, count - 1)) for i in range(count)))
        paths = []
        total = len(frames)
        for index, frame in enumerate(frames, start=1):
            temp_scene.frame_set(frame)
            target = os.path.join(key_dir, f'关键帧_{frame:05d}.png')
            temp_scene.render.filepath = target
            bpy.ops.render.render(scene=temp_scene.name, write_still=True)
            paths.append(target)
            if progress_callback:
                progress_callback(index, total)
        return paths
    finally:
        if window:
            window.scene = old_scene
        bpy.data.scenes.remove(temp_scene)


class V3D_ModelOption(PropertyGroup):
    model_id: StringProperty(name='模型 ID')
    provider: StringProperty(name='提供方')
    context_length: IntProperty(name='上下文长度', default=0, min=0)


class V3D_LogLine(PropertyGroup):
    timestamp: StringProperty(name='时间')
    stage: StringProperty(name='阶段')
    level: StringProperty(name='级别')
    message: StringProperty(name='信息')


class V3D_Settings(PropertyGroup):
    video_path: StringProperty(name='视频文件', subtype='FILE_PATH')
    output_dir: StringProperty(name='输出目录', subtype='DIR_PATH', default=DEFAULT_OUTPUT_DIR)
    api_url: StringProperty(name='接口 URL（支持 /v1）', description='聊天接口可填写到 /v1，插件会自动补齐 /chat/completions')
    api_mode: EnumProperty(
        name='接口类型',
        items=[
            ('MODEL_UPLOAD', '视频上传接口', '直接上传视频并返回模型文件'),
            ('CHAT', '聊天兼容接口', '兼容 /v1/chat/completions 的文本接口'),
        ],
        default='MODEL_UPLOAD',
    )
    api_key: StringProperty(name='接口密钥', subtype='PASSWORD')
    model_name: StringProperty(name='模型名称')
    auth_type: EnumProperty(
        name='认证方式',
        items=[
            ('BEARER', 'Bearer', '在 Authorization 中使用 Bearer 密钥'),
            ('X_API_KEY', 'X-API-Key', '在 x-api-key 请求头中使用密钥'),
            ('NONE', '不使用密钥', '不附加认证请求头'),
        ],
        default='BEARER',
    )
    file_field: StringProperty(name='文件字段', default='video', description='接口接收视频的字段名')
    prompt: StringProperty(
        name='重建说明',
        default='请根据视频生成物体的可编辑 3D 模型，保留主要外形和材质。',
    )
    output_format: EnumProperty(
        name='输出格式',
        items=[
            ('glb', 'GLB', '推荐的通用模型格式'),
            ('gltf', 'glTF', '带外部资源的 glTF 模型'),
            ('obj', 'OBJ', '通用网格格式'),
            ('fbx', 'FBX', '适合部分 DCC 软件'),
        ],
        default='glb',
    )
    status_url_template: StringProperty(
        name='状态 URL 模板',
        description='异步接口填写，例如 https://服务地址/jobs/{job_id}',
    )
    poll_interval: IntProperty(name='轮询间隔（秒）', default=3, min=1, max=60)
    poll_count: IntProperty(name='最大轮询次数', default=60, min=1, max=600)
    timeout: IntProperty(name='请求超时（秒）', default=300, min=10, max=3600)
    keyframe_count: IntProperty(name='关键帧数量', default=12, min=4, max=60)
    auto_import: BoolProperty(name='完成后自动导入', default=True)
    remember_key: BoolProperty(name='保存接口密钥', default=False)
    config_loaded: BoolProperty(default=False, options={'HIDDEN'})
    last_model_path: StringProperty(name='最近模型路径')
    last_message: StringProperty(name='最近状态')
    progress_percent: IntProperty(name='建模进度', default=0, min=0, max=100)
    progress_stage: StringProperty(name='当前阶段', default='尚未开始')
    progress_running: BoolProperty(name='正在处理', default=False, options={'HIDDEN'})
    current_stage: StringProperty(name='当前阶段代码', default='idle', options={'HIDDEN'})
    quality_preset: EnumProperty(
        name='质量档位',
        items=[
            ('FAST', '快速', '少量关键帧，适合快速验证'),
            ('BALANCED', '均衡', '速度和稳定性的推荐平衡'),
            ('DETAILED', '精细', '更多关键帧，尽量保留细节'),
        ],
        default='BALANCED',
    )
    project_path: StringProperty(name='当前项目目录', subtype='DIR_PATH')
    project_state_path: StringProperty(name='状态文件路径', subtype='FILE_PATH', options={'HIDDEN'})
    project_log_path: StringProperty(name='日志文件路径', subtype='FILE_PATH', options={'HIDDEN'})
    task_status: EnumProperty(
        name='任务状态',
        items=[
            ('IDLE', '待命', '尚未开始'),
            ('RUNNING', '处理中', '任务正在运行'),
            ('COMPLETED', '已完成', '任务已完成'),
            ('FAILED', '失败', '任务失败'),
        ],
        default='IDLE',
    )
    stage_current: IntProperty(name='阶段当前值', default=0, min=0)
    stage_total: IntProperty(name='阶段总值', default=0, min=0)
    elapsed_seconds: FloatProperty(name='已用时间', default=0.0, min=0.0)
    task_started_at: FloatProperty(name='任务开始时间', default=0.0, options={'HIDDEN'})
    log_lines: CollectionProperty(type=V3D_LogLine)
    model_options: CollectionProperty(type=V3D_ModelOption)
    model_index: IntProperty(name='模型列表索引', default=0, min=0, options={'HIDDEN'})


class V3D_OT_select_video(Operator):
    bl_idname = 'v3d.select_video'
    bl_label = '选择视频'
    bl_options = {'REGISTER'}

    filepath: StringProperty(subtype='FILE_PATH')

    def execute(self, context):
        context.window_manager.v3d_settings.video_path = self.filepath
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class V3D_OT_select_output(Operator):
    bl_idname = 'v3d.select_output'
    bl_label = '选择目录'
    bl_options = {'REGISTER'}

    directory: StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        context.window_manager.v3d_settings.output_dir = self.directory
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class V3D_OT_save_config(Operator):
    bl_idname = 'v3d.save_config'
    bl_label = '保存接口配置'
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.v3d_settings
        try:
            _save_config(settings)
            settings.config_loaded = True
            settings.last_message = '接口配置已保存到 Blender 用户偏好。'
            self.report({'INFO'}, settings.last_message)
            return {'FINISHED'}
        except Exception as exc:
            settings.last_message = '保存配置失败：' + str(exc)
            self.report({'ERROR'}, settings.last_message)
            return {'CANCELLED'}


class V3D_OT_load_config(Operator):
    bl_idname = 'v3d.load_config'
    bl_label = '读取已保存配置'
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.v3d_settings
        if _load_saved_config(settings):
            settings.config_loaded = True
            settings.last_message = '已读取 Blender 用户偏好中的接口配置。'
            self.report({'INFO'}, settings.last_message)
            return {'FINISHED'}
        self.report({'WARNING'}, '暂时没有已保存的接口配置。')
        return {'CANCELLED'}


class V3D_OT_extract_keyframes(Operator):
    bl_idname = 'v3d.extract_keyframes'
    bl_label = '抽取关键帧'
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.v3d_settings
        video = bpy.path.abspath(settings.video_path)
        if not os.path.isfile(video):
            self.report({'ERROR'}, '请先选择有效的视频文件。')
            return {'CANCELLED'}
        _progress_begin(settings)
        success = False
        try:
            paths = _extract_keyframes(
                video,
                _output_directory(settings),
                settings.keyframe_count,
                progress_callback=lambda index, total: _progress_update(
                    settings,
                    5 + 90 * index / max(1, total),
                    f'正在抽取关键帧：{index}/{total}',
                ),
            )
            settings.last_message = f'已抽取 {len(paths)} 张关键帧。'
            success = True
            self.report({'INFO'}, settings.last_message)
            return {'FINISHED'}
        except Exception as exc:
            settings.last_message = '抽帧失败：' + str(exc)
            self.report({'ERROR'}, settings.last_message)
            return {'CANCELLED'}
        finally:
            _progress_end(settings, success, settings.last_message)


def _v1_root(url):
    endpoint = str(url or '').strip().rstrip('/')
    marker = endpoint.lower().find('/v1')
    if marker >= 0:
        return endpoint[:marker] + '/v1'
    return endpoint + '/v1'


def _chat_endpoint(url):
    return _v1_root(url) + '/chat/completions'


def _models_endpoint(url):
    return _v1_root(url) + '/models'


def _chat_request(settings, content, max_tokens=2048):
    payload = {
        'model': settings.model_name.strip(),
        'messages': [{'role': 'user', 'content': content}],
        'stream': False,
        'max_tokens': max_tokens,
    }
    headers = _headers(settings)
    headers['Content-Type'] = 'application/json'
    return _request_json(
        _chat_endpoint(settings.api_url),
        payload=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers=headers,
        timeout=settings.timeout,
    )


def _model_records(response):
    if isinstance(response, dict):
        records = response.get('data') or response.get('models') or response.get('model_list') or response.get('模型') or []
    else:
        records = response if isinstance(response, list) else []
    if isinstance(records, dict):
        records = list(records.values())
    result = []
    for item in records:
        if isinstance(item, str):
            model_id = item.strip()
            provider = ''
            context_length = 0
        elif isinstance(item, dict):
            model_id = str(item.get('id') or item.get('model') or item.get('name') or item.get('模型名称') or '').strip()
            provider = str(item.get('owned_by') or item.get('provider') or item.get('提供方') or '').strip()
            context_length = int(item.get('context_length') or item.get('max_model_len') or 0)
        else:
            continue
        if model_id:
            result.append((model_id, provider, context_length))
    unique = {}
    for model_id, provider, context_length in result:
        unique.setdefault(model_id, (provider, context_length))
    return [(model_id, *values) for model_id, values in sorted(unique.items())]


class V3D_UL_model_list(UIList):
    bl_idname = 'V3D_UL_model_list'

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.model_id)
        if item.provider:
            row.label(text=item.provider)


class V3D_OT_refresh_models(Operator):
    bl_idname = 'v3d.refresh_models'
    bl_label = '读取模型列表'
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.v3d_settings
        if settings.progress_running:
            self.report({'WARNING'}, '任务运行中，暂时不能刷新模型列表。')
            return {'CANCELLED'}
        if not settings.api_url.strip():
            self.report({'ERROR'}, '请先填写接口 URL。')
            return {'CANCELLED'}
        try:
            endpoint = _models_endpoint(settings.api_url)
            response = _request_json(endpoint, headers=_headers(settings), timeout=settings.timeout)
            records = _model_records(response)
            if not records:
                raise RuntimeError('接口返回中没有找到模型列表。')
            current = settings.model_name.strip()
            settings.model_options.clear()
            for model_id, provider, context_length in records:
                item = settings.model_options.add()
                item.model_id = model_id
                item.provider = provider
                item.context_length = context_length
            selected_index = next((index for index, item in enumerate(settings.model_options) if item.model_id == current), 0)
            settings.model_index = selected_index
            settings.model_name = settings.model_options[selected_index].model_id
            _save_config(settings)
            settings.last_message = f'已从 {_models_endpoint(settings.api_url)} 读取 {len(records)} 个模型。'
            self.report({'INFO'}, settings.last_message)
            return {'FINISHED'}
        except urllib.error.HTTPError as exc:
            settings.last_message = f'读取模型列表返回 HTTP {exc.code}。'
            self.report({'ERROR'}, settings.last_message)
            return {'CANCELLED'}
        except Exception as exc:
            settings.last_message = '读取模型列表失败：' + str(exc)
            self.report({'ERROR'}, settings.last_message)
            return {'CANCELLED'}


class V3D_OT_use_model(Operator):
    bl_idname = 'v3d.use_model'
    bl_label = '使用选中模型'
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.v3d_settings
        if not settings.model_options or settings.model_index >= len(settings.model_options):
            self.report({'WARNING'}, '请先读取模型列表。')
            return {'CANCELLED'}
        settings.model_name = settings.model_options[settings.model_index].model_id
        _save_config(settings)
        settings.last_message = '已选中模型：' + settings.model_name
        self.report({'INFO'}, settings.last_message)
        return {'FINISHED'}


class V3D_OT_test_api(Operator):
    bl_idname = 'v3d.test_api'
    bl_label = '测试接口'
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.v3d_settings
        if not settings.api_url.strip():
            self.report({'ERROR'}, '请填写接口 URL。')
            return {'CANCELLED'}
        if not settings.model_name.strip():
            self.report({'ERROR'}, '请填写模型名称。')
            return {'CANCELLED'}
        try:
            if settings.api_mode == 'CHAT':
                response = _chat_request(settings, '只回复：接口测试成功')
                reply = _find_first_value(response, ('content', 'text', 'answer', 'response'))
                if reply:
                    settings.last_message = '接口测试成功：' + str(reply)[:160]
                else:
                    settings.last_message = '接口已连通，但返回格式未识别：' + _safe_text(response)[:160]
            else:
                request = urllib.request.Request(settings.api_url.strip(), headers=_headers(settings), method='GET')
                with urllib.request.urlopen(request, timeout=settings.timeout) as result:
                    settings.last_message = f'接口可访问，HTTP {result.status}。'
            self.report({'INFO'}, settings.last_message)
            return {'FINISHED'}
        except urllib.error.HTTPError as exc:
            settings.last_message = f'接口测试返回 HTTP {exc.code}。'
            self.report({'ERROR'}, settings.last_message)
            return {'CANCELLED'}
        except Exception as exc:
            settings.last_message = '接口测试失败：' + str(exc)
            self.report({'ERROR'}, settings.last_message)
            return {'CANCELLED'}


def _response_text(response):
    value = _find_first_value(response, ('content', 'text', 'answer', 'response'))
    return value if isinstance(value, str) else _safe_text(value or response)


def _extract_json(text):
    cleaned = str(text).strip()
    if '```' in cleaned:
        chunks = cleaned.split('```')
        cleaned = max(chunks, key=len).strip()
        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:].strip()
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start < 0 or end <= start:
        raise RuntimeError('模型没有返回 JSON 建模参数。')
    return json.loads(cleaned[start:end + 1])


def _number_list(value, default, size=3):
    if isinstance(value, (list, tuple)):
        values = list(value)[:size]
        values.extend([default] * (size - len(values)))
        return [float(v) for v in values]
    return [float(default)] * size


def _material(name, color, metallic=0.0, roughness=0.4):
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    material.diffuse_color = (*color, 1.0)
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get('Principled BSDF')
    if bsdf:
        bsdf.inputs['Base Color'].default_value = (*color, 1.0)
        bsdf.inputs['Metallic'].default_value = metallic
        bsdf.inputs['Roughness'].default_value = roughness
    return material


def _part_material(index, color):
    if isinstance(color, str) and color.startswith('#') and len(color) in (7, 9):
        try:
            rgb = tuple(int(color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
        except ValueError:
            rgb = (0.42, 0.46, 0.50)
    elif isinstance(color, (list, tuple)) and len(color) >= 3:
        rgb = tuple(max(0.0, min(1.0, float(v))) for v in color[:3])
    else:
        rgb = (0.42, 0.46, 0.50)
    return _material('智能模型材质_%02d' % index, rgb, 0.25, 0.34)


def _ai_collection():
    name = ADDON_PREFIX + '智能建模结果'
    collection = bpy.data.collections.get(name)
    if not collection:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    return collection


def _clear_ai_collection(collection):
    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def _link_to_collection(obj, collection):
    for old_collection in list(obj.users_collection):
        old_collection.objects.unlink(obj)
    collection.objects.link(obj)


def _bevel(obj, width=0.04, segments=3):
    modifier = obj.modifiers.new('柔和边缘', 'BEVEL')
    modifier.width = width
    modifier.segments = segments
    modifier.limit_method = 'ANGLE'
    return obj


def _assign_material(obj, material):
    if material:
        obj.data.materials.append(material)
    return obj


def _create_ai_part(part, index, collection):
    kind = str(part.get('type', part.get('类型', 'box'))).lower()
    name = str(part.get('name', part.get('名称', '部件_%03d' % index)))
    loc = _number_list(part.get('position', part.get('位置', [0, 0, 0])), 0.0)
    rot = _number_list(part.get('rotation', part.get('旋转', [0, 0, 0])), 0.0)
    dims = _number_list(part.get('dimensions', part.get('尺寸', part.get('size', [1, 1, 1]))), 1.0)
    radius = float(part.get('radius', part.get('半径', max(dims[0], dims[1]) * 0.5)))
    depth = float(part.get('depth', part.get('深度', dims[2])))

    if kind in ('box', 'cube', '盒', '立方体', '长方体'):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
        obj = bpy.context.object
        obj.dimensions = dims
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        _bevel(obj, max(0.005, min(dims) * 0.04), 3)
    elif kind in ('cylinder', '圆柱', '圆柱体'):
        bpy.ops.mesh.primitive_cylinder_add(vertices=64, radius=radius, depth=depth, location=loc)
        obj = bpy.context.object
        _bevel(obj, max(0.005, min(radius, depth) * 0.08), 3)
    elif kind in ('sphere', 'uv_sphere', '球', '球体'):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=radius, location=loc)
        obj = bpy.context.object
    elif kind in ('torus', '圆环', '环'):
        minor = float(part.get('minor_radius', part.get('小半径', min(dims[0], dims[1]) * 0.12)))
        major = float(part.get('major_radius', part.get('大半径', radius)))
        bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, major_segments=96, minor_segments=16, location=loc)
        obj = bpy.context.object
    elif kind in ('cone', '圆锥', '圆锥体'):
        bpy.ops.mesh.primitive_cone_add(vertices=64, radius1=radius, radius2=float(part.get('top_radius', radius * 0.55)), depth=depth, location=loc)
        obj = bpy.context.object
        _bevel(obj, max(0.005, min(radius, depth) * 0.06), 3)
    else:
        # Unknown categories are kept usable by falling back to a box.
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
        obj = bpy.context.object
        obj.dimensions = dims
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        _bevel(obj, max(0.005, min(dims) * 0.04), 3)
    obj.name = ADDON_PREFIX + name
    obj.rotation_euler = tuple(math.radians(v) for v in rot)
    _assign_material(obj, _part_material(index, part.get('color', part.get('颜色'))))
    obj['智能建模类型'] = kind
    obj['智能建模参数'] = json.dumps(part, ensure_ascii=False)
    _link_to_collection(obj, collection)
    return obj


def _build_ai_model(spec, source_video, model_name, progress_callback=None):
    if not isinstance(spec, dict):
        raise RuntimeError('模型返回的建模参数不是 JSON 对象。')
    parts = spec.get('parts') or spec.get('部件') or spec.get('components') or spec.get('组件')
    if not isinstance(parts, list) or not parts:
        raise RuntimeError('JSON 中没有可生成的 parts/部件 列表。')
    collection = _ai_collection()
    _clear_ai_collection(collection)
    root = bpy.data.objects.new(ADDON_PREFIX + '智能模型根节点', None)
    collection.objects.link(root)
    root['来源视频'] = os.path.abspath(source_video)
    root['分析模型'] = model_name
    root['建模参数'] = json.dumps(spec, ensure_ascii=False)
    created = []
    valid_parts = [part for part in parts if isinstance(part, dict)]
    total = len(valid_parts)
    for index, part in enumerate(valid_parts, start=1):
        if isinstance(part, dict):
            obj = _create_ai_part(part, index, collection)
            obj.parent = root
            created.append(obj)
            if progress_callback:
                progress_callback(index, total)
    if not created:
        raise RuntimeError('没有生成有效的模型部件。')
    bpy.ops.object.select_all(action='DESELECT')
    for obj in created:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = created[0]
    return created, root


def _save_generated_json(spec, output_dir):
    path = os.path.join(output_dir, '视频转3D_建模参数_%s.json' % time.strftime('%Y%m%d_%H%M%S'))
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
    return path


def _export_objects(objects, output_dir, output_format):
    original_selected = list(bpy.context.selected_objects)
    original_active = bpy.context.view_layer.objects.active
    ext = output_format
    path = os.path.join(output_dir, '视频转3D智能模型_%s.%s' % (time.strftime('%Y%m%d_%H%M%S'), ext))
    try:
        bpy.ops.object.select_all(action='DESELECT')
        for obj in objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = objects[0]
        _export_selected(path, output_format)
        return path
    finally:
        bpy.ops.object.select_all(action='DESELECT')
        for obj in original_selected:
            if obj.name in bpy.data.objects:
                obj.select_set(True)
        if original_active and original_active.name in bpy.data.objects:
            bpy.context.view_layer.objects.active = original_active


class V3D_OT_ai_build_model(Operator):
    bl_idname = 'v3d.ai_build_model'
    bl_label = '智能分析并建模'
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.v3d_settings
        if settings.api_mode != 'CHAT':
            self.report({'ERROR'}, '请先将接口类型切换为“聊天兼容接口”。')
            return {'CANCELLED'}
        video = bpy.path.abspath(settings.video_path)
        if not os.path.isfile(video):
            self.report({'ERROR'}, '请先选择有效的视频文件。')
            return {'CANCELLED'}
        if not settings.api_url.strip() or not settings.model_name.strip():
            self.report({'ERROR'}, '请先填写接口 URL 和模型名称。')
            return {'CANCELLED'}
        _progress_begin(settings)
        success = False
        try:
            _set_stage(settings, 'preparing', 2, '正在创建独立项目目录')
            output_dir, frames_dir = _create_project_workspace(settings, video)
            preset = QUALITY_PRESETS.get(settings.quality_preset, QUALITY_PRESETS['BALANCED'])
            target_count = max(4, min(8, int(preset['keyframes'])))
            _set_stage(settings, 'preparing', 4, f'质量档位：{preset["label"]}（{target_count} 张关键帧）')
            keyframes = _extract_keyframes(
                video,
                frames_dir,
                target_count,
                progress_callback=lambda index, total: _set_stage(
                    settings,
                    'extracting',
                    5 + 30 * index / max(1, total),
                    f'正在抽取关键帧：{index}/{total}',
                    index,
                    total,
                ),
            )
            selected = keyframes[:6]
            _set_stage(settings, 'extracting', 36, f'关键帧完成：共 {len(keyframes)} 张', len(keyframes), len(keyframes))
            content = [{
                'type': 'text',
                'text': (
                    '你是通用三维建模参数分析器。请根据下面同一物体的不同角度关键帧，输出严格 JSON，禁止 Markdown 和解释文字。'
                    '使用 3 到 15 个可编辑基础部件，类型只能是 box、cylinder、sphere、torus、cone。'
                    '尺寸使用相对单位，让整体高度或最大尺寸约为 2。坐标为物体局部坐标。'
                    'JSON 格式必须为：'
                    '{"object_name":"物体名称","parts":[{"name":"部件名称","type":"box|cylinder|sphere|torus|cone",'
                    '"dimensions":[x,y,z],"position":[x,y,z],"rotation":[rx,ry,rz],"color":[r,g,b]}]}'
                ),
            }]
            for index, frame_path in enumerate(selected, start=1):
                with open(frame_path, 'rb') as handle:
                    encoded = base64.b64encode(handle.read()).decode('ascii')
                content.append({
                    'type': 'image_url',
                    'image_url': {'url': 'data:image/png;base64,' + encoded},
                })
                _set_stage(settings, 'analyzing', 36 + 5 * index / max(1, len(selected)), f'正在准备图片：{index}/{len(selected)}', index, len(selected))
            _set_stage(settings, 'analyzing', 42, f'正在调用 {settings.model_name.strip()}，请等待接口返回')
            response = _chat_request(settings, content, max_tokens=3000)
            _set_stage(settings, 'analyzing', 72, '接口返回，正在解析建模参数')
            reply = _response_text(response)
            spec = _extract_json(reply)
            _set_stage(settings, 'saving', 75, '正在保存建模参数 JSON')
            json_path = _save_generated_json(spec, output_dir)
            object_name = spec.get('object_name') or spec.get('名称') if isinstance(spec, dict) else None
            _write_project_metadata(settings, video, parameter_file=json_path, object_name=object_name)
            _set_stage(settings, 'building', 78, '正在生成可编辑模型部件')
            objects, root = _build_ai_model(
                spec,
                video,
                settings.model_name,
                progress_callback=lambda index, total: _set_stage(
                    settings,
                    'building',
                    78 + 14 * index / max(1, total),
                    f'正在生成模型部件：{index}/{total}',
                    index,
                    total,
                ),
            )
            _set_stage(settings, 'saving', 93, '正在保存 Blender 项目')
            blend_path = os.path.join(output_dir, '视频转3D智能模型_%s.blend' % time.strftime('%Y%m%d_%H%M%S'))
            bpy.ops.wm.save_as_mainfile(filepath=blend_path)
            _set_stage(settings, 'exporting', 96, '正在导出模型文件')
            exported = _export_objects(objects, output_dir, settings.output_format)
            settings.last_model_path = exported
            _write_project_metadata(settings, video, parameter_file=json_path, blend_file=blend_path, model_file=exported, part_count=len(objects))
            settings.last_message = '智能建模完成：%d 个部件，已输出 GLB/模型文件。' % len(objects)
            success = True
            self.report({'INFO'}, settings.last_message)
            return {'FINISHED'}
        except Exception as exc:
            settings.last_message = '智能建模失败：' + str(exc)
            self.report({'ERROR'}, settings.last_message)
            return {'CANCELLED'}
        finally:
            _progress_end(settings, success, settings.last_message)


class V3D_OT_call_api(Operator):
    bl_idname = 'v3d.call_api'
    bl_label = '调用外部模型'
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.v3d_settings
        if settings.api_mode == 'CHAT':
            self.report({'ERROR'}, '当前是聊天兼容接口，只能进行文本测试；请切换为视频上传接口调用 3D 重建服务。')
            return {'CANCELLED'}
        video = bpy.path.abspath(settings.video_path)
        if not os.path.isfile(video):
            self.report({'ERROR'}, '请先选择有效的视频文件。')
            return {'CANCELLED'}
        if not settings.api_url.strip():
            self.report({'ERROR'}, '请填写接口 URL。')
            return {'CANCELLED'}
        if not settings.model_name.strip():
            self.report({'ERROR'}, '请填写模型名称。')
            return {'CANCELLED'}
        try:
            if settings.remember_key:
                _save_config(settings)
            fields = {
                'model': settings.model_name.strip(),
                'prompt': settings.prompt,
                'output_format': settings.output_format,
            }
            body, content_type = _multipart(fields, settings.file_field.strip() or 'video', video)
            headers = _headers(settings)
            headers['Content-Type'] = content_type
            response = _request_json(settings.api_url.strip(), payload=body, headers=headers, timeout=settings.timeout)
            response, model_url = _poll_result(settings, response)
            if not model_url:
                message = _safe_text(response)
                settings.last_message = '接口已返回，但未找到模型下载地址：' + message[:300]
                self.report({'WARNING'}, settings.last_message)
                return {'FINISHED'}
            output_dir = _output_directory(settings)
            ext = _guess_extension(model_url, response, settings.output_format)
            filename = '视频重建模型_' + time.strftime('%Y%m%d_%H%M%S') + '.' + ext
            model_path = os.path.join(output_dir, filename)
            _download(model_url, model_path, timeout=settings.timeout)
            settings.last_model_path = model_path
            settings.last_message = '模型下载完成：' + model_path
            if settings.auto_import:
                _import_model(model_path)
                settings.last_message += '，已导入 Blender。'
            self.report({'INFO'}, settings.last_message)
            return {'FINISHED'}
        except urllib.error.HTTPError as exc:
            settings.last_message = f'接口返回 HTTP {exc.code}。'
            self.report({'ERROR'}, settings.last_message)
            return {'CANCELLED'}
        except Exception as exc:
            settings.last_message = '调用失败：' + str(exc)
            self.report({'ERROR'}, settings.last_message)
            return {'CANCELLED'}


class V3D_OT_import_model(Operator):
    bl_idname = 'v3d.import_model'
    bl_label = '导入模型'
    bl_options = {'REGISTER'}

    filepath: StringProperty(subtype='FILE_PATH')

    def execute(self, context):
        settings = context.window_manager.v3d_settings
        path = bpy.path.abspath(self.filepath or settings.last_model_path)
        if not os.path.isfile(path):
            self.report({'ERROR'}, '没有找到可导入的模型文件。')
            return {'CANCELLED'}
        try:
            _import_model(path)
            settings.last_model_path = path
            settings.last_message = '模型已导入 Blender。'
            self.report({'INFO'}, settings.last_message)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, '导入失败：' + str(exc))
            return {'CANCELLED'}

    def invoke(self, context, event):
        if context.window_manager.v3d_settings.last_model_path:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class V3D_PT_main(Panel):
    bl_label = '视频转 3D 模型'
    bl_idname = 'V3D_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '视频转3D'

    def draw(self, context):
        layout = self.layout
        settings = context.window_manager.v3d_settings
        if not settings.config_loaded:
            _load_saved_config(settings)
            settings.config_loaded = True

        progress_box = layout.box()
        progress_box.label(text='建模进度')
        progress_row = progress_box.row()
        progress_row.enabled = False
        progress_row.prop(settings, 'progress_percent', text=f'{settings.progress_percent}% ', slider=True)
        progress_box.label(text=settings.progress_stage or '尚未开始', icon='TIME')
        if settings.stage_total:
            progress_box.label(text=f'当前计数：{settings.stage_current}/{settings.stage_total}')
        if settings.elapsed_seconds > 0:
            progress_box.label(text=f'已用时间：{settings.elapsed_seconds:.1f} 秒')
        timeline = progress_box.column(align=True)
        stage_index = next((index for index, (key, _) in enumerate(PIPELINE_STAGES) if key == settings.current_stage), -1)
        for index, (key, label) in enumerate(PIPELINE_STAGES):
            if settings.task_status == 'COMPLETED' or index < stage_index:
                marker = '✓'
            elif index == stage_index and settings.progress_running:
                marker = '▶'
            else:
                marker = '○'
            timeline.label(text=f'{marker} {label}')

        box = layout.box()
        box.label(text='一、输入视频')
        box.prop(settings, 'quality_preset', text='质量档位')
        preset = QUALITY_PRESETS.get(settings.quality_preset, QUALITY_PRESETS['BALANCED'])
        box.label(text=f'{preset["description"]}，智能建模使用 {preset["keyframes"]} 张关键帧。')
        row = box.row(align=True)
        row.prop(settings, 'video_path', text='视频')
        row.operator('v3d.select_video', text='', icon='FILE_FOLDER')
        row = box.row(align=True)
        row.prop(settings, 'output_dir', text='输出')
        row.operator('v3d.select_output', text='', icon='FILE_FOLDER')
        row = box.row(align=True)
        row.prop(settings, 'keyframe_count', text='关键帧数量')
        row.operator('v3d.extract_keyframes', text='抽取关键帧', icon='RENDER_STILL')

        box = layout.box()
        box.label(text='二、外部模型接口')
        box.prop(settings, 'api_mode', text='接口类型')
        box.prop(settings, 'api_url', text='接口 URL（可填 /v1）')
        box.prop(settings, 'api_key', text='接口密钥')
        model_row = box.row(align=True)
        model_row.prop(settings, 'model_name', text='当前模型')
        model_row.operator('v3d.refresh_models', text='读取模型', icon='FILE_REFRESH')
        if settings.model_options:
            box.template_list('V3D_UL_model_list', 'v3d_models', settings, 'model_options', settings, 'model_index', rows=min(6, len(settings.model_options)))
            box.operator('v3d.use_model', text='使用选中模型', icon='CHECKMARK')
        else:
            box.label(text='模型列表尚未读取，可点击“读取模型”。')
        box.label(text='模型列表地址：' + _models_endpoint(settings.api_url)[:160])
        box.prop(settings, 'auth_type', text='认证方式')
        box.prop(settings, 'file_field', text='视频字段')
        box.prop(settings, 'output_format', text='输出格式')
        box.prop(settings, 'prompt', text='重建说明')
        box.prop(settings, 'auto_import', text='完成后自动导入')
        box.prop(settings, 'remember_key', text='保存接口密钥')
        row = box.row(align=True)
        row.operator('v3d.save_config', text='保存接口配置')
        row.operator('v3d.load_config', text='读取已保存配置')
        box.operator('v3d.test_api', text='测试接口')
        api_ready = bool(settings.api_url.strip() and settings.model_name.strip() and (settings.api_key.strip() or settings.auth_type == 'NONE'))
        box.label(text='接口状态：已配置' if api_ready else '接口状态：待填写 URL、模型和密钥', icon='CHECKMARK' if api_ready else 'ERROR')
        box.label(text='配置保存在 Blender 用户偏好中，不写入项目文件。')

        advanced = layout.box()
        advanced.label(text='高级设置')
        advanced.prop(settings, 'status_url_template', text='状态 URL')
        row = advanced.row(align=True)
        row.prop(settings, 'poll_interval', text='轮询间隔')
        row.prop(settings, 'poll_count', text='轮询次数')
        advanced.prop(settings, 'timeout', text='请求超时')

        row = layout.row(align=True)
        row.operator('v3d.call_api', text='调用外部模型', icon='WORLD')
        row.operator('v3d.import_model', text='导入模型', icon='IMPORT')
        row = layout.row(align=True)
        row.operator('v3d.export_model', text='输出模型', icon='EXPORT')
        row = layout.row(align=True)
        row.operator('v3d.ai_build_model', text='智能分析并建模', icon='MODIFIER')
        if settings.project_path:
            project_box = layout.box()
            project_box.label(text='当前项目')
            project_box.label(text=settings.project_path[:180], icon='FILE_FOLDER')
            project_box.label(text='状态文件、日志和关键帧已写入项目目录。')
        log_box = layout.box()
        log_box.label(text=f'实时日志（最近 {min(8, len(settings.log_lines))} 条）')
        if settings.log_lines:
            for item in list(settings.log_lines)[-8:]:
                log_box.label(text=f'[{item.timestamp}] {item.message}'[:180])
        else:
            log_box.label(text='开始任务后显示处理日志。')
        layout.label(text='参考项目化 3D 重建流程：每次生成都会创建独立项目目录。')
        if settings.last_message:
            layout.label(text=settings.last_message[:180], icon='INFO')


classes = (
    V3D_AddonPreferences,
    V3D_LogLine,
    V3D_ModelOption,
    V3D_Settings,
    V3D_OT_select_video,
    V3D_OT_select_output,
    V3D_OT_save_config,
    V3D_OT_load_config,
    V3D_OT_extract_keyframes,
    V3D_UL_model_list,
    V3D_OT_refresh_models,
    V3D_OT_use_model,
    V3D_OT_test_api,
    V3D_OT_call_api,
    V3D_OT_import_model,
    V3D_OT_export_model,
    V3D_OT_ai_build_model,
    V3D_PT_main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.v3d_settings = PointerProperty(type=V3D_Settings)


def unregister():
    if hasattr(bpy.types.WindowManager, 'v3d_settings'):
        del bpy.types.WindowManager.v3d_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
