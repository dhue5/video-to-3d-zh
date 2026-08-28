# VideoTo3D Studio

VideoTo3D Studio 是一个 Windows 本地桌面工作台：导入视频，抽取关键帧，使用本地 `FFmpeg/FFprobe + COLMAP + Brush` 完成相机重建和 Gaussian Splatting 训练，最终输出 `final.ply`，全程不需要 Blender。

软件同时保留本地 Hunyuan3D 网格模式和 Blender 兼容模式，但默认核心是本地 Gaussian Splatting 流水线。

## 快速开始

需要：

- Windows
- Node.js 22 或更高版本
- FFmpeg 和 FFprobe
- COLMAP
- Brush 0.3+（支持 CLI 的版本）
- 支持 CUDA 或其他 Brush 图形后端的 GPU（推荐独立显卡）

双击 `start_video_to_3d.bat`，或者在仓库根目录执行：

```powershell
node app/server.mjs
```

浏览器打开 `http://127.0.0.1:43120`。首次使用选择“本地 COLMAP + Brush（免 Blender）”，填写 COLMAP 和 Brush 路径，并确认 FFmpeg/FFprobe 可执行。默认可填写命令名 `colmap`、`brush`，也可以填写完整路径。

## 核心本地流水线

```text
输入视频
  → FFprobe 读取时长、分辨率、帧率
  → FFmpeg 均匀抽取关键帧
  → COLMAP 特征提取、顺序匹配、相机位姿重建
  → Brush 训练 Gaussian Splats
  → 输出 final.ply
```

软件把每一步的 stdout/stderr 写入实时日志和项目 `logs/pipeline.log`。COLMAP 重建失败时会明确提示检查视频的重叠视角、运动稳定性和纹理；Brush 训练失败时会提示检查图形后端、显存和数据集。

Brush 的 CLI 形式是“数据集路径 + 训练参数”，软件使用类似以下参数：

```text
brush <colmap_dataset> --total-steps 15000 --max-resolution 1600 \
  --max-splats 7000000 --export-path <output> \
  --export-name export_{iter}.ply
```

## 可选本地网格模式

选择“本地 Hunyuan3D 网格（免 Blender）”时，软件连接本机 Hunyuan3D API 的 `/health` 和 `/generate`，使用中间关键帧生成 GLB/OBJ 网格。按 [Hunyuan3D-2 官方仓库](https://github.com/Tencent-Hunyuan/Hunyuan3D-2) 安装模型和 API 服务即可。

## 兼容 Blender 模式

选择“Blender 程序化（兼容旧流程）”时，才需要填写 Blender 路径和外部视觉模型接口。该模式保留旧版“AI 分析 + 基础几何体”的可编辑工作流，不是默认核心。

## 工作流

1. 选择任意物体的视频，并填写输出目录。
2. 选择快速、均衡或精细质量。
3. 点击“检查本地引擎”，确认 COLMAP 和 Brush 可执行。
4. 点击“开始生成”。
5. 查看百分比进度和日志，完成后使用 `final.ply`，可用 Brush、SuperSplat 或其他 Gaussian Splatting 查看器打开。

软件将每次任务组织成独立项目，包含视频信息、关键帧、COLMAP 数据、Brush 中间结果、状态文件、日志和 `outputs/final.ply`。默认输出为当前 Windows 用户的 `Downloads` 文件夹。

## 配置与安全

软件配置保存在：

```text
%LOCALAPPDATA%\\VideoTo3DStudio\\settings.json
```

API 密钥默认只在当前运行期间使用；勾选“保存密钥”后才保存到本机配置。密钥不会写入源码、`.blend`、项目 `project.json`、Git 历史或 GitHub Release。请不要把个人配置文件复制到仓库。

## 项目目录

```text
<Downloads>/<时间>_<视频名>/
├─ source/                 # 输入视频所在项目副本位置
├─ work/frames/            # FFmpeg 关键帧
├─ work/colmap/            # database.db、images、sparse/0
├─ work/brush/             # Brush 训练导出和参数
├─ logs/pipeline.log       # 完整流水线日志
├─ project.json
├─ state.json
└─ outputs/final.ply       # 最终 Gaussian Splatting 模型
```

## OOOSplat 参考边界

软件参考 OOOSplat 的项目化任务、阶段进度、日志和质量档位工作流，但没有复制 OOOSplat 的代码、二进制或资源。OOOSplat 官方仓库的代码和文档采用 Apache-2.0，品牌与商标另行处理；本项目自身采用 MIT License。

## 故障排查

- 抽帧失败：确认 FFmpeg/FFprobe 可执行，并且视频路径无权限问题。
- COLMAP 失败：使用有明显纹理、稳定运动和足够重叠视角的视频；反光手表表盘、纯色墙面和快速抖动会降低注册率。
- Brush 失败：确认 Brush 是支持 CLI 的版本，并检查显卡驱动、图形后端、显存和 `--max-resolution`/`--max-splats`。
- 本地引擎检查失败：优先填写 `colmap.exe` 和 `brush.exe` 的完整路径，再点击“检查本地引擎”。

