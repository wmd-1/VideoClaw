# Handoff 6：SLB 统一前缀接入与前端文案·入口开关改造

> 覆盖 2026-10-27 ~ 10-28 的一段前端改造：文案更名、Pipeline 入口开关、隐藏联网搜索、
> 身份门禁（先取消、后恢复、最终改为**身份缺失阻断页**）、以及**新增 SLB 统一前缀 `/Bocom_Video_Generation`**（含媒体地址前缀修复）。
> 后端本轮**零源码改动**（另一会话在并行改后端）。
> 任何人要动前端构建、SLB 转发或排查「页面能开但 /api、/code 挂」时，先读本文。
> 2026-10-28 增量（交通银行示例、Sandbox 预览收口、门禁改为身份缺失阻断页、后端会话交接要点）见 §9。

---

## 1. 结论速览

| 主题                                                                               | 状态                                                                                                              |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| 文案：删 H3/MiniMax-H3、`文艺短视频` → `模板制作`、`数字人口播` → `其他` | 已完成（前端）                                                                                                    |
| Pipeline 入口开关（`.env` 控制）                                                 | 已完成：模板制作/其他默认 disabled 弹窗，动作迁移默认隐藏                                                         |
| 隐藏所有「联网搜索」按钮                                                           | 已完成                                                                                                            |
| 首页灵感示例（交通银行）                                                           | 已完成：6 张卡片改为交行主题（介绍交通银行 / 惠民贷 / 手机银行 / 信用卡 / 数字人民币 / 绿色金融），输入框占位同步 |
| 前端身份门禁                                                                       | **已改为身份缺失阻断页**（2026-10-28）：不再手工输入登录名；无身份时显示“未获取到用户身份”；身份接口统一带部署前缀 |
| Sandbox URL 预览媒体                                                               | 已完成：图片/音频预览改走 `withBasePathUrl`（后端绝对地址剥域名补前缀，外部 CDN 原样）                          |
| SLB 统一前缀 `/Bocom_Video_Generation`                                           | 已完成（Next `basePath` + 全站路径补前缀）                                                                      |
| 媒体（`/code/...`）未带前缀导致 404/图片视频不显示                               | 已完成（`withBasePathUrl` 归一化绝对地址）；媒体改由后端 owner-checked 流式路由，`/code/` 追加 `user_login` |
| 后端容器不可用                                                                     | 已解决（`Exited 127` 根因是 Docker/WSL 重启后的陈旧 bind mount，重建容器即恢复；详见 §7.1）                    |

---

## 2. 关键事实（区分实测与推断）

### 2.1 SLB 必须「保留前缀」，这是唯一自洽方案（实测）

前端按 Next `basePath=/Bocom_Video_Generation` 构建，**页面路由本身也在前缀下**，因此 SLB 不能剥前缀。
用两个本地 nginx 容器（同一上游、只改 SLB 配置）对照实测：

| 请求                                                 | 现状配置（`proxy_pass ...:3500/`，剥前缀） | 改后配置（`proxy_pass ...:3500`，保留前缀） |
| ---------------------------------------------------- | -------------------------------------------- | --------------------------------------------- |
| `/Bocom_Video_Generation/`                         | 404                                          | 308 → 200                                    |
| `/Bocom_Video_Generation/sandbox`                  | 404                                          | 200                                           |
| `/Bocom_Video_Generation/jtyh.png`                 | 404                                          | 200                                           |
| `/Bocom_Video_Generation/_next/static/chunks/*.js` | 404                                          | 200                                           |
| `/Bocom_Video_Generation/api/identity/mode`        | 404                                          | 500（= 已进 rewrite，后端不可用）             |
| `/Bocom_Video_Generation/code/result`              | 404                                          | 500（同上）                                   |

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

### 2.6 浏览器侧验证（2026-10-28 已补齐）

早前本机无浏览器，验证只到函数级 + 产物内联 + 服务端链路。2026-10-28 用 IDE 内置浏览器补齐，结论：

| 检查                     | 实测结果                                                                                                  |
| ------------------------ | --------------------------------------------------------------------------------------------------------- |
| 参考图阶段 12 张图       | 全部 `http://<host>:3500/Bocom_Video_Generation/code/result/image/...` → 200                           |
| 视频路径对照             | 带前缀 `/Bocom_Video_Generation/code/result/video/...` → 200；缺前缀 `/code/result/video/...` → 404 |
| `performance` 全量资源 | 91 条请求全部带前缀，`resourceMissingPrefix` 为空                                                       |
| Sandbox URL 预览         | 站内 `http://host:port/code/...` 被改写为 `/Bocom_Video_Generation/code/...`；外部 CDN 保持原样       |
| 身份门禁                 | `mode` 200（带前缀）；清除 `video-claw.login-name` 后显示“未获取到用户身份”阻断页，无 input/form；`single` 仍直接 `local` |

取证截图在 `tmp_verify/`（`mc_*`、`sb_fix_*`、`identity_gate_clear_ls_check.png`）。

---

## 3. 改动清单（全部前端）

### 3.1 构建期开关（`next.config.ts` + `lib/pipelineAvailability.ts`）

| 变量                                 | 默认                        | 作用                                                                      |
| ------------------------------------ | --------------------------- | ------------------------------------------------------------------------- |
| `NEXT_PUBLIC_ENABLE_STANDARD`      | `false`                   | 模板制作：未开放时点击弹「该功能暂未开发，敬请期待！」，直连 URL 只看提示 |
| `NEXT_PUBLIC_ENABLE_DIGITAL_HUMAN` | `false`                   | 「其他」入口（原数字人口播），同上                                        |
| `NEXT_PUBLIC_HIDE_ACTION_TRANSFER` | `true`                    | 动作迁移：`true` = 侧边栏不显示；`false` = 正常可用                   |
| `NEXT_PUBLIC_BASE_PATH`            | `/Bocom_Video_Generation` | 部署前缀；显式留空或 `/` = 根路径部署                                   |

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

### 3.4 身份门禁（`lib/identityGuard.tsx`）：2026-10-28 改为身份缺失阻断页

**当前状态（2026-10-28）**：不再显示登录名输入表单。未获取到身份时挂载
`components/IdentityMissing.tsx`，白色空白页居中显示：

- 标题：`未获取到用户身份`
- 提示：`请从GUWP门户重新进入本平台。若反复出现此提示，请联系系统管理员处理。`
- 操作：`重试`（清理 `localStorage: video-claw.login-name` 后刷新）

**身份来源优先级**：URL `?User=` / `?user=` → `localStorage: video-claw.login-name` →
GUIP SDK `globalInfo()` / `promise.globalInfo()`；以上都没有时仍请求 `/api/identity/me`，
兼容 GUIP/反代在请求上直接注入 `X-User-Login-Name`。`single` 模式仍直接视为 `local` 管理员。

**有效入口**：`app/layout.tsx` 已改为从 `lib/identityGuard.tsx` 引入 `IdentityProvider`；
`apiFetch`、`mode`、`me`、`logout` 均经该模块并保留 `withBasePath` 前缀适配。
旧 `lib/identity.tsx` 已收敛为兼容 re-export（仅防旧引用失效），**不要把门禁逻辑加回旧文件**。

**历史坑（仍有效）**：不要整文件覆盖成 10-27 的旧门禁版；旧版 `fetch("/api/identity/mode")`
走根路径，前缀环境下 404，后端返回 HTML，前端会报
`Unexpected token '<', "<!DOCTYPE "... is not valid JSON`。

`tmp_identity_backup/` 仅作历史备份；本次未使用。`AppShell` 底部仍显示当前身份并提供
`切换身份`，`app/settings/page.tsx` 的只读提示保持不变。

### 3.5 统一前缀接入

- 新增 `lib/basePath.ts`：
  - `BASE_PATH`（构建期常量，默认 `/Bocom_Video_Generation`）
  - `withBasePath(path)`：给站内绝对路径补前缀（幂等）
  - `withBasePathUrl(url)`：**归一化后端下发的绝对地址** —— 路径命中 `/code/`、`/api/` 时丢弃域名、改写成
    同源 + 前缀；`/code/` 媒体自动追加 `user_login`（取 localStorage 身份）；外部 CDN、`data:`、`blob:` 原样返回
- `lib/identityGuard.tsx`：`apiFetch` 统一给相对路径补前缀（全站 API 单点收口）；旧 `lib/identity.tsx` 为兼容 re-export；
- `lib/workflowApi.ts`：`DIRECT_API_BASE` 默认改为**同源相对路径**（浏览器不再直连 8500），
  `EventSource` 地址经 `withBasePath`；显式设置 `NEXT_PUBLIC_API_URL` 可回到「直连后端端口」的老形态；
- `components/stages/utils.ts: assetUrl()`、`components/pipelines/PipelinePage.tsx: assetHref()`、
  `components/Sandbox/Sandbox.tsx: toMediaUrl()`：全部改用 `withBasePathUrl`；
  额外把 `result/xxx`、`/result/xxx` 归一为 `/code/result/xxx`（媒体路由是 `/code/result/{path}`）；
- `components/BrandHeader.tsx`、`components/TopBar.tsx`：logo `/jtyh.png` 补前缀；
- `components/pipelines/PipelinePage.tsx`：模板预览 `iframe src={template.preview_url}` 改为 `assetHref(...)`；
- `components/Sandbox/Sandbox.tsx`（2026-10-28 补漏）：URL 模式的**图片/音频预览**原先是 `<img src={value}>`
  / `<audio src={value}>` 裸用用户粘贴的地址，后端绝对地址（含 `/code/`）会缺前缀；现统一走
  `withBasePathUrl(value)`。上传预览 `previewUrl` 是本地 `blob:`，保持原样；
- `lib/identityGuard.tsx`（2026-10-28）：`apiFetch`、`mode`、`me`、`logout` 全部补前缀；无身份显示阻断页（见 §3.4）。

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

| # | 检查             | 通过标准                                                                      |
| - | ---------------- | ----------------------------------------------------------------------------- |
| 1 | 容器状态         | frontend 运行；backend 运行（附 health）                                      |
| 2 | 后端自身         | `GET :8500/api/health` → 200                                               |
| 3 | 前端产物         | chunk 内含 `withBasePathUrl` 且内联了前缀字符串                             |
| 4 | 直连前端         | `GET /` → 404（basePath 生效）；`GET /Bocom_Video_Generation` → 200/308 |
| 5 | 前端→后端转发   | `/api/identity/mode` 200；`/code/result` 307（Next 规范化）或直连后端 401/404 |
| 6 | 前端日志         | 近 15 分钟无 `ENOTFOUND`/`ECONNREFUSED`                                   |
| 7 | 经 SLB           | `SLB_BASE/Bocom_Video_Generation` 200/308；其 `/api/identity/mode` 200    |
| 8 | 浏览器侧（手动） | 见下                                                                          |

> 两个易误判的点（脚本已处理）：
>
> 1. `/api/health` **不在**前端 rewrite 白名单（只有 sessions/stages/models/config/tasks/pipelines/sandbox 等），
>    用它判断转发会永远 404 —— 脚本改用 `/api/identity/mode`；
> 2. `/code/result` 现在命中媒体路由，缺 `user_login` 返回 401（属后端可达），脚本按 PASS 处理；
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
[PASS] GET /Bocom_Video_Generation/code/result → 307（Next 规范化；直连后端为 401，可达）
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

1. ~~**浏览器渲染未实测**~~ **已补验（2026-10-28）**：媒体地址、外部地址透传、门禁登录均已在真实浏览器实测，见 §2.6。
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
2. **身份门禁：已改为身份缺失阻断页（2026-10-28）**：无手工输入；无身份时显示
   “未获取到用户身份”，身份来源为 URL `?User=` → localStorage → GUIP SDK → `/me` 请求头。
   有效入口为 `lib/identityGuard.tsx`；旧 `lib/identity.tsx` 仅为兼容 re-export。
   当前后端 `mode=guip`、`/api/identity/me` 正常；若目标机后端尚未上线身份接口，应先设 `VC_USERS__MODE=single`。
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
- 提交分区（2026-10-28）：`0fb2530 feat(frontend): 接入 SLB 统一前缀并收敛文案与入口开关` 已提交
  （前缀接入、入口开关、文案更名、隐藏联网搜索、首页交行示例、Sandbox 预览收口、本 handoff / `scripts/selfcheck-deploy.sh` / README / .env.example 片段）；
  **未提交**：`lib/identityGuard.tsx`、`components/IdentityMissing.tsx`、`lib/identity.tsx`（兼容 re-export）与相关导入调整；本文档后续修订。
- 后端源码、`docker-compose.yml`、`docs/designs/`、`tmp_identity_backup/` 与另一会话的改动共存于工作区，**不在前端提交范围**。
- 判定「前缀没生效」的唯一可靠手段是 §5 的第 3、4、5、8 项，不要靠肉眼。

---

## 9. 2026-10-28 增量与后端会话交接要点

### 9.1 本次又改了什么（相对 §3）

| 改动                   | 文件                                                | 要点                                                                                                                                                                                            |
| ---------------------- | --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 首页灵感示例改交行主题 | `config/examples.ts`、`components/HomePage.tsx` | 6 张卡片：介绍交通银行 / 惠民贷 / 手机银行 / 信用卡优惠 / 数字人民币 / 绿色金融；点击写入输入框；占位改为「描述你的视频创意... 例如：介绍交通银行」                                             |
| Sandbox URL 预览收口   | `components/Sandbox/Sandbox.tsx`                  | `<img src={value}>` / `<audio src={value}>` → `withBasePathUrl(value)`；特意不用 `toMediaUrl`（否则外部 CDN 会被拼成 `/code/result/https://...`）；`previewUrl` 是 `.blob:` 不改 |
| 身份门禁改阻断页       | `lib/identityGuard.tsx`、`components/IdentityMissing.tsx` | 不再手工输入；无身份显示“未获取到用户身份”；身份接口全部带前缀；细节见 §3.4 |
| 迁移包刷新             | `frontend-latest.tar.gz`（仓库根，未入库）        | 本轮未重新打包；运行时 `BUILD_ID=r5coNlCMsekfYGXzYptTC`、`basePath=/Bocom_Video_Generation` |

### 9.2 当前构建与实测状态

- `BUILD_ID = r5coNlCMsekfYGXzYptTC`；`.next-runtime` 已清空式重组装，`docker compose restart frontend` 已生效；
- `GET /` → 404、`GET /Bocom_Video_Generation` → 200、`/api/identity/mode` → 200（均带前缀）；
- 浏览器：清除 `video-claw.login-name` 后显示“未获取到用户身份”阻断页，无 input/form；截图 `tmp_verify/identity_gate_clear_ls_check.png`；
- 前端域名访问会从 `127.0.0.1` 重写为 `localhost`（Next 行为），不影响前缀与身份头。

### 9.3 给后端会话：必须知道的契约（前端已改，后端无需为此改代码）

1. **浏览器不再直连后端端口**：`/api/*`、SSE（`EventSource`）与 `/code/*` 全部经前端同源 + 部署前缀转发。
   后端不需要为前端开 CORS；保留也无害。
2. **`next.config.ts` 的 rewrites 是硬编码白名单**：后端新增路由（如 `/api/identity/*`）必须同步加进 rewrite，
   否则前端请求会命中 Next 自己的 404 页（返回 HTML，前端 `resp.json()` 报 `Unexpected token '<'`）。
   本轮 `identity` 就是这个坑。新增接口时请告知前端，或评估合并为 `/api/:path*` 统一兜底。
3. **媒体不再由静态目录直出**：后端 `GET/HEAD /code/result/{path}?user_login=` 做 owner 校验并流式返回；
   前端把 `result/xxx`、`/result/xxx` 统一归一为 `/code/result/xxx` 并追加 `user_login`。
4. **媒体地址建议下相对路径**：若下绝对地址，只有 `pathname` 以 `/code/` 或 `/api/` 开头才会被改写为「同源 + 前缀」，
   其它域名（如推理服务内网地址）会原样透传，浏览器可能不可达。
5. **身份接口契约（前端已按此实现）**：
   - `GET /api/identity/mode` → `{mode: "guip"|"single", allow_url_user: bool}`；
   - `GET /api/identity/me`（可带 `X-User-Login-Name`；缺失/无效回 401）→ `{mode, login, is_admin}`；
   - `POST /api/identity/logout` 会被调用（失败不阻断）。
     前端门禁：`guip` 无身份 → “未获取到用户身份”阻断页；`single` 直接视为 `local` 管理员。
6. **上线顺序**：若目标机后端尚未部署 `identity` 路由，用户会看到身份阻断页；
   可选：先上后端，或临时 `VC_USERS__MODE=single`。
7. **本轮实测发现的后端问题（与本改造无关，供排查）**：主流程视频阶段 12 段全部失败，报
   `missing_input_image`，期望 `/app/code/result/image/<session>/Scenes/seg_01_01.jpg`，
   而盘上只有 `seg_01_01_upload_v1.png`（上传版与生成版命名/回退策略不一致）。
8. **流式与 Range**：后端本地/MinIO 的 GET/HEAD、206/416 已用 TestClient + 本机 curl 验证；SSE 经 Next rewrite、
   浏览器拖动进度条尚未实测。若出现「能播不能拖」或「进度最后一次性刷新」，先查 SLB 的 `proxy_buffering off` 与 Range 头透传。

### 9.4 媒体网关收口（2026-10-28）

- 新增 `api/routers/media.py`：`GET/HEAD /code/result/{path}?user_login=`；缺失/非法 401，owner 不匹配 404；
  `download=1` 返回 attachment；其他为 inline。
- 本地文件与 MinIO 对象统一流式输出；支持 `Range` 206、非法 Range 416、`Content-Range/Accept-Ranges/Content-Length`；
  MinIO `stat/open_stream` 走线程池，流结束关闭句柄。
- 已移除 `StaticFiles` 匿名挂载；`/code/data/**` 保持 404。媒体 URL 由 `withBasePathUrl` 自动追加 `user_login`。
- 已验证：本地/MinIO 全量 200、Range 206、坏 Range 416、HEAD、下载 disposition、错误用户 404；
  `missing_at` 在文件缺失时写入，实时按 session/stage count。
- 已补 saved-media：`GET /api/space/saved/{saved_id}/media?user_login=` 无 API 头，按 saved_items owner 校验，从收藏桶流式返回；
  `/api/space/saved` 的 media.url 为不带 login 的相对路径，由 `savedMediaUrl()` 追加。

### 9.5 个人空间（2026-10-28）

- 新增 `api/routers/space.py`：`/api/space/works`、`/counts`、`/saved`、`POST/DELETE /api/space/saved`；
  works 聚合 session post_production + completed pipeline task，owner 过滤，分页与 counts 同口径。
- 新增 `/space` 页面：作品/收藏 tab、计数、预览、下载、收藏/取消；
  **模块 tab 与 `pipelineAvailability` 共用构建期开关：隐藏或未开放（模板制作/动作迁移/其他）不渲染**；
  “全部”也过滤掉隐藏域作品。
- `/api/space/*` 已加入 Next rewrite；媒体使用 `mediaUrl`/`savedMediaUrl`，不再裸拼 `/code/`。
- 后端已测：works/counts、session/task 聚合、幂等收藏、超限 409、取消 404、源删除后 saved-media 200/206、owner 不匹配 404。

### 9.6 风险与上线口径

- 媒体 `?user_login=` 是**已接受的冒读缺口**：任何能访问媒体网关的人改 query 即可读取他人媒体；不得写成“安全隔离通过”。审计日志需能关联 query login、IP、路径和结果码。
- `X-User-Login-Name` 是 GUIP/反代信任输入，不是认证凭据；API 自报头伪造同为 accepted risk，不伪装成身份认证。
- 旧 `?mt=` 与裸 `/code/...` 链接：无 `user_login` 返回 401，`/code/data/**` 返回 404；旧书签需要重新从页面打开。
- 存量单机无 GUIP：上线前先设 `VC_USERS__MODE=single`；GUIP 就绪后再切 `guip`。`config.yaml` 改动后 restart，`.env` 改动后 `docker compose up -d`。
- 保存收藏前确认 `videoclaw-saved` 桶存在；saved-media 与源 artifact 分离，源会话删除不删除收藏副本。

### 9.7 复现命令

```bash
cd <repo>/video-claw/video-claw/frontend
export BACKEND_PORT=$(grep -E '^BACKEND_PORT=' ../../../.env | cut -d= -f2 | tr -d ' ')
BACKEND_INTERNAL_URL=http://backend:${BACKEND_PORT} NEXT_PUBLIC_BACKEND_PORT=${BACKEND_PORT} npm run build
mkdir -p .next-runtime && rm -rf .next-runtime/* .next-runtime/.[!.]*
cp -a .next/standalone/. .next-runtime/
mkdir -p .next-runtime/.next/static && cp -a .next/static/. .next-runtime/.next/static/
cp -a public .next-runtime/public
cd <repo> && docker compose restart frontend
./scripts/selfcheck-deploy.sh
SLB_BASE=http://12.244.66.225 ./scripts/selfcheck-deploy.sh   # 连 SLB 一起查
```
