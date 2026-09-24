# 提示词改写（可选阶段）

> 本阶段仅在 `design_agent.enable=true`（`config.yaml` 或 `.env` 的 `VC_DESIGN_AGENT__ENABLE`）时存在，
> 位于参考图（停点5）与视频生成之间。默认禁用时主流程保持六阶段，跳过本文档。

执行阶段：把每个分镜的镜头描述改写为 MiniMax H3 规范的提示词（通过 Design Agent Platform 的 `minimax-h3-prompt-writing` 能力，T2VA 文本模式，改写正文为英文）。

## 前置条件

- 分镜阶段已完成（本阶段按 `segment_id` 逐条改写）。
- `design_agent.enable=true` 且 `design_agent.base_url` 已配置；未配置时执行会返回明确错误。

## 请求

```bash
curl -X POST "http://localhost:8000/api/project/{session_id}/execute/prompt_rewrite" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "xxx"}'
```

> ⚠️ 每个分镜需向外部改写服务串行提交一轮请求，阶段耗时为分钟级；请耐心等待 SSE 进度事件。

## 停点说明

此阶段有 1 个停点：改写完成后需要用户确认。

## 产物结构

```json
{
  "session_id": "外部改写会话 ID",
  "items": [
    {
      "id": "seg_01_01",
      "original_prompt": "改写输入的分镜描述",
      "rewritten_prompt": "H3 规范提示词（英文）",
      "input_mode": "T2VA",
      "duration": 8,
      "status": "done"
    }
  ]
}
```

## 实时反馈

SSE 会逐条发送条目完成事件：

```json
{
  "type": "progress",
  "data": {
    "asset_complete": {"type": "items", "id": "seg_01_01", "status": "done"}
  }
}
```

## 停点：改写完成，等待用户确认后继续下一阶段

向用户展示各分镜的改写结果（`items[].rewritten_prompt`），发送前端 URL
（`http://{local_ip}:3000/?session={session_id}&stage=prompt_rewrite`），询问确认。

## 常用操作

```bash
# 单条重新生成
curl -X POST "http://localhost:8000/api/project/{session_id}/intervene" \
  -H "Content-Type: application/json" \
  -d '{"stage": "prompt_rewrite", "modifications": {"regenerate_items": ["seg_01_01"]}}'

# 按用户修改意见修订（复用外部会话多轮上下文）
curl -X POST "http://localhost:8000/api/project/{session_id}/intervene" \
  -H "Content-Type: application/json" \
  -d '{"stage": "prompt_rewrite", "modifications": {"revise_items": [{"id": "seg_01_01", "instruction": "改成雨天"}]}}'

# 用户直接编辑文本
curl -X PATCH "http://localhost:8000/api/project/{session_id}/artifact/prompt_rewrite" \
  -H "Content-Type: application/json" \
  -d '{"items": [{"id": "seg_01_01", "rewritten_prompt": "用户编辑后的文本"}]}'
```

## 继续下一阶段

用户确认后调用 `POST /api/project/{session_id}/continue`。视频生成阶段会自动使用改写结果；
个别失败条目可直接跳过（视频阶段对失败条目回退默认提示词拼装）。

## 常见问题

| 错误 | 原因 | 解决方法 |
|------|------|----------|
| `提示词改写阶段未启用` | `design_agent.enable=false` | 在设置页「提示词改写服务」开启，或 `.env` 配置 `VC_DESIGN_AGENT__ENABLE=true` |
| `提示词改写服务地址未配置` | `design_agent.base_url` 为空 | 填写 Design Agent Platform 服务地址 |
| `提示词改写服务不可达` | 外部服务未启动/地址错误 | 检查外部服务状态后逐条重试 |
| 条目 status=failed | 外部会话配额（429）/超时 | 稍后单条重新生成；不阻塞其他条目 |
