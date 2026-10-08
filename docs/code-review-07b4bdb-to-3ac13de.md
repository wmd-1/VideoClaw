# VideoClaw 提交区间代码评审报告（第二轮）

- **评审日期**：2026-09-30
- **修复更新**：2026-09-30（基于 HEAD `3ac13de` 工作区）——C-1 与 I-A 已修复并在既有 Docker 环境验证通过；修复随 openspec 落地于 `native-h3-prompt-rewriter` tasks §9.4 与 `video-audio-video-references` tasks §7.4（spec 同步增补接线级验收与统一守卫 Requirement）。M-a~M-f 本轮仅出方案待定夺，未动代码。
- **评审范围**：`682841e..3ac13de`（`07b4bdb` 及其后共 9 个提交；HEAD `3ac13de`）
  - `07b4bdb` feat(video): 统一视频参数体系（fps/short_edge/时长夹取）→ openspec `video-generation-parameter-system`
  - `fd5c486` feat(video): 自定义视频媒体参考（音频/视频）贯通 → openspec `video-audio-video-references`
  - `cdea2a1` feat(workflow): 提示词改写阶段（外部 Design Agent Platform）→ openspec `minimax-h3-prompt-rewrite-stage`
  - `441d18c` fix(video): 评审加固（C1 沙盒路径白名单 / I3 多能力 AND / M2 sync 标识）
  - `5a0052a` fix(config): 启动校验提示指向设置页，验证脚本端口自适应与断言动态化
  - `bfea86d` docs(handoff): 交接 H3 改写阶段缺陷与验证缺口
  - `e676739` docs(h3-rewrite): 承接评审加固至原生改写器并复核代码评审报告
  - `7305eae` docs(h3-rewrite): 决策依据与 Handoff、MiniMax-H3 参考文档
  - `3ac13de` feat(h3-rewrite): 提示词改写器原生内嵌（替换外部 Design Agent Platform 数据源，31 files +2165/-1190）→ openspec `native-h3-prompt-rewriter`（**本轮重点**）
- **对照规格**：`openspec/changes/native-h3-prompt-rewriter/`（proposal/design/specs/tasks/verification-report）、`docs/决策：H3 提示词改写内嵌化——原生 Skill 集成方案.md`（§5.2 两级模式契约、§5.6 A–D 质量四件套、§8 验收项 = 实现强约束）、其余三个 change 的 specs
- **前轮报告**：`docs/code-review-6ba2f19-to-cdea2a1.md`（问题编号 C1、I1–I4、M1–M3 沿用其定义）
- **复核说明**：本报告中 C-1 与 I-A 两项新发现，报告撰写者已对评审者结论逐条独立核对源码证据（字面量、规范值出处、金标脚本、白名单覆盖面），均属实；前轮未修项核实表亦经抽查确认。

---

## 一、优点（Strengths）

1. **配置迁移扎实**
   `config.py` 新增 `h3_rewrite` 段并逐项归一化（`_coerce_config` L174-192）；`VC_H3_REWRITE__*` 覆盖注册（L278、L422-517）与旧键 `VC_DESIGN_AGENT__*`、旧段 `design_agent` 的一次性告警+pop（L497-502、L187-192）齐全；`_strip_env_overridden` 同步支持 `h3_rewrite.` 路径的 env 恢复（L631-640），与既有 env 语义一致。

2. **数据源残留基本清零**
   `models/design_agent_client.py` 已删除；全仓 `DesignAgentClient`/`design_agent`/`VC_DESIGN_AGENT__` 仅剩文档、`config.py` 迁移逻辑与 `verify_h3_rewrite_toggle.py`（迁移提示测试）三处**预期**引用，代码/前端零悬空 import。

3. **知识资产来源锁定可信**
   `base-en.txt`、`ref-en.txt` 的 sha256 与 `references/VERSION` 清单逐字节吻合（实测 `2cfebc09…`/`1e574f35…`，行数 222/341）；`SKILL.md` 未作运行时加载——与设计一致。

4. **质量四件套实现完整**
   五级优先级分节（`_render_sections` L319-340 + `system_zh.txt` L25-35）、continuity 链（`_rewrite_segments` L538-539）、grounding 独立开关+路径/hash 缓存+失败保留模式回退 `text_only`（L269-315、L514-520）、有界重试（`_rewrite_one` L389-417，恰 1+2 次，确定退出）均落地；`_validate_h3_prompt`（L120-168）覆盖字段/顺序/时长/标签/抽象词/英文正文。

5. **验证脚本断言真实行为（接线层除外，见 C-1）**
   `verify_native_h3_rewriter.py` 精确断言调用次数（3a2/3b2/3c3）、缓存命中零 VLM 调用（5b2）、失败回退模式不变（5c2）、禁用态明确拒绝（7a）；`verify_stage_disable_migration.py` 真实演练"停点→禁用→continue→推进"死锁场景。

6. **前轮加固确实进代码**：C1/I1/I3/I4/M1/M2 均在 HEAD 有据可查（详见核实表）；`e676739` 声称的加固**确已进代码**而非仅文档。

---

## 二、问题（Issues）

### Critical（必须修复）

#### C-1　首尾帧 mode resolver 永不触发，两级契约（§5.2）在生产路径失效

> **状态（2026-09-30）：已修复 ✅** 接线层改用规范值 `start_end_frame` 并在入口 trim/lower 归一；金标脚本改用真实输入后四模式全 PASS（FL2VA 首次以生产输入通过）；`verify_native_h3_rewriter.py` 新增 5e/5f 接线级用例（含"非规范值不触发"互斥断言）。以下为原始发现，保留存档。

**位置**：`video-claw/video-claw/backend/core/agents/prompt_rewrite_agent.py#L495`（`_rewrite_segments`）

**问题**：判定尾帧的条件用了错误的字面量：

```python
# L495
if video_generation_mode == "start_end" and i < len(segments) - 1:
    next_selected = ...(segments[i + 1] ... selected)
```

全系统 `video_generation_mode` 的规范取值是 **`"start_end_frame"`**，不是 `"start_end"`（三处均已独立核实）：
- 前端枚举 `frontend/config/models.ts#L14`：`'first_frame' | 'start_end_frame' | 'reference'`；
- 视频阶段 `core/agents/video_agent.py#L162`、API `api/routers/workflow.py#L27` 均用 `"start_end_frame"`；
- 会话 meta 原样透传（`_merge_session_params`），无任何 `start_end_frame→start_end` 归一。

因此 L495 恒为 `False` → `next_selected` 恒空 → `has_last_frame` 恒 `False` → **`resolve_h3_mode` 在真实运行中永远不会产出 `FL2VA`/`L2VA`**（首尾帧分镜一律退化为 `I2VA`），`_grounding_images` 的 FL2VA 尾帧分支（L308-314）也随之成为死代码。

**为什么重要**：§5.2 两级模式契约是 proposal 明文的"实现强约束，不得简化或省略"。首尾帧（FL2VA）是 H3 主力模式之一：改写层把它误判为 I2VA 后，英文提示词只描述首帧、不书写首→尾帧过渡与尾帧对齐，而传输层仍按 `fl2va`+尾帧下发（`_task_for_input` 只认 `image_path or last_image_path`），产生语义错配——**不报错、不崩溃，是静默降级**，直接降低成片质量。

**测试为何没抓到（false green，已核实）**：
- `tmp_verify/h3_golden.py#L49` FL2VA 金标用例自身把 `vgm` 写成 `"start_end"`（L87 期望值同样用 `vgm=="start_end"`）——"金标四模式硬门槛"验证的是一个**生产环境不存在的输入值**，自洽通过；`verification-report.md` 的"四模式全部 PASS"结论因此不成立；
- `verify_native_h3_rewriter.py` 1c/1d 直接对纯函数 `resolve_h3_mode` 传 `has_last_frame=True`，绕开了 `_rewrite_segments` 的接线，同样掩盖。

**修复**：
```python
if str(video_generation_mode).strip().lower() == "start_end_frame" and i < len(segments) - 1:
    ...
```
同时：`h3_golden.py` 的 FL2VA 用例改用真实 `"start_end_frame"`；新增一条走 `process()`/`_rewrite_segments` 的端到端用例，断言首尾帧会话产物 `input_mode=="FL2VA"`，堵住"纯函数绿、接线红"的盲区。

---

### Important（应当修复）

#### I-A　沙盒 `image`/`reference_videos` 未走路径白名单——C1 加固只堵了音频一支

> **状态（2026-09-30）：部分修复 ⚠️ → 2026-10-07 已全部修复 ✅** 本轮（`8d02e47`）新增 `_guard_local_media`（复用共享 helper `resolve_within_allowed_dirs`），但**只接在 `/api/sandbox/video`**；同一文件的 i2i（`req.image`）与 vlm（`req.images`）仍是原样透传，该缺口在第三轮评审登记为 I-1，并另发现本地路径守卫误拒了远程 URL 输入（第三轮 I-2）。两者已于 2026-10-07 收口：守卫改名 `_guard_media_input`（+ 列表版 `_guard_media_inputs`），video/i2i/vlm 三端统一套用，并对 `http(s)/data:/file://oss://` 远程形态原样透传；`verify_sandbox_path_guard.py` 由 17 项扩至 32 项 ALL PASS（新增 15 项）。详见 `docs/code-review-3ac13de-to-eaa012a.md`。以下为原始发现，保留存档。

**位置**：`video-claw/video-claw/backend/api/routers/sandbox.py#L475、L485`；消费端 `models/custom_video.py#L600、L721`

**问题**：C1 修复仅将 `resolve_within_allowed_dirs` 接在 `audio_url` 解析上（`sandbox.py` 全文唯一调用在 L197 的 `_resolve_media_reference_url` 内）。同一 handler 的 `req.image`（L475 `image_path=req.image`）与 `req.reference_videos`（L485）被**原样**传入 `VideoClient.generate_video`，适配层直接 `open(path, "rb")` 并 multipart 上传到所配置的远端 `base_url`（`custom_video.py` L599-603、L720-723）。

本服务零鉴权：调用方可将 `image`/`reference_videos` 填成任意绝对路径（`/etc/passwd`、`~/.ssh/*`、他人会话文件），内容会被读取并**发送到外部推理服务**——比 C1 原缺陷（复制到静态目录）更进一步的"读取+外传"。`441d18c` 提交信息"杜绝零鉴权下的任意文件读取"的目标只覆盖了 1/3 入参。`reference_videos` 正是 `fd5c486` 本区间新增入参。

**修复**：对 sandbox 全部媒体参考入参（`image`、`last_image` 若有、`audio_url`、`reference_videos`）统一套用 `resolve_within_allowed_dirs`（`files.py#L46-L56`），越界即 400 明确拒绝；建议抽一个"媒体入参守卫"入口 helper 收敛所有支路，杜绝逐次遗漏（与工作流侧、`files.py` 上传接口共用同一份）。

---

### Minor（可以更好）

#### M-a　`backend/docs/api.md` 残留"外部改写会话"表述
`api.md#L344-L346`：「复用外部改写会话」「向外部会话追加一轮修订」是 design_agent 时代残留，与同文件 L342「本地 LLM 调用」自相矛盾。属数据源替换的文档残留，应更新措辞。

#### M-b　`.env.example` 遗留旧变量注释
`ENABLE` 行已改 `VC_H3_REWRITE__`，但下方两行仍注释 `VC_DESIGN_AGENT__BASE_URL`/`VC_DESIGN_AGENT__LOGIN_NAME`，与新语义不一致（无功能影响）。

#### M-c　`system_zh.txt` 未按设计字面"内联两份指南全文"
`system_zh.txt`（39 行）仅提炼原则并"引用"指南，指南实际由 `_query_llm`（L375-380）按模式单独附带。功能上更优（只注入相关指南），但与设计 D1/tasks 1.2 的字面表述偏离——请确认有意，并在决策文档补一句实现取舍。

#### M-d　VLM 未配置时 grounding 静默降级；`enable` 不从旧段继承
`prompt_rewrite_agent.py#L288-L300`：VLM 缺省/异常被 `except` 吞并回退 `text_only`（符合 D5"正交回退"），但每分镜仅一条 warning，用户可能长期无感知——建议文档/设置页明示"未配置 VLM 时改写自动降级为纯文本"。另：`h3_rewrite.enable=true` 不会从旧 `design_agent.enable` 继承（默认回 false，仅一次性告警），存量启用户升级后该阶段被静默停用；鉴于外部数据源已死，此取舍合理，但请确认预期。

#### M-e　`temperature` 字段入库但未透传
`config.py#L95-L96` 已注明"预留"，`_query_llm` 未传 `temperature`。建议设置页/文档保持"未生效"标注一致，避免错觉。

#### M-f　`5a0052a` 断言动态化后普遍变弱（非假绿）
`verify_models_filter.py`/`verify_prune.py` 由"精确集合相等"改为"非空且 ⊆ custom_ids"——仍能拦截内置模型泄漏，但不再断言具体可用模型集。端口自适应取 `BACKEND_PORT` 环境变量，端口不符时表现为连接失败（假红而非假绿），可接受。

---

## 三、前轮未修项核实表（基于 HEAD `3ac13de`，均已抽查代码证据）

| 条目 | 当前状态 | 证据 |
|---|---|---|
| **C1** 沙盒路径白名单 | **已修且未回归**（覆盖面不全 → 新问题 I-A） | `sandbox.py` L170-208；`files.py` L46-56（realpath + CODE_DIR/TEMP_DIR 前缀校验，防 `..`/软链） |
| **I1** 停用 prompt_rewrite 存量会话死锁 | **已修（真进代码，非仅文档）** | `orchestrator.py` `_get_next_stage` L495-513（ValueError→`_FULL_STAGE_ORDER` 回退）+ L735-739（禁用阶段状态冻结）+ `continue_workflow` L1244-1255；`verify_stage_disable_migration.py` L37-58 端到端 |
| **I2** `_ensure_session` client 泄漏 | **因删除而消失，本地路径无同类泄漏** | `design_agent_client.py` 已删；`llm_client.py` L131-137 自定义模型每调用 `finally: custom_client.close()`；`PromptRewriteAgent` 每执行新建（`orchestrator.py` L964），`_grounding_cache/_llm/_vlm` 随实例回收 |
| **I3** 多能力 AND 过滤 | **已修且未回归** | `config_model.py` L965/L970 `issubset`；`pipelines.py` `_split_abilities` L28-36 |
| **I4** `versions` 共享引用 | **已修** | `prompt_rewrite_agent.py` `_make_item` L438 `list((prev_item or {}).get("versions") or [])` 显式浅拷贝；`_append_version` L447-452 仅追加不原地改元素 |
| **M1** `fetchEnabledStages` 负缓存 | **已修** | `TopBar.tsx` L34-56：`negativeCacheUntil` + `NEGATIVE_CACHE_TTL_MS=60_000`，`!resp.ok` 与 `catch` 两分支均写负缓存 |
| **M2** sync 直出标识 | **已修且未回归** | `custom_video.py` L181-188 成功返回 `sync://<sha1[:16]>`，`except OSError` 退 basename |
| **M3** `.env.example` 结尾换行 | 原"部分成立"项，`.env.example` 无换行问题仍在（纯整洁性） | — |

---

## 四、建议（Recommendations）

1. **先修 C-1 并补接线级测试**：本轮唯一改变对外行为契约的缺陷。金标脚本改用真实 `"start_end_frame"` 之外，务必加一条走 `process()` 的 FL2VA 端到端断言——"金标四模式硬门槛"应验证生产输入空间，而非与实现自洽的构造值。
2. **统一媒体参考守卫**：`resolve_within_allowed_dirs` 收敛为对所有 sandbox 媒体入参（image/last_image/audio/videos）生效的入口校验，消除 C1 覆盖面不一致（I-A）。
3. **清理文档/示例残留**（M-a/M-b），避免"数据源已内嵌"与"外部会话"表述并存误导后来者。
4. **确认三处实现取舍并回写决策文档**（M-c/M-d/M-e）：指南按需附带、VLM 缺省静默降级、temperature 预留——若均为有意，一句话注明即可。
5. **验证脚本分层经验**：纯函数单测 + 端到端接线断言缺一不可；后续 change 的验收报告（如 `verification-report.md`）应标注每条硬门槛走的是哪一层，假绿即可定位。

---

## 五、结论（Assessment）

**是否可以合并/已可发布？** ~~否——修复后可~~ → **2026-09-30 更新：C-1 与 I-A 已修复并验证，可发布**（M-a~M-f 为文档/措辞/测试精度类，不阻塞，方案另见 `docs/minor-plan-07b4bdb-to-3ac13de.md`）。

**理由**：原生改写器的架构迁移、配置治理、质量四件套与 I1/I2/I3/I4/M1/M2 承接都扎实且可验证；但 **C-1**（首尾帧 mode resolver 因错误字面量全程失效、且被金标测试以同样的错误输入掩盖）破坏了 §5.2 强约束契约的实际可用性，**I-A** 把 C1 已确立的任意文件读取防护漏在了同批新增的 `reference_videos`/`image` 入参上。二者修复合入、并补端到端 FL2VA 用例后，即可放行；Minor 项可随后跟进。

*评审方式说明：区间文件多、`3ac13de` 尤重，按"规格→改写器主体→编排/前端接线→配置/资产→验证脚本→C1/I3 回归"分轮自审完成；后端判断基于源码通读与既有验证脚本静态核对（未重建镜像），新发现与核实表条目已由报告撰写者独立复核源码证据。*
