# VideoLab

MiniMax H3 本地视频工作台。后端只使用 Python 标准库，前端为单文件原生 HTML/CSS/JS，不需要安装额外依赖或 custom_nodes。

## 安装与启动

需要 Windows 10+、Python ≥ 3.10，以及支持 MiniMax H3 的 ComfyUI 便携版。模型权重不包含在本项目中。

```sh
git clone https://github.com/unistar-ai/VideoLab.git
cd VideoLab
```

双击 `启动_VideoLab.bat`。程序自动使用上级目录中的 `..\ComfyUI_windows_portable`，也可用 `VIDEOLAB_COMFY_HOME` 或 `--comfy-home` 指定其他位置。

- VideoLab：`http://127.0.0.1:8766`（被占用时依次尝试后续端口）
- ComfyUI：`http://127.0.0.1:8189`，独立于 ImageLab 的 8188
- 也可在便携版目录运行 `run_h3.bat`，手动启动 8189 后端
- 退出时只关闭 VideoLab 自己启动的 ComfyUI；已在 8189 运行的实例会被复用，不会被关闭

调试：`python app.py --no-window`

## 模型

ComfyUI 需为支持 MiniMax H3 的 0.42.1 或兼容版本。以下五个文件必须全部存在，文件名必须完全一致；VideoLab 不会用相似名称或目录中的其他权重替代。

| 相对 `ComfyUI\models\` 的路径 |
|---|
| `diffusion_models\minimax_h3_fl2va_pruned_int8_convrot.safetensors` |
| `diffusion_models\minimax_h3_ref2va_pruned_int8_convrot.safetensors` |
| `text_encoders\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` |
| `vae\minimax_h3_video_vae_fp16.safetensors` |
| `vae\minimax_h3_audio_vae_fp32.safetensors` |

权重不包含在本项目中，模型许可与代码的 MIT 许可各自独立。基础模型未齐全时，健康检查和生成接口会明确列出缺失文件。

Turbo 是可选能力，以下 LoRA 放入 `ComfyUI\models\loras\`，只影响对应模式，缺失不会禁用标准画质：

| 用途 | 文件名 |
|---|---|
| T2V / I2V Turbo | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` |
| R2V Turbo | `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` |

## 使用

- **文生视频**：输入提示词直接生成。
- **首尾帧视频**：上传可选首帧、尾帧，至少选择一张。图片用于锚定输出视频的起始或结束画面，使用 fl2va 权重。
- **参考图视频（R2V）**：上传 1–9 张参考图，并用文字描述角色、物体或场景如何出现在视频中，使用原生 ref2va 工作流。参考图不是首帧，也不会自动成为视频画面。图片按添加顺序编号，删除中间图片后剩余图片自动重新编号；提示词用 `<Picture 1>` 到 `<Picture 9>` 按相同顺序指代，例如：
  ```
  让 <Picture 1> 中的角色走进 <Picture 2> 中的场景，转身向镜头挥手，伴有轻柔背景音乐。
  ```
- 首尾帧和参考图均来自本机，上传后写入 ComfyUI 的 `input` 目录。
- **标准画质**：默认 20 步，质量优先，适合最终结果。
- **Turbo 加速**：通过对应官方 LoRA 减少采样步数，适合快速预览和迭代；T2V/I2V 默认 8 步，R2V 默认 4 步，画质和细节可能不如标准模式。切换视频模式或画质会套用对应默认步数，之后仍可手动调整。
- 默认 `864×480`、`5 秒（124 帧）`，所有模式使用 `res_multistep + simple`。提供横屏、竖屏、方形和高清档位。
- 时长 `5–15 秒`，24 fps，帧数向上对齐到 `17k+5`。
- 批量 `1–4` 个，逐个串行生成。随机种子每次不同；固定种子在批量中依次递增。
- 生成中可点停止或按 Esc；已完成的视频会保留并写入历史。
- 主预览支持播放、声音和保存。作品库支持 MP4/WebM 的浏览、筛选、删除及打开所在文件夹。
- `history.json` 保存最近 100 条任务，点击历史可恢复提示词、Turbo 状态、实际步数、其他参数、种子和对应的首尾帧或参考图。删除历史不会删除视频。

输出由 `SaveVideo` 写为 H.264 MP4，文件名前缀为 `video/VideoLab`，通常位于 `ComfyUI\output\video\`。视频任务单个等待上限为 4 小时。

当前不包含提示词增强、参考视频/音频、ControlNet 或多引擎切换。

## 文件与日志

- `app.py`：本地服务、H3 API graph、任务轮询和历史/作品库接口
- `ui.html`：工作台界面
- `videolab.log`：VideoLab 运行日志
- `comfyui.log`：VideoLab 启动的 ComfyUI 日志
- `_window_profile\`：独立 Edge/Chrome 应用窗口配置

关闭应用窗口后，窗口检测与前端心跳共同决定退出；也可点右上角“退出”立即结束。
