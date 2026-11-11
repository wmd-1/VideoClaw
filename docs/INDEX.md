# docs 文档索引

> 本目录按**文档性质**分类。工程活文档与展示向 README 保留在 `docs/` 根（被根 README 与多处 openspec 任务直接引用，路径不变）；其余文档归入子目录。

## 目录结构

| 位置 | 性质 | 说明 |
|---|---|---|
| `README.md` / `README_EN.md` | 展示向 | 作品集、演示视频、营销向介绍（中/英），由根 `README.md` 链接 |
| `WORKFLOW.md` | 工程活文档 | 主流程链路详解：阶段处理流程、产物结构、用户介入点、失败兜底 |
| `reviews/` | 评审记录 | 提交区间代码评审报告、Minor 处置方案与专题审查；区间评审按轮次递增，专题审查独立编号 |
| `handoffs/` | 跨会话交接 | 事实澄清、缺陷根因、容器内复现命令与交接边界 |
| `decisions/` | 方案决策 | 定稿快照，保留历史原貌，后续修订以追加说明方式处理 |
| `reference/` | 外部与运维参考 | 模型服务手册、迁移机配置模板、打包迁移 tips |
| `designs/` | 方案设计 | 面向未实施需求的方案提案与设计取舍；实施前需转 OpenSpec 四工件，定稿快照再入 `decisions/` |

## 一、reviews/（评审记录）

| 文件 | 轮次 | 评审区间 | 评审日期 |
|---|---|---|---|
| `code-review-6ba2f19-to-cdea2a1.md` | 第一轮 | `08502d5..cdea2a1` | 2026-09-27 |
| `code-review-07b4bdb-to-3ac13de.md` | 第二轮 | `682841e..3ac13de` | 2026-09-30 |
| `code-review-3ac13de-to-eaa012a.md` | 第三轮 | `3ac13de..eaa012a` | 2026-10-07 |
| `code-review-eaa012a-to-9bb51fe.md` | 第四轮 | `eaa012a..9bb51fe`（18 个提交） | 2026-10-12 |
| `code-review-9bb51fe-to-2f720f8.md` | 第五轮 | `9bb51fe..2f720f8`（23 个提交，即 `452e96d` 及之后） | 2026-10-19 |
| `minor-plan-07b4bdb-to-3ac13de.md` | 配套方案 | Minor 项 M-a~M-f 的处置方案（源自第二轮） | 2026-09-30 |

约定：提交区间报告命名 `code-review-<起始短SHA>-to-<结束短SHA>.md`；区间端点取“上轮报告终点 → 本轮 HEAD”，因此新一轮的第一个提交就是上轮终点的下一个提交，两轮不重叠也不漏审。问题编号（C1/I1–I4/M1–M3 → C-1/I-A/M-a → I-1/M-1 → C-1/I-1/M-1）跨轮沿用同名编号的含义，新一轮新发现用本轮前缀并在头部声明“引用上轮问题时标注‘上轮’”；每份报告头部保留「修复更新」段，并含「上轮未修项核实表」。

### 专题审查（不接续提交区间轮次）

专题报告按主题与日期命名，记录当前源码基线，不占用上述区间评审轮次；未实施的配套优化计划归入 `designs/`。

| 文件 | 范围／基线 | 状态 | 日期 |
|---|---|---|---|
| [工作流与Prompts审查报告-2026-10-29.md](reviews/工作流与Prompts审查报告-2026-10-29.md) | 七阶段工作流、63 份提示词模板及实际消费链路；源码基线 `123f62a` | 静态审查，问题待处理，未运行模型／验收；[配套优化计划](designs/工作流与Prompts优化计划-2026-10-29.md) | 2026-10-29 |

## 二、handoffs/（跨会话交接）

| 文件 | 交接内容 |
|---|---|
| `Handoff 1：VideoClaw + H3 Skill + H3 Adapter.md` | MiniMax-H3 以原生 Skill + Adapter 接入 VideoClaw（不引入 OpenHarness / LangGraph） |
| `Handoff 2：VideoClaw + LangGraph.md` | 用 LangGraph 接管核心创作流程的 Agent/Workflow 编排（备选路线） |
| `Handoff 3：H3 提示词改写阶段 - 待办与验证缺口.md` | 改写阶段的待办事项与验证缺口事实交接（含容器内复现方式） |
| `Handoff 4：H3 原生改写器 - 评审加固承接（I1·I4·M1）.md` | 第一轮评审加固项中仍适用于原生实现的部分 |
| `Handoff 5：前端构建端口硬编码故障与修复.md` | 构建期端口硬编码致 API 500 故障的根因、修复与治理（含复现命令、免网络重建配方、验收标准、交接边界与 655c519 留痕更正） |
| `Handoff 6：SLB 统一前缀接入与前端文案·入口开关改造.md` | SLB 前缀 `/Bocom_Video_Generation` 接入（Next basePath）、Pipeline 入口开关/文案更名/隐藏联网搜索/首页交行示例、身份门禁（先取消后恢复）与身份接口前缀、媒体地址前缀修复与自检脚本 `scripts/selfcheck-deploy.sh`；§9 为 2026-10-28 增量与后端会话交接要点 |

区分：Handoff 用于缺陷交接与跨会话续作；需求/方案层面的正式提案走 openspec（proposal / specs / design / tasks + validate）。

## 三、decisions/（方案决策）

| 文件 | 决策内容 |
|---|---|
| `决策：H3 提示词改写内嵌化——原生 Skill 集成方案.md` | 提示词改写阶段内嵌化的选型结论（输入为 Handoff 1/2 与外部平台交接） |

## 四、reference/（外部与运维参考）

| 文件 / 目录 | 用途 |
|---|---|
| `MiniMax-H3/MiniMax-H3-SGLang-Cookbook.md` / `.html` | SGLang 部署 MiniMax-H3 官方手册副本（2026-11-04 抓取；附录 A 为 2026-09-29 builder 快照，已标注过期） |
| `MiniMax-H3/MiniMax-H3-vLLM-Recipes.md` / `.html` | vLLM 部署 MiniMax-H3 官方手册副本（2026-11-04 抓取，默认配置 hardware=b300 · variant=default · task=t2va） |
| `MiniMax-H3/MiniMax-H3-vLLM-Recipes-2xRTX-5090.md` / `.html` | 同一 vLLM 手册的 2×RTX 5090 配置副本（2026-11-04 抓取；含 6 个 task 取值的逐行差异核对记录） |
| `Qwen-Image-2.1/vLLM-Omni-Recipe-Qwen-Image-2.1.md` / `.html` | Qwen-Image-2.1 与 2.1-Turbo 的 vLLM-Omni 官方 in-repo recipe（2026-11-06 抓取，Turbo 无独立 recipe、同一文件内；头部已标注与模型卡/recipes 页的口径冲突） |
| `Qwen-Image-2.1/Qwen-Image-2.1-Model-Card.md` / `.html` | Qwen 官方 HuggingFace 模型卡（10 张参考图、RGBA、推荐 40 步、分辨率预设最大 2752×1536；经 hf-mirror 取回并与镜像页逐节核对） |
| `Qwen-Image-2.1/Qwen-Image-2.1-Turbo-Model-Card.md` / `.html` | Qwen 官方 Turbo 模型卡（8 步调度随权重保存、`num_inference_steps` 单独设置不生效、可用 `sigmas` 覆盖） |
| `Qwen-Image-2.1/vLLM-Recipes-Page-Qwen-Image-2.1.md` / `.html` | recipes.vllm.ai 的按配置导出（hardware=gb300 · variant=default · task=t2i；已核对 i2i 仅 3 行元数据差异） |
| `Qwen-Image-2.1/vLLM-Omni-Image-Generation-API.md` / `.html` | vLLM-Omni `/v1/images/generations` 协议参考（文生图端点字段表） |
| `Qwen-Image-2.1/vLLM-Omni-Image-Edit-API.md` / `.html` | vLLM-Omni `/v1/images/edits` 协议参考（图生图 multipart 字段表与限制） |
| `迁移机配置参考：MiniMax-H3 完整 config.md` | 推理机分离部署时的完整 `config.yaml` 模板与自检命令 |
| `tips.md` | 打包迁移（tar 排除清单）等运维小抄 |

## 五、旧路径对照（2026-10-12 目录重整）

本次仅移动文件位置，**文件名与正文结论均未改动**。已归档 openspec 变更（`openspec/changes/archive/`）作为历史快照保持原貌，其中出现的旧路径按本表解读：

| 旧路径 | 新路径 |
|---|---|
| `docs/code-review-*.md` | `docs/reviews/code-review-*.md` |
| `docs/minor-plan-07b4bdb-to-3ac13de.md` | `docs/reviews/minor-plan-07b4bdb-to-3ac13de.md` |
| `docs/Handoff 1～4：*.md` | `docs/handoffs/Handoff 1～4：*.md` |
| `docs/决策：H3 提示词改写内嵌化——原生 Skill 集成方案.md` | `docs/decisions/决策：H3 提示词改写内嵌化——原生 Skill 集成方案.md` |
| `docs/MiniMax-H3_docs/` | `docs/reference/MiniMax-H3/` |
| `docs/迁移机配置参考：MiniMax-H3 完整 config.md` | `docs/reference/迁移机配置参考：MiniMax-H3 完整 config.md` |
| `docs/tips.md` | `docs/reference/tips.md` |

`docs/README.md`、`docs/README_EN.md`、`docs/WORKFLOW.md` 未移动。

## 六、同时修正：展示向 README 的图片相对路径

`docs/README.md` 与 `docs/README_EN.md` 从仓库根拷入 `docs/` 后，本地图片引用仍写 `video-claw-pics/…`、`./FilmAgent-pics/…`，从 `docs/` 目录渲染会指向不存在的 `docs/video-claw-pics/` 而裂图。本次统一改为 `../` 前缀（每份 21 处引用 / 共 42 处，21 个唯一路径，各改动 15 行），正文文字与外链（GitHub user-attachments 视频、 shields 徽标等）未动。校验：逐条按 `docs/` 相对解析，21 个路径全部命中真实文件。

## 七、designs/（方案设计）

| 文件 | 状态 | 内容 |
|---|---|---|
| `用户与个人空间设计方案.md` | v18.3 文档修订（13–17 组已实施、复审有条件通过；R15 已补；整体未验收） | 参照 `Design_Agent_Platform` 的 `USER_SPACE_ISOLATION_HANDOFF_2026-10-23`，采用「上游 GUIP 统一身份 + 单租户按 `login_name` 字符串隔离」（`guip`/`single` 双模式）、**PostgreSQL 元数据 + MinIO 对象存储**、媒体控制器、`/space` 个人空间与收藏转存的完整方案；含剧本/角色/分镜等 stage 中间产物入库、**每阶段完成写 DB 检查点并支持恢复**、「单进程 ≠ 单用户单任务」说明、信任边界/SSE 约束、改造清单、分期、验证断言、风险与 Q1–Q8 决策记录 |
| [GUIP-SDK接入与前端身份解析方案-2026-11-11.md](designs/GUIP-SDK接入与前端身份解析方案-2026-11-11.md) | v3 复核无阻断项（Q1–Q8 按用户指令采用建议、未逐项确认；未实施） | `guip` 模式加载部署方提供的 `GuipAPI.xc2.js`（仓库占位、真实 SDK 不入库）、`?User=` / `globalInfo` 解析优先级、身份纯内存、无身份阻断；含加载契约、部署/验收 A29–A34、风险与回滚 |
| `用户与个人空间-数据库方案.md` | v15 实施子方案（接入已实施；整体未验收） | **PostgreSQL + MinIO**：sessions / session_stage_artifacts（含 status/version/checkpoint）/ artifact_files / pipeline_tasks / sandbox_records / sandbox_paths / saved_items；repository、stage 检查点事务、JSON→DB 与本地→MinIO 迁移、备份/回滚、媒体预签名、compose 服务与镜像变更、测试与风险；SQLite fallback 已移除，local 仅旧文件读取 |
| [用户空间-R15-nginx-X-Accel-Redirect方案-2026-11-10.md](designs/用户空间-R15-nginx-X-Accel-Redirect方案-2026-11-10.md) | v4 审核修订草案（未实施） | R15 媒体加速：覆盖 `/code/` 与 saved-media；边缘 nginx 处理后端鉴权后的 X-Accel；固定 MinIO upstream + 字节保真 presign query；HEAD 留后端；SSE 泛 location；网络隔离、真实 IP、限流与日志作用域；不承诺自动回退、不做 missing 回调 |
| [会话状态镜像导出方案-2026-11-04.md](designs/会话状态镜像导出方案-2026-11-04.md) | 设计提案（v2，未实施、未转 OpenSpec；Q1–Q4 已拍板） | 将 DB 中**已提交**的会话元数据与各阶段产物（剧本正文、角色/场景、分镜、改写提示词、选中版本引用、状态图）**以及 Pipeline 任务与沙盒记录**单向镜像为宿主可读的 JSON/Markdown，落 `./data/code/mirror/{sessions,tasks,sandbox}/<owner_key>/<id>/`；含数据来源（`load_committed` / `task_view` / `_record_view` + `FileRepo`，及四处待新增只读查询方法）、M1–M8 不变量（非状态源、失败隔离、与 `result/`、`work/executions`、`_refs` 目录隔离、归属分区、原子写与 symlink 拒绝、fingerprint 去重、不含秘密、幂等 GC）、三档触发选型（已定定时 60s + CLI，不采提交即时）、`VC_MIRROR__*` 配置四处改动、既有机制影响核对表、容器内 T1–T14 验收矩阵与宿主实测取证、~3d 分步工作量、R1–R7 风险与 Q5/Q6 未拍板项；附零改动替代做法与修订记录 |
| [工作流与Prompts优化计划-2026-10-29.md](designs/工作流与Prompts优化计划-2026-10-29.md) | 待确认的优化提案，未实施 | 承接 [工作流专题审查](reviews/工作流与Prompts审查报告-2026-10-29.md)：局部契约修复 → H3／视频有效输入统一 → 正文语义与模板分型 → 缓存／调用／上下文优化；含问题映射、待确认决策、Docker 验收矩阵和分步确认边界 |

区分：`designs/` 放未定稿、待评审的方案提案；`decisions/` 放已拍板的定稿快照（保留历史原貌）。机制性变更实施前仍需按项目约定在 `openspec/changes/` 补齐 proposal / specs / design / tasks 四工件。
