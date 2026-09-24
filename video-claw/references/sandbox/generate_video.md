# 视频生成

使用图片或文字生成视频片段（15秒以内）。

## 请求

```bash
curl -X POST "http://localhost:8000/api/sandbox/video" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "wan2.6-i2v-flash",
    "prompt": "一只猫在草地上奔跑",
    "image": "code/result/image/user_upload/cat.png",
    "ratio": "16:9",
    "resolution": "720P",
    "duration": 5
  }'
```

## 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| model | | 模型，默认 wan2.6-i2v-flash |
| prompt | ✅ | 视频描述 |
| image | | 参考图片路径（文生视频可省略） |
| ratio | | 画幅比例，默认 16:9（可选 9:16 / 1:1 / 4:3 / 3:4 / 21:9） |
| resolution | | 分辨率档位，默认 720P（可选 1080P；也支持显式 `WxH`，如 `1280x720`） |
| duration | | 时长（秒），默认 5；实际按模型 `capabilities.duration{min,max}` 夹取 |
| fps | | 帧率（可选）；仅自定义模型声明 `capabilities.fps` 支持列表时注入对应协议字段，越界夹取到最近支持值，未声明时忽略 |
| short_edge | | 短边像素（可选，如 768）；模型声明或显式指定时，vllm-omni 走 `short_edge`+`aspect_ratio`、sglang 注入 `target{short_edge, aspect_ratio, duration_seconds}` |
| audio_url | | 音频参考（可选，唇形同步等）：HTTP(S) URL、`data:` URL，或本地已上传文件路径（后端自动转换为服务端可访问 URL）；仅对声明 `audio_reference` 能力的自定义视频模型生效 |
| reference_videos | | 参考视频列表（可选，主体/背景迁移等）：已上传视频文件路径（mp4/mov/webm），与图片参考可组合；仅对声明 `video_reference` 能力的自定义视频模型生效 |

> 参数超出模型能力范围时会被夹取到边界值（不会直接失败），夹取/忽略事实记录在后端日志，并随响应 `warnings` 字段透出。

## 音视频参考（自定义视频模型，vllm-omni / MiniMax-H3 ref2va）

- **音频参考**：`audio_url` 支持三种形态——HTTP(S) URL 直填；`data:` URL（`data:audio/wav;base64,...`）；本地文件路径（如 `/api/upload_media` 返回的路径），后端会自动复制到 `result/sandbox/uploads/` 并按 backend 对外基址组装绝对 URL 提交。
- **跨机可达性要求**：backend 与推理服务通常部署在不同机器上，组装出的 URL 必须能被**推理服务端**访问（两者需同网段或放通防火墙）。若不可达，请改填公网 HTTP(S) URL 或 `data:` URL。
- **参考视频**：`reference_videos` 为本地视频文件路径列表，后端以 `input_references` multipart 重复上传（MIME 按文件类型推断），可与 `image` 图片参考按顺序组合。
- **失败语义**：媒体参考被服务端拒绝（4xx）时请求按既有回退序列处理，最终失败且错误信息包含服务端响应与目标地址，不会静默退化为"无参考"请求。
- **模型选择**：仅在设置页为自定义模型声明 `audio_reference` / `video_reference` 能力标签后，相应入口才会展示该模型；未声明能力的模型直接调用时返回能力不支持错误。

## 可用模型

| 模型 | 说明 |
|------|------|
| wan2.6-i2v-flash | 默认，最快 |
| wan2.6-i2v | |
| kling-v3 | |
| kling-v2-6 | |
| jimeng_ti2v_v30_pro | |

## ⚠️ 路径格式

- `image` 必须使用 `code/result/...` 格式的**相对路径**
- 禁止使用完整 URL 或本地路径

## 响应

```json
{
  "success": true,
  "video_path": "code/result/sandbox/videos/xxx.mp4",
  "record_id": "xxx",
  "warnings": ["duration 20s -> 15s（模型能力范围 4-15s）"]
}
```

`warnings` 为可选字段：仅当请求参数被夹取或忽略时返回，记录实际的参数调整事实。

## 获取视频文件

```python
# 直接从后端目录复制
backend_path = "/code/result/sandbox/videos/{record_id}.mp4"
local_path = "~/.openclaw/workspace/temp_imgs/{record_id}.mp4"
shutil.copy2(backend_path, local_path)
```

## 注意事项

1. 生成的视频较短（15秒以内）
2. 如果需要生成长视频，请使用完整工作流

## 常见问题

| 错误 | 原因 | 解决方法 |
|------|------|----------|
| `curl: (7) Failed to connect` | 后端未运行 | 启动后端服务 |
| `404 Not Found` | 路径格式错误 | 使用 `code/result/...` 格式 |
| `"error": "image not found"` | 图片文件不存在 | 检查图片路径是否正确 |
| `"success": false` | API Key 额度用完或无效 | 检查对应平台的 API Key |