# VideoTo3D Studio

VideoTo3D Studio 是一个 Windows 本地桌面工作台：导入视频，抽取关键帧，调用外部 AI 模型生成物体结构描述，再交给 Blender 创建可编辑模型并导出。

## 快速开始

需要：

- Windows
- Node.js 22 或更高版本
- Blender 5.2 或更高版本
- FFmpeg 和 FFprobe
- 一个可访问的 OpenAI 风格聊天接口（可选；没有接口时也可以使用演示建模）

双击 `start_video_to_3d.bat`，或者在仓库根目录执行：

```powershell
node app/server.mjs
```

浏览器打开 `http://127.0.0.1:43120`。首次使用先填写本机工具路径，再填写 API URL、API 密钥和模型名称。地址支持根地址、`/v1`、`/v1/model`、`/v1/models` 或完整聊天接口地址，软件会统一调用：

```text
GET  <地址>/v1/models
POST <地址>/v1/chat/completions
```

## 工作流

1. 选择任意物体的视频，并填写输出目录。
2. 选择快速、均衡或精细质量。
3. 点击“读取模型”或直接填写模型名称。
4. 点击“开始建模”。
5. 查看百分比进度和日志，完成后下载 `.blend`、`.glb`、`.gltf`、`.obj` 或 `.fbx`。

软件将每次任务组织成独立项目，包含视频信息、关键帧、AI 建模参数、状态文件、日志和输出文件。默认输出为当前 Windows 用户的 `Downloads` 文件夹。

## 配置与安全

软件配置保存在：

```text
%LOCALAPPDATA%\\VideoTo3DStudio\\settings.json
```

API 密钥默认只在当前运行期间使用；勾选“保存密钥”后才保存到本机配置。密钥不会写入源码、`.blend`、项目 `project.json`、Git 历史或 GitHub Release。请不要把个人配置文件复制到仓库。

## 建模执行器

`blender_build.py` 只负责读取 AI 返回的建模参数、创建基础几何体、保存 `.blend` 并导出模型，不包含网络请求或 API 配置。当前支持盒体、圆柱、球体、圆环和圆锥；这是可编辑 MVP，后续可以替换为更高精度的重建器。

## OOOSplat 参考边界

软件参考 OOOSplat 的项目化任务、阶段进度、日志和质量档位工作流，但没有复制 OOOSplat 的代码、二进制或资源。OOOSplat 官方仓库的代码和文档采用 Apache-2.0，品牌与商标另行处理；本项目自身采用 MIT License。

## 故障排查

- Blender 启动失败：确认填写的是 `blender.exe` 完整路径。
- 抽帧失败：确认 FFmpeg/FFprobe 可执行，并且视频路径无权限问题。
- API 失败：检查 URL、认证方式和模型名称；先点击“测试接口”。
- 建模结果简单：当前是 AI 分析加基础几何体重建，不保证工业级尺寸精度或完整材质还原。

