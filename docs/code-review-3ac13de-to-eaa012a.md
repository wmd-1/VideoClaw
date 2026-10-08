# VideoClaw 提交区间代码评审报告（第三轮）

- **评审日期**：2026-10-07
- **评审范围**：`3ac13de..eaa012a`（上轮报告终点之后的 5 个提交，HEAD `eaa012a`，工作区起始干净）
  - `8d02e47` fix(h3-rewrite)：第二轮评审 C-1（首尾帧字面量接线）+ I-A（沙盒媒体入参守卫）
  - `076b727` fix(h3-rewrite)：Minor 加固承接 M-a~M-f
  - `386ac59` fix(video-config)：高级参数首屏按模型能力联动（`capsReady`）
  - `ed0b874` docs(tips)：打包迁移 tar 命令与 sudo 残留笔误
  - `eaa012a` fix(h3-rewrite)：外部平台残留文案清理 + 一键生成阶段数动态取数
  - 规模：17 files，+491/−82
- **对照基准**：`docs/code-review-07b4bdb-to-3ac13de.md`（C-1/I-A/M-a~M-f 定义与修复要求）、`docs/minor-plan-07b4bdb-to-3ac13de.md`（M-c 采纳现状 / M-d 仅加汇总日志 / M-e 维持预留）、openspec `native-h3-prompt-rewriter` tasks §9.4/§10、`video-audio-video-references` tasks §7.4、`video-generation-parameter-system` tasks §6.1（openspec 工件按项目约定不入库）
- **评审方式**：单个 Code Reviewer 独立评审（Superpowers `requesting-code-review` 流程，只读、不派发二级评审）；报告撰写者对关键结论逐条以源码 grep / 容器内实测交叉核实，其中**评审者漏判一项回归（本报告 I-2）由核实阶段发现**
- **前轮报告**：`docs/code-review-07b4bdb-to-3ac13de.md`（第二轮）、`docs/code-review-6ba2f19-to-cdea2a1.md`（第一轮）

## 修复更新

2026-10-07（本轮）：**I-1、I-2 已修复，M-3 一并收口**，均在既有 `video-claw-backend` 容器内验证通过（见 §五）。M-1/M-2/M-4/M-5 未动，登记为待办（见 §六）。

2026-10-08（承接）：**M-1、M-2、M-4 已实施并在容器内验证通过**；M-5 经拍板 **维持现状**（已接受取舍，不动码，重议条件登记于 openspec）。至此本轮 Critical/Important/Minor 全部关闭，无遗留代码项。

---

## 一、优点（Strengths）

1. **C-1 修复真正打通接线层，无残留**
   `core/agents/prompt_rewrite_agent.py` L474 入口 `.strip().lower()` 归一、L496 判定 `== "start_end_frame"`。全仓 grep 确认非规范 `"start_end"` 仅剩 `verify_native_h3_rewriter.py` 5f 的互斥负例输入与文档说明；前端枚举、`video_agent.py` L162/L597/L681、`workflow.py`、`stages.py` 与 resolver 内部判定统一到规范字面量。

2. **测试灵敏度实质升级（直击本项目"自洽假绿"历史踩坑）**
   `tmp_verify/h3_golden.py` L49/L87 改用真实 `"start_end_frame"` 且经 `agent.process()` → `_rewrite_segments` 走真实接线（FL2VA 用例带 IMG1/IMG2 首尾选中图），实现回退即 `input_mode` 由 FL2VA 跌为 I2VA → 断言红；`verify_native_h3_rewriter.py` L244-264 的 5e/5f 是一对**互斥**断言（规范值必须产出 FL2VA、非规范值必须不产出），双向覆盖回归。

3. **M-d 日志断言的环境敏感陷阱被识别并修复**
   `verify_native_h3_rewriter.py` L212 补 `_agent_logger.setLevel(_logging.DEBUG)`，堵掉"根 logger 默认 WARNING 滤掉 INFO 导致断言恒真"；5c4/5c5 以具体降级数（"2/2"）断言，计数丢失即红。

4. **`capsReady` 三态设计闭环、无死锁分支**
   `frontend/components/TopBar.tsx` L227-287：无选中模型 → `videoCaps=null` + `capsReady=true`（回落全量默认）；fetch 成功 → 先 `setCapsReady(true)` 再判 `!caps` 决定是否夹取；`catch` → `setCapsReady(true)`。L302-303 以 `durationMin=1 / durationMax=0` 让加载期自然产出空数组，从源头消除"闪现 11–15s、21:9 后被静默改回"。

5. **I-A 时序与语义正确**
   守卫在 `_start_active_task` / `generate_video` **之前**执行，越界不发起生成；合法件透传 `os.path.realpath` 结果，与 `resolve_within_allowed_dirs` 语义一致，路径归一不丢。

6. **文案残留清理彻底**
   grep 结果：`Design Agent Platform` 仅剩 `prompt_rewrite_agent.py` L6 的"已废弃"说明；`外部会话` 仅剩 `session_format.md` L225 的否定句；`六个阶段` 仅剩 `WorkflowPanel.tsx` L1402 代码注释（非 UI 文案）。`HomePage.tsx` L484 tooltip 已按 `useEnabledStages().length` 动态取数，符合前端文案动态化规范。

---

## 二、问题（本轮新发现）

### Critical（必须修）

无。

### Important

#### I-1　I-A 的"全部媒体入参统一守卫"只接了 video 端点，同文件 i2i / vlm 未纳入 —— **已修 ✅**
- 位置：`backend/api/routers/sandbox.py` vlm handler（原 L334-343，`image_paths=req.images` 原样透传）、i2i handler（原 L410-420，`image_paths=[req.image]`）
- 问题：下游适配层对本地路径就是"open + base64/multipart 外传"（`models/image_gpt.py` L65-70、`models/image_seedream.py` L186-190、`models/custom_image.py` L143-146）。零鉴权下 `POST /api/sandbox/i2i {"image":"/etc/passwd"}`、`POST /api/sandbox/vlm {"images":["~/.ssh/id_rsa"]}` 的任意文件读取+外传风险仍在。
- 为何重要：与上轮批评的"C1 只堵了 audio_url"**结构完全同构**；而 `8d02e47` 提交信息与上轮报告修复段写的是"全部媒体入参"，承诺未兑现。灵敏度抽测实证：把守卫退化为透传后，i2i 越界请求 `success=True` 且路径确实抵达适配层（见 §五）。
- 修法（已实施）：`_guard_local_media` → `_guard_media_input`，新增 `_guard_media_inputs` 列表助手；i2i 的 `req.image`、vlm 的 `req.images` 逐条套用，均在 `_start_active_task` **之前**拒绝；`input_data` 存守卫后的规范化路径（历史记录回放口径一致）。

#### I-2　`8d02e47` 的守卫打断了沙盒图片"URL 地址"输入模式（评审者未识别，核实阶段发现）—— **已修 ✅**
- 位置：`frontend/components/Sandbox/Sandbox.tsx` L232-256（`ImageUploader` 的 `inputMode==='url'`，vlm/i2i/video 三端共用同一字段）；`sandbox.py` 原 `_guard_local_media`
- 问题：`https://example.com/a.png` 既非绝对路径也非 `CODE_DIR` 内文件 → 走 `candidates` 找不到 → 抛"参考图片 文件不存在"，**video 端远程首帧图自 `8d02e47` 起被守卫误拒**。
- 为何重要：这是上轮加固引入的**功能回归**，且是 UI 明示的输入模式；`verify_sandbox_media_refs.py` 只断言了 `audio_url` 的 HTTP 透传，图片侧无覆盖，故两轮均未暴露。
- 修法（已实施）：守卫对 `http://`/`https://`/`data:`/`file://`/`oss://` 远程形态原样透传（与 `_resolve_media_reference_url` L184 的 audio 口径一致，且适配层本就支持——`vlm_client.py` L124/L157、`video_dashscope.py` L517、`image_client.py` L279、`custom_common.py` L157）；远程形态不经本地 open，无越界读取面。

### Minor

| 编号 | 问题 | 位置 | 状态 |
|---|---|---|---|
| M-1 | `ids == expected and bool(ids or True)` 是**永真式**（`ids or True` 恒真），M-f 所称"双保险"的这半为死代码，易被误读为已有额外守卫 | `tmp_verify/verify_models_filter.py` L57 | **已修 ✅**（2026-10-08，删永真式） |
| M-2 | `expected` 仅按 `types` 推导，未叠供应商完整性判定；存在"声明但 provider 残缺"的自定义模型时会 false-red（方向安全但不鲁棒） | 同上 L43-52；对照 `models/config_model.py` L851-868 | **已修 ✅**（2026-10-08，新增 `custom_provider_ok` 谓词 + 合成自检 + 与后端逐项漂移校核） |
| M-3 | 历史记录中 `reference_image` 存守卫后绝对路径、`reference_videos` 存原始输入，回放/审计归一程度不一致 | `sandbox.py` video `input_data` | **已修 ✅**（改存 `reference_video_paths`，并同步 i2i/vlm 存守卫值） |
| M-4 | `.env.example` 文件末尾仍无换行（前轮 M3 遗留，本轮改该文件未顺手修） | `.env.example` | **已修 ✅**（2026-10-08，仅补单个终止换行，正文逐字节不变） |
| M-5 | `capsReady` 三态未覆盖"fetch 既不 resolve 也不 reject"的悬挂，理论上永久 disabled（概率极低，属"以极小概率禁用换首屏正确"的取舍） | `TopBar.tsx` L236-284 | **已接受取舍（2026-10-08 拍板）**，不动码；重议条件登记于 openspec |

---

## 三、上轮未修项核实表

| 条目 | 状态 | 证据 |
|---|---|---|
| **C-1** 首尾帧字面量接线 | **已修，未回归** | `prompt_rewrite_agent.py` L472-474/L496；`h3_golden.py` L49/L87；`verify_native_h3_rewriter.py` L28-33、L244-264；全仓 grep 无残留 |
| **I-A** 沙盒媒体入参统一守卫 | 本轮复核为**部分修**（仅 video 端）→ 随 I-1 **已修至 i2i/vlm** | `sandbox.py` L211-240（守卫）、L343-352（vlm）、L425-441（i2i）、L480-491（video）；`verify_sandbox_path_guard.py` 17 → 27 项 |
| **M-a** api.md "外部会话"残留 | **已修** | `backend/docs/api.md` L342-346（改本地版本链/continuity 语义，补降级与 temperature 说明） |
| **M-b** `.env.example` 旧 `VC_DESIGN_AGENT__*` | **已修**（M3 末行换行未顺手处理 → 本报告 M-4） | `.env.example` diff |
| **M-c** `system_zh.txt` 内联策略 | **按方案"采纳现状"**落地，无实现变更 | `8d02e47`/`076b727` 提交说明；代码未动 |
| **M-d** grounding 降级可观测性 | **已修** | `prompt_rewrite_agent.py` L567-572 汇总日志；`settings/page.tsx` L76；`verify_native_h3_rewriter.py` L211-231 |
| **M-e** temperature 预留标注 | **已修** | `settings/page.tsx` L82；`api.md` L342 |
| **M-f** 断言精度恢复 | **部分修**：`verify_models_filter.py` 已恢复动态精确期望 + 多能力 AND 交集；残留 M-1 永真式。方案点名的 `verify_prune.py` 经核实现有断言本就是集合相等（L30/L33/L39/L67），属**原报告口径过宽**，非缺陷 | `verify_models_filter.py` L33-85 |

---

## 四、架构与流程建议

1. **媒体入参守卫应上升为统一入口约定**，而非逐端点补线：新增沙盒/工作流端点时，凡把用户可控字符串交给适配层 open/上传，都必须经 `_guard_media_input(s)`。建议在 `api.md` 或 `WORKFLOW.md` 固化该约束。
2. **提交信息与整改范围必须严格对齐**：本轮"全部媒体入参"与"仅 video 端"的落差正是上轮 C1→I-A 的同构问题复发的原因。范围受限时应显式写"仅 X 端，Y 端待后续 change"。
3. **加固类改动须同步补"合法输入形态"的正向断言**：C-1/I-A 只验了拒绝路径，未验 URL/data: 这类既有正向用法，导致 I-2 回归漏网。
4. **验证脚本禁止永真式与不可判定断言**（M-1）；期望集应尽量从同一份 raw config 显式重实现谓词，而非只取 `types`（M-2）。

---

## 五、容器内验证证据（既有镜像，未新建/重建）

```bash
# 以下均在项目根，对既有 video-claw-backend 容器执行（代码以只读 bind mount 进 /app）
docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_sandbox_path_guard.py      # 27 项 ALL PASS（含新增 i2i/vlm 10 项）
docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_sandbox_media_refs.py       # ALL PASS（audio/视频参考回归）
docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_workflow_audio_ref.py      # ALL PASS（工作流侧回归）
docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_video_params.py             # ALL PASS
docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_video_agent_prompt.py       # 6 项 PASS
docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_native_h3_rewriter.py       # C-1/M-d 相关，评审期间全绿
# I-1 灵敏度抽测：容器内进程级把 _guard_media_input 退化为透传 →
#   「i2i 绝对路径越界被拒绝」「i2i 越界拒绝时不调用适配层」均判红，证明断言有真实灵敏度、且原风险确为活口
```

I-2 的正向验证：新增第 17 组断言覆盖 `https://` 与 `data:` 在 video / i2i / vlm 三端原样透传（此前无任何用例）。

**2026-10-08 追加（M-1/M-2 承接）**：

```bash
docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_models_filter.py            # 由 10 项增至 22 项 ALL PASS
docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_multi_ability_filter.py     # ALL PASS（同能力域回归）
docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_prune.py                    # ALL PASS（同推导法回归）
```

新增的 22 项包含：5 项「供应商完整性」谓词合成自检（完整/protocol 未注册/base_url 非 http(s)/provider 未定义/CUSTOM_PROTOCOLS 非空防自检空转）与 6 项「谓词与后端 `model_availability` 逐项一致」漂移交叉校核。不依赖本机配置即可验证谓词分支覆盖，避免“声明但 provider 残缺”环境下的 false-red。

---

## 六、待办处置与 openspec 挂接（已于 2026-10-08 全部收口，未新建 change）

- **M-1**（`verify_models_filter.py#L57` 永真式）→ `video-generation-parameter-system` tasks **7.1**：已实施 ✅（改回 `ids == expected` 并重写该条描述为「类型声明 ∧ 供应商完整」推导集）
- **M-2**（期望集未叠加供应商完整性谓词）→ 同 change tasks **7.2**：已实施 ✅（新增 `custom_provider_ok`，四处期望集均参与；谓词独立重实现不复用被测 `model_availability`，另加合成自检与逐项漂移交叉校核）
- **M-4**（`.env.example` 末行换行）→ `video-audio-video-references` tasks **7.7**：已实施 ✅（仅补单个终止换行，`cmp` 确认正文逐字节不变）
- **M-5**（`capsReady` 悬挂）→ 经拍板 **维持现状、不动码**：登记于 `video-generation-parameter-system` tasks **7.3** 与该 change design 的 Risks/Trade-offs，含重议触发条件（实测首屏长期不可点、或能力拉取引入新来源时再加 `AbortController`+超时并同步 spec Scenario）
- **openspec 契约同步已完成**（本轮代码 `fa78e83` 先于规格落地，属必要回填）：`video-audio-video-references` 的守卫 Requirement 改名为「沙盒媒体入参统一路径守卫」并覆盖三端（含逐条守卫、列表入参原子拒绝、`input_data` 存规范化值），新增「远程形态原样透传」与「列表入参含越界条目时整请求拒绝」两个 Scenario（后者是 I-2 的契约，缺它下轮加固可能再次误拒 URL 输入）；proposal 修正 I-A 的“全部媒体入参”过宽表述并追加 I-1/I-2 条目；design 新增 D6 记录守卫的三点取舍。两个 change `openspec validate --type change --strict` 均 valid（openspec 工件不入库）。
- **仍未清的项目（非本轮发现）**：`video-audio-video-references` tasks **6.3**、`video-generation-parameter-system` tasks **5.3** —— 均需真实视频端点/真模型的真机验证，本环境不具备；两个 change 因此**暂不能归档**。

## 七、结论

**可以承接吗：是（I-1 / I-2 / M-3 修复后）。**

理由：核心整改 C-1 经"字面量归一 + 金标真实输入 + 互斥接线断言 + 全仓 grep"四层交叉验证，实现与测试可信度高，且难得地把本项目反复踩到的"验证脚本自洽假绿"真正修掉了。本轮唯一不充分处是 I-A 的范围承诺（I-1）及其引入的输入模式回归（I-2），两者已由同一个"统一守卫 + 远程形态透传"改动收口并经容器实测；余下均为测试脚本鲁棒性与 UI 兜底类 Minor，不阻塞。
