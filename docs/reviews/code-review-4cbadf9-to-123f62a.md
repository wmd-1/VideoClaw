# VideoClaw 提交区间代码评审报告（第六轮）

- **报告日期**：2026-10-29。
- **审查范围**：`4cbadf953914476a6baa73ed5a1e78200078f5d1^..123f62a25144ec7182fb35cea8079aaf8a06ed7e`，包含用户指定的起始提交，共 **13 个提交、132 个文件，+14300/−567**。文件名采用用户指定的首个提交；实际 diff 左端是该提交的父提交。
- **方法**：一次 CodeReview 子代理完成区间审查，结合逐提交差异、HEAD 源码、调用链与 Git 归因；随后主线程对用户补充的其他会话报告逐项映射，并回源核实新增证据。不是仅审最后提交，也不是仅按净 diff 作结论。
- **补充材料**：[用户与个人空间方案审核报告-2026-10-28](../designs/用户与个人空间方案审核报告-2026-10-28.md)。该文与代码基线同为 `123f62a`，但属于本地补充材料，不计入上述 Git 变更规模。
- **证据边界**：本次为静态审查，未运行后端/前端测试、构建、浏览器、迁移或真实模型调用。以下触发场景均为源码可推导、待运行复验的场景，不是实测记录。历史 PASS 和工件勾选不冒充本次验证。
- **修改范围**：仅新增本报告；未修改业务代码、原方案、其他会话报告、OpenSpec、配置或部署，未提交 Git。审查开始时工作区干净，期间其他会话产生的未跟踪文件不计入代码审查范围。
- **编号**：本轮代码问题使用 R01—R22，规格冲突使用 D01；其他会话报告使用“方案审核 R1”等前缀，前轮问题明确标注“第五轮”。

## 修复更新

截至本报告写入，**R01—R22 均未在本会话修复或运行复验，D01 未修改工件**。本报告是问题记录与后续修正建议，不是已经批准的实施方案或上线放行单。后续应逐项追加修复提交、验证命令和证据，不能用整体“已完成”关闭所有条目。

## 一、结论与风险总表

**不建议将当前 HEAD 按“用户空间隔离、PostgreSQL/MinIO 持久化与恢复已完整交付”验收上线，也不宜直接执行正式认领与文件迁移。**

去重后确认 **22 项代码问题（9 项 P1、13 项 P2）和 1 项 P2 规格冲突**。主要风险集中于：新 DB/媒体网关读取路径已经启用，但正式生成、人工编辑、媒体消费及恢复路径尚未形成完整闭环。剧本修订和保真导入另有并发覆盖与标注消费缺陷。

P1 表示影响核心可用性、数据持久性或输入归属、应在相应功能上线前修复；P2 表示限定场景下的功能或一致性问题，其中迁移类 P2 同样阻塞正式迁移。各项置信度为高，依据为确定的源码调用路径；不等价于已在部署环境复现。

| 编号 | 级别 | 问题 | 归因 |
|---|---|---|---|
| R01 | P1 | 上传剧本导入遗漏临时文件 owner 校验 | `01306b2` 隔离接入遗漏 |
| R02 | P1 | Pipeline/sandbox 正式写入未接 DB 和媒体索引 | `01306b2` 新读路径与旧写路径不一致 |
| R03 | P1 | 人工上传、PATCH、跨阶段同步未完整落入 DB | `01306b2` |
| R04 | P1 | 新阶段不上传不可变 MinIO 对象，重跑仍可覆盖旧媒体 | `01306b2` 新契约未接旧生成链 |
| R05 | P1 | JSON 优先恢复绕过最后 completed DB 检查点 | `01306b2` |
| R06 | P1 | 认领后继续导入撤销 owner，重复导入覆盖新元数据 | `01306b2` |
| R07 | P1 | 物化早于会话快照注入，实际消费仍读 public_path | `01306b2` |
| R08 | P1 | 修订预览回写旧正文，覆盖等待期间已保存编辑 | `9841f67` |
| R22 | P1 | Pipeline EventSource 无身份头，guip 下事件流 401 | `01306b2` 新依赖与旧客户端冲突 |
| R09 | P2 | 媒体字典被推导成 set，丢失条目 ID | `01306b2` |
| R10 | P2 | MinIO 物化结果丢失 trusted，合法 Pipeline 输入被拒绝 | `01306b2` |
| R11 | P2 | 推理端仍收到无身份参数的裸 `/code` URL | `01306b2` 网关变更造成回归 |
| R12 | P2 | Sandbox 视频接口把可选图片变成必填 | `01306b2` |
| R13 | P2 | 预览后新增的阶段 2 手改未经确认即被覆盖 | `9841f67` |
| R14 | P2 | 导入 action 行标注未进入分镜单元 | `48a51b1` |
| R15 | P2 | 手工修改导入正文后仍消费旧行标注 | `48a51b1` |
| R16 | P2 | 空间和收藏选中输入/中间视频而非 final | `01306b2` |
| R17 | P2 | `--files` 未限制 owner，与 `--all-files` 无范围区别 | `01306b2` |
| R18 | P2 | 已有 local 索引永久跳过，不能增量迁移 MinIO | `01306b2` |
| R19 | P2 | 首次文件上传失败未建立 local 回退索引 | `01306b2` |
| R20 | P2 | DB running 标记失败后会话执行权不释放 | `01306b2` |
| R21 | P2 | single 模式 URL 登录名覆盖 local，API/媒体身份分裂 | `01306b2` |
| D01 | P2，规格 | 删除返回 409，却要求 runner 停止写入 | 本地 `user-space-isolation` spec 内部冲突 |

### 不应误报的既有取舍

- API 自报身份头可伪造、媒体 `?user_login=` 可冒读，均为明确接受的风险；本报告不将其重新包装成认证漏洞，也不据此宣称强隔离安全。
- 媒体浏览通道的接受边界不豁免用户输入路径的 owner 检查，也不豁免 API 与媒体有效身份一致性。
- 路径模型 A、单进程、浏览器不用预签名 URL、收藏独立副本和沙盒不进入个人空间均保留。
- custom 视频适配器按声明矩阵明确拒绝不支持的远程形态，不等于实现失效。

## 二、与其他会话方案审核报告的对照

补充报告的 **7 项核心判断经对照成立**。其中 5 项与区间审查首轮结果重合；Pipeline SSE 和规格删除语义由该报告补充，已回源核实。重复导入覆盖业务元数据的影响并入 R06，不另增重复编号。

| 方案审核编号 | 本报告编号 | 对照结论 |
|---|---|---|
| R1：JSON 优先恢复 | R05，关联 R03 | 同一核心问题；本报告同时强调人工变更/跨阶段同步提交缺口。 |
| R2：新阶段未接不可变 MinIO | R04，关联 R02 | 阶段链判断一致；本报告进一步核查 Pipeline/sandbox 正式写路径。 |
| R3：显式认领与重复导入覆盖 | R06 | 合并 owner 回退和最新业务元数据被旧 JSON 覆盖两种触发。不能只验证行数不增加。 |
| R4：Pipeline EventSource | R22 | 补充确认。前端确有真实调用，后端 guip 依赖确实要求头；默认无额外网关注入时订阅失败。 |
| R5：文件迁移范围扩大 | R17 | 一致；不是把 bob 对象写入 alice 前缀，而是操作范围超出声明。 |
| R6：local 行无法转换 | R18，关联 R19 | 一致；另需覆盖“原本没有索引且首次上传失败”时的 local 回退。 |
| R7：409 与停止写入冲突 | D01 | 一致，归为规格问题，不误报当前 409 拒绝删除代码有错，也不要求新增 cancel。 |

对补充报告的使用边界：

1. 其 `openspec validate` 通过与 60/62 完成是另一会话的记录；本次直接读取 tasks 确认仅 10.1/10.2 未勾，但未重跑 OpenSpec CLI。
2. 不采用简单相加的“21＋7”问题数；重合项保持一处修复和验证入口。
3. 其“保留方案方向、修正集成和验收状态”建议合理。DB 权威、不可变对象、fetch SSE 与 409 拒绝删除已有明确契约，不必重新作为未决架构选型。
4. R06 修复不能把所有业务用 `SessionRepo.upsert` 一并改为只插入；应为迁移提供专用非覆盖入口。
5. 两份报告都没有证明历史测试从未执行；结论是现有实现和可见断言不能支撑完整完成声明。

## 三、P1 代码问题

### R01：上传剧本导入遗漏临时文件 owner 校验

**位置**：[workflow.py L145–152](../../video-claw/video-claw/backend/api/routers/workflow.py#L145-L152)、[script_agent.py L419–431](../../video-claw/video-claw/backend/core/agents/script_agent.py#L419-L431)。

**触发与证据**：使用本人身份创建 `script_source=upload` 项目，`file_path` 指向已知的另一用户 `users/<owner_key>/...` 文件。上传导入分支绕过已有 owner 检查的 `merge_uploaded_file_into_idea`，保存原文件名；Agent 实际读取时只验证位于 `TEMP_DIR`，未验证文件归属。

**影响**：另一用户的上传文档被读取并导入当前会话。不需要伪造另一用户的 API 头；不是逃出上传目录的任意文件读取，而是隔离改造遗漏新消费入口。导入功能来自 `48a51b1`，归属缺口在 `01306b2` 隔离接入中形成。

**修复方向**：创建及消费入口复用临时文档路径＋owner 校验，显式传入会话 owner；legacy 裸文件名仅按既定 single 兼容规则放行。

**关闭断言**：双 owner 上传，A 引用 B 的文件被拒绝且不读取文档、不创建可见导入产物；本人文件和已批准的 legacy 场景正常。

### R02：Pipeline、sandbox 正式写入未接 DB 和媒体索引

**位置**：[pipelines/storage.py L44–51](../../video-claw/video-claw/backend/pipelines/storage.py#L44-L51)、[sandbox.py L148–160](../../video-claw/video-claw/backend/api/routers/sandbox.py#L148-L160)、[media.py L105–109](../../video-claw/video-claw/backend/api/routers/media.py#L105-L109)。

**证据**：Pipeline 的 `run_pipeline_task → mark_completed → update_task → save_task` 仍写 JSON；sandbox 成功后的 `_add_record → _save_history` 仍写历史 JSON。新媒体网关要求 `artifact_files`，空间查询则依赖 `TaskRepo`。仓储存在，不代表正式写入已调用它；任务仓储调用主要在启动清扫、空间查询和 legacy 导入，沙盒仓储写入见 legacy 导入。

**影响**：新生成的本地 Pipeline/sandbox 媒体缺索引，本人通过新网关读取仍 404；新 Pipeline 成片不进入空间。已导入任务经旧删除入口删除时，DB/MinIO 生命周期不同步；仅恢复 DB 也不能使旧 JSON 查询接口恢复记录。此处不泛化为原始远程 URL 一律不可读。

**修复方向**：正式创建、查询、更新、完成、删除统一接 repository；产物上传、文件登记与业务终态形成提交边界，不能以事后手工导入充当常规持久化。

**关闭断言**：从真实入口使用隔离假模型完成任务/沙盒后，无手工插行即可查到业务记录与媒体；任务出现在空间；删除及 DB 恢复后的行为一致。

### R03：人工上传、PATCH、跨阶段同步未完整落入 DB

**位置**：[orchestrator.py L1868–1876](../../video-claw/video-claw/backend/core/orchestrator.py#L1868-L1876)、[L1464–1466](../../video-claw/video-claw/backend/core/orchestrator.py#L1464-L1466)、[L1735–1740](../../video-claw/video-claw/backend/core/orchestrator.py#L1735-L1740)、[L1349–1351](../../video-claw/video-claw/backend/core/orchestrator.py#L1349-L1351)。

**证据**：人工图片上传、PATCH 正文/版本选择/取消预览主要调用 `save_session_to_disk`；其 DB 镜像仅写 sessions 元数据，未提交阶段 payload/新增文件。Agent 完成路径仅提交当前阶段，剧本修订同时重建的 `character_design` 没有纳入同次提交。

**影响**：图片上传成功却缺网关索引；只依赖 DB 恢复时丢失人工修改或上传选择，取消的预览可能恢复出来；剧本与阶段 2 在内存/JSON 一致、DB 中却版本不一致。JSON 优先暂时遮蔽部分丢失现象，不等于 DB 持久化完整。

**修复方向**：统一产物变更提交入口，明确所有受影响阶段、文件和版本；跨阶段同步同事务提交，避免只为上传补一行索引而遗漏 PATCH。

**关闭断言**：上传、修改正文、选择版本、取消预览、应用修订后，重构 DB 恢复路径分别保留相同结果；关联阶段不得半提交。

### R04：阶段完成未上传不可变对象，重跑仍可覆盖旧媒体

**位置**：[checkpoint_store.py L80–101](../../video-claw/video-claw/backend/core/checkpoint_store.py#L80-L101)、[L114–133](../../video-claw/video-claw/backend/core/checkpoint_store.py#L114-L133)、[editor_agent.py L118–127](../../video-claw/video-claw/backend/core/agents/editor_agent.py#L118-L127)。

**证据**：`mirror_completed` 扫描本地文件，登记 `storage_kind=local`，提交原 payload；没有 execution_id 对象发布和 canonical payload 替换。后期仍使用固定 `<sid>_ep<n>.mp4` 并由 `ffmpeg -y` 覆盖。

**影响**：PG＋MinIO 备份不包含新阶段的全部二进制，本地文件不能被当作可重建缓存；重跑可覆盖旧检查点引用的同一路径。固定输出名是旧行为，但本区间承诺的不可变对象与最后 completed 恢复没有隔离它。

**修复方向**：执行生成 execution_id，上传到新 key/public_path 后原子提交 payload、文件行和指针；失败只清理本次对象。不能通过把新产物长期登记 local 来关闭现有契约。

**关闭断言**：真实阶段产物进入 MinIO；两次同名执行 key/public_path 不同；第二次提交失败后旧媒体校验值不变；隔离环境无本地缓存仍可读取和消费。

### R05：JSON 优先恢复绕过最后 completed DB 检查点

**位置**：[orchestrator.py L1347–1351](../../video-claw/video-claw/backend/core/orchestrator.py#L1347-L1351)、[L1884–1899](../../video-claw/video-claw/backend/core/orchestrator.py#L1884-L1899)、[L1969–1971](../../video-claw/video-claw/backend/core/orchestrator.py#L1969-L1971)。

**证据**：阶段先保存 JSON，再提交完成检查点；启动先装 JSON，DB 加载跳过已存在 sid；磁盘加载用 `setdefault` 合并 DB payload，不能替换同名阶段。

**触发**：DB 已有成功快照 A，重跑写出部分或新结果 B 到 JSON，随后失败或完成事务回滚。启动/懒加载仍保留 B，而非最后已提交的 A；旧 JSON owner 也可压过 DB 认领结果。

**影响**：repository 保留旧 payload/version 的正确性不能保证引擎恢复正确；完成事务之外的 session/JSON 持久化破坏整体原子性。

**修复方向**：存在 DB 资源时，以 DB owner、元数据和最后 completed payload 为权威；JSON 仅作显式 legacy 导入/备份。完成状态与阶段/文件由同一事务提交，失败还要恢复内存可见状态。

**关闭断言**：注入完成事务失败并保留冲突 JSON，重构引擎及懒加载均恢复 A，version 不增加，无半提交文件行，不批量下载对象。

### R06：显式认领被后续导入撤销，重复导入覆盖最新元数据

**位置**：[legacy_import.py L274–290](../../video-claw/video-claw/backend/db/legacy_import.py#L274-L290)、[L87–105](../../video-claw/video-claw/backend/db/legacy_import.py#L87-L105)、[sessions.py L27–35](../../video-claw/video-claw/backend/db/repositories/sessions.py#L27-L35)。

**证据**：CLI 先 `OwnerRepo.reassign`，随后无条件执行普通 JSON 导入；导入取旧 JSON owner，通用 upsert 冲突时更新 owner、status、current_stage、progress、meta 等字段。

**两个独立触发**：① JSON 保留 `owner=local`，执行 local→alice 认领，同一命令又把父资源 owner 写回 local；② 首次导入后用户更新 DB，再普通导入旧 JSON，最新业务元数据被覆盖。仅验证总行数不增加不能证明幂等。

**影响**：认领不能保持，已迁移文件 owner 与父资源可能分裂；正常业务状态被旧备份回退。文件行写入时的 owner trigger 不能替代父资源后续更新的一致性。

**修复方向**：认领与导入分离；迁移使用专用“仅不存在时插入”入口，不覆盖既有 owner/元数据/检查点；不要把所有业务 upsert 改成不更新。同步核查 task/sandbox 分支，明确参数组合、dry-run 和审计。

**关闭断言**：保留原 JSON，认领后及重复普通导入后 owner 均不回退；DB 最新元数据/检查点保持；dry-run 无写入，预览与执行范围一致。

### R07：媒体物化早于真实会话快照注入

**位置**：[orchestrator.py L396–398](../../video-claw/video-claw/backend/core/orchestrator.py#L396-L398)、[L1128–1130](../../video-claw/video-claw/backend/core/orchestrator.py#L1128-L1130)、[editor_agent.py L65–70](../../video-claw/video-claw/backend/core/agents/editor_agent.py#L65-L70)。

**证据**：`prepare_stage_execution` 物化顶层输入，`execute_stage` 随后注入尚未物化的 `_session_artifacts`。后期优先从快照读 `clips[].selected`，直接执行 `os.path.exists`。

**触发与影响**：PATCH 选择媒体后 state 保存 `/code/result/...`，Docker 实际文件位于 `/app/code/result/...`；public_path 被当成本地路径，片段被跳过，最终可能报没有可拼接视频。顶层 `selected_clips` 已物化不能修复有 clips 列表的主路径；视频 Agent 的快照参考图消费也需同步检查。

**修复方向**：在快照注入后，对本阶段真正需要的媒体字段按 owner 校验并物化；持久化 state 保持 canonical，不改成启动时下载全部产物。

**关闭断言**：PATCH public_path 后执行视频/后期可读取所选媒体，API/DB 仍返回 public_path；MinIO-only 输入同样可消费。

### R08：生成修订预览覆盖等待期间已经保存的正文编辑

**位置**：[script_agent.py L383–386](../../video-claw/video-claw/backend/core/agents/script_agent.py#L383-L386)、[orchestrator.py L1281–1284](../../video-claw/video-claw/backend/core/orchestrator.py#L1281-L1284)。

**触发顺序**：预览取正文 A 快照并等待 LLM → 同会话合法 PATCH 保存正文 B → Agent 返回 `deepcopy(A) + pending_revision` → 编排器用整个 payload 替换现有剧本。

**影响**：已保存的 B 丢失，“生成预览不改正文”不成立。应用时指纹校验不能补救，因为当前正文已被预览回写为 A，恰好与预览基线相同。

**修复方向**：预览提交时锁内比较当前基线，仅合并 `pending_revision`；基线变化则拒绝过期预览，不回写旧正文。

**关闭断言**：阻塞假 LLM，PATCH 保存 B 后释放预览，B 保持；预览明确过期或被拒绝，不能以应用阶段校验替代预览提交校验。

### R22：Pipeline EventSource 无法携带 guip 身份头

**来源**：用户补充的方案审核 R4；主线程追加回源确认。

**位置**：[workflowApi.ts L383–400](../../video-claw/video-claw/frontend/lib/workflowApi.ts#L383-L400)、[PipelinePage.tsx L876–914](../../video-claw/video-claw/frontend/components/pipelines/PipelinePage.tsx#L876-L914)、[pipelines.py L386–393](../../video-claw/video-claw/backend/api/routers/pipelines.py#L386-L393)、[identity.py L70–78](../../video-claw/video-claw/backend/core/identity.py#L70-L78)。

**证据**：真实页面仍调用原生 `EventSource`，没有走 API 头注入；后端事件路由在 `01306b2` 增加 `get_current_user`，中间件只从 `X-User-Login-Name` 取值。guip 缺头会被 `safe_login` 拒绝。Git blame 证明 EventSource 是旧实现，`0fb2530` 只增加 basePath，新身份依赖才使旧链路失配。

**影响与边界**：在前端负责注入身份、代理不额外补头的部署中，创建/查询 API 正常，事件订阅却 401；页面收不到进度和完成事件，错误回调还清除 running 展示，后端任务可能继续执行。若可信网关另行注入身份，现象可能不同；本次未核实实际代理配置。数据库方案 P13 已明确要求 fetch SSE，因此不是新提架构偏好。

**修复方向**：fetch＋ReadableStream 复用身份头和既有寻址；处理跨 chunk 帧、非 2xx、AbortController 清理及迟到事件归属。不能扩大业务 query 身份回退、删后端依赖或切 single 来规避。

**关闭断言**：guip 下通过真实前端调用收到 snapshot/progress/终态；缺身份 401、他人任务 404；分块、切换任务、卸载不丢帧或串写。仅后端带头请求成功不能关闭本项。

## 四、P2 代码问题

### R09：字典型媒体选择被转换成 set

**位置**：[media_refs.py L263–269](../../video-claw/video-claw/backend/core/media_refs.py#L263-L269)。

字典推导遗漏 `sub_key:`，实际生成 set。`selected_images`/`selected_clips` 等合法 `{id: path}` 经过归一化后丢失条目 ID；下游按 ID 取选择失效，预期 `.keys()` 的兼容分支还可能异常。不能泛化为所有视频执行都会崩溃，但字典形状必定被破坏。

**修复/验收**：保留原 key，只归一化 value；以两个不同条目及嵌套媒体值断言结果仍是字典、ID/选择不丢失，并经过真实干预入口。

### R10：MinIO 物化结果丢失 trusted，合法输入被二次拒绝

**位置**：[media_refs.py L201–213](../../video-claw/video-claw/backend/core/media_refs.py#L201-L213)、[pipelines/utils.py L86–95](../../video-claw/video-claw/backend/pipelines/utils.py#L86-L95)。

本人媒体仅在 MinIO 时，`materialize` 返回带 trusted 的 `ResolvedMedia`，下载位置为 `code/work/_refs/...`；wrapper 却用 `str(...)` 去掉标记。动作迁移/数字人口播的 `copy_input_file → guard_media_input` 再按用户输入白名单检查，把合法内部 `_refs` 路径拒绝。

**修复/验收**：保留可信返回值类型，不扩大原始用户输入允许根。MinIO-only 本人参考可经过完整复制入口，他人和伪造 `_refs` 仍拒绝。

### R11：推理引用 URL 与新媒体网关必需身份参数冲突

**位置**：[custom_video.py L567–574](../../video-claw/video-claw/backend/models/custom_video.py#L567-L574)、[sandbox.py L224–235](../../video-claw/video-claw/backend/api/routers/sandbox.py#L224-L235)、[media.py L97–109](../../video-claw/video-claw/backend/api/routers/media.py#L97-L109)。

跨机 sglang 本地参考仍拼 `{base}/code/...`；sandbox 本地音频复制后也生成裸 `/code` URL。网关缺 `user_login` 返回 401，音频副本另缺文件索引。新增 `reference_urls` 没有业务接线。HTTP 候选失效时，无共享文件系统的推理机也不能读取 `file://` 候选。

**边界**：不包括原始远程 URL 或直接 multipart 传输；第五轮远程输入协议分流修复本身仍有效。

**修复/验收**：core 提供推理专用候选（MinIO 预签名或约定的可用本地引用），models 消费候选而非拼裸网关 URL。用无共享文件系统的隔离取图端验证可达性及音频索引，不调用付费模型。

### R12：Sandbox 视频接口把可选参考图片变为必填

**位置**：[sandbox.py L495–502](../../video-claw/video-claw/backend/api/routers/sandbox.py#L495-L502)。

文生视频或仅参考视频请求中 `req.image` 为空，仍执行 `_media_input("") → canonicalize`，抛 `empty media path`；合法 Optional 输入在模型调用前失败。

**修复/验收**：仅有图片时归一化，空值保持可选；覆盖纯文本、仅参考视频、图片三种请求，不能只测试图片路径。

### R13：预览后新增的阶段 2 手改没有重新确认就被覆盖

**位置**：[script_agent.py L396–401](../../video-claw/video-claw/backend/core/agents/script_agent.py#L396-L401)、[orchestrator.py L584–590](../../video-claw/video-claw/backend/core/orchestrator.py#L584-L590)。

预览生成时 `user_edited_items=[]`，之后用户修改阶段 2 描述，再应用预览。应用只检查旧差异和剧本指纹；阶段 2 PATCH 不改变该指纹，重建时覆盖最新手改而不要求 `confirm_overwrites`。

**修复/验收**：应用时重新比较当前阶段 2，或绑定其版本；变化后重新预览/确认，锁内提交前再校验。与 R08 不重复：本项是预览完成后的关联资产确认窗口。

### R14：导入 action 标注没有进入分镜单元

**位置**：[script_import.py L496–514](../../video-claw/video-claw/backend/core/script_import.py#L496-L514)、[storyboard_agent.py L408–417](../../video-claw/video-claw/backend/core/agents/storyboard_agent.py#L408-L417)。

保真导入把普通动作正文标为 `action`，分镜却只按标注处理 dialogue，动作仍要求原文包含 `<action>`。例如仓库普通正文“小陈走进办公室，把材料放在桌上。”不会生成 unit。动作虽保留在 annotated 文本，未进入按 units 组织的正式分段输入；纯动作剧本可能直接报未识别动作或台词。

**修复/验收**：消费有效 action 标注生成非对白单元，不要求用户修改保真原文。覆盖纯动作及动作/台词混合导入到真实分镜消费链。

### R15：手改导入正文后仍使用旧全局行标注

**位置**：[ScriptStage.tsx L377–386](../../video-claw/video-claw/frontend/components/stages/ScriptStage.tsx#L377-L386)、[orchestrator.py L1475–1485](../../video-claw/video-claw/backend/core/orchestrator.py#L1475-L1485)、[storyboard_agent.py L274–293](../../video-claw/video-claw/backend/core/agents/storyboard_agent.py#L274-L293)。

手动插入/删除行后仍保留旧 `line_start/line_end`；PATCH 只清修订预览，不使 `line_annotations/scene_spans` 失效。分镜越界判断仍基于 `meta.original_text` 行数，新正文按旧坐标解释，台词可能落入旧 heading/meta 被跳过，speaker/地点错配。

**修复/验收**：正文变化时共享派生标注失效处理，或用正文签名绑定并重新生成；不能只比旧基线最大行号。覆盖删除标题、首行插入、跨集编辑后无错误归属或漏台词。

### R16：个人空间及收藏选择输入/中间视频而非最终成片

**位置**：[space_service.py L165–175](../../video-claw/video-claw/backend/core/space_service.py#L165-L175)、[action_transfer.py L57–65](../../video-claw/video-claw/backend/pipelines/action_transfer.py#L57-L65)。

作品聚合取第一个 `kind=video` 即退出，只有没找到视频才读 `output.video_path`。动作迁移的 `input_video` 在 final 前，其他流水线也先追加中间片段；`save_work` 复制该聚合结果的 `_source`。

**影响**：作品、下载、收藏副本指向输入或首个中间片段，work_id 却标为 final。已导入 DB 的任务即可触发，不必先修 R02。

**修复/验收**：优先明确标识的 final，再回退权威 output；以多个视频 artifacts 且 final 非首项断言播放、下载和收藏副本均为最终成片。

### R17：`--files` 与 `--all-files` 无实际范围区别

**位置**：[legacy_import.py L263–290](../../video-claw/video-claw/backend/db/legacy_import.py#L263-L290)。

CLI 合并所有 owner 的 sessions/tasks/sandbox 后，两个 flag 走同一个全量 `_import_files`，没有目标 owner 过滤和 all-files 二次确认。执行 `--fallback-owner alice --files` 可上传 bob 的文件，扩大操作范围；仍按各自 owner 生成对象键，不是跨 owner 对象前缀污染。

**修复/验收**：先明确“缺 owner 填充值”和“本次文件目标 owner”的参数关系，再实现独立筛选与全量确认协议，同步工件和示例。双 owner fixture 下单 owner 模式只处理目标；缺全量确认/参数冲突须在写入前拒绝，dry-run 同范围。

### R18：已有 local 文件索引被永久跳过

**位置**：[legacy_import.py L224–228](../../video-claw/video-claw/backend/db/legacy_import.py#L224-L228)。

只要 public_path 已有行便跳过，不检查 `storage_kind`。阶段已登记 local 或旧文件已有 local 索引时，重复 `--files`/`--all-files` 也无法迁入 MinIO；“有索引”被误当作“对象迁移完成”。

**修复/验收**：已有 local 行上传校验后原位更新，已完成 MinIO 记录才可幂等跳过；失败保留 local。保持 public_path、owner、父资源与唯一性不变，验证迁移和重入两次状态。

### R19：首次上传失败没有建立可读的 local 回退索引

**位置**：[legacy_import.py L235–258](../../video-claw/video-claw/backend/db/legacy_import.py#L235-L258)、[media.py L105–109](../../video-claw/video-claw/backend/api/routers/media.py#L105-L109)。

首次导入无索引但本地文件存在的产物，上传抛 `StorageError` 后直接 continue，文件行只在成功时创建；元数据导入也不建立 local 行。因此保留 JSON/本地文件仍不能让新网关读取，返回 404。

**修复/验收**：为归属明确、路径合法的发现文件建立 local 可读状态，上传及校验成功后转换；失败保留并允许重试。与 R18 联合覆盖“无行→local→minio”和“已有 local→minio”，不是仅保护原有行。

### R20：`mirror_running` 失败后会话执行权不释放

**位置**：[orchestrator.py L1247–1269](../../video-claw/video-claw/backend/core/orchestrator.py#L1247-L1269)。

先把 sid 加入 `_active_sessions`，再调用会重新抛异常的 `mirror_running`；该调用位于清理执行权的 try/finally 保护范围之外。DB 写入异常后 Agent 尚未启动，sid 却永久留 active，后续执行报 already running，删除也可能受阻，直到进程重启。

**修复/验收**：从登记执行权开始保护 DB 标记、首次保存和执行；异常一致释放 active、boot_id 并恢复状态。注入首次 DB 标记失败，同进程恢复 DB 后可再次执行，无需重启。

### R21：single 模式 URL 身份覆盖 local，API 与媒体身份分裂

**位置**：[identityGuard.tsx L34–41](../../video-claw/video-claw/frontend/lib/identityGuard.tsx#L34-L41)、[L120–123](../../video-claw/video-claw/frontend/lib/identityGuard.tsx#L120-L123)、[basePath.ts L47–58](../../video-claw/video-claw/frontend/lib/basePath.ts#L47-L58)。

single 页面带 `?User=alice`，初始化把存储设为 local，但后续 `apiFetch → identityHeaders → getLoginName` 再优先读 URL 并存入 alice。后端 single API 固定操作 local，媒体 URL 却从存储追加 alice，因此能列数据但 local 媒体 404。

**修复/验收**：API 与媒体共用解析模式后的有效身份；single 固定 local，不被后续 URL 重新覆盖。覆盖带/不带 User 的 single 页面及 guip 正常场景。此项是身份一致性错误，不是要求取消已接受的媒体冒读边界。

## 五、D01：409 拒绝删除与 runner 停止写入的规格冲突

**来源及位置**：方案审核 R7；[artifact-checkpointing spec L96–113](../../openspec/changes/user-space-isolation/specs/artifact-checkpointing/spec.md#L96-L113)、[tasks L62](../../openspec/changes/user-space-isolation/tasks.md#L62)。本地 OpenSpec 工件不保证受 Git 跟踪，单列为契约缺陷而非代码提交缺陷。

Requirement 要求 running 删除返回 409、本期不实现 cancel；Scenario 却写“返回 409；runner 不再写入该任务”。拒绝删除后任务仍存在，停止写入相当于新增取消/冻结语义，两者冲突。

[当前路由 L379–382](../../video-claw/video-claw/backend/api/routers/pipelines.py#L379-L382) 对 pending/running 返回 409，符合拒绝删除方向，不应为迁就错误场景阻止 runner 正常落盘。

**修订建议**：pending/running 删除返回 409，资源和文件不删除，runner 继续执行并持久化；“不得写回已删除任务”只约束允许删除的终态及竞态。同步澄清 tasks 的“删除不写回”。

**关闭断言**：拒绝删除后仍可收到进度与最终结果；终态删除和他人 404 按既有契约执行。本报告未修改 spec/tasks，也未新增 cancel。

## 六、第五轮修复与未关闭项核实

完整对照[第五轮报告](code-review-9bb51fe-to-2f720f8.md)。下表“修复成立”仅指源码核实，不代表本次重跑其历史验证。

| 第五轮项 | 修复提交 | 本轮结论 |
|---|---|---|
| C-1：Pipeline 任意本地复制及整个 `/code` 暴露 | `5a7b5bc` | 原问题修复成立：共享守卫、别名覆盖、使用守卫返回值、排除 `code/data`。R02/R10 是后续接线问题。 |
| S-1：project/start 任意文档路径 | `5a7b5bc` | 原穿越修复成立；受控相对路径与 realpath 在。R01 是新导入路径的 owner 遗漏，不是任意文档读取原样复发。 |
| I-1：custom_video 远程输入错误本地化 | `d4f5d6c` | 原协议分流成立；远程不伪造 file URI，明确拒绝不支持的形态并捕获 OSError。新裸网关 URL 回归见 R11。 |
| M-1：tasks 分隔符不一致 | `8e41fed` | 前后端分隔符、大小写、去空、去重和保序口径一致，有共享 fixture。 |
| M-2：custom_image 多图丢弃 | `d4f5d6c` | dataurl 分支发送全部参考图，不再只取首图。 |
| M-3：OpenAI 服务端接受 url/dataurl | 未有真实服务端取证 | **保持未关闭**。请求形状/mock 不能证明服务端接受；本次也未调用端点。 |
| M-4：宿主构建端口兜底 | `8e41fed` | prebuild 和 Next 生产构建检查存在。 |
| M-5：宣传片别名优先级 | `8e41fed` | 按出现位置、同位置较长别名优先处理。 |
| M-6：多 worker 破坏 boot_id 前提 | `8e41fed` | 环境及 workers 参数检查存在；单进程是接受前提。 |
| M-7：未知/改写阶段视频兜底文案 | `8e41fed` | 图像、视频、未知阶段文案分流。 |
| S-2：上传标识与路径写入 | `53fb947` | 白名单、相关读写复用和 realpath 检查存在。R03 是文件登记遗漏，不是穿越写入复发。 |

前轮结尾“M-1～M-7 已修”的概括不能覆盖同文明确保留的 M-3，后续汇总继续单列服务端接受性未取证。

custom 视频边界复核：sglang HTTP(S) 图片按远程 conditions URI 处理；sglang 远程视频、vllm-omni/openai 远程图片/视频，以及不支持的 data/oss 视频引用明确拒绝，符合限定矩阵。不能因共享媒体层认识某协议，就推断所有适配器都必须支持它。真实服务端兼容性未在本次验证。

## 七、提交覆盖与验收状态

### 7.1 全部提交覆盖

| 提交 | 审查内容 |
|---|---|
| `4cbadf9` | 第五轮报告与接受边界，作为历史基线。 |
| `5a7b5bc` | 用户输入共享守卫、Pipeline 别名/复制端、静态目录收窄。 |
| `d4f5d6c` | custom 图片多图、视频远程矩阵、URI 候选及异常回退。 |
| `8e41fed` | tasks 分隔符、别名、构建端口、单进程、兜底文案。 |
| `53fb947` | 上传标识和路径、会话读写/删除相关入口。 |
| `01243d9` | 修复状态逐项对照，历史 PASS 不充当本次实测。 |
| `9cd7f37` | 浏览器取证按历史记录处理。 |
| `8402a75` | 修复归档、主规格同步与未关闭边界。 |
| `9841f67` | 修订预览/应用/取消、指纹、历史、删除确认、阶段 2 重建，发现 R08/R13。 |
| `48a51b1` | 正文保真切片、角色/场景和集号、行标注、分镜消费，发现 R14/R15。 |
| `0fb2530` | basePath、API/媒体 URL、开关、Next 构建与宿主组装；未确认独立于后续身份改造的必修问题。 |
| `01306b2` | 身份/owner、DB/对象/媒体网关、恢复、正式业务接线、迁移、空间/收藏及前端。 |
| `123f62a` | 仅本地迁移备份忽略规则，无业务逻辑问题。 |

### 7.2 工件完成声明与集成缺口

| 变更/任务 | 已有实现基础 | 不能据此关闭的缺口 |
|---|---|---|
| `script-instruction-revision` | 预览结构、集号约束、指纹、确认、历史上限、取消和阶段 2 复用 | R08/R13 的并发与确认时效；DB 接入 R03。 |
| `script-import-fidelity` | 原文切片/还原、显式描述、集数回写、行标注 | R14 动作消费、R15 手改失效；隔离遗漏 R01。 |
| 隔离 4A | 主要 session/task/SSE/config 入口依赖与 owner 检查 | R01 临时文档消费与 R22 前端订阅接线。 |
| 路径 5.2/5.5 | canonicalize/materialize/rehydrate/reference_urls 原语 | R07/R09/R10/R11；不能用原语测试替代实际消费链。 |
| 存储 5.1、检查点 6.2/6.3 | MinIO 适配器、仓储事务、version>0 查询 | R03/R04/R05/R20 的正式提交/恢复闭环。 |
| 任务/沙盒完成及删除 | repository、owner 字段、JSON 锁与陈旧处理 | R02 正式 CRUD、文件登记和生命周期。 |
| 空间/收藏 8.1–8.3 | owner 查询、合并分页、每 owner 锁、独立副本和唯一约束 | R02 来源缺失、R16 final 选择错误。未确认额外收藏归属绕过。 |
| Legacy 3.7 | CLI 参数、元数据/时间转换与文件上传基础 | R06/R17/R18/R19；认领、范围、转换、失败回退须组合验收。 |
| 前端 4.5/9.2、11.3/P13 | 身份门禁、API 头、媒体 query、basePath | R21 有效身份分裂、R22 真实 SSE 未带头。 |
| 上线 10.1/10.2 | 当前仍未勾选 | 待执行本身不是缺陷；迁移工具缺陷关闭前不应放行。 |

另有一处非新增运行故障的验收记录偏差：[tasks 2.2](../../openspec/changes/user-space-isolation/tasks.md#L14) 已勾固定 MinIO release、不得出现 latest，但 [docker-compose.yml L37](../../docker-compose.yml#L37) 仍为 `minio/minio:latest`。应核对并修正完成声明/版本约定；本报告不据此断言当前镜像必然故障，也未更改镜像或 Compose。

### 7.3 可见测试的真实证明范围

已读测试会手工建立媒体文件行、直接构造空间/收藏 repository 数据、直接测试阶段 complete/running/fail，以及直接调用 ensure_local。它们能证明有价值的底层原语，不能替代：

`真实生成/编辑入口 → 上传对象 → 业务与文件事务 → 媒体网关 → 空间/收藏 → 重启恢复`。

修正 tasks 时保留已完成的适配器和仓储工作，将缺失集成拆为待完成项；不要无差别撤销整个任务组，也不要把全勾选或 PASS 数量当成真实接线的证明。保留 A/P 编号到测试入口、关键断言、日志和代码/环境版本的对应关系。

## 八、执行记录与验证限制

### 已执行的只读检查

- `git status --short`、`git rev-parse HEAD`、区间 `git log`、`git diff --stat/--shortstat`：确认固定基线及 13 提交规模。
- 对功能提交使用 `git show`、父提交差异和 `git blame` 区分新增、回归、既有行为；主线程另核实 EventSource 与后端身份依赖的引入提交。
- Read/SearchSymbol/Grep/Glob 等静态检索：代码、前轮报告、主方案/数据库方案、相关 OpenSpec；对用户补充报告全文阅读并逐项映射。
- CodeReview 子代理执行 `docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'`：其检查时只见运行中的 PostgreSQL、MinIO，未见运行中的后端容器。此为当时容器列表，不等于后端镜像不存在，也不证明当前配置或挂载版本一致。

### 未执行

没有运行宿主后端脚本，也没有运行 Docker 内测试、API 冒烟、迁移 CLI、容器启停/重建、镜像构建、依赖安装、前端 lint/tsc/build、浏览器或真实模型调用。没有读取 `.env` 密钥用于报告，没有操作真实用户数据。另一会话的 OpenSpec validate 及历史浏览器取证仅作来源说明。

本次保持静态审查；没有因为未见运行后端容器而绕过 Docker 规则。并发、DB 故障注入、MinIO-only 恢复、真实代理 SSE 均待在获准的隔离环境验证。

## 九、后续处置与联合复验建议

以下为待确认建议，不代表已获实施、修改工件或迁移授权。

1. **先统一问题与工件**：使用第二节对照表承接两份报告；修订 D01，重核完成状态与验收映射；保留 DB 权威、不可变对象和 API/媒体通道的现行方向。
2. **优先关闭输入归属及用户编辑损失**：R01、R08、R13，避免继续产生跨 owner 导入或已保存内容回退。
3. **存储/恢复/消费联合修复**：R02–R05、R07、R09–R11、R20。只修 DB 恢复或只补索引都不足以关闭整个链路。
4. **迁移工具作为整体处理**：R06、R17–R19；先明确目标 owner 参数和全量授权/确认协议，保持原 JSON 和本地文件。不要未获批准运行真实迁移。
5. **补齐前端与剧本链**：R12、R14–R16、R21–R22；分别验证真实入口，不以组件或仓储 mock 替代。
6. **最终放行**：所有阻塞项有代码/工件版本和可复现证据后，再确认迁移范围、停写窗口、备份点及回滚窗口，分别授权执行和归档。

仍需人工明确的主要事项是迁移参数语义、全量操作确认方式及正式迁移窗口；不是重新争论已有 DB 权威等契约。独立前端镜像是否支持不挂载 `.next-runtime`、空间后续分页是否纳入本批，可另明确范围，不计为未经契约证明的代码缺陷。

### 联合回归矩阵

| 组 | 问题 | 必须保留的真实边界 |
|---|---|---|
| 身份/输入 | R01/R21/R22 | 双 owner 文档输入；single 带 User；guip 实际前端 SSE；缺身份/他人资源状态码。 |
| 正式写入 | R02/R03 | 从真实生成、上传、PATCH 入口写入，不手工预置 artifact_files 来绕过登记链。 |
| 对象/事务/恢复 | R04/R05/R20 | 同名重跑新 key；DB 完成失败；冲突 JSON；启动/懒加载；首次 running 失败后重试。 |
| 媒体消费 | R07/R09/R10/R11/R12 | public_path PATCH、映射形状、MinIO-only trusted、跨机取图、无图片请求。 |
| 修订并发 | R08/R13 | 预览等待期间编辑正文；预览完成后编辑阶段 2；不得静默覆盖。 |
| 导入/分镜 | R14/R15 | 纯动作、动作台词混合、删标题/插行后标注失效与正确消费。 |
| 空间/收藏 | R16，关联 R02 | 多个 video 且 final 非首项；播放/下载/收藏一致；删除源后副本仍可读。 |
| 迁移 | R06/R17/R18/R19 | 认领后重入、DB 更新后重入、双 owner 范围、local 转换及首次上传失败回退。 |
| 删除语义 | D01 | pending/running 删除 409 后仍可正常完成，终态删除与他人 404。 |

所有后端测试和测试性质验证必须在项目已有 Docker 环境执行；复用已有镜像，不触发构建，不另造测试镜像。必须核实测试数据库、桶/对象前缀、目录确实隔离；假模型只替换付费产出，不绕过待测引擎/CLI/事务链。若现有环境不足，先说明并确认方案；不得擅改 Dockerfile、Compose、依赖或启动脚本。前端按既有约定验证，需要 UI 取证时记录网络与截图。

## 十、值得保留的实现与跟踪口径

第五轮共享路径守卫、上传目标检查、custom 远程矩阵和多图修复值得保留。repository 的 owner 过滤、唯一约束、外键和阶段事务，MinIO public endpoint client、Range/HEAD 与流释放，以及收藏独立副本/用户锁，提供了有价值的基础。剧本原文切片保真、集号约束和同名资产复用也不应因接线缺陷被整体推翻。

后续每项关闭至少记录：**处置结论 → 修复代码/工件版本 → 对应测试及关键断言 → 实际命令/环境/退出状态 → 必要的 DB、对象或网络证据**。仅静态修正必须标“运行待验”；本报告当前全部新问题保持待处理，不以建议内容冒充修复完成。
