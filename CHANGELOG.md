# 更新日志

## v0.5.0 — COLMAP + Brush 本地核心流水线

- 将本地默认建模核心改为 `FFmpeg/FFprobe → COLMAP → Brush`。
- 增加 COLMAP 特征提取、顺序匹配、相机位姿重建和 sparse 数据集准备。
- 增加 Brush CLI 训练参数、GPU 开关、训练进度解析和 `outputs/final.ply` 发布。
- 默认不依赖 Blender；保留 Hunyuan3D 网格和 Blender 兼容模式。
- 增强本地引擎检查，启动前可检查 COLMAP 与 Brush。

## v0.4.0 — 本地建模模式

- 增加本地 Hunyuan3D API 建模引擎，默认不依赖 Blender。
- 增加本地引擎地址、健康检查和网格模型直接输出。
- 保留 Blender 程序化兼容模式，外部视觉模型接口仅在该模式使用。
- 调整桌面软件文档和发布包说明。

## v0.3.0 — VideoTo3D Studio 桌面软件 MVP

- 增加无需 npm 依赖的本地桌面工作台和中文浏览器界面。
- 增加项目创建、历史项目、阶段进度、百分比、耗时、取消和实时日志。
- 增加视频信息读取、关键帧抽取、外部聊天模型分析和 Blender 5.2 重建执行器。
- 增加 GLB、glTF、OBJ、FBX 和 `.blend` 输出。
- 增加本机配置保存，API 密钥不进入源码、仓库或发布包。
- 默认输出位置为 Windows 用户的 `Downloads` 文件夹。
- 保留 Blender 插件作为高级工作流入口。

## v0.2.0 — 首个开源发布版

- 增加中文 Blender 插件面板。
- 增加视频关键帧抽取。
- 增加聊天兼容接口和视频上传接口。
- 增加 `/v1/models` 模型列表读取与模型选择。
- 增加 Blender 用户偏好配置保存。
- API 密钥使用密码字段显示，不写入 `.blend` 或源码。
- 增加快速、均衡、精细三档质量档位。
- 增加项目目录、状态文件、日志和百分比进度。
- 增加 GLB、glTF、OBJ、FBX 输出。
- 默认输出位置设为 Windows 用户下载目录。
- 参考 OOOSplat 的项目化任务组织方式，但不包含其代码或资源。

