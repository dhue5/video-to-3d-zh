# 安装与首次配置

## VideoTo3D Studio 桌面软件

桌面软件是推荐入口，适用于手表和其他物品的视频建模。

1. 安装 Windows 版 Node.js 22+、FFmpeg/FFprobe、COLMAP 和支持 CLI 的 Brush。
2. 解压 GitHub Release 中的 `video_to_3d_studio-v0.5.0.zip`。
3. 双击 `app/start_video_to_3d.bat`；软件会在浏览器打开本地控制台。
4. 选择“本地 COLMAP + Brush（免 Blender）”，填写 `colmap` 和 `brush`，或填写完整路径。
5. 点击“检查本地引擎”，确认两个命令都能响应。
6. 选择视频与质量档位，点击“开始生成”，在进度条和日志中查看阶段进度。

主流程为 `FFmpeg/FFprobe → COLMAP → Brush → final.ply`，不依赖 Blender。Brush 使用可用图形后端进行 Gaussian Splatting 训练，建议使用独立显卡。

也可以选择“本地 Hunyuan3D 网格（免 Blender）”直接输出网格，或选择“Blender 程序化（兼容旧流程）”使用旧版工作流。

软件配置保存在 `%LOCALAPPDATA%\\VideoTo3DStudio\\settings.json`。API 密钥默认不保存，只有主动勾选“保存密钥”才写入本机配置；不要把该文件提交到 GitHub。输出默认位于 `C:\\Users\\你的用户名\\Downloads`。

项目目录会包含 `project.json`、`state.json`、`source`、`work/frames`、`logs/pipeline.log`、`model_spec.json` 和 `outputs`，便于恢复、检查和重复导出。

> 当前实现是“视频关键帧 + COLMAP 相机重建 + Brush Gaussian Splatting”的 MVP，输出为 `final.ply`。

## 方式一：安装发布 ZIP

1. 打开 Blender 5.2 LTS。
2. 进入 **编辑 > 偏好设置 > 插件**。
3. 点击 **安装**，选择 `video_to_3d_zh-v0.2.0.zip`。
4. 搜索“视频转 3D”，勾选插件名称左侧的启用开关。
5. 在 3D 视图按 `N`，打开 **视频转3D** 面板。

不要把 ZIP 解压后只选择内部的 Python 文件安装；应直接选择完整的插件 ZIP。

## 方式二：开发版安装

将仓库中的 `video_to_3d_zh` 文件夹复制到 Blender 的插件目录：

```text
%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\video_to_3d_zh
```

然后重新打开 Blender，并在偏好设置中启用插件。

## 接口配置

在 **视频转3D > 外部模型接口** 中：

- 接口类型选择“聊天兼容接口”。
- 接口 URL 推荐填写供应商的 `/v1` 根地址。
- 点击“读取模型”，插件会访问 `/v1/models`。
- 选择模型后点击“使用选中模型”。
- 点击“保存接口配置”保存 URL、模型和其他非敏感设置。
- 只有在确认本机安全时才勾选“保存接口密钥”。
- 点击“测试接口”验证 `/v1/chat/completions`。

插件支持填写根地址、`/v1`、`/v1/model`、`/v1/models` 或完整聊天接口地址，并会自动规范到标准路径。

## 输出位置

默认输出位置为：

```text
C:\Users\你的用户名\Downloads
```

可以在输入视频区域手动修改输出目录。每次智能建模会在该目录下创建独立的项目文件夹。

## 常见问题

### 插件不能启用

确认选择的是完整 ZIP，并且 ZIP 内部存在：

```text
video_to_3d_zh/__init__.py
```

启用后仍未出现面板时，完全退出 Blender 后重新打开。

### 读取不到模型

确认接口 URL、认证方式和密钥正确，并确认供应商提供 `GET /v1/models`。如果供应商只支持单个模型调用，可以直接在“当前模型”中填写模型名，再点击“测试接口”。

### 建模失败

查看面板底部的实时日志和项目目录中的 `logs/pipeline.log`。常见原因包括接口超时、模型不支持图片输入、返回内容不是 JSON，或视频关键帧不足以判断物体结构。

