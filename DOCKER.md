# Video-Claw Docker 部署

用 Docker Compose 一键启动前后端,无需在宿主机安装 Python / Node / ffmpeg。

所有 docker 相关文件都集中在**仓库根目录**(`/root/projects/VideoClaw/`),`backend/`、`frontend/` 源码目录保持干净。

## 目录结构

```
/root/projects/VideoClaw/      <- 仓库根,也是构建上下文 (build context)
├── docker-compose.yml         # 编排:backend + frontend
├── Dockerfile.backend         # 后端镜像
├── Dockerfile.frontend        # 前端镜像
├── docker-entrypoint.sh       # 后端启动前:生成 config、强制 0.0.0.0、软链持久化配置
├── .dockerignore              # 统一忽略规则(排除仓库内无关目录,服务两个 Dockerfile)
├── .env.example               # 可选:覆盖宿主机端口
├── DOCKER.md                  # 本文档
├── video-claw/
│   └── video-claw/
│       ├── backend/           # 后端源码 (FastAPI)
│       └── frontend/          # 前端源码 (Next.js)
└── FilmAgent/  *-pics/  README*.md  LICENSE   # 仓库内其他内容(与构建无关,已被 .dockerignore 排除)
```

Dockerfile 内部用 `COPY video-claw/video-claw/backend/ ...` / `COPY video-claw/video-claw/frontend/ ...` 取源码,所以构建上下文设为仓库根即可。

运行后会在仓库根下生成持久化数据目录 `./data/`(已被 `.gitignore` 忽略)。

## 快速开始

前置条件:已安装 [Docker](https://docs.docker.com/get-docker/)(含 Compose v2)。

```bash
cd /root/projects/VideoClaw     # 仓库根(docker 文件所在)

# 1. 构建并后台启动
docker compose up -d --build

# 2. 查看日志(等健康检查通过)
docker compose logs -f backend

# 3. 访问
#    前端:  http://localhost:3000
#    后端:  http://localhost:8000/api/health
```

首次启动时,后端容器会自动从 `config.yaml.example` 生成一份配置文件到宿主机
`./data/config/config.yaml`,你需要在里面填入 API Key(见下文)。

## 配置 API Key

编辑宿主机上的配置文件(**不要进容器改,改完重启即可**):

```bash
vi ./data/config/config.yaml
# 填入各 provider 的 api_key / access_key 等

docker compose restart backend
```

> 后端运行时也会通过 `POST /api/config` 写回这个文件,所以它始终是唯一配置来源。

文件位置:`./data/config/config.yaml`(容器内通过软链映射到 `/app/config.yaml`)。

## 数据与迁移

所有运行时状态都落在仓库根的 `./data/` 下,**方便整体迁移**:

| 宿主机路径                     | 容器路径       | 内容                              |
| ------------------------------ | -------------- | --------------------------------- |
| `./data/config/config.yaml`    | `/app/config.yaml` | 配置(API Key、模型、生成参数) |
| `./data/code/result/`          | `/app/code/result` | 生成的图片 / 视频 / 脚本       |
| `./data/code/data/sessions/`   | `/app/code/data/sessions` | 会话状态             |
| `./data/code/data/tasks/`      | `/app/code/data/tasks`    | 任务状态             |

**迁移到新机器**:把整个仓库根目录(`/root/projects/VideoClaw/`,含 `./data/`)拷过去,执行 `docker compose up -d --build` 即可,数据与配置不丢失。

## 自定义端口

```bash
cp .env.example .env
# 编辑 .env,例如:
#   FRONTEND_PORT=8080
#   BACKEND_PORT=9000
docker compose up -d
```

## 常用命令

```bash
docker compose up -d --build   # 构建并启动
docker compose ps              # 查看状态
docker compose logs -f         # 跟随日志
docker compose down            # 停止并删除容器(数据保留在 ./data)
docker compose down -v         # 同上,并删除匿名卷(不影响 ./data)
```

## 设计说明

- **docker 文件集中仓库根**:`docker-compose.yml`、两个 `Dockerfile`、`docker-entrypoint.sh`、`.dockerignore` 等都在仓库根,源码目录 `video-claw/video-claw/{backend,frontend}` 不夹杂 docker 文件。构建上下文为仓库根(`context: .`),Dockerfile 内用带前缀的 `COPY` 取源码。
- **精简构建上下文**:`.dockerignore` 排除了仓库根下与构建无关的 `FilmAgent`、`*-pics`、`video-claw/references`、`README*.md`、`LICENSE`、`.git` 等,避免把无关大目录打进 context。
- **前后端通信**:前端 Next.js 通过服务端 rewrite 代理把 `/api/*`、`/code/*` 转发到后端。容器间通过 compose 网络,后端服务名为 `backend`,故前端环境变量 `BACKEND_INTERNAL_URL=http://backend:8000`。本地开发不设该变量时,默认回退到 `http://127.0.0.1:8000`,**与原有行为完全一致**。
- **绑定地址**:后端 `config.yaml` 默认 `server.host: 127.0.0.1`,容器内无法被其他容器访问。`docker-entrypoint.sh` 启动时强制写入 `0.0.0.0`(可用 `BACKEND_HOST` 覆盖),无需改动源码逻辑。
- **ffmpeg / Playwright**:后端镜像已内置 ffmpeg 与 headless Chromium,因此视频拼接、一键 pipeline(用到 playwright)可直接运行,无需联网二次下载。
- **前端镜像**:`next.config.ts` 启用 `output: "standalone"`,多阶段构建后 runner 镜像仅含精简的独立 server,体积更小、启动更快。

## 排查

- 前端能打开但接口 502/超时:检查 `docker compose logs backend`,确认后端健康(`docker compose ps` 中 backend 应为 `healthy`)。
- 生成视频/图片报错:先确认 `./data/config/config.yaml` 中对应 provider 的 Key 已填写、`models.*` 指定了有效模型名。
- 端口冲突:改 `.env` 里的端口后重新 `up -d`。
