# Handoff 6：SLB 统一前缀接入与前端文案·入口开关改造

> 覆盖 2026-10-27 ~ 10-28 的一段前端改造：文案更名、Pipeline 入口开关、隐藏联网搜索、
> 取消身份门禁、以及**新增 SLB 统一前缀 `/Bocom_Video_Generation`**（含媒体地址前缀修复）。
> 后端本轮**零源码改动**（另一会话在并行改后端）。
> 任何人要动前端构建、SLB 转发或排查「页面能开但 /api、/code 挂」时，先读本文。

---

## 1. 结论速览

| 主题 | 状态 |
| --- | --- |
| 文案：删 H3/MiniMax-H3、`文艺短视频` → `模板制作`、`数字人口播` → `其他` | 已完成（前端） |
| Pipeline 入口开关（`.env` 控制） | 已完成：模板制作/其他默认 disabled 弹窗，动作迁移默认隐藏 |
| 隐藏所有「联网搜索」按钮 | 已完成 |
| 前端身份门禁取消 | 已完成；门禁版代码暂存 `tmp_identity_backup/` |
| SLB 统一前缀 `/Bocom_Video_Generation` | 已完成（Next `basePath` + 全站路径补前缀） |
| 媒体（`/code/...`）未带前缀导致 404/图片视频不显示 | 已完成（`withBasePathUrl` 归一化绝对地址） |
| 后端容器不可用 | 已解决（`Exited 127` 根因是 Docker/WSL 重启后的陈旧 bind mount，重建容器即恢复；详见 §7.1） |

---

## 2. 关键事实（区分实测与推断）

### 2.1 SLB 必须「保留前缀」，这是唯一自洽方案（实测）

前端按 Next `basePath=/Bocom_Video_Generation` 构建，**页面路由本身也在前缀下**，因此 SLB 不能剥前缀。
用两个本地 nginx 容器（同一上游、只改 SLB 配置）对照实测：

| 请求 | 现状配置（`proxy_pass ...:3500/`，剥前缀） | 改后配置（`proxy_pass ...:3500`，保留前缀） |
| --- | --- | --- |
| `/Bocom_Video_Generation/` | 404 | 308 → 200 |
| `/Bocom_Video_Generation/sandbox` | 404 | 200 |
| `/Bocom_Video_Generation/jtyh.png` | 404 | 200 |
| `/Bocom_Video_Generation/_next/static/chunks/*.js` | 404 | 200 |
| `/Bocom_Video_Generation/api/identity/mode` | 404 | 500（= 已进 rewrite，后端不可用） |
| `/Bocom_Video_Generation/code/result` | 404 | 500（同上） |

所需 SLB 改动（两行 + 三项增强）：

```nginx
location /Bocom_Video_Generation {         # 去掉结尾 /
    proxy_pass http://<部署机>:3500;       # 去掉 proxy_pass 末尾的 /
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $http_host;      # 用 $http_host 保留端口
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;                   # SSE 任务进度实时刷新
    proxy_cache off;
    client_max_body_size 200m;             # 上传剧本/大图不被 413
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
}
# http 级建议加：absolute_redirect off; server_name_in_redirect off; port_in_redirect off;
```

### 2.2 为什么 `Design_Agent_Platform` 能「剥前缀」而我们不行（实测+源码）

- 它是 **Vite SPA**：`base: '/design_agent/'` 只让**资源与链接**带前缀，页面由 `try_files ... /index.html` 在根路径兜底，网关剥前缀后天然匹配（`nginx/gateway.conf`）。
- 我们是 **Next.js**：`basePath` 会同时把**路由**挪到前缀下，剥掉即 404。
- Next 没有「资源带前缀、路由在根」的中间态（`assetPrefix` 只管资源，不管 `<Link>` 与 `/api`）。

### 2.3 404 与 500 的判据（排障第一原则）

- **404** = 路径没打通：SLB 剥了前缀 / 前端产物不是带 basePath 的构建。
- **500** = 路径已到前端 rewrite，但后端不可用：容器没起、服务名解析不到（`ENOTFOUND backend`）、端口错。

### 2.4 后端未起会让「页面跳转很慢」（实测数据）

后端容器不在 compose 网络时，前端 rewrite 的 DNS 解析要重试：

```
500 耗时 12.9s  /api/identity/mode
500 耗时 15.0s  /api/models?model_type=llm
500 耗时 16.9s  /api/sandbox/history
200 耗时 0.004s /sandbox            ← 页面本身秒回
```

浏览器同源只给 6 个并发连接，首页会同时发 `models×5 + tasks + sessions + history`，
侧边栏还每 3 秒轮询 → 连接被挂起请求占满 → 客户端路由（RSC）排队，表现为「点菜单很久才跳」。
**不是前端路由 bug，是后端没起。**

### 2.5 后端会下发「不带前缀的绝对媒体地址」（实测）

`backend/api/routers/sandbox.py`：`return base_url.rstrip("/") + "/code/" + relative`，
配合 `data/` 里实际存的 `http://testserver/code/result/...` 形态，浏览器会拿到
`http://<SLB>/code/result/video/...`（**缺前缀**）。已在前端统一归一化（见 §3.5）。

### 2.6 浏览器侧未验证（如实标注）

本机无浏览器，无头 Chromium 安装被取消两次。因此**没有实测**「真实页面渲染出的 `<video src>` / F12 里的请求 URL」；
验证止步于：函数级单测 + 构建产物内联检查 + 服务端链路（桩后端）实测。补验证方法见 §5 第 8 项。

---

## 3. 改动清单（全部前端）

### 3.1 构建期开关（`next.config.ts` + `lib/pipelineAvailability.ts`）

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `NEXT_PUBLIC_ENABLE_STANDARD` | `false` | 模板制作：未开放时点击弹「该功能暂未开发，敬请期待！」，直连 URL 只看提示 |
| `NEXT_PUBLIC_ENABLE_DIGITAL_HUMAN` | `false` | 「其他」入口（原数字人口播），同上 |
| `NEXT_PUBLIC_HIDE_ACTION_TRANSFER` | `true` | 动作迁移：`true` = 侧边栏不显示；`false` = 正常可用 |
| `NEXT_PUBLIC_BASE_PATH` | `/Bocom_Video_Generation` | 部署前缀；显式留空或 `/` = 根路径部署 |

- `next.config.ts`：注入上述常量（进程环境 > 仓库根 `.env` > 代码默认），并设置 `basePath`；
  `rewrites` 的 source 由 Next 自动加前缀（实测：根 `/api/*` 404、带前缀 `/api/*` 命中）。
- `lib/pipelineAvailability.ts`：`isPipelineAvailable()`（弹窗拦截）+ `isPipelineHidden()`（导航隐藏）。

### 3.2 文案与入口名

- `app/pipelines/standard/page.tsx`：标题 `文艺短视频` → `模板制作`（副标题同步）；
- `app/pipelines/digital-human/page.tsx`：标题 → `其他`，副标题 → `更多创作能力正在开发中，敬请期待`；
- `components/AppShell.tsx`：侧边栏与任务面板标签同步（`模板制作` / `其他`）；
- `config/models.ts`、`components/settings/CustomModelsSection.tsx`、`app/settings/page.tsx`、`components/stages/PromptRewriteStage.tsx`：删除 H3 / MiniMax-H3 / FastH3 字样。

### 3.3 隐藏联网搜索

- `components/HomePage.tsx`：删除按钮与 `web_search` 参数；
- `components/Sandbox/Sandbox.tsx`：删除 LLM 模式的联网搜索开关与 `body.web_search`。

### 3.4 取消前端身份门禁（`lib/identity.tsx`）

- 删除「正在校验身份…」与「请输入登录名」两处拦截 UI，`IdentityProvider` 始终渲染子节点；
- 身份链路保留：`apiFetch` / `X-User-Login-Name` / `localStorage: video-claw.login-name`；
- 无本地登录名时兜底 `FALLBACK_LOGIN = "local"`，保证请求仍带合法身份头；
- `mode` / `me` 接口不可用时不再阻塞；
- 门禁版代码与恢复补丁存于 `tmp_identity_backup/`（README 内有 `git apply` 用法，已实测可正向应用）。
- **恢复门禁时注意**：`app/layout.tsx`、`AppShell` 底部「切换身份」、`app/settings/page.tsx` 的只读提示都还在，
  只有门禁拦截页被移除。

### 3.5 统一前缀接入

- 新增 `lib/basePath.ts`：
  - `BASE_PATH`（构建期常量，默认 `/Bocom_Video_Generation`）
  - `withBasePath(path)`：给站内绝对路径补前缀（幂等）
  - `withBasePathUrl(url)`：**归一化后端下发的绝对地址** —— 路径命中 `/code/`、`/api/` 时丢弃域名、改写成
    同源 + 前缀；外部 CDN、`data:`、`blob:` 原样返回
- `lib/identity.tsx`：`apiFetch` 统一给相对路径补前缀（全站 API 单点收口）；
- `lib/workflowApi.ts`：`DIRECT_API_BASE` 默认改为**同源相对路径**（浏览器不再直连 8500），
  `EventSource` 地址经 `withBasePath`；显式设置 `NEXT_PUBLIC_API_URL` 可回到「直连后端端口」的老形态；
- `components/stages/utils.ts: assetUrl()`、`components/pipelines/PipelinePage.tsx: assetHref()`、
  `components/Sandbox/Sandbox.tsx: toMediaUrl()`：全部改用 `withBasePathUrl`；
  额外把 `result/xxx`、`/result/xxx` 归一为 `/code/result/xxx`（后端静态挂载是 `/code/result`）；
- `components/BrandHeader.tsx`、`components/TopBar.tsx`：logo `/jtyh.png` 补前缀；
- `components/pipelines/PipelinePage.tsx`：模板预览 `iframe src={template.preview_url}` 改为 `assetHref(...)`。

### 3.6 文档

- `.env.example`：新增 `NEXT_PUBLIC_BASE_PATH` 段落，更新 Pipeline 开关说明；
- `README.md`：访问地址改为 `http://<host>:<FRONTEND_PORT>/Bocom_Video_Generation/`，补充前缀与 SLB 说明。

---

## 4. 构建与部署（前缀是构建期常量）

> `basePath`、`NEXT_PUBLIC_*` 全部在 **构建时**固化，运行时改 `.env` 无效。

```bash
cd <仓库>/video-claw/video-claw/frontend
export BACKEND_PORT=$(grep -E '^BACKEND_PORT=' ../../../.env | cut -d= -f2 | tr -d ' ')
BACKEND_INTERNAL_URL=http://backend:${BACKEND_PORT} NEXT_PUBLIC_BACKEND_PORT=${BACKEND_PORT} npm run build
# 清空式组装（保留目录 inode，避免 Docker 挂载失效）
mkdir -p .next-runtime && rm -rf .next-runtime/* .next-runtime/.[!.]*
cp -a .next/standalone/. .next-runtime/
mkdir -p .next-runtime/.next/static && cp -a .next/static/. .next-runtime/.next/static/
cp -a public .next-runtime/public
docker compose restart frontend
```

注意：

- `NEXT_PUBLIC_BASE_PATH` 未配置时默认 `/Bocom_Video_Generation`；要根路径部署必须**显式**设空值（`NEXT_PUBLIC_BASE_PATH=`）或 `/`；
- 迁移服务器上的产物不会自动更新，**必须重新构建**，否则仍是旧行为（典型特征：F12 里出现根路径 `/code/`）；
- 若宿主机用 `npm run build` 时未加 `BACKEND_INTERNAL_URL` / `NEXT_PUBLIC_BACKEND_PORT`，构建会直接失败（既有断言）。

---

## 5. 自检命令（`scripts/selfcheck-deploy.sh`）

```bash
# 本机链路
./scripts/selfcheck-deploy.sh

# 连 SLB 一起查
SLB_BASE=http://12.244.66.225 ./scripts/selfcheck-deploy.sh

# 端口覆盖
FRONTEND_PORT=3500 BACKEND_PORT=8500 ./scripts/selfcheck-deploy.sh
```

检查项与判据：

| # | 检查 | 通过标准 |
| --- | --- | --- |
| 1 | 容器状态 | frontend 运行；backend 运行（附 health） |
| 2 | 后端自身 | `GET :8500/api/health` → 200 |
| 3 | 前端产物 | chunk 内含 `withBasePathUrl` 且内联了前缀字符串 |
| 4 | 直连前端 | `GET /` → 404（basePath 生效）；`GET /Bocom_Video_Generation` → 200/308 |
| 5 | 前端→后端转发 | `/api/identity/mode` 200；`/code/result` 404/403（后端可达） |
| 6 | 前端日志 | 近 15 分钟无 `ENOTFOUND`/`ECONNREFUSED` |
| 7 | 经 SLB | `SLB_BASE/Bocom_Video_Generation` 200/308；其 `/api/identity/mode` 200 |
| 8 | 浏览器侧（手动） | 见下 |

> 两个易误判的点（脚本已处理）：
> 1. `/api/health` **不在**前端 rewrite 白名单（只有 sessions/stages/models/config/tasks/pipelines/sandbox 等），
>    用它判断转发会永远 404 —— 脚本改用 `/api/identity/mode`；
> 2. `/code/result` 会被后端 StaticFiles 307/308 重定向到 `/code/result/`（属可达），脚本按 PASS 处理；
>    日志项只取最近 5 分钟，且当前接口 200 时把窗口内的历史报错降级为 PASS。

浏览器控制台手工确认媒体地址：

```js
performance.getEntriesByType('resource').map(r => r.name)
  .filter(n => n.includes('/code/') || n.includes('/result/'))
// 应全部为 <域名>/Bocom_Video_Generation/code/...；出现根路径 /code/ 即为旧产物
```

本机最近一次执行（2026-10-28，后端恢复后，SLB 分支指向模拟容器 `:8081`）：

```
[PASS] video-claw-frontend 运行中
[PASS] video-claw-backend 运行中（health=healthy）
[PASS] GET :8500/api/health → 200
[PASS] 产物含媒体地址前缀逻辑（withBasePathUrl）
[PASS] 产物中内联了前缀 /Bocom_Video_Generation
[PASS] GET / → 404（符合 basePath 生效）
[PASS] GET /Bocom_Video_Generation → 200
[PASS] GET /Bocom_Video_Generation/api/identity/mode → 200
[PASS] GET /Bocom_Video_Generation/code/result → 307（后端静态挂载点重定向，可达）
[PASS] 日志仅存恢复前的历史报错，当前接口已 200
[PASS] 经 SLB GET /Bocom_Video_Generation → 200
[PASS] 经 SLB GET /Bocom_Video_Generation/api/identity/mode → 200
结果：通过 12，失败 0，跳过 0
```

真实媒体文件验证（后端恢复后，2026-10-28）：

```
数据文件            data/code/result/sandbox/videos/5fdddc97.mp4
经改后 SLB (8081)   200  video/mp4  size=44
直连前端 (3500)     200
旧 SLB 配置 (8080)  404      ← 剥前缀
```

---

## 6. 未验证项与风险

1. **浏览器渲染未实测**（见 §2.6）：媒体地址修复只到函数级+产物级证据。
2. **SSE 经 Next rewrite 转发**：本轮把浏览器直连 8500 改成同源经 rewrite（因为 SLB 环境浏览器到不了后端端口）。
   Next 16 的 rewrite 是否稳定流式透传未实测（后端未起，无法跑工作流）；若出现「进度不动、最后一次性刷新」，
   先查 SLB `proxy_buffering off`，再考虑在 SLB 上把 `/Bocom_Video_Generation/api/*` 直接路由到后端（剥前缀）。
3. **长请求/视频生成**：`/api/sandbox/video` 以前走浏览器直连以绕开代理，现在经 rewrite，超时行为未验证。
4. **Range 请求**：播放器拖进度条依赖 `Range` 透传，链路未实测；若「能播不能拖」，检查 SLB 是否改写请求头。
5. **上传大小**：SLB 默认 `client_max_body_size 1m`，导入剧本/大图会 413，需在 SLB 放开（前端/反代层无法替代）。
6. **根路径部署**：`NEXT_PUBLIC_BASE_PATH=` 构建体已实测可用（`/` 200、`/sandbox` 200），但与当前 SLB 方案互斥。

---

## 7. 遗留待办

1. **后端 127 已解决（2026-10-28）**，根因与镜像无关：
   - `docker inspect video-claw-backend` 的 `State.Error` 显示 `runc create failed ... error mounting ... to rootfs at "/app/__init__.py": not a directory` ——
     Docker Desktop/WSL 重启后，容器记录里的 bind mount 源路径（`/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/<hash>`）已失效，容器在 init 阶段直接失败（127）；
   - 修复：`docker rm -f video-claw-backend` 后 `docker compose up -d postgres minio backend` 重新创建容器即可（宿主上的源码文件本身完好）；
   - `minio` 镜像此前 tag 为固定版本且本地缺失，已改为本地已有的 `minio/minio:latest`（`docker-compose.yml` 由用户修改）；
   - 现状：`backend Up (healthy)`、`/api/health` 200。
   - **同类现象识别**：任何容器 `Exited (127)` 且 `State.Error` 出现 `not a directory` / `no such file or directory` 的挂载错误，
     基本都是 Docker/WSL 重启后的陈旧挂载，重建容器而非重建镜像。
2. **身份门禁**：`tmp_identity_backup/` 是临时目录（未纳入 git），后续要恢复请按其中 README 执行；
   恢复前确认后端身份链路已定稿，否则前端发不出/发错身份头会 401。
3. **本地模拟容器**（用户自测用，**尚未清理**）：
   - `slb-sim-broken` → `0.0.0.0:8080`（复现现状 SLB：剥前缀，404）
   - `slb-sim-fixed` → `0.0.0.0:8081`（修正配置：保留前缀，200）
   - 配置在 `/tmp/slb-configs/`（临时目录，重启机器会丢）；清理：`docker rm -f slb-sim-broken slb-sim-fixed`
4. **前端 `.next-runtime` 需要在目标机重建**：迁移服务器上若没重新构建，所有前缀修复都不生效。
5. **`docker-compose.yml` 未被修改**（曾尝试加 `slb-shim` 前缀补偿层，已按用户要求撤回；当前 3500 直接由 frontend 发布）。

---

## 8. 交接边界

- 本文范围内的一切改动**只在 `video-claw/video-claw/frontend/**`、`.env.example`、`README.md`、`scripts/selfcheck-deploy.sh`**；
  后端源码（另一会话在改）与 `docker-compose.yml`（他人改动）本轮未触碰、也不要基于本文去改。
- 前端所有改动均为**未提交工作区改动**（`git status` 可见 20+ 个 `M` 与 3 个新增 `lib/*.ts(x)`），提交前请与他人改动区分。
- 判定「前缀没生效」的唯一可靠手段是 §5 的第 3、4、5、8 项，不要靠肉眼。
