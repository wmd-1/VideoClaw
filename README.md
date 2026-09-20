<!-- 最后更新：2026-09-16 -->

# VideoClaw（AI 创意视频生成系统）

面向创意视频生产的 AI 导演系统：把一句想法 / 一段梗概拆解为可执行影视工作流，覆盖 **剧本策划 → 角色/场景设计 → 分镜规划 → 参考图生成 → 视频生成 → 后期剪辑** 六阶段，每个阶段都有停点确认、可干预修改、可智能续写；同时提供三类一次性 Pipeline（文艺短视频 / 动作迁移 / 数字人口播）与临时工作台（单点模型调用）。

单体仓库：应用代码位于 `video-claw/video-claw/`（FastAPI 后端 + Next.js 前端），`video-claw/` 同时是一套 OpenClaw Agent Skill（`SKILL.md` + `references/`）。

> 作品展示、演示视频与营销向介绍见 [docs/README.md](docs/README.md)。

## 功能总览

| 能力域       | 前端入口                       | 后端入口                                 | 产物                                                  | 说明                                                |
| ------------ | ------------------------------ | ---------------------------------------- | ----------------------------------------------------- | --------------------------------------------------- |
| 六阶段主流程 | `/`                          | `/api/project/**`                      | 剧本 / 角色与场景图 / 分镜 / 参考图 / 视频片段 / 成片 | 阶段间停点确认，支持干预、修改重生成与剧情续写      |
| 临时工作台   | `/sandbox`                   | `/api/sandbox/**`                      | 单次生成结果                                          | 单独调用 LLM / VLM / 文生图 / 图生图 / 视频生成     |
| 文艺短视频   | `/pipelines/standard`        | `/api/pipelines/standard/tasks`        | 图文 / 动态短视频                                     | 文案切分 → 配图 + TTS → HTML 模板渲染 → 合成成片 |
| 动作迁移     | `/pipelines/action-transfer` | `/api/pipelines/action_transfer/tasks` | 动作迁移视频                                          | 参考图 + 动作视频 + 提示词                          |
| 数字人口播   | `/pipelines/digital-human`   | `/api/pipelines/digital_human/tasks`   | 数字人视频                                            | 人物图 + 口播文案，多片段尾帧衔接                   |
| 全局设置     | `/settings`                  | `GET/PUT /api/config`                  | 写回`config.yaml`                                   | 密钥 / 模型 / 生成参数可视化配置                    |
| 多端协作     | —                             | —                                       | —                                                    | OpenClaw Skill、微信 / 飞书消息通道                 |

主流程视频生成支持三种方式（首页 / 顶栏 / 设置页可选择，分别配置模型）：`first_frame` 首帧生视频（默认，最稳定）、`start_end` 首尾帧生视频、`reference` 参考图生视频。

## 技术架构

```
浏览器 ── http://localhost:3000 ──▶ frontend（Next.js standalone, :3000）
        │   server 端 rewrite 同源代理：/api/* 与 /code/* → backend
        ▼
        backend（FastAPI + uvicorn, :8000, 单进程）
        │   ├── /api/project/**    六阶段工作流（start / execute / artifact / intervene / continue / stop）
        │   ├── /api/sandbox/**    临时工作台（llm / vlm / t2i / i2i / video 单次调用）
        │   ├── /api/pipelines/**  一次性 Pipeline（standard / action_transfer / digital_human）
        │   ├── /api/tasks/**      任务查询 + SSE 事件流（进度 / 产物 / 完成）
        │   └── /api/config · /api/models · /api/sessions · /api/health · /code（静态产物）
        ▼
   外部模型 API：DashScope / OpenAI / Gemini / DeepSeek / 火山方舟 ARK / Kling
        ▼
   backend/code/   产物：result/{script,image,video,task}；元数据：data/{sessions,tasks}
```

- **工作流引擎**：`core/orchestrator.py` 管理六阶段状态机与产物累积（`artifacts[stage]`），每阶段执行后返回 `requires_intervention`，前端展示产物并等待用户 `continue` 或 `intervene`（修改后重生成）。
- **Agent 体系**：每个阶段一个 Agent，统一实现 `process(input_data, intervention) -> {"payload", "requires_intervention", "completed"}` 接口（`core/agents/base_agent.py`）。
- **模型接入层**：所有模型在 `models/config_model.py` 的 `MODEL_CONFIG` 注册表统一登记（provider / 能力标签 / 并发 / 价格），前端与 Pipeline 通过 `/api/models?media_type=&ability=` 按能力标签筛选可用模型。
- **Pipeline 引擎**：任务创建即返回 `task_id`，后台 asyncio 执行，进度与产物经进程内事件总线 + SSE（`/api/tasks/{id}/events`）实时推送；SSE 订阅保存在进程内存，**服务须单进程运行**。
- **配置单源**：`config.yaml` 是唯一配置来源——可直接编辑文件，也可由 WebUI 设置页经 `PUT /api/config` 写回；Docker 下该文件持久化在宿主 `./data/config/`。

### 技术栈

| 层   | 技术                                                                                                      |
| ---- | --------------------------------------------------------------------------------------------------------- |
| 后端 | Python 3.9+ / FastAPI / uvicorn（单进程）；uv 管理依赖；Playwright（HTML 模板渲染）+ ffmpeg（音视频处理） |
| 前端 | Next.js 16（App Router，`output: "standalone"`）/ React 19 / Tailwind CSS 4；npm                        |
| 模型 | LLM / VLM / 文生图 / 图生图 / 视频生成多 provider 适配（见「模型接入层」）                                |
| 部署 | Docker 多阶段构建 + Docker Compose；运行时数据落在宿主`./data/`                                         |

## 仓库结构

```
VideoClaw/
├── docker-compose.yml          # 编排 backend + frontend（只运行本地镜像，不构建）
├── Dockerfile.backend          # 后端镜像（uv + ffmpeg + headless Chromium）
├── Dockerfile.frontend         # 前端镜像（多阶段 → standalone runner）
├── docker-entrypoint.sh        # 后端启动前：生成 config、强制 0.0.0.0、软链持久化配置
├── .env.example                # 可选：覆盖宿主机端口（FRONTEND_PORT / BACKEND_PORT）
├── docs/                       # 展示向 README（README.md / README_EN.md：作品集与演示）
├── video-claw-pics/  FilmAgent-pics/   # 展示图与论文配图
├── FilmAgent/                  # 系列工作（SIGGRAPH Asia 2024 论文代码）
└── video-claw/                 # OpenClaw Agent Skill 根目录
    ├── SKILL.md                # skill 正文：停点表与工作流规则（共 7 个停点）
    ├── references/             # OpenClaw 集成参考文档（workflow / sandbox / pipelines / run_project / send_message）
    └── video-claw/
        ├── backend/            # FastAPI 后端（:8000）
        │   ├── api_server.py   # 入口：uvicorn.run
        │   ├── config.py       # 配置加载与目录常量（CODE_DIR / RESULT_DIR / ...）
        │   ├── session.py      # 会话 JSON 持久化（SessionManager）
        │   ├── api/            # 路由 / Schema / 服务（app.py 组装 FastAPI 实例）
        │   │   └── routers/    # workflow · sandbox · pipelines · configuration · files · sessions · stages · health
        │   ├── core/           # orchestrator.py（工作流引擎）+ agents/（各阶段 Agent）
        │   ├── models/         # 模型注册表 + LLM/VLM/图像/视频调用客户端
        │   ├── pipelines/      # 一次性 Pipeline（standard / action_transfer / digital_human + runner/storage/events）
        │   ├── prompts/        # 提示词模板（按阶段分类）+ loader.py
        │   ├── templates/      # 解说短视频 HTML 模板（1080x1920 / 1920x1080 / 1080x1080）
        │   ├── docs/           # api.md（接口文档）+ session_format.md（会话格式）
        │   └── code/           # 数据与产物（result/ + data/）
        └── frontend/           # Next.js 前端（:3000）
            ├── app/            # 路由页：/ · /sandbox · /settings · /pipelines/*
            ├── components/     # stages/（六阶段 UI）· Sandbox/ · pipelines/ · 布局组件
            ├── lib/            # workflowApi.ts（REST 客户端）· modelRegistry.ts
            ├── config/         # models.ts · examples.ts
            └── next.config.ts  # server 端 rewrite 代理 + standalone 输出
```

## 核心模块详解

### 后端 · 六阶段工作流引擎

`core/orchestrator.py` 定义阶段枚举与顺序：

| 阶段（phase）            | Agent                   | 实现文件                            | 职责                                       |
| ------------------------ | ----------------------- | ----------------------------------- | ------------------------------------------ |
| `script_generation`    | ScriptWriterAgent       | `core/agents/script_agent.py`     | 剧本 / 分集生成、续写判断                  |
| `character_design`     | CharacterDesignerAgent  | `core/agents/character_agent.py`  | 角色与场景特征提取、参考原画生成与优选     |
| `storyboard`           | StoryboardAgent         | `core/agents/storyboard_agent.py` | 分镜拆解（镜头视角 / 动作描述 / 参考内容） |
| `reference_generation` | ReferenceGeneratorAgent | `core/agents/reference_agent.py`  | 分镜参考图生成、评估与优选                 |
| `video_generation`     | VideoDirectorAgent      | `core/agents/video_agent.py`      | 分镜图 → 视频片段（三种生成方式）         |
| `post_production`      | VideoEditorAgent        | `core/agents/editor_agent.py`     | 片段拼接、字幕与成片导出                   |
| （辅助）                 | DoctorAgent             | `core/agents/doctor_agent.py`     | 提示词诊断与改写                           |

- 会话状态持久化到 `code/data/sessions/{session_id}.json`（Session ID 为毫秒级时间戳），格式见 `backend/docs/session_format.md`。
- OpenClaw 侧的停点约束（每个阶段必须展示产物并等待确认）定义在 `video-claw/SKILL.md`。

### 后端 · 模型接入层

`models/` 下每个 provider 一个适配客户端，公共基类统一重试与响应解析：

| 能力 | 客户端                                                                                         | 支持 provider                                        |
| ---- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| LLM  | `llm_client.py` + `llm_{dashscope,deepseek,gpt,gemini}.py`                                 | DashScope / DeepSeek / OpenAI / Gemini               |
| VLM  | `vlm_client.py` + `vlm_{dashscope,gpt,gemini}.py`                                          | DashScope / OpenAI / Gemini                          |
| 图像 | `image_client.py` + `image_{dashscope,gpt,seedream}.py`（+ `image_processor.py` 后处理） | DashScope（通义万相）/ OpenAI / 火山方舟（Seedream） |
| 视频 | `video_client.py` + `video_{dashscope,kling,seedance}.py`                                  | DashScope（Wan）/ Kling / 火山方舟（Seedance）       |

- 注册表 `models/config_model.py::MODEL_CONFIG` 登记每个模型的 `provider / type（能力标签）/ concurrency / price`，是模型清单的权威来源。
- 支持**自定义供应商与模型**：`api_providers` 下新增供应商（`protocol` = `openai` / `vllm-omni` / `sglang`），并在 `custom_models` 中通过 `provider` 引用注册模型（LLM/VLM/文生图/图生图/视频）；注册表查询时合并自定义条目，客户端路由「注册表优先」（未注册模型直接报错而非落入默认供应商）。适配层见 `models/custom_{common,llm,image,video}.py`：图像支持 `b64_json`/URL/二进制三种响应，视频为异步任务（创建→轮询→下载，multipart 主形态、零鉴权兼容、超时上限与尽力取消）。
- 连通性测试：`POST /api/models/test`（接受未保存草稿，不持久化配置，媒体类会发起一次真实生成）；设置页「自定义供应商/自定义模型」提供逐类型测试按钮。
- `/api/models` 支持按 `media_type`（image/video）与 `ability`（text_to_image、image_to_video、reference_image、action_transfer、digital_human 等）筛选，前端与 Agent 均以此选择模型。

### 后端 · Pipeline 引擎

| 模块       | 文件                                                     | 职责                                                                                                                         |
| ---------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 注册与调度 | `pipelines/runner.py`                                  | `PIPELINE_REGISTRY`（standard / quick_create / action_transfer / digital_human）→ 后台执行、标记 running/completed/failed |
| 文艺短视频 | `pipelines/standard.py`                                | 文案切分 → 配图 + TTS → HTML 模板渲染（Playwright）→ 图片拼接 / 动态视频合成                                              |
| 动作迁移   | `pipelines/action_transfer.py`                         | 参考图 + 动作视频 + 提示词 → 动作迁移模型调用                                                                               |
| 数字人口播 | `pipelines/digital_human.py`                           | 人物图 + 口播文案 → 分句语音 + 多片段视频（尾帧衔接）+ 音轨替换                                                             |
| 任务存储   | `pipelines/storage.py`                                 | 任务元数据`code/data/tasks/{task_id}.json`（status / progress / input / output / artifacts）与产物目录维护                 |
| 事件推送   | `pipelines/events.py`                                  | 进程内发布订阅 → SSE（snapshot / progress / artifact / completed / failed，含心跳）                                         |
| 公共工具   | `pipelines/utils.py` · `api_media.py` · `tts.py` | 模板渲染、媒体封装、语音合成等                                                                                               |

### 后端 · 提示词与模板

- `prompts/` 按用途分类：`script` / `character` / `setting` / `storyboard` / `reference` / `video` / `style` / `doctor` / `pipelines`，配合 `prompts/loader.py`（支持 `_zh` / `_en` 语言回退）加载。
- `templates/` 为解说类短视频的 HTML 模板（按画幅 1080x1920 / 1920x1080 / 1080x1080 与风格区分），`/api/pipelines/standard/templates` 提供列表与预览接口。

### 后端 · API 层

`api/app.py` 组装 FastAPI 实例：CORS 全开、`/code` 静态挂载产物目录，并注册 8 个路由：

| 路由器        | 文件                         | 主要端点                                                                                                                                           | 职责                            |
| ------------- | ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| workflow      | `routers/workflow.py`      | `POST /api/project/start`；`execute/{stage}`；`status`；`artifact/{stage}`（+`upload_image`）；`intervene`；`continue`；`stop`     | 六阶段工作流                    |
| sandbox       | `routers/sandbox.py`       | `POST /api/sandbox/{llm,vlm,t2i,i2i,video}`；`GET /api/sandbox/history`                                                                        | 临时工作台                      |
| pipelines     | `routers/pipelines.py`     | `POST /api/pipelines/{standard,action_transfer,digital_human}/tasks`；`GET /api/tasks`；`GET /api/tasks/{id}/events`（SSE）；模板列表 / 预览 | Pipeline 与任务                 |
| configuration | `routers/configuration.py` | `GET/PUT /api/config`                                                                                                                            | 配置读写（写回`config.yaml`） |
| files         | `routers/files.py`         | `POST /api/upload_file` · `/api/upload_media`；`DELETE /api/cache/temp`                                                                     | 文件上传与缓存清理              |
| sessions      | `routers/sessions.py`      | `GET/DELETE /api/sessions`                                                                                                                       | 会话管理                        |
| stages        | `routers/stages.py`        | `GET /api/stages`                                                                                                                                | 阶段元数据                      |
| health        | `routers/health.py`        | `GET /api/health`                                                                                                                                | 健康检查                        |

完整接口文档见 `backend/docs/api.md` 与 `video-claw/references/`。

### 前端

- 路由（App Router）：`/` 主流程（首页项目列表 / 新建 / 生成配置）、`/sandbox` 临时工作台、`/settings` 设置页、`/pipelines/{standard,action-transfer,digital-human}` 三类 Pipeline。
- 组件：`components/stages/` 对应六阶段的交互 UI（ScriptStage / CharacterStage / StoryboardStage / ReferenceStage / VideoStage / PostProductionStage，及 StageProgress / StageActions / ImageLightbox / RewriteResultBadge）；`components/Sandbox/`、`components/pipelines/PipelinePage.tsx`。
- 数据层：`lib/workflowApi.ts` 封装工作流 REST 调用；`lib/modelRegistry.ts` + `config/models.ts` 维护模型清单与能力标签展示。
- 代理：`next.config.ts` 通过 server 端 rewrites 把 `/api/*`、`/code/*` 同源转发到 `BACKEND_INTERNAL_URL`（默认 `http://127.0.0.1:8000`；Docker 内为 `http://backend:8000`）。

### 多端集成

- **OpenClaw Skill**：`video-claw/SKILL.md` 定义 7 个停点（停点 0-6：项目规划 → 模型配置 → 剧本 → 角色/场景 → 分镜 → 参考图 → 视频；后期剪辑无需确认），规则要求每个阶段展示产物并等待用户确认；`references/` 提供 init / workflow / sandbox / pipelines / send_message 各类调用文档。
- **微信 / 飞书**：消息交互说明见 `video-claw/references/send_message/{wechat,feishu}.md`。

## 快速开始（Docker，推荐）

宿主机只需 Docker（含 Compose v2）+ Node 22+（仅用于构建前端运行产物）。后端镜像已内置 Python / ffmpeg / headless Chromium。

```bash
cd <仓库根>

# 1) 构建镜像（首次；compose 只运行镜像、不构建镜像）
docker build -f Dockerfile.backend  -t video-claw-backend:latest  .
docker build -f Dockerfile.frontend -t video-claw-frontend:latest .

# 2) 构建前端运行产物并组装（首次必须执行；改前端代码后重复此步）
cd video-claw/video-claw/frontend
npm ci                                    # npm 12+ 报 EALLOWREMOTE 时改用 npm ci --allow-remote=all
BACKEND_INTERNAL_URL=http://backend:8000 NEXT_PUBLIC_BACKEND_PORT=${BACKEND_PORT:-8000} npm run build
rm -rf .next-runtime && mkdir .next-runtime
cp -a .next/standalone/. .next-runtime/
mkdir -p .next-runtime/.next/static && cp -a .next/static/. .next-runtime/.next/static/
cp -a public .next-runtime/public

# 3) 启动
cd <仓库根>
docker compose up -d
docker compose logs -f backend            # 等健康检查通过
```

- 前端：[http://localhost:3000](http://localhost:3000)；后端健康检查：[http://localhost:8000/api/health](http://localhost:8000/api/health)
- 首次启动自动在宿主生成 `./data/config/config.yaml`，填入 API Key 后 `docker compose restart backend`（也可在 WebUI「设置」页填写）。

## 本地开发（不构建镜像）

```bash
# 后端（:8000）
cd video-claw/video-claw/backend
uv sync                       # 或 python -m venv venv + pip install -r requirements.txt
cp config.yaml.example config.yaml     # 填入 API Key
uv run python api_server.py

# 前端（:3000，新终端；代理默认回退 http://127.0.0.1:8000）
cd video-claw/video-claw/frontend
npm install
npm run dev                   # 开发模式；生产模式为 npm run build && npm start
```

也可使用一键安装脚本：`cd video-claw/video-claw && ./install.sh`（Windows 为 `install.bat`）。

## 构建与代码更新（Docker）

- **镜像构建的唯一入口**是 `docker build -f Dockerfile.backend / Dockerfile.frontend`（compose 服务只有 `image:` + `pull_policy: never`，`docker compose up -d` 不构建、不拉取）；迁移机器可用 `docker save` / `docker load`。
- **改后端代码免重建**：`docker-compose.yml` 将后端源码逐项只读（`:ro`）挂载进容器 `/app`（不能整目录挂载，会遮蔽镜像内的 `.venv`、`docker-entrypoint.sh`、`config.yaml` 软链等）。改完执行 `docker compose restart backend` 生效；依赖文件（`pyproject.toml` / `uv.lock`）不挂载，改依赖需重建镜像。
- **改前端代码免重建**：前端是 standalone 编译产物，按「快速开始」步骤 2 重新构建并组装 `.next-runtime/` 后 `docker compose restart frontend` 生效。compose 以 `create_host_path: false` 只读挂载该目录——未构建时 `up` 会直接报错而不是静默建空目录。
- **注意**：standalone 产物在**构建时**固化 rewrites 代理地址，运行时环境变量不生效，因此宿主机构建必须带 `BACKEND_INTERNAL_URL=http://backend:8000`（`Dockerfile.frontend` 中已内置该变量）。

## 配置说明

配置单源为 `config.yaml`（Docker 下位于宿主 `./data/config/config.yaml`，容器内经软链映射为 `/app/config.yaml`），也可在 WebUI「设置」页修改后自动写回：

| 段落              | 关键字段                                                                                                               | 说明                                                                                             |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `server`        | `host` / `port` / `log_level` / `access_log`                                                                   | 服务绑定与日志；启动参数改动需重启后端生效（Docker 下`host` 由 entrypoint 强制为 `0.0.0.0`） |
| `api_providers` | `common.proxy`；各 provider 的 `api_key` / `base_url` / `enable_proxy`                                         | 平台密钥与代理；`enable_proxy` 控制该 provider 是否走公共代理                                  |
| `models`        | `llm` / `vlm` / `image_t2i` / `image_it2i` / `video_first_frame` / `video_start_end` / `video_reference` | 主流程默认模型；Pipeline 在各自页面单独选模型，不读该默认值                                      |
| `custom_models` | `id` / `provider` / `model` / `types` / `abilities` / `concurrency` | 自定义模型注册列表（引用 `api_providers` 中的自定义供应商）；`concurrency` 缺省 1（本地单卡服务最保守） |
| `generation`    | `style` / `video_ratio` / `video_resolution` / `video_generation_mode`                                         | 主流程默认生成参数（`video_generation_mode` 默认 `first_frame`）                             |

配置来源优先级：**进程环境变量 > `.env` 文件 > `config.yaml` > 默认值**（字段级覆盖）。`.env`（`cp .env.example .env`）支持三类覆盖：

- `VC_PROVIDER_<名称>__PROTOCOL|BASE_URL|API_KEY|ENABLE_PROXY`（内置/自定义供应商通用，供应商名大写、中划线转下划线）；
- `VC_MODEL_LLM|VLM|IMAGE_T2I|IMAGE_IT2I|VIDEO_FIRST_FRAME|VIDEO_START_END|VIDEO_REFERENCE`；
- `VC_CUSTOM_MODEL_<序号>__ID|PROVIDER|MODEL|NAME|TYPES|ABILITIES|CONCURRENCY`（列表字段逗号分隔；与 `config.yaml` 中同 `id` 条目逐字段合并，.env 优先）。

被 `.env` 覆盖的字段在设置页**只读**并标注「来自 .env」，保存设置不会将其写回；修改 `.env` 后执行 `docker compose up -d backend`（重建容器）生效。

> 前端浏览器直连（SSE）的后端端口在**构建期**由 `NEXT_PUBLIC_BACKEND_PORT` 固化，需与 `.env` 的 `BACKEND_PORT` 一致（构建命令已自动读取）；修改端口后需重新构建前端产物。
> 设置页已不再展示 API Server / Common / 内置供应商密钥，统一通过上述 `.env` 变量或 `config.yaml` 配置。

相关环境变量：

| 变量                                 | 使用方                                   | 说明                                                                           |
| ------------------------------------ | ---------------------------------------- | ------------------------------------------------------------------------------ |
| `BACKEND_HOST` / `BACKEND_PORT`  | backend 容器（entrypoint）               | 覆盖写入`server.host` / `server.port`，默认 `0.0.0.0:8000`               |
| `FRONTEND_PORT` / `BACKEND_PORT` | 宿主`.env`（`cp .env.example .env`） | compose 发布到宿主机的端口，默认 3000 / 8000                                   |
| `BACKEND_INTERNAL_URL`             | 前端（构建期 + 运行期）                  | 代理目标；本地默认`http://127.0.0.1:8000`，Docker 内 `http://backend:8000` |
| `VC_PROVIDER_*` / `VC_MODEL_*` / `VC_CUSTOM_MODEL_*` | backend（compose `env_file` 注入；本地直跑读仓库根/backends 下 `.env`） | 后端配置覆盖层（供应商/默认模型/自定义模型），优先级高于 `config.yaml`，详见上方「配置说明」 |
| `VC_SERVER__*` / `VC_COMMON__*` | backend（同上） | API Server（host/port/log_level/access_log）与 Common（proxy/print_model_input）配置；未配置时 `server.port` 自动对齐 `.env` 的 `BACKEND_PORT` |
| `NEXT_PUBLIC_BACKEND_PORT` | 前端（构建期） | 浏览器直连后端的端口（SSE），需与 `.env` 的 `BACKEND_PORT` 一致；缺省 8000 |

## 服务拓扑与端口

| 服务         | 端口                          | 说明                                                                            |
| ------------ | ----------------------------- | ------------------------------------------------------------------------------- |
| frontend     | 3000:3000                     | Next.js standalone server；server 端 rewrite 代理`/api/*`、`/code/*`        |
| backend      | 8000:8000（容器内不额外发布） | FastAPI + uvicorn，单进程；`/code` 静态托管产物                               |
| 外部模型 API | 443                           | DashScope / OpenAI / Gemini / DeepSeek / 火山方舟 ARK / Kling（按所选模型访问） |

## 数据与产物

Docker 下所有运行时状态落在宿主 `./data/`（迁移机器时整体拷贝即可）；本地开发直接落在 `backend/code/`：

| 宿主路径                       | 容器路径                    | 内容                                                               |
| ------------------------------ | --------------------------- | ------------------------------------------------------------------ |
| `./data/config/config.yaml`  | `/app/config.yaml`        | 配置（API Key、模型、生成参数）                                    |
| `./data/code/result/`        | `/app/code/result`        | 生成产物：`script/`、`image/`、`video/`、`task/<task_id>/` |
| `./data/code/data/sessions/` | `/app/code/data/sessions` | 主流程会话元数据`<session_id>.json`                              |
| `./data/code/data/tasks/`    | `/app/code/data/tasks`    | Pipeline 任务元数据`<task_id>.json`                              |

- **Session ID**：毫秒级时间戳；**Task ID**：`YYYYMMDD_HHMMSS_随机Hash`。
- Pipeline 产物按任务隔离在 `result/task/<task_id>/`（分段音频 / 视频、故事板 JSON、`final.mp4` 等）；上传的媒体文件经 `/api/upload_media` 落到任务目录后被引用。

## 测试与校验

- 后端：无独立单测套件；以 `GET /api/health` + `GET /api/models` 做启动冒烟，实际功能冒烟走 `/sandbox` 或 Pipeline 页面。
- 前端：`npm run lint`（eslint）；`npm run build`（含 TypeScript 检查，同时产出 standalone 产物用于部署）。

## 说明

- **单进程约束**：SSE 订阅（`pipelines/events.py`）与运行中任务状态保存在进程内存，后端必须单进程运行；不要以多 worker 方式启动。
- **对话与产物留存**：会话与任务均为 JSON 元数据 + 磁盘产物，服务重启不丢；`DELETE /api/tasks/{id}` 会同步删除元数据与产物目录。
- **Docker 设计**：compose 只运行本地镜像（`pull_policy: never`，缺失即报错）；`/app` 内源码只读挂载与镜像运行时文件（`.venv` 等）共存，依赖环境始终来自镜像。
- 展示向 README（作品集 / 演示视频）见 [docs/README.md](docs/README.md)；工程文档入口：`backend/docs/api.md`、`video-claw/SKILL.md`、`video-claw/references/`。
