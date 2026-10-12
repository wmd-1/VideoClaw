# Handoff：基于 VideoClaw 集成 MiniMax-H3 Skill + H3 Adapter

## 1. 任务目标

基于当前 `HITsz-TMG/VideoClaw` 代码继续开发，在**不引入 OpenHarness、不引入 LangGraph、不重构现有 Agent/Pipeline 架构**的前提下，将 MiniMax-H3 集成成 VideoClaw 的原生视频生成后端。

本阶段目标：

```text
用户需求
  ↓
VideoClaw 现有 Agent / Pipeline
  ↓
H3 Prompt Skill
  ↓
生成 H3 专用 Prompt / multimodal context
  ↓
H3 Adapter
  ↓
MiniMax-H3 API
  ↓
task_id
  ↓
查询任务状态
  ↓
视频产物
  ↓
回写 VideoClaw 现有 Asset / Task / Project 流程
```

核心原则：

> **Skill 负责“怎么写 H3 Prompt”，Adapter 负责“怎么调用 H3”，VideoClaw 现有 Pipeline 负责“什么时候调用”。**

不要让 H3 Skill 直接承担 API 调用，也不要让 H3 Adapter 承担 Agent 推理逻辑。

---

## 2. 先做代码勘察，不要直接修改

先完整阅读当前仓库实际代码，不要根据 README 猜目录和接口。

重点确认：

```text
video-claw/video-claw/backend/
video-claw/video-claw/frontend/
```

重点定位：

1. 当前主流程 Pipeline 的实现。
2. 剧本、角色/场景、分镜、参考图、视频生成各阶段对应的 service / agent / task。
3. 当前视频模型统一接口或适配层。
4. 当前 `first_frame`、`first_last_frame`、`reference_image` 三种视频生成方式对应的代码。
5. `config.yaml` 中 `api_providers`、`models` 和视频模型配置结构。
6. Task / Asset / Project 的状态与产物保存方式。
7. 前端视频模型下拉框、视频生成方式配置页面和 API。
8. 当前测试入口和项目既有测试约定。

**在任何代码修改前，先输出实施计划，等待用户批准后再修改。**

---

## 3. 集成 H3 Skill

从 MiniMax-H3 官方仓库引入：

```text
skills/h3-prompt-writing/
├── SKILL.md
└── references/
    ├── base-en.txt
    └── ref-en.txt
```

不要直接复制整个 MiniMax-H3 仓库。

Skill 的职责只有：

```text
自然语言需求
+
VideoClaw 当前分镜结构
+
输入素材信息
        ↓
H3 Prompt Compiler
        ↓
H3 专用 Prompt
```

必须按照官方 Skill 的要求，先判断输入模式：

```text
T2VA
I2VA
FL2VA
L2VA
Ref2VA
```

再读取对应 reference 文档生成 Prompt。

不要自己重新发明 H3 Prompt 格式。

对于 Ref2VA，必须支持官方定义的多模态 reference 语义，包括 image / video / audio 等素材，并保持官方要求的字段名、顺序和时间标记。

---

## 4. 增加 H3 Adapter

增加一个独立的 H3 Adapter / Provider，不要把 H3 API 请求散落在 Pipeline、Agent 或 API Route 中。

建议抽象成：

```python
class H3Adapter:
    async def create_video(request) -> H3Task:
        ...

    async def get_task_status(task_id) -> H3TaskStatus:
        ...

    async def download_result(task_id) -> VideoArtifact:
        ...
```

建议定义统一请求模型：

```python
H3VideoRequest(
    mode=...,
    prompt=...,
    inputs=...,
    duration=...,
    resolution=...,
    ratio=...,
)
```

其中 `inputs` 必须能够表达：

```text
text
first_frame
last_frame
reference_image
reference_video
reference_audio
```

以及未来 H3 新增的输入类型。

不要把 H3 原始 API JSON 直接暴露给上层。

---

## 5. H3 API 行为

按照 MiniMax-H3 官方 API 示例实现：

```text
POST /v2/video_generation
        ↓
task_id
        ↓
GET /v2/query/video_generation/{task_id}
        ↓
success
        ↓
task.content.url
        ↓
download MP4
```

错误状态、超时、网络错误、HTTP 非 2xx、模型返回失败均需要统一映射到 VideoClaw 自己的 Task/Asset 错误体系。

**不要在 HTTP request 生命周期里同步阻塞等待整个 H3 视频生成。**

必须复用 VideoClaw 当前任务系统或后台任务机制：

```text
request
  ↓
创建 VideoClaw Task
  ↓
submit H3
  ↓
保存 h3_task_id
  ↓
后台查询
  ↓
success / failed
  ↓
产物保存
```

H3 官方脚本当前也是 `task_id` 后再查询结果，而不是同步返回最终视频。

---

## 6. VideoClaw 模式映射

不要简单把 H3 当成另一个“普通 I2V 模型”。

需要建立能力映射：

```text
VideoClaw 模式
        ↓
H3 capability / mode
```

具体映射必须结合当前 VideoClaw 代码实际语义确认，而不是机械映射名称。

重点处理：

### 首帧生视频

对应 H3 支持的 first-frame 类生成能力。

输入：

```text
first_frame
+
prompt
```

### 首尾帧生视频

对应 H3 first-and-last-frame 能力。

输入：

```text
first_frame
last_frame
+
prompt
```

### 参考图生视频

优先设计成 H3 Ref2VA 能力：
