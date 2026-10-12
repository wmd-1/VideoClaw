# Handoff：基于 VideoClaw 引入 LangGraph 作为 Agent Workflow 编排层

## 1. 任务目标

基于当前 `HITsz-TMG/VideoClaw` 继续开发。

本方案与“VideoClaw + H3 Skill + H3 Adapter”方案的区别是：

> **不仅接入 H3，而是使用 LangGraph 接管 VideoClaw 核心创作流程中的 Agent/Workflow 编排。**

目标：

```text
用户需求
  ↓
LangGraph Agent
  ↓
需求理解
  ↓
剧本
  ↓
角色 / 场景
  ↓
分镜
  ↓
参考图
  ↓
H3 / 其他视频模型
  ↓
质量检查
  ↓
通过 ─────────→ 下一阶段
失败 ─────────→ 修改 Prompt / 重生成
  ↓
成片
```

LangGraph 官方定位就是用于长时间运行、有状态、需要持久化、恢复、Human-in-the-loop 和复杂确定性/Agent 混合工作流的编排。

---

## 2. 核心架构原则

不要把 VideoClaw 完全改造成 LangGraph App。

保留：

```text
VideoClaw Frontend
VideoClaw FastAPI
VideoClaw Project
VideoClaw Asset
VideoClaw Task
VideoClaw Model Adapter
```

增加：

```text
LangGraph Workflow Layer
```

目标架构：

```text
                    VideoClaw Web UI
                           │
                           ↓
                    VideoClaw API
                           │
                  ┌────────┴────────┐
                  │                 │
             Business Layer    LangGraph Runtime
                  │                 │
         Project / Asset / Task     │
                  │                 │
                  └───────┬─────────┘
                          ↓
                    Graph State
                          ↓
       ┌──────────┬───────┼───────────┐
       ↓          ↓       ↓           ↓
    Script     Cast     Shot       Video
       ↓          ↓       ↓           ↓
                  H3 Skill + Adapter
```

---

## 3. 一个非常重要的边界

必须定义两个状态源：

### VideoClaw 是业务事实来源

负责：

```text
Project
Asset
Storyboard
Shot
Video
用户最终编辑结果
```

### LangGraph 是执行状态来源

负责：

```text
当前 graph 节点
Agent 中间状态
路由决策
checkpoint
interrupt
resume
retry metadata
```

**不要让 LangGraph State 取代 VideoClaw 的业务数据库。**

尤其不要把：

```text
视频二进制
图片二进制
大段历史 Prompt
完整 Base64
```

直接放进 Graph State。

Graph State 只保存：

```text
project_id
task_id
asset_id
shot_id
prompt
model
mode
status
decision
metadata
```

大文件全部继续进入 VideoClaw Asset 系统。

---

## 4. 先勘察现有 VideoClaw

修改前必须完整阅读：

```text
video-claw/video-claw/backend/
video-claw/video-claw/frontend/
```

重点定位：

```text
Project
Task
Asset
Pipeline
Agent
Storyboard
Video generation
Model adapters
API routes
```

建立现有流程图：

```text
API
 ↓
Service
 ↓
Agent
 ↓
Pipeline
 ↓
Model
 ↓
Asset
```

然后再决定哪些节点进入 LangGraph。

**不要为了引入 LangGraph 而把所有现有 service 重写成 Node。**

---

## 5. Graph State 设计

建立明确的 State Schema。

建议：

```python
class VideoCreationState(TypedDict):
    project_id: str

    user_request: str

    script_id: str | None

    character_ids: list[str]

    scene_ids: list[str]

    shot_ids: list[str]

    current_shot_id: str | None

    current_asset_ids: list[str]

    generation_mode: str | None

    video_model: str | None

    prompt: str | None

    h3_task_id: str | None

    current_step: str

    step_status: str

    qa_result: dict | None

    error: dict | None
```

不要让 State 与数据库 Entity 完全 1:1 映射。

---

## 6. Graph 第一版

第一版先做一个最小但完整的主 Graph：

```text
START
  ↓
analyze_request
  ↓
generate_script
  ↓
generate_characters_scenes
  ↓
generate_storyboard
  ↓
generate_reference_assets
  ↓
generate_videos
  ↓
quality_check
  ↓
finalize
  ↓
END
```

然后再逐步加入条件边：

```text
quality_check
    ├── pass → finalize
    └── fail → revise_prompt
                    ↓
                generate_video
                    ↓
                 quality_check
```

不要第一版就设计几十个 Agent。

---

## 7. Human-in-the-loop

充分利用 LangGraph 的：

```text
interrupt()
checkpoint
resume
```

将 VideoClaw 原本“用户可以查看、确认、修改、继续”的产品机制与 Graph 结合。

适合插入人工介入的位置：

```text
剧本完成
角色/场景完成
分镜完成
参考图完成
视频完成
最终成片前
```

例如：

```text
generate_storyboard
        ↓
interrupt
        ↓
用户修改分镜
        ↓
resume
        ↓
generate_reference_assets
```

LangGraph 的 Human-in-the-loop 依赖 checkpoint 保存 graph state，并通过 thread ID 恢复执行。

生产环境必须使用持久化 checkpointer，不允许只使用内存 checkpoint。

---

## 8. H3 Skill 集成

保留 MiniMax-H3 官方：

```text
h3-prompt-writing
```

作为独立 Skill。

LangGraph 不负责 H3 Prompt 知识本身。

职责：

```text
LangGraph Node
      ↓
读取 H3 Skill
      ↓
根据当前 State 判断 T2VA / I2VA / FL2VA / L2VA / Ref2VA
      ↓
生成 H3 Prompt
```

然后：

```text
H3 Prompt Node
      ↓
H3 Adapter
      ↓
MiniMax-H3
```

即：

```text
LangGraph = 编排
H3 Skill = Prompt 专业知识
H3 Adapter = API 调用
```

---

## 9. H3 异步任务处理

MiniMax-H3 API 是：

```text
POST create
   ↓
task_id
   ↓
GET query
   ↓
success / failed
```

因此不得在 FastAPI 请求中同步等待。

推荐：

```text
LangGraph
  ↓
submit_h3_video
  ↓
保存 h3_task_id
  ↓
进入 waiting 状态
  ↓
后台任务系统负责 polling
  ↓
成功后更新 VideoClaw Task / Asset
  ↓
恢复对应 graph execution
```

注意：

> **LangGraph 是 Workflow Orchestrator，不是视频任务队列。**

不能简单地在 Graph Node 中：

```python
while not done:
    sleep(10)
    query()
```

然后让一个 Web Worker 长时间阻塞。

如果现有 VideoClaw 已经有后台 Task 机制，应优先复用现有机制。

---

## 10. 并行分镜

LangGraph 第二阶段实现：

```text
Storyboard
    ↓
Shot 1 ──→ H3
Shot 2 ──→ H3
Shot 3 ──→ H3
Shot 4 ──→ H3
    ↓
      Merge
        ↓
       QA
```

要求：

```text
一个 Shot 的失败不能导致全部 Shot 重跑
```

每个 Shot 必须具有独立状态：

```text
shot_id
generation_task_id
asset_id
status
retry_count
qa_result
```

这样才能实现局部重试。

---

## 11. QA / 自动返工

引入 LangGraph 后，必须体现它相对于现有 Pipeline 的价值。

至少实现：

```text
generate_video
       ↓
video_qa
       ↓
┌──────┴──────┐
↓             ↓
PASS          FAIL
↓             ↓
next          revise_prompt
                  ↓
             regenerate
```

QA 第一阶段可以做：

```text
任务是否成功
视频文件是否完整
时长是否正确
画面分辨率是否正确
输入素材是否成功引用
```

第二阶段再扩展：

```text
人物一致性
动作一致性
场景一致性
Prompt 遵循度
音画同步
```

---

## 12. 持久化设计

必须配置持久化 Checkpointer。

推荐生产环境使用数据库型 checkpointer。

Graph 必须使用：

```text
thread_id
```

与 VideoClaw：

```text
project_id
```

建立稳定映射。

建议：

```text
thread_id = video_project:{project_id}
```

但实际实现必须检查长度、字符集以及现有数据库约束。

LangGraph 官方 checkpoint 机制可用于恢复会话、Human-in-the-loop、故障恢复和时间旅行等能力；checkpoint 是 graph execution 的状态快照。

---

## 13. API 层

不要把 LangGraph 的内部 Graph State 直接暴露给前端。

对 VideoClaw 前端提供业务接口：

```text
POST /projects/{id}/run
POST /projects/{id}/resume
POST /projects/{id}/interrupt
GET  /projects/{id}/workflow
GET  /projects/{id}/workflow/events
```

前端只需要知道：

```text
当前阶段
当前 Shot
当前 Task
是否等待用户
是否运行中
是否失败
```

不要让前端依赖：

```text
node_name
edge_name
checkpoint_id
```

这些都应该属于内部实现。

---

## 14. SSE / WebSocket

需要将 LangGraph execution events 转换为 VideoClaw 当前前端事件模型。

例如：

```text
graph_started
node_started
node_completed
task_created
task_running
task_completed
human_review_required
graph_paused
graph_resumed
graph_failed
graph_completed
```

前端继续使用自己的 UI 状态体系。

不要让 LangGraph event schema 直接成为前端公共 API。

---

## 15. 错误恢复

至少实现：

```text
LLM 调用失败
图片生成失败
H3 create 失败
H3 task failed
H3 polling 超时
Asset 写入失败
QA failed
用户中断
服务重启
```

需要区分：

```text
可重试
不可重试
等待用户
需要重新生成
```

对于视频生成：

```text
create_h3_task
```

和：

```text
query_h3_task
```

必须采用不同重试策略。

避免因为网络抖动导致重复创建 H3 视频任务。

---

## 16. 非目标

本阶段不要：

```text
不要引入 OpenHarness
不要重写整个 VideoClaw 前端
不要把所有现有 service 全部 Graph 化
不要删除现有 Pipeline
不要让 LangGraph 取代 VideoClaw Project / Asset / Task 数据模型
不要把视频 / 图片二进制放进 Graph State
不要把 LangGraph checkpoint 当作业务数据库
不要让 HTTP 请求长期阻塞等待 H3
不要第一版实现几十个 Agent
```

---

## 17. 分阶段实施

### Phase 1

只完成：

```text
VideoClaw
+
LangGraph
+
最小主 Graph
+
H3 Skill
+
H3 Adapter
```

Graph：

```text
request
 ↓
script
 ↓
storyboard
 ↓
h3_video
 ↓
result
```

---

### Phase 2

加入：

```text
checkpoint
human review
resume
```

---

### Phase 3

加入：

```text
parallel shots
local retry
QA
automatic prompt revision
```

---

### Phase 4

加入：

```text
复杂多 Agent 协作
导演 Agent
编剧 Agent
分镜 Agent
视觉 Agent
视频 QA Agent
```

---

## 18. 测试要求

需要验证：

```text
单次完整 Graph
Graph 中断
Graph resume
服务重启后 resume
H3 task polling
H3 task failure
单个 Shot retry
并行 Shot
QA fail → prompt revise → regenerate
```

后端测试必须遵循项目现有 Docker 测试/运行环境；不要在宿主机直接运行后端测试。没有可复用测试环境时，先报告环境限制，不自行构建临时测试镜像。

---

## 19. 完成标准

```text
[ ] LangGraph 成为可选的 Workflow Runtime
[ ] VideoClaw 原有业务模型保留
[ ] Project / Asset / Task 仍由 VideoClaw 管理
[ ] Graph State 与业务实体解耦
[ ] H3 Skill 可作为 Prompt 专业知识使用
[ ] H3 Adapter 独立
[ ] H3 异步任务不阻塞 HTTP
[ ] Graph 支持 checkpoint
[ ] Graph 支持 interrupt / resume
[ ] 服务重启后可恢复
[ ] 单 Shot 可以独立失败和重试
[ ] QA 可以触发自动返工
[ ] 前端无需理解 LangGraph 内部结构
[ ] 原有 VideoClaw 能力不回退
```

最终交付时必须说明：

```text
1. 哪些原有 Pipeline 被保留
2. 哪些流程迁移进 LangGraph
3. Graph State 定义
4. Checkpointer 方案
5. Human-in-the-loop 位置
6. H3 Skill / Adapter 如何接入
7. H3 异步任务如何恢复
8. 哪些节点支持并行
9. 哪些节点支持局部重试
10. 测试结果
11. 当前限制
12. 后续多 Agent 扩展点
```
