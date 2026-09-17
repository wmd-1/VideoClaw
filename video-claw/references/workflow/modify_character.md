# 修改角色/场景提示词

在第二阶段完成后，用户可以修改角色或场景的视觉提示词并重新生成。

## 修改角色提示词

```bash
curl -X PATCH "http://localhost:8000/api/project/{session_id}/artifact/character_design" \
  -H "Content-Type: application/json" \
  -d '{
    "characters": [
      {
        "id": "char_1",
        "visual_prompt": "新的视觉提示词"
      }
    ]
  }'
```

## 重新执行角色/场景设计

```bash
curl -X POST "http://localhost:8000/api/project/{session_id}/execute/character_design" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "xxx"}'
```

## 参数说明

| 参数 | 说明 |
|------|------|
| characters[].id | 角色 ID |
| characters[].visual_prompt | 新的视觉提示词 |

> 修改后需要重新执行阶段才能生效。

## 常见问题

| 错误 | 原因 | 解决方法 |
|------|------|----------|
| `curl: (7) Failed to connect` | 后端未运行 | 启动后端服务 |
| `404 Not Found` | session_id 错误 | 确认 session_id 正确 |
| PATCH 成功但无变化 | 需要重新执行阶段 | 调用 execute/character_design 重新生成 |
| 角色 ID 不存在 | ID 错误 | 从 artifact 中获取正确的角色 ID |

---

## 模型不可用（model_unavailable）时的处理

本流程的重新生成（`execute` / `intervene`）会先做模型可用性预检；若返回 **409** 且 `detail.code == "model_unavailable"`：

1. 预检失败不会触发任何生成，先向用户说明原因（模型未注册 / 供应商缺失或不完整 / 内置缺 Key）；
2. 可改为上传自有图片完成条目：`POST /api/project/{session_id}/artifact/character_design/upload_image`（`item_type=characters|settings`，`item_id` 为条目 id）；
3. 或先更换模型再重试（会话级 `PATCH /api/project/{session_id}/models` 或设置页修复配置）。