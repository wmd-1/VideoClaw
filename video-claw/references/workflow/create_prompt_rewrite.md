# 提示词改写（可选阶段）

> 本阶段仅在 `h3_rewrite.enable=true`（`config.yaml` 或 `.env` 的 `VC_H3_REWRITE__ENABLE`）时存在，
> 位于参考图（停点5）与视频生成之间。默认禁用时主流程保持六阶段，跳过本文档。

执行阶段：把每个分镜的镜头描述改写为 MiniMax H3 规范的提示词（原生实现：知识资产内嵌于
`prompts/prompt_rewrite/`，本地 LLM 调用；输入模式由 `video_generation_mode` 与素材角色推导，
改写正文为英文）。

## 前置条件

- 分镜阶段已完成（本阶段按 `segment_id` 逐条改写）。
- `h3_rewrite.enable=true`；未启用时执行会返回明确错误（提示 `VC_H3_REWRITE__ENABLE`）。

## 请求

```bash
curl -X POST "http://localhost:8000/api/project/{session_id}/execute/prompt_rewrite" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "xxx"}'
```

> ⚠️ 每个分镜串行发起一次本地 LLM 调用（结构校验失败时最多追加 2 次带违规清单的修订调用），阶段耗时为分钟级；请耐心等待 SSE 进度事件。

## 停点说明

此阶段有 1 个停点：改写完成后需要用户确认。

## 产物结构

```json
{
  "items": [
    {
      "id": "seg_01_01",
      "original_prompt": "改写输入的分镜描述",
      "rewritten_prompt": "H3 规范提示词（英文）",
      "input_mode": "I2VA",
      "duration": 8,
      "continuity": "主体与风格连续性摘要",
      "grounding": {"image_hash": "...", "described": true},
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

# 按用户修改意见修订（携带上一版提示词与连续性摘要）
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
| `提示词改写阶段未启用` | `h3_rewrite.enable=false` | 在设置页「H3 提示词改写」开启，或 `.env` 配置 `VC_H3_REWRITE__ENABLE=true` |
| `结构校验未通过：...` | LLM 输出漏字段/抽象词/标签不一致 | 系统已自动回喂重试 ≤2 次；仍失败可单条重新生成 |
| 条目 status=failed | LLM 调用失败 / 校验重试耗尽 | 单条重新生成；不阻塞其他条目；视频阶段对失败条目回退默认拼装 |
| `grounding.text_only=true` | VLM 不可用或 grounding 开关关闭 | 改写仍完成（纯文本输入，模式不变）；可配置 `h3_rewrite.vlm_model` 后重试 |
