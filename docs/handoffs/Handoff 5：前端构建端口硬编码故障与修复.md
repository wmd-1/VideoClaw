# Handoff 5：前端构建端口硬编码故障与修复

> 记录 2026-10-13 一次「品牌更名为交心-Video + 换 Logo」部署引入的 API 500 故障：
> 根因、修复与治理、**复现命令与验收标准**、**交接边界**、留痕更正与待办归属。
> 后续任何人改动前端构建/端口配置前，先读本文；需重建验证环境时直接照 §3.2 执行。

---

## 1. 故障事实（区分实测与推断）

- **现象**：前端页面可打开，但所有经前端转发的 `/api/*` 返回 500（实测 `sessions`/`stages`/`models`/`config`/`tasks`/`sandbox` 全 500）。
- **推断项（依据来源标注）**：「SSE 实时推送连不上后端」**未实跑工作流验证**（会真实调用模型、产生费用），依据为产物级静态证据：旧产物客户端 chunk 为
  `t.default.env.NEXT_PUBLIC_BACKEND_PORT||"8000"`（构建期该变量未设 → 未被内联 → 浏览器运行时取 `8000`）；修复后同一处已内联为 `let r="8500"`。后续若要坐实，需在一次真实阶段执行中观察 `EventSource` 连接端口。
- **根因**：构建期端口硬编码与 `.env` 实际端口（`BACKEND_PORT=8500`）不一致，两处值被烤进 standalone 产物：
  1. `Dockerfile.frontend`（旧）`ENV BACKEND_INTERNAL_URL=http://backend:8000` —— 服务端 rewrites 代理目标烤错 → API 全 500；
  2. `lib/workflowApi.ts:17` `process.env.NEXT_PUBLIC_BACKEND_PORT || '8000'` —— 浏览器 SSE 直连兜底端口烤错 → 实时推送失败。
- **引入路径**：`c2ac17c`（品牌更名）后的重建操作直接执行了 `docker build -f Dockerfile.frontend`，未注意到 `.env` 端口为 8500 而非 Dockerfile 硬编码的 8000，产出带错端口的镜像 `3b2fdabacf89` 并组装 `.next-runtime`。
- **关键性质**：standalone 产物在**构建时**固化上述两个值，运行时环境变量无效；故障只能靠带正确端口重新构建修复。
- **为何长期没暴露**：README「快速开始」步骤 2 的宿主机 `npm run build` 路径早有端口注入说明，但**镜像构建路径没有对应防护**，构成单点漏洞。

## 2. 修复与治理（本次已完成）

1. **运行时修复**（先行）：以 `node:22-alpine` 镜像为构建机，构建期注入 `BACKEND_INTERNAL_URL=http://backend:8500` 与 `NEXT_PUBLIC_BACKEND_PORT=8500`，重筑 `.next-runtime` 并 `docker compose restart frontend`。实测：`required-server-files.json` → `http://backend:8500`、chunk 兜底 `8500`、经前端 3500 的 `/api/sessions` `/api/stages` `/api/models` `/api/config` `/api/tasks` 全部 200。
2. **根因治理**（本次提交）：
   - `Dockerfile.frontend`：硬编码改为 `ARG BACKEND_PORT=8000`，由单一参数派生 `BACKEND_INTERNAL_URL` 与 `NEXT_PUBLIC_BACKEND_PORT` 两个 ENV，从结构上杜绝两值不一致；
   - 新增 `build-frontend.sh`：构建入口脚本，自动从仓库根 `.env` 读取 `BACKEND_PORT`（优先级：环境变量 > `.env` > 默认 8000；
     走默认值会告警，彻底去掉兜底属 §7 已立项的 `frontend-build-port-fail-fast`）并传 `--build-arg`；
   - `README.md`：快速开始与「构建与代码更新」节更新，明确**两条构建路径**（镜像 / 宿主机 npm build）都必须保持端口与 `.env` 一致，并说明故障特征（不报错但 API 500）。
3. **镜像漂移清理**：以正确参数重建 `video-claw-frontend:latest`（新镜像 `75fdd2ec9953`，已验证 baked 值 8500 无 8000 残留），`docker compose up -d frontend` 使运行容器从旧镜像 `aca645...` 切到新镜像。
4. **顺手清理**：删除零引用的 `public/logo.jpg`（Logo 已换 `jtyh.png`）与临时脚本 `tmp_verify/png_trim.py`。
   - ✅ 部署态已同步（2026-10-14）：按 §3.2 免网络配方重新构建并**清空式组装** `.next-runtime` 后，
     `curl http://127.0.0.1:3500/logo.jpg` 现返回 **404**，`.next-runtime/public/` 已无该文件（实测）。
5. **验证边界（如实标注）**：未实跑任何阶段执行/生成（避免真实调用模型）；后端本轮零源码改动，
   仅在既有容器内复跑 `ruff check .` → All checks passed；前端门禁已恢复并实测：eslint
   0 errors / 38 warnings、tsc --noEmit exit 0。
6. **实施后的重新部署（2026-10-14）**：`./build-frontend.sh` 产出 `video-claw-frontend:latest` = `d64317b2174a`
   （镜像内 baked 30 处均为 `http://backend:8500`、客户端 `8500` 且无 `"8000"`、`public` 已无 `logo.jpg`）；
   由该镜像导出 standalone 并**清空式组装** `.next-runtime`，再 `docker compose up -d frontend` 使运行容器
   切到新镜像——同时消除了 §2.3 遗留的“容器跑旧镜像 / `:latest` 是新镜像”漂移。验收见 §3.3.2。

## 3. 复现命令与验收标准

### 3.1 复现（故障态）

```bash
# 0) 先确认后端容器在不在（本项目环境已多次出现 backend 静默退出，退出码 127）
#    baked 值全对但 /api 全 500 的真实原因往往就是这个，与端口无关
docker ps --format '{{.Names}}\t{{.Status}}'      # 只剩 frontend 即为容器不在
docker compose up -d backend                       # 复用现有镜像恢复（无需重建）

# 前提：backend 容器已在 .env 指定的 BACKEND_PORT（本机为 8500）监听，前端容器跑的是错端口的产物

# 1) 经前端转发的业务 API 全 500（页面本身能打开，容易误判为“前端正常”）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3500/api/stages     # → 500
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8500/api/health    # → 200（直连正常）

# 2) 从前端容器内探代理目标，确认“拒绝连接 vs 200”
docker compose exec -T frontend sh -lc \
  "wget -q -O - http://backend:8000/api/health 2>&1 | head -2; wget -q -O - http://backend:8500/api/health"  # 前者 Connection refused

# 3) 查 baked 值（产物为单一事实源，运行时环境变量无效）
grep -rhoE 'http://backend:[0-9]+' video-claw/video-claw/frontend/.next-runtime/.next/required-server-files.json | sort | uniq -c
grep -rho 'NEXT_PUBLIC_BACKEND_PORT||"8000"\|let r="8500"' video-claw/video-claw/frontend/.next-runtime/.next/static | sort | uniq -c
```

baked 值的三个藏身处：`.next/required-server-files.json`、`.next/routes-manifest.json`、客户端 `.next/static/chunks/*.js`。

### 3.2 免网络重建配方（本次实际使用，网络抖时唯一可行路径）

背景：宿主机 `npm ci` 本轮连续因 `ETIMEDOUT` / `ENOTFOUND registry.npmjs.org` 失败；`node_modules` 也一度被 npm 12 的 safe-delete 中断损坏。

```bash
REPO=/home/wmd/projects/VideoClaw
FE=$REPO/video-claw/video-claw/frontend

# 1) 复用 Dockerfile.frontend 的 deps 层拿到完整 node_modules（命中缓存则全程不走网络）
docker build --target deps -t vc-fedeps:tmp -f Dockerfile.frontend .

# 2) 以该镜像为构建机：源码只读挂载，`/app/node_modules` 用镜像内的，`.next` 输出回宿主
docker run --rm -e BACKEND_INTERNAL_URL=http://backend:8500 -e NEXT_PUBLIC_BACKEND_PORT=8500 \
  -v $FE/package.json:/app/package.json:ro -v $FE/package-lock.json:/app/package-lock.json:ro \
  -v $FE/next.config.ts:/app/next.config.ts:ro -v $FE/tsconfig.json:/app/tsconfig.json:ro \
  -v $FE/postcss.config.mjs:/app/postcss.config.mjs:ro \
  -v $FE/app:/app/app:ro -v $FE/components:/app/components:ro -v $FE/lib:/app/lib:ro \
  -v $FE/config:/app/config:ro -v $FE/public:/app/public:ro -v $FE/.next:/app/.next \
  vc-fedeps:tmp sh -c "npm run build; chown -R 1000:1000 /app/.next"

# 3) 清空式组装（必须保留目录本身，否则挂载 inode 失效）
cd $FE && mkdir -p .next-runtime && rm -rf .next-runtime/* .next-runtime/.[!.]*
cp -a .next/standalone/. .next-runtime/
mkdir -p .next-runtime/.next/static && cp -a .next/static/. .next-runtime/.next/static/
cp -a public .next-runtime/public
docker compose -f $REPO/docker-compose.yml restart frontend

# 4) 回填宿主机 node_modules（恢复 eslint/tsc/build 门禁）
docker run --rm -v $FE/node_modules:/target vc-fedeps:tmp \
  sh -c "cp -a /app/node_modules/. /target/ && chown -R 1000:1000 /target"
docker rmi vc-fedeps:tmp   # 收尾：不留临时镜像标签
```

### 3.3 验收标准（四条缺一不可）

| # | 验收项 | 命令/方式 | 预期 |
|---|---|---|---|
| 1 | 代理链路 | `curl -w '%{http_code}'` 逐一访问 3500 的 `/api/sessions` `/api/stages` `/api/models?model_type=llm` `/api/config` `/api/tasks` `/api/sandbox/tasks` | 全部 **200** |
| 2 | 服务端 baked | `grep -rhoE 'http://backend:[0-9]+' .next-runtime/.next/required-server-files.json \| sort \| uniq -c` | **只含 `backend:8500`**，无 8000 |
| 3 | 客户端 baked | `grep -rho '8500\|"8000"' .next-runtime/.next/static \| sort \| uniq -c` | 含 `8500`，无 `"8000"` 兜底 |
| 4 | 部署态 UI | 浏览器打开首页并点入一条历史会话 | 无 500 红字、无错误浮层、控制台无 error |

另附（lint 批次 `655c519` 所修缺陷的回归项）：进入 `?session=1790047810282&stage=reference_generation` 与
`&stage=video_generation`（两阶段均 pending 且无产物，即“空产物”路径），控制台不得出现 `Too many re-renders`。

#### 3.3.1 本轮验收结果（2026-10-14，重新组装后全部通过）

| # | 验收项 | 实测结果 |
|---|---|---|
| 1 | 代理链路 | 6 个 `/api/*` 全部 **200** |
| 2 | 服务端 baked | `required-server-files.json` + `routes-manifest.json` 共 30 处，**只含 `http://backend:8500`** |
| 3 | 客户端 baked | 含 `8500`，无 `"8000"` 兜底 |
| 4 | 部署态 UI（浏览器人工） | 首页品牌/Logo 正常、历史卡片 8 张、Network 中 `/api` 无 5xx（仅 2 个 304 缓存）；点入会话「信任的底线」正常渲染角色 3 + 场景 1、6/6 图加载、控制台无 error；`/sandbox` 模型下拉有选项 |
| 附 | C-1 两条深链回归 | `stage=reference_generation` / `stage=video_generation` 均无 `Too many re-renders`，空产物显示“等待上一阶段完成...” |

证据：`tmp_verify/fixverify_01_home.png`、`fixverify_01_home_history.png`、`fixverify_02_session.png`、
`fixverify_03_ref_empty.png`、`fixverify_04_vid_empty.png`、`fixverify_05_sandbox.png`。

当时首页仍常驻红色提示「backend/config.yaml 缺少主流程默认模型」且两个创作按钮 `disabled=true`
（属 §7 的运行配置问题，与本节验收无关）；已于同日按用户决定补齐 `video_first_frame` 后消失，见 §3.3.2。

#### 3.3.2 机制实施后的验收结果（2026-10-14，全部通过）

| 项 | 实测 |
|---|---|
| 有 `.env` | `BACKEND_PORT=8500，来源：.env` + 烤入值两行日志 |
| `BACKEND_PORT=9999` | `来源：环境变量`，烤入值为 `backend:9999` / `9999` |
| `.env` 存在但无该键 | `rc=1` + 6 行指引（修正 pipefail 静默退出后补实测） |
| 无 `.env`（`mv .env` 实跑完整脚本） | `rc=1`、stdout 0 行、stderr 6 行、`video-claw-frontend:latest` ID 未变 |
| 不带 `--build-arg` 直接 `docker build -f Dockerfile.frontend` | 失败（builder 断言），未产生镜像标签 |
| `docker build --target deps` | `rc=0`（不受断言影响，免网络取用仍可用） |
| 代理链路 / baked / 部署态 UI | 6 个 `/api/*` 全 200；新镜像与 runtime 均只含 `backend:8500`；首页红色提示消失、输入创意后两按钮启用、设置页首帧显示 `m3`；两条空产物深链无 `Too many re-renders`（证据 `tmp_verify/apply_01..04*.png`） |

---

## 4. 交接边界

**不动的文件 / 不做的事**

- `data/config/config.yaml`：属用户运行配置（首页按钮 disabled 的真正原因在此，非代码缺陷），Agent 不代填。
- 已提交历史：`c2ac17c` / `655c519` **不改写**，纠偏统一落在本节 §6。
- `docker-compose.yml`：仅顶部注释同步指向 `./build-frontend.sh`（`9fffaaf`），**服务定义/挂载/端口/健康检查零变更**（已逐行核对 diff 仅命中注释行，并 `docker compose config -q` 通过）；compose 仍是 `image:` + `pull_policy: never`，不触发构建。
- 镜像不做 `docker prune`：只清理本次自建临时标签 `vc-fedeps:tmp` 与缓存卷。

**待办归属**

| 项 | 归属 | 状态 |
|---|---|---|
| 构建日志如实标注端口来源 + 默认值告警 + 笔误 | 本次已完成 | ✅ `41dcf94`（三分支均实测） |
| README「唯一入口」措辞 + compose 注释 | 本次已完成 | ✅ `9fffaaf` |
| `logo.jpg` 部署态下线 | 本次已完成 | ✅ 已重新组装，实测 404（见 §2.4） |
| 去掉 8000 默认值、缺失即 `exit 1`、Dockerfile 断言 | 本次已实施（OpenSpec `frontend-build-port-fail-fast`） | ✅ 实测见 §3.3.2 |
| 补齐 `models.video_first_frame` | 用户决定（已选 `m3`） | ✅ 已写入并 restart backend，首页入口已解锁 |
| `video_start_end` / `video_reference` 两槽位 | 用户（切模式时再选；`m3` 能匹配当前模式但不匹配这两种） | ⏸ 不阻塞 |
| 首页文档上传打到 `localhost:8000` | 已实测复现；待另开 change 修复（见 §7） | ⏸ 与本故障无关 |

**产物类边界**：`.next-runtime/`、`.next/`、`node_modules/` 均被 gitignore，**不得作为交接证据源**；要证明部署状态，只引「§3.3 实测输出」。

---

## 5. 与其他工件的关系

- **机制性防护已立项并实施**：上述“默认值不能静默兜底”“绕过脚本的 `docker build` 必须失败”由
  OpenSpec change **`frontend-build-port-fail-fast`** 承接（proposal / specs / design / tasks 齐备，
  `openspec validate` 通过；代码与文档已在本批提交里实施，实测记录见 §3.3.2；验收后可归档）。
  `openspec/` 为本地工件目录、不入版本库，故本文只引用**变更名**、不引用路径行号。
- **OpenSpec `prompt-rewrite-draft-merge-preservation`（活跃）**：其 tasks 2.1/2.2 要求“清草稿的触发条件改为条目
  签名（`id + rewritten_prompt`），事件视图字段变化不得触发”。这与 lint 提交 `655c519` 里 ReferenceStage/VideoStage 的
  `useMemo` 稳定化是同一原则（**渲染期同步的比较键必须稳定**）。实施该 change 时，**不得把 `PromptRewriteStage` 的
  比较键改回数组引用**，否则本文件记录的无限自渲染会复发。
- **lint 两笔提交（`a051bc6` / `655c519`）与本故障无因果**，仅时间相邻；`655c519` 验证段里的部署描述见 §6 更正。
- **机制性防护的归属**：凡“默认值不能静默兜底”这类需求正式固化，按项目分工应走
  OpenSpec（proposal / specs / design / tasks + validate），不应只停留在本 handoff 的待办里——
  已按此方式立 `frontend-build-port-fail-fast`。

---

## 6. 历史留痕更正

- `655c519`（lint 提交）验证段中"经代理转发后端 /api/stages、/api/models 正常"描述的状态，在 `c2ac17c` 后的故障窗口内**并不成立**，至本次运行时修复后才重新成立。特此更正，供后续检索者知悉。
- `c2ac17c` 提交信息未记录"重建镜像 + 组装产物 + 重启容器"这一引入故障的部署动作；已提交历史不改写，以本文为补充留痕。

## 7. 遗留事项与待办

**运行配置（已按用户决定处理）**

- 曾长期提示「backend/config.yaml 缺少主流程默认模型」并使两个创作按钮 disabled：因为
  `data/config/config.yaml` 的 `models.video / video_first_frame / video_start_end / video_reference` 四个槽位全空，
  而 `generation.video_generation_mode: first_frame` 使 `HomePage.tsx:162-169` 只取 `video_first_frame || video`。
- 已处理（2026-10-14）：按用户选择写入 `models.video_first_frame: m3`（`m3` 声明了 `first_frame_i2v`，与当前模式能力匹配），
  `docker compose restart backend` 后 `/api/config` 已回显该值；浏览器实测：红色提示消失、输入创意后
  「逐步创作」「一键生成」`disabled=false`、设置页首帧下拉显示 `m3`。
- 仍未填：`video_start_end` / `video_reference`。故意不填 m3——它没声明 `start_end_frame_i2v` / `reference_to_video`；
  若日后切到这两种模式，同一拦截会再次出现（预期行为），此时可选已注册模型 `my-model`（它声明了这些能力）。
  注：`HomePage.tsx:163-164` 对这两个槽位有内置默认值（`wan2.7-i2v` / `wan2.7-r2v`），空位时不会报“缺少默认模型”。

**机制残留风险（已实施完毕）**

- 来源标注与告警（`41dcf94`）：`PORT_SOURCE` 如实区分环境变量 / .env，修复了原 `${BACKEND_PORT:+环境变量或 .env}` 恒为真的缺陷。
- 彻底去掉兜底（本次实施，OpenSpec `frontend-build-port-fail-fast`）：
  ① `build-frontend.sh` 两来源都取不到时 `exit 1` 并输出 6 行可执行指引（不再回退 8000）；
  ② `Dockerfile.frontend` 改为 `ARG BACKEND_PORT=`（无默认值）+ builder 阶段在任何 `COPY` 之前断言非空，
     因此不带 `--build-arg` 直接 `docker build -f Dockerfile.frontend` 会立即失败；`--target deps` 不受影响。
  实测：有 `.env`→`8500/来源：.env`；`BACKEND_PORT=9999`→`9999/环境变量`；`mv .env` 后跑脚本→`rc=1`、stdout 0 行、
  stderr 6 行指引、`video-claw-frontend:latest` ID 未变（未产生镜像）；不带参直接构建→ BuildKit 在断言层退出并输出该提示、未产生镜像。
- 实施中修正了一个设计假设：design D2 原认为“`grep` 失败被 `cut` 掩盖”，实测**不成立**（`set -o pipefail` 会把
  非零状态传出命令替换，导致脚本在打印指引前就静默退出）。已加 `|| true` 并把结论回写 design.md。

**文档一致性（已完成 `9fffaaf`）**

- ✅ README L235「镜像构建的唯一入口是 `docker build -f Dockerfile.backend / Dockerfile.frontend`」已改为
  “后端 `docker build`、前端 `./build-frontend.sh`，勿绕过脚本”（不绕过脚本正是本次故障的引入路径）。
- ✅ `build-frontend.sh:16`「改了**口令**」→「改了**端口**」。
- ✅ `docker-compose.yml` 顶部过时的 `docker build -f Dockerfile.frontend` 命令已改指向脚本（纯注释）。
- ✅ `logo.jpg` 部署态已下线（实测 404，见 §2.4）。

**同族缺陷（已实测复现，待另开 change 修复）**

- `components/HomePage.tsx:273` 的文档上传（同文件 L426 的 `<input type="file">`，支持 doc/docx/txt/md/pdf）请求
  `${process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000'}/api/upload_file`：
  ① `NEXT_PUBLIC_API_BASE_URL` 全仓从未被设置（唯一引用就是这一行），永远走到 `localhost:8000`；
  ② `next.config.ts` 的 rewrites 只覆盖 `/api/upload_media`，**不含** `/api/upload_file`（而后端路由存在于
     `api/routers/files.py:59`）。
- 实测证据（2026-10-14，两层取证）：
  · 命令行：`POST :8500/api/upload_file` → **200**（后端路由正常）；`POST :8000/api/upload_file` → curl rc=7（连接被拒）；
    `POST :3500/api/upload_file` → **404**，而对照组 `:3500/api/upload_media` → 405（证明 rewrite 机制本身工作，只有该路径未覆盖）；
    宿主 `ss -ltn` 确认 `:8000` 无监听。
  · 浏览器：向该文件控件传入一个临时 txt（`tmp_verify/probe_upload.txt`，一次性探针、已删除，用任意小 txt 可复现）
    实际发出 `POST http://localhost:8000/api/upload_file` →
    `net::ERR_CONNECTION_REFUSED`；控制台 `上传错误: TypeError: Failed to fetch`；页面 alert「上传提取内容失败，请重试」；
    未出现上传成功态。截图 `tmp_verify/apply_02_upload_fail.png`（取证方法：给真实 input 设置文件并派发 `change` 事件，
    与用户选文件走同一段 `handleFileUpload`；为避开阶段式 alert 临时包装了 `window.alert`/`console.error`/`fetch`）。
- 建议修法（归下一个 change，本变更不改前端源码）：与其他接口一致改走相对路径 `/api/upload_file`，
  并在 `next.config.ts` 补上该 rewrite；或至少复用已有的 `DIRECT_API_BASE` 而不是另引一个从未设置过的变量名。

**环境事实（已修，但需知晓）**

- 宿主机 `node_modules` 曾被一次失败的 `npm ci`（npm 12 safe-delete 保护中断）损坏，后已从 deps 镜像回填修复（现 671MB、属主
  1000:1000，`npx eslint`/`npx tsc` 已可正常跑）；宿主机 npm 12 的 `EALLOWREMOTE` 限制可用 `npm ci --allow-remote=all` 绕过
  （README L196 已注）。本轮实测容器内走公网仍有 `ETIMEDOUT` / `ENOTFOUND` 抖动，因此优先用 §3.2 的免网络配方。

## 8. 教训（防再犯）

1. **构建期即部署期**：凡"烤进产物"的配置（代理目标、`NEXT_PUBLIC_*`），改 `.env` 后必须重新构建，且构建命令要从 `.env` 读值而不是靠人记——`build-frontend.sh` 即为此存在。
2. **两条构建路径要平等防护**：只给宿主机路径写注意事项不够，任何官方构建路径都应有机制性保障。
3. **端口类故障静默且致命**：前端不报错、后端日志无异常，只有逐层查 baked 产物才能定位；验证部署是否健康要实测 API 状态码，不能只看页面能否打开。
4. **重建镜像 ≠ 改变线上行为**：compose 把 `.next-runtime` **整目录只读挂载到 `/app`**，镜像自带的产物被完全遮蔽。因此
   “改了前端代码”只需组装 runtime + `restart frontend`，**不需要重建镜像**；反之重建镜像也不会生效。本次误操作（为一次品牌
   改动去 `docker build`）正是踩在这个认知偏差上：动作本身多余，却把错端口产物带上了线。
5. **门禁不可用时不要“降低验收面”**：宿主机 `npm ci` 挂掉后，正确做法是换构建环境（§3.2）把验证跑全，而不是只验页面能打开就
   提交。尤其：仅验静态渲染（标题/Logo）会完全错过 API 代理这类回归，`655c519` 验证段已覆盖的项必须重新跑一遍才算不回归。
6. **500 不等于端口错**：本轮就遇到一次“baked 值全对但 /api 全 500”，真实原因是 backend 容器已以退出码 127 静默离开
   （本环境当日多次出现）。排障顺序应当是 §3.1 的第 0 步：先 `docker ps`，再查 baked 值。
7. **假设不能当验收**：design D2 对 `set -e` / `pipefail` 行为的假设被 tasks 1.3 的实测推翻（脚本会静默退出而不输出指引）。
   凡是“失败路径”的需求，必须用 `echo $?` + 捕获 stderr 实际跑一遍，不能只看语法。
