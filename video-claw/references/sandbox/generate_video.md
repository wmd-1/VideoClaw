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

> 参数超出模型能力范围时会被夹取到边界值（不会直接失败），夹取/忽略事实记录在后端日志，并随响应 `warnings` 字段透出。

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