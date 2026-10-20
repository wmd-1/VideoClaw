# docs 文档索引

> 本目录按**文档性质**分类。工程活文档与展示向 README 保留在 `docs/` 根（被根 README 与多处 openspec 任务直接引用，路径不变）；其余文档归入子目录。

## 目录结构

| 位置 | 性质 | 说明 |
|---|---|---|
| `README.md` / `README_EN.md` | 展示向 | 作品集、演示视频、营销向介绍（中/英），由根 `README.md` 链接 |
| `WORKFLOW.md` | 工程活文档 | 主流程链路详解：阶段处理流程、产物结构、用户介入点、失败兜底 |
| `reviews/` | 评审记录 | 提交区间代码评审报告与 Minor 处置方案，按轮次递增 |
| `handoffs/` | 跨会话交接 | 事实澄清、缺陷根因、容器内复现命令与交接边界 |
| `decisions/` | 方案决策 | 定稿快照，保留历史原貌，后续修订以追加说明方式处理 |
| `reference/` | 外部与运维参考 | 模型服务手册、迁移机配置模板、打包迁移 tips |

## 一、reviews/（评审记录）

| 文件 | 轮次 | 评审区间 | 评审日期 |
|---|---|---|---|
| `code-review-6ba2f19-to-cdea2a1.md` | 第一轮 | `08502d5..cdea2a1` | 2026-09-27 |
| `code-review-07b4bdb-to-3ac13de.md` | 第二轮 | `682841e..3ac13de` | 2026-09-30 |
| `code-review-3ac13de-to-eaa012a.md` | 第三轮 | `3ac13de..eaa012a` | 2026-10-07 |
| `code-review-eaa012a-to-9bb51fe.md` | 第四轮 | `eaa012a..9bb51fe`（18 个提交） | 2026-10-12 |
| `code-review-9bb51fe-to-2f720f8.md` | 第五轮 | `9bb51fe..2f720f8`（23 个提交，即 `452e96d` 及之后） | 2026-10-19 |
| `minor-plan-07b4bdb-to-3ac13de.md` | 配套方案 | Minor 项 M-a~M-f 的处置方案（源自第二轮） | 2026-09-30 |

约定：报告命名 `code-review-<起始短SHA>-to-<结束短SHA>.md`；区间端点取“上轮报告终点 → 本轮 HEAD”，因此新一轮的第一个提交就是上轮终点的下一个提交，两轮不重叠也不漏审。问题编号（C1/I1–I4/M1–M3 → C-1/I-A/M-a → I-1/M-1 → C-1/I-1/M-1）跨轮沿用同名编号的含义，新一轮新发现用本轮前缀并在头部声明“引用上轮问题时标注‘上轮’”；每份报告头部保留「修复更新」段，并含「上轮未修项核实表」。

## 二、handoffs/（跨会话交接）

| 文件 | 交接内容 |
|---|---|
| `Handoff 1：VideoClaw + H3 Skill + H3 Adapter.md` | MiniMax-H3 以原生 Skill + Adapter 接入 VideoClaw（不引入 OpenHarness / LangGraph） |
| `Handoff 2：VideoClaw + LangGraph.md` | 用 LangGraph 接管核心创作流程的 Agent/Workflow 编排（备选路线） |
| `Handoff 3：H3 提示词改写阶段 - 待办与验证缺口.md` | 改写阶段的待办事项与验证缺口事实交接（含容器内复现方式） |
| `Handoff 4：H3 原生改写器 - 评审加固承接（I1·I4·M1）.md` | 第一轮评审加固项中仍适用于原生实现的部分 |
| `Handoff 5：前端构建端口硬编码故障与修复.md` | 构建期端口硬编码致 API 500 故障的根因、修复与治理（含复现命令、免网络重建配方、验收标准、交接边界与 655c519 留痕更正） |

区分：Handoff 用于缺陷交接与跨会话续作；需求/方案层面的正式提案走 openspec（proposal / specs / design / tasks + validate）。

## 三、decisions/（方案决策）

| 文件 | 决策内容 |
|---|---|
| `决策：H3 提示词改写内嵌化——原生 Skill 集成方案.md` | 提示词改写阶段内嵌化的选型结论（输入为 Handoff 1/2 与外部平台交接） |

## 四、reference/（外部与运维参考）

| 文件 / 目录 | 用途 |
|---|---|
| `MiniMax-H3/MiniMax-H3-SGLang-Cookbook.md` / `.html` | SGLang 部署 MiniMax-H3 官方手册副本 |
| `MiniMax-H3/MiniMax-H3-vLLM-Recipes.md` / `.html` | vLLM 部署 MiniMax-H3 官方手册副本 |
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
