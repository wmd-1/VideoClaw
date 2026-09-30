# 决策：H3 提示词改写内嵌化——采用「原生 Skill 资产内嵌」方案

> 日期：2026-09-28（初审）/ 2026-09-28（质量复审 + 外部评审意见吸收后修订）/ 2026-09-29（二轮评审修订）
> **代码基线声明**：本文档基于**本地开发工作树**（已合入 `prompt_rewrite` 阶段的 commit `cdea2a1`，28 files / +2081 行）。GitHub upstream `HITsz-TMG/VideoClaw` 主分支当前**不含** `prompt_rewrite_agent.py` 与阶段注入逻辑——上游复核时请以本地分支为准，后续合入上游后本声明作废。
> **阶段数约定（全文统一）**：VideoClaw 基础工作流为**六阶段**；`prompt_rewrite` 是**可选注入阶段**，启用（`h3_rewrite.enable=true`）后形成**七阶段**。后文所有"六阶段/七阶段"均按此口径。
>
> 输入：三份 handoff 文档
> - Handoff A（外部平台交接）：`openspec/changes/embed-openharness-h3-prompt-writer/handoff-from-design-agent-platform.md` —— Design_Agent_Platform 已剥离该能力，建议 VideoClaw 内嵌 OpenHarness（`oh -p`）自建
> - Handoff B（原生集成）：`docs/Handoff 1：VideoClaw + H3 Skill + H3 Adapter.md` —— 不引入 OpenHarness / LangGraph，H3 Skill + Adapter 原生集成
> - Handoff C（LangGraph 编排）：`docs/Handoff 2：VideoClaw + LangGraph.md` —— 用 LangGraph 接管核心工作流编排
>
> 术语约定：本文的「Skill 内嵌」指 **H3 Prompt 知识资产（SKILL.md + references 指南）内嵌进 `prompts/` 模板体系**，由 prompt loader 静态加载、单次 LLM 调用消费；**不是**实现通用 Skill Runtime、也不在运行时动态加载技能。官方 H3 Skill 本身是纯 Markdown + reference files 的可移植资产（无外部 API / 专有 runtime 依赖），这是本方案成立的前提。

---

## 0. 结论（TL;DR）

**采用 Handoff B（原生 H3 Skill + H3 Adapter）的架构原则，继承 Handoff A 的知识资产（但不用 OpenHarness 运行时），不采用 LangGraph。**

核心判断（工程化表述）：

> H3 提示词改写是**封闭的结构化任务**：输入模式可由 VideoClaw 既有业务状态（`video_generation_mode` + 素材角色）确定，参考指南可静态选择，输出结构固定，且现有工作流已具备停点、恢复与持久化。因此当前引入 OpenHarness 或 LangGraph 不会解决本阶段的核心问题，反而会增加运行时、依赖和回归成本。（通俗版：为 652 行提示词知识支付一个运行时的价格，不值。）

**执行成本口径（全文统一）**：每个分镜以**一次 LLM 调用为正常路径**；结构校验失败时**最多追加 2 次带违规清单的修订调用**（§5.6 D）——这不是 Agent Loop（无自主工具循环、迭代次数有界、退出条件确定），但严格说也不是"只有一次调用"。

**质量优先修订（复审后追加，见 §5.6）**：架构选型不变，但实施计划强化四处——跨分镜上下文链、角色知识注入、VLM 参考图 grounding 提前到 Phase 1、校验器与金标集全面加码。其中第一处修正了一个原方案的真实质量回退（无状态逐分镜调用导致的主角/风格不一致），第三处把原生方案相对原平台的最大质量超越点从 Phase 2 提前到 Phase 1。

**外部评审吸收（修订二）**：按独立评审意见修正 9 处——LangGraph checkpoint 对比表述去夸大（§4.3）、模式映射改为 resolver（§5.2）、grounding 独立开关 + 缓存（§5.6 C）、continuity 明确为辅助信息非事实真源（§5.6 A）、LLM-judge 降为回归指标不作唯一硬门槛（§8）、术语统一（本文档头）、资产来源锁定（§5.3）、删除前引用审计（§5.3）、基线声明（文档头）。

---

## 1. 决策背景：为什么现在必须选

三份 handoff 的出现有一个共同前提——**外部数据源已经死了**：

```bash
# 2026-09-28 实测
curl -X POST http://localhost:8848/design_agent/v1/sessions \
  -d '{"session_type": "minimax-h3-prompt-writing"}'
# → 422 "session_type 'minimax-h3-prompt-writing' 已废弃：该能力已剥离，请调用方自建"
```

我们两周前提交的 `prompt_rewrite` 阶段（commit `cdea2a1`，28 files / +2081 行）中，`DesignAgentClient` 走的正是这条链路。该阶段的产品形态（停点、条目、重生成、修订、版本记录、前端、文档）全部有效且已验证，**只有数据源一层需要替换**。因此无论选哪个方案，都是"换引擎不换车身"——方案之间的差异就集中在"换什么引擎、付多大代价"。

---

## 2. 现状盘点：VideoClaw 已经有什么

决策的最大权重来自现状。当前代码库已具备：

| 能力 | 位置 | 与三方案的关系 |
|---|---|---|
| 完整工作流引擎（阶段状态机、停点/干预/续跑、跨阶段同步、会话持久化、重启恢复、后台单条重生成） | `video-claw/video-claw/backend/core/orchestrator.py` | LangGraph 接管的对象已存在且久经测试；现有引擎已具备**阶段级**状态持久化、停点与恢复——当前需求不需要 LangGraph 的 execution checkpoint（两者的概念差异见 §4.3） |
| 多 provider LLM 客户端 | `models/llm_client.py`（DashScope/DeepSeek/OpenAI/Gemini + 自定义 openai 兼容） | 原生 Skill 方案的执行层，**零新增依赖** |
| 提示词模板体系 | `prompts/`（按阶段分类）+ `loader.py`（`_zh`/`_en` 回退） | H3 Skill 的 references 就该住在这里 |
| **MiniMax-H3 视频生成适配已完成** | `models/custom_video.py`（t2va/fl2va/ref2va 任务推断、vllm-omni/sglang 双协议、超时与尽力取消） | **Handoff B 里的 "H3 Adapter" 已经建好了** |
| `prompt_rewrite` 阶段全套产品形态 | `core/agents/prompt_rewrite_agent.py`、前端 `PromptRewriteStage.tsx`、文档、`.env` 开关 | 只需把 `DesignAgentClient`（外部 HTTP）换成本地调用 |
| 失败重试的成熟模式 | `core/agents/doctor_agent.py`（模板+LLM+校验+有限重试） | 原生改写调用的工程范式样板 |

**关键事实**：Handoff B 的两大交付物中，H3 Adapter 已存在；H3 Skill 的本质是 652 行提示词知识（SKILL.md 40 行 + base-en.txt 222 行 + ref-en.txt 341 行 + system-prompt.py 45 行，见 `openspec/changes/embed-openharness-h3-prompt-writer/assets/`）。

---

## 3. 三方案对比

| 维度 | A：内嵌 OpenHarness（`oh -p`） | B：原生 Skill + Adapter | C：LangGraph 编排 |
|---|---|---|---|
| 新增运行时依赖 | openharness-ai（重，含 agent 循环/工具系统） | **无** | langgraph + checkpointer |
| Docker 镜像影响 | **需改 Dockerfile.backend 并重建**（依赖文件不挂载，`uv.lock` 变更即重建） | 无（依赖不变，源码热挂载生效） | 需改依赖并重建 |
| 单次改写的执行成本 | 一次完整 agent 进程：启动 + 技能发现 + 读 SKILL.md + 读 references + 多轮工具循环 + LLM | **正常路径 1 次 LLM 调用，校验失败最多 +2 次修订调用**（有界、确定退出） | 一次 graph 节点执行（内部仍是 LLM 调用） |
| 改动面（相对已提交的 prompt_rewrite 阶段） | 换数据源 + 新增 wrapper 服务 + 镜像重建 | **只换数据源**（`DesignAgentClient` → 本地 LLM 调用） | 重写整个主流程编排层，全阶段回归 |
| 工程陷阱 | 源码级核实的坑：json 模式错误静默、`-p` 权限自动放行需工具黑名单、max_turns 默认 200 需收紧、TUI npm 联网卡死（Handoff A §2 列了 9 条） | 提示词移植保真度（可控，见 §6 风险） | 双状态源、事件翻译、checkpoint 持久化——每个都是新子系统 |
| 与现有架构契合度 | 引入第二套 agent 体系，与现有 Agent/Prompt/模型层平行 | **完全落入现有分层**（Skill=prompts/，Adapter=models/，时机=orchestrator） | 替换现有引擎，重复建设停点/持久化/SSE |
| 主要收益 | 技能文件原样运行（含 agent 自主读文档的循环） | 最小代价、最快路径、解除外部耦合 | 长期编排能力（QA 自动返工、并行 Shot、时间旅行） |
| 收益的真实性 | agent 循环对本任务是**负资产**（见 §4.1） | — | QA 返工等能力在现有引擎上加一个阶段即可实现，不需要换引擎 |

## 4. 逐方案分析

### 4.1 方案 A（内嵌 OpenHarness）：为 652 行知识引入一个框架，不划算

Handoff A 的价值极高——它做了源码级核实（`oh -p` 的输出契约、错误静默、权限放行、技能发现路径），且资产交接完整。**但它推荐的运行时是错的**：

1. **任务形态不匹配**。`oh -p` 的设计场景是"需要 agent 自主决定读哪些文档、调哪些工具"的开放任务。而 H3 改写是**封闭任务**：输入模式可预先确定（见 §5.2），要读的 reference 文档随之确定，输出结构固定。agent 循环在这里只贡献不确定性（多轮工具调用的耗时与失败面），不贡献正确性。
2. **每分镜一个进程**。逐分镜串行改写 N 个 segment = N 次进程冷启动 + N 次技能发现 + N 次文件读取循环。现有 `LLM().query` 单调用路径与其相比，延迟和方差都低一个量级。
3. **镜像纪律冲突**。本项目 compose 只运行本地镜像、依赖文件不挂载（README「构建与代码更新」明确：改依赖需重建镜像）。为一个纯文本变换重建后端镜像，运维代价与收益严重不成比例。
4. Handoff A §2 自己列出的 9 条注意事项（错误静默、权限放行、max_turns……）本质上是**运行时在和调用方搏斗**——这是方案不合适的信号。

### 4.2 方案 B（原生集成）：改动最小、完全落入现有分层

Handoff B 的核心原则完全正确，且与现状惊人地契合：

> Skill 负责"怎么写 H3 Prompt"，Adapter 负责"怎么调用 H3"，现有 Pipeline 负责"什么时候调用"。

映射到本仓库就是：

- **Skill** → `prompts/prompt_rewrite/`（references 原样搬入 + 提炼后的系统提示词模板）
- **Adapter** → `models/custom_video.py`（MiniMax-H3 适配**已完成**，无需新建 `H3Adapter` 类——Handoff B 撰写时可能未意识到该适配层已存在）
- **时机** → `orchestrator.py` + `prompt_rewrite_agent.py`（**已完成**）

需要做的只剩一件事：把 `prompt_rewrite_agent.py` 的数据源从外部 sessions API 换成本地 LLM 调用。这正是我们刚验证过的工程范式（`doctor_agent.py` 的模板+校验+有限重试）。

### 4.3 方案 C（LangGraph）：当前需求不需要它，替换现有引擎的回归风险不可接受

**先说严谨的部分**：LangGraph checkpoint 与 VideoClaw session JSON **不是等价物**。前者是"graph execution 的执行位置状态"（当前走到哪个节点、当时的 state 快照、从哪条边继续），后者是"业务阶段产物与状态的持久化"。VideoClaw 现有引擎是一个**线性阶段状态机**（基础六阶段 `script → character → storyboard → reference → video → post`，可选注入 `prompt_rewrite` 后为七阶段），不存在任意拓扑的执行图，因此也就不存在需要 execution checkpoint 来表达的东西。

正确的论证不是"我们已经有等价物"，而是：

> **当前需求（阶段推进、停点确认、干预修改、服务重启后恢复会话）在现有引擎的能力范围内，不需要 LangGraph 提供的额外能力。**

现有引擎与 LangGraph 概念的功能对应（注意：是功能覆盖，非概念等价）：

| LangGraph 概念 | VideoClaw 现有功能覆盖 |
|---|---|
| `interrupt()` / resume | waiting 停点 + `intervene` / `continue` 接口 |
| 状态持久化与恢复 | session JSON 落盘（`code/data/sessions/`），服务启动时从磁盘恢复 |
| Graph State | `WorkflowState` + `artifacts[stage]`（业务状态） |
| 事件流 | SSE progress / asset_complete 事件体系 |

LangGraph 的真实优势在**未来场景**：非线性 Graph、复杂返工子图、跨节点任意恢复（时间旅行）。这些场景一旦出现，现有"阶段列表 + 停点"模型确实会表达吃力——届时再引入（触发条件见 §7）。

替换现有引擎意味着**整个主流程全量回归**——这是本项目最高价值的资产，而当下换来的新能力没有一项必须在引擎层实现：

- QA 返工 = 在现有引擎上加一个可选阶段（和 `prompt_rewrite` 完全同构，我们刚证明这条路两周能走完）；
- 并行分镜 = `video_agent.py` 已有 `ThreadPoolExecutor` 并发 + 模型注册表并发控制；
- 阶段回退与历史产物恢复 = session JSON 按阶段累积产物、`_sync_artifacts_cross_stages` 已处理上游变更后的产物重同步——**但这不等同于 LangGraph 的 execution time travel / replay**（后者可从任意执行点重放），当前需求同样不依赖后者。

Handoff C 自己写了 17 条"不要"（§16），这是范围失控风险的自我供认。**结论：不采用，但保留为远期演进选项**（触发条件见 §7）。

---

## 5. 选定方案：原生 H3 Skill 资产内嵌（含实施要点）

### 5.1 架构

```
                        VideoClaw
                            │
                     WorkflowEngine（阶段 / 停点 / 恢复）
                            │
                     prompt_rewrite 阶段（已存在，换数据源）
                            │
            ┌───────────────┼────────────────┐
            │               │                │
            ▼               ▼                ▼
   character/scene      continuity       VLM grounding
   权威事实注入          上下文链          （可选，独立开关）
   （B，事实真源）      （A，辅助信息）    （C，看图不写词）
            │               │                │
            └───────────────┴────────────────┘
                            ▼
                  H3 Prompt Compiler
                  （正常路径 1 次 LLM 调用，低 temperature；
                    校验失败最多 +2 次带违规清单的修订调用）
                            │
                     ┌──────┴───────┐
                     │ H3 Validator │（D：字段级硬校验）
                     └──────┬───────┘
                            │ fail → 带违规清单回喂，重试 ≤ 2
                            ▼
                     H3 最终提示词
                     （产物 / 停点 / 重生成 / 修订 / 版本——不变）
                            ▼
                     video_agent → models/custom_video.py
                            ▼
                        MiniMax-H3
```

各组件职责边界：WorkflowEngine 只管阶段与停点；H3 Prompt 知识资产（`prompts/prompt_rewrite/references/`）提供官方规范；VLM 只负责"看图"产出客观描述、不写最终提示词；Validator 只负责硬规范；LLM 负责真正的改写；`custom_video.py` 负责模型调用。**知识资产由 prompt loader 静态加载（系统提示词 + 按模式选定的指南内联），无 Skill Runtime、无运行时动态发现。**

### 5.2 关键洞察：输入模式由 resolver 推导，不需要 agent 自判

原平台的 agent 需要自主判定 T2VA/I2VA/FL2VA/L2VA/Ref2VA（因为用户输入是自由格式）。**VideoClaw 的优势是它自己就掌握判定所需的事实**——但 `video_generation_mode` 是**默认映射依据而非绝对一一映射**，最终模式由 `video_generation_mode` + 输入素材角色共同决定（H3 官方语义：I2VA = 首帧作为具体目标帧；FL2VA = 首尾帧；Ref2VA = 需要持续跟踪多个 reference relationship 的全参考模式。"有参考图"并不天然等于 Ref2VA——一张图只是首帧时是 I2VA，多张人物/场景/道具参考且要求跨段持续引用时才是 Ref2VA）：

```
VideoClaw video_generation_mode（默认依据）
        + 输入素材角色（图像数量与用途：首帧/尾帧/人物场景参考集）
        ↓
   H3 mode resolver（prompt_rewrite_agent 内实现，纯函数可单测）
        ↓
T2VA / I2VA / FL2VA / L2VA / Ref2VA → 选定指南模板
```

默认映射表（resolver 的初始规则）：

| VideoClaw `video_generation_mode` | 素材角色（现状） | H3 模式 | 指南 |
|---|---|---|---|
| `first_frame`（首帧生视频，默认） | 单图 = 该分镜首帧 | I2VA | base-en.txt |
| `start_end`（首尾帧生视频） | 两图 = 首帧 + 尾帧 | FL2VA | base-en.txt |
| `reference`（参考图生视频） | 多图 = character_design 人物/场景参考集，跨分镜持续引用 | Ref2VA | ref-en.txt |
| （无图像输入的纯文本场景） | 无 | T2VA | base-en.txt |
| （尾帧单图，当前无此模式） | 单图 = 尾帧 | L2VA | base-en.txt |

resolver 化的收益：未来 H3 新增能力或 VideoClaw 素材角色变化（如首帧模式叠加参考集）时改 resolver 一处即可，不会把自己锁死在模式等式里；映射是纯函数，可独立单测；`input_mode` 从"事后标注"变为"事前事实"。这比原平台方案更强，且顺带解锁了原平台因素材通道未打通而不可用的 I2VA/FL2VA/Ref2VA 模式。

**两级模式契约（以当前代码为事实源核实，2026-09-29）**——"改写层模式"与"传输层 task"是两个粒度，不是同一词汇表：

- **改写层模式**（resolver 输出，决定读哪份指南、提示词怎么写）：T2VA / I2VA / FL2VA / L2VA / Ref2VA（H3 官方提示词语义）；
- **传输层 task**（`models/custom_video.py::_task_for_input` L333-344 实际输出，仅有三种）：`t2va` / `fl2va` / `ref2va`——**I2VA 与 L2VA 在 API task 层不存在**，均归并到 `fl2va`，由帧语义区分：vllm-omni 走 `extra_params.frame_indices`（首帧图单输入 → 无 frame_indices 即首帧；仅尾帧 → `[-1]`；首尾帧 → `[0, -1]`），sglang 走 `conditions` 的 `role: keyframe` + `frame_index: 0/-1`；带媒体参考集 → `ref2va`。

两层的一致性契约（e2e 断言内容，替代此前笼统的"模式一致"）：

| resolver 改写模式 | 图像输入组合 | `_task_for_input` 输出 | 帧语义参数 |
|---|---|---|---|
| T2VA | 无图 | `t2va` | — |
| I2VA | 首帧单图 | `fl2va` | 无 frame_indices / keyframe@0 |
| L2VA | 尾帧单图 | `fl2va` | `[-1]` / keyframe@-1 |
| FL2VA | 首帧+尾帧 | `fl2va` | `[0, -1]` / keyframe@0+@-1 |
| Ref2VA | 参考集（含图片/视频） | `ref2va` | — |

断言规则：对同一会话，resolver(改写模式) 的指南选择必须落在 `_task_for_input(同输入组合)` 所属的传输类（I2VA/L2VA/FL2VA → `fl2va`；Ref2VA → `ref2va`；T2VA → `t2va`）。OpenSpec 中此表作为 resolver 与 e2e 测试的共同契约引用。

### 5.3 改动清单（相对当前 HEAD）

| 动作 | 文件 | 说明 |
|---|---|---|
| 搬入 | `prompts/prompt_rewrite/references/{base-en,ref-en}.txt` | 从 handoff 资产原样拷入（逐字节保真，永久保存，不再依赖外部仓库） |
| 新增 | `prompts/prompt_rewrite/references/VERSION` | **资产来源锁定**：记录 `source: MiniMax-AI/MiniMax-H3`（经 Design_Agent_Platform 交接）、`skill: h3-prompt-writing`、来源 commit、`imported_at: 2026-09-28`、两份指南的内容 hash。官方 Skill 更新时可 diff 判断是否同步 |
| 新增 | `prompts/prompt_rewrite/system_zh.txt` | 从 `system-prompt.py` 的 `SYSTEM_PROMPT` 提炼：保留角色设定、技能工作流、输出规范（字段名/标签/时长 4~15s/禁抽象词/代码块输出）；**剔除**平台专属护栏（IDENTITY_GUARD、WORKSPACE_GUARD、离线容器声明、"读 /root/.openharness/..."的路径引用——改为直接内联所选指南全文）。**`SKILL.md` 不作为运行时文件直接加载**：其工作流语义与输出规范经提炼进入本文件；`references/` 两份指南则逐字节原样保留（即官方 652 行资产中，实际运行时消费的是 system_zh.txt 提炼版 + 两份指南原文） |
| 修改 | `core/agents/prompt_rewrite_agent.py` | `DesignAgentClient` 外部调用 → `self._cancellable_query(LLM(), ...)` 本地调用；模式 resolver（§5.2）；**串行执行 + 上下文链 + 角色注入（§5.6 A/B）**；VLM grounding（§5.6 C，受独立开关控制）；结构校验失败带错误回喂重试（§5.6 D）；删除外部会话复用/重建逻辑 |
| 新增 | `core/agents/prompt_rewrite_agent.py` 内校验器 + mode resolver | 字段级结构校验（§5.6 D，复用 doctor_agent 的"校验-回喂"范式）；resolver 为纯函数，独立单测 |
| 删除 | `models/design_agent_client.py` 及其验证脚本 | **删除前先做引用审计**：ripgrep `DesignAgentClient` / `design_agent` / `VC_DESIGN_AGENT__` / `minimax-h3-prompt-writing` 全仓（backend / frontend / tmp_verify / docs / .env.example / config），确认零残留后再删（含禁用态验证脚本 `verify_design_agent_client.py`、`verify_design_agent_config.py` 的适配） |
| 修改 | `config.py` / `config.yaml.example` / `.env.example` / 设置页 | `design_agent` 段替换为 `h3_rewrite` 段：`enable`（沿用现有开关语义与阶段注入逻辑）、`grounding_enable`（**VLM grounding 独立开关**，默认 true，`VC_H3_REWRITE__GROUNDING_ENABLE` 可覆盖）、`llm_model`（改写模型）、`vlm_model`（grounding 模型，缺省回退会话 vlm）、`temperature`（默认 0.3）；`VC_DESIGN_AGENT__*` 更名 `VC_H3_REWRITE__*`（启动日志一次性迁移提示） |
| 保留 | 引擎阶段注入、产物结构、前端组件、`api.md`/`session_format.md`/SKILL.md 文档主体 | 仅更新数据源与配置名相关表述 |

### 5.4 明确不做（对齐 Handoff B 的边界 + 本决策补充）

- 不引入 OpenHarness、不引入 LangGraph、不重构 orchestrator；
- 不新建 `H3Adapter` 类——`models/custom_video.py` 的 MiniMax-H3 适配已覆盖 Handoff B §4-§6 的全部要求（异步任务、模式映射、错误映射）；
- 不做 QA 自动返工阶段——独立后续变更，与本次数据源替换解耦。

### 5.5 分期

- **Phase 1（本次变更）**：数据源内嵌化 + 模式确定性映射 + **质量四件套（§5.6 A–D：上下文链 / 角色注入 / VLM grounding / 强化校验）**。完成即恢复并**超越**被外部剥离打断的能力（原平台多模态通道不通，VLM grounding 是其做不到的），且不再有任何跨项目运行时依赖。
- **Phase 2（可选后续）**：改写结果的自动化质量评分（LLM-judge 常驻入产物 `quality_score` 字段，低于阈值自动触发一次修订）。
- **Phase 3（可选后续）**：视频质量评估阶段（QA 返工），在现有引擎上加阶段实现，与 `prompt_rewrite` 同构。

### 5.6 质量保障机制（质量优先复审后新增，本节为实施强约束）

原生的单次 LLM 调用相对原平台会话式 agent 有一个真实回退和三个缺失，全部在此补偿：

**A. 跨分镜上下文链（补偿会话式改写的一致性优势；辅助信息，非事实真源）**

原平台在同一会话内改写多个分镜，主角外观 / 场景基调 / 风格词汇天然连续；无状态逐分镜调用会丢失这一点（分镜 5 的主角描述可能与分镜 1 矛盾）。补偿方式：

- 阶段内**保持串行**（现有实现即串行，不引入并发），第 N 个分镜的调用注入前 N-1 个已改写条目的**连续性摘要**：各条目的 `subject_definitions`（Ref2VA）或 `integrated_multimodal_description` 的主体句、以及整体风格基调句；
- 摘要由改写 agent 自身产出（每条目改写完成后追加提取一次主体与风格关键词，存入产物条目 `continuity` 字段），不额外调用 LLM；
- **上下文优先级（防错误沿链传播的硬约束）**——continuity 是"辅助一致性信息"，**不得覆盖权威事实**；系统提示词按以下优先级注入与声明：
  1. `character_design` / `settings` 权威事实（B 项注入）
  2. 当前分镜 `shots[].content` 的明确要求
  3. 用户明确修改意见（干预/修订指令）
  4. continuity 摘要（仅当 1-3 未覆盖时生效）
  5. LLM 自由补充（明确标注为推理，服从以上全部）
- 若某前序条目的主体描述与角色权威事实冲突，以权威事实为准并在当前条目忽略该条 continuity（前序错误不沿链放大）。

**B. 角色与场景知识注入（对齐 character_design 既定设定）**

- 改写输入注入 `character_design` 产物中**该分镜 `characters` 命中的角色外观描述**（`video_agent._build_character_section` 已有同源逻辑，复用其提取方式）与场景（settings）描述；
- 使 H3 提示词的 subject 描述与第二阶段确立的角色设计一致，而不是由改写模型凭空重述。

**C. VLM 参考图 grounding（I2VA / FL2VA / Ref2VA 模式；独立开关，从 Phase 2 提前）**

- `first_frame` / `start_end` / `reference` 模式下，把该分镜**用户已选中的参考图**（`reference_generation.scenes[].selected`，及首尾帧模式的下一分镜尾帧）交 VLM（`models/vlm_client.py`，模型取 `h3_rewrite.vlm_model`，缺省回退会话 vlm）生成一段客观画面描述，注入改写输入的"首帧/参考图实际内容"栏；
- **独立功能开关** `h3_rewrite.grounding_enable`（默认 true，`.env` 可覆盖）：grounding 与阶段启停解耦——关掉 grounding 后阶段仍可用纯文本模式运行，不会因为不想付 VLM 成本而失去整个改写能力；
- **grounding 结果缓存**：以参考图路径 + 文件内容 hash（或 asset 版本号）为 key 缓存 VLM 描述（会话级 dict 即可，产物条目同时落 `grounding` 字段含 `image_hash`），同图重生成提示词（单条重试 / 修订）不重复调 VLM；
- 这解决了纯文本改写的核心风险：用户换选了不同版本参考图、或参考图阶段 VLM 评估改写过 `visual_prompt` 后，分镜文本与图上实际内容脱节——H3 的首帧对齐指令要求提示词与图严格一致；
- VLM 不可用 / 失败 / 开关关闭时**回退为"无 grounding 的文本输入"——保留 resolver 已确定的 H3 模式不变，只是不注入视觉描述**，并在条目记录 `grounding: "text_only"`；**绝不因此把模式降级为 T2VA**（模式与 grounding 是正交维度），不阻塞改写；
- VLM 职责边界：只产出客观画面描述（"图里实际画了什么"），**不参与写 H3 提示词**——最终改写仍由 LLM 统一完成，保证输出结构与风格的一致性；
- 这是原平台明确做不到的（其多模态素材通道未打通，仅 T2VA 可用），是原生方案质量超越的主要来源。

**D. 结构校验器与有限重试（强化）**

- 校验规则（对齐两份指南的硬性规范）：必填字段存在且顺序正确（基础三字段 / Ref2VA 六段）；时长声明与目标一致且在 4~15s；Ref2VA 的参考标签（`<Picture 1>` 等）在全部段落一致且无未定义标签；禁抽象词黑名单（cinematic、beautiful、stunning 等，指南明令禁止）；正文语言为英文（对话/歌词/画面文字除外）；
- 失败处理：**带具体违规项回喂重试，最多 2 次**（对齐 doctor_agent 的 MAX_DOCTOR_ATTEMPTS 范式）；仍失败则条目 `failed` 并记录违规清单——绝不静默放行不合格输出；
- 模型参数：默认低 temperature（0.3 档，可配），`llm_model` 建议默认配置强模型（原平台实测使用 deepseek-v4-pro 档位），设置页可换。

---

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 提示词移植失真（system-prompt.py 有平台耦合） | references 两份指南**逐字节原样搬入**，不做任何改写；系统提示词只删不改（删的均为平台护栏）；金标回归用例直接继承 Handoff A §8 |
| **无状态调用导致跨分镜不一致（主角/场景/风格矛盾）** | §5.6 A 上下文链：串行 + 注入前序条目主体与风格摘要，系统提示词强制一致性约束 |
| **前序改写错误沿 continuity 链传播放大** | §5.6 A 五级优先级：character/scene 权威事实 > 分镜明确要求 > 用户修改 > continuity > LLM 自由补充；continuity 与权威事实冲突时以权威事实为准并忽略该条 |
| **分镜文本与实际参考图脱节**（用户换版本 / visual_prompt 被 VLM 改写） | §5.6 C VLM grounding（独立开关）：选中参考图交 VLM 生成客观描述注入改写输入；同图 hash 缓存避免重复调用；VLM 失败/关闭回退为无 grounding 的文本输入（**保留模式不变**）并落 `grounding` 标记 |
| 无 agent 自主循环后，模型不守规范（漏字段/加解释文字/抽象词） | §5.6 D 字段级校验器 + 带违规清单回喂重试 2 次；低 temperature；`_extract_prompt` 代码块提取兜底 |
| LLM 模型能力不足导致改写质量下降（原平台用 deepseek-v4-pro） | `h3_rewrite.llm_model` 可配且默认建议强模型档位；金标集 + judge 评分验收（§8） |
| 上下文链使后段分镜的输入逐条变长（token 增长） | 连续性摘要有上限（只保留主体句 + 风格关键词，非全文）；分镜数为个位数到十几，实测确认 token 量可控 |
| VLM grounding 增加阶段耗时 | VLM 描述调用按分镜一次、可与会话 vlm 复用；条目级并行仅限 VLM 描述（改写本体仍串行保一致性）；超时回退纯文本 |
| 模式映射与视频阶段实际传参不一致（改写层 vs 传输层两粒度） | 两级模式契约表（§5.2，已按 `custom_video.py::_task_for_input` 代码核实）：resolver 与 `_task_for_input` 读同一会话参数与图像输入组合，e2e 按 §5.2 契约表断言两层归类一致 |
| 旧 `.env` 的 `VC_DESIGN_AGENT__*` 静默失效 | 更名后在启动日志输出一次性迁移提示；设置页沿用"来自 .env"只读展示机制 |

---

## 7. 与另两份 handoff 的关系

- **从 Handoff A 继承**：全部知识资产（references、SKILL.md 工作流语义、SYSTEM_PROMPT 的输出规范部分）与 §8 验证清单；**不继承** OpenHarness 运行时与 wrapper 服务。资产目录 `openspec/changes/embed-openharness-h3-prompt-writer/assets/` 在实施时拷入 `prompts/` 后归档。
- **Handoff C 归档为远期选项**。重新评估 LangGraph 的触发条件（满足其一）：
  1. 工作流出现非线性拓扑（如逐 Shot 的 QA 返工子图、分支叙事），现有"阶段列表 + 停点"模型表达吃力；
  2. 需要跨进程/跨服务的长事务编排（当前单进程 + session JSON 已足够）；
  3. 现有引擎的干预/恢复语义出现无法向后兼容的扩展需求；
  4. 需要多个 Agent 节点之间的动态循环/分支协作，且现有"阶段 + hook + intervention"机制难以表达。
  在此之前，任何编排增强优先以"加阶段/加钩子"实现——`prompt_rewrite` 的完整落地（含开关注入、停点、条目重生成、跨阶段同步）已证明该路径的边际成本很低。

---

## 8. 验证清单（实施完成的验收标准）

继承 Handoff A §8 并按本方案与质量修订强化：

**金标集（multi-mode，全部入库 `tmp_verify/`，可重复执行）**

1. T2VA 金标：雨夜猫用例（继承自 Handoff A §5/§8），输出符合 base-en.txt 的 Final Prompt Structure（三字段、4~15s 时长一致、英文正文、无抽象词）；
2. I2VA / FL2VA / Ref2VA 金标各一：构造带选中参考图的会话，断言 `input_mode` 正确、VLM grounding 描述进入输入、首帧/参考对齐指令存在；
3. **LLM-judge 评分脚本**：对金标集输出按四维评分（结构合规 / 具体性-无抽象词 / 时长一致性 / 标签一致性，Ref2VA 用例含标签维）；**作为质量回归基线与诊断工具**随变更交付（分层定位见文末"质量验收口径"，不作唯一硬门槛）；
4. **跨分镜一致性用例**：3+ 分镜同一主角，断言各条目主体描述关键词一致（连续性链生效）。

**功能与回归**

5. 模式映射（两级契约）：`first_frame`/`start_end`/`reference` 三种会话配置下，resolver 的改写模式选择与 `custom_video.py::_task_for_input` 的传输 task 按 §5.2 契约表一致（I2VA/L2VA/FL2VA → `fl2va` + 对应帧语义；Ref2VA → `ref2va`；T2VA → `t2va`）；
6. 校验-重试路径：构造漏字段 / 抽象词 / 标签不一致输出，断言带违规清单回喂重试至多 2 次后成功或条目 failed 带违规清单；
7. VLM 回退路径：VLM 不可用时条目 `grounding: "text_only"`、改写不阻塞；
8. 开关回归：`VC_H3_REWRITE__ENABLE=false`（默认）时 `/api/stages` 返回基础六阶段（不含可选注入的 `prompt_rewrite`）、引擎阶段序列不含该阶段、前端无该 Tab；启用后形成七阶段（沿用既有 `verify_prompt_rewrite_stage.py` 断言集）；
9. 端到端：改写 → 停点确认 → MiniMax-H3 视频生成（`custom_video.py`）全链路；
10. 禁用态既有回归脚本（`verify_config_layer.py` 等 4 个）ALL PASS。

**质量验收口径（两层门槛，避免 judge 波动阻塞交付）**：

- **硬门槛（release gate，必须全过才算完成）**：deterministic validator 全部 PASS（schema 字段与顺序 / 时长一致 / 标签一致 / 模式一致 / 无缺失字段 / 无抽象词黑名单命中）——即上面第 1、2、5、6 项。这是确定性校验，不受模型或 judge 波动影响；
- **软门槛（质量回归指标 / 诊断，不阻塞交付但阻塞"质量无回退"结论）**：LLM-judge 四维评分（第 3 项）作为回归基线记录与趋势监控；换模型 / 模型版本升级时若 judge 分显著下降（如任一维降 ≥ 1 分），触发人工复核而非自动 fail——judge 本身有波动，不能让它成为唯一硬门槛；
- 判定为"质量回退"的 judge 差异必须逐条归因（模型档位 / 提示词 / 校验规则）并给出处理决定，不允许静默接受。

---

## 9. 后续步骤

1. 以本文档为依据新建 OpenSpec 变更（建议名：`native-h3-prompt-rewriter`），proposal 引用本文档结论（含本文全部修订）；
2. 实施 §5.3 改动清单（预计改动量 600~800 行，含校验器 / resolver / grounding / 金标集与 judge 脚本；仍远小于引入 OpenHarness 或迁移 LangGraph 的代价）；
3. 归档 `embed-openharness-h3-prompt-writer`（资产已吸收进 `prompts/`，VERSION 清单留档）与 `minimax-h3-prompt-rewrite-stage`（产品形态已落地、数据源被本次变更替换）。
