# Handoff 5：前端构建端口硬编码故障与修复

> 记录 2026-10-13 一次「品牌更名为交心-Video + 换 Logo」部署引入的 API 500 故障：
> 根因、修复、根因治理与留痕更正。后续任何人改动前端构建/端口配置前，先读本文。

---

## 1. 故障事实（已核实，非推断）

- **现象**：前端页面可打开，但所有经前端转发的 `/api/*` 返回 500；SSE 实时推送连不上后端。
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
   - 新增 `build-frontend.sh`：构建入口脚本，自动从仓库根 `.env` 读取 `BACKEND_PORT`（优先级：环境变量 > `.env` > 默认 8000）并传 `--build-arg`；
   - `README.md`：快速开始与「构建与代码更新」节更新，明确**两条构建路径**（镜像 / 宿主机 npm build）都必须保持端口与 `.env` 一致，并说明故障特征（不报错但 API 500）。
3. **镜像漂移清理**：以正确参数重建 `video-claw-frontend:latest`（新镜像 `75fdd2ec9953`，已验证 baked 值 8500 无 8000 残留），`docker compose up -d frontend` 使运行容器从旧镜像 `aca645...` 切到新镜像。
4. **顺手清理**：删除零引用的 `public/logo.jpg`（Logo 已换 `jtyh.png`）与临时脚本 `tmp_verify/png_trim.py`。

## 3. 历史留痕更正

- `655c519`（lint 提交）验证段中"经代理转发后端 /api/stages、/api/models 正常"描述的状态，在 `c2ac17c` 后的故障窗口内**并不成立**，至本次运行时修复后才重新成立。特此更正，供后续检索者知悉。
- `c2ac17c` 提交信息未记录"重建镜像 + 组装产物 + 重启容器"这一引入故障的部署动作；已提交历史不改写，以本文为补充留痕。

## 4. 遗留事项（不属于本次范围）

- 首页提示「backend/config.yaml 缺少主流程默认模型」、创作按钮 disabled：`data/config/config.yaml` 的 `models.video / video_first_frame / video_start_end / video_reference` 四个槽位为空所致，属运行配置，需用户自行在设置页或 `.env` 补齐（已注册可选视频模型 `m3` / `my-model`）。
- 宿主机 `node_modules` 曾被一次失败的 `npm ci`（npm 12 safe-delete 保护中断）损坏，后已从 deps 镜像回填修复；宿主机 npm 12 的 `EALLOWREMOTE` 限制可用 `npm ci --allow-remote=all` 绕过（README L196 已注）。

## 5. 教训（防再犯）

1. **构建期即部署期**：凡"烤进产物"的配置（代理目标、`NEXT_PUBLIC_*`），改 `.env` 后必须重新构建，且构建命令要从 `.env` 读值而不是靠人记——`build-frontend.sh` 即为此存在。
2. **两条构建路径要平等防护**：只给宿主机路径写注意事项不够，任何官方构建路径都应有机制性保障。
3. **端口类故障静默且致命**：前端不报错、后端日志无异常，只有逐层查 baked 产物才能定位；验证部署是否健康要实测 API 状态码，不能只看页面能否打开。
