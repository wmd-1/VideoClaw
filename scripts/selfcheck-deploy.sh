#!/usr/bin/env bash
# Video-Claw 部署自检：SLB 前缀 + 前端产物 + 前后端链路
#
# 用法：
#   ./scripts/selfcheck-deploy.sh                          # 只查本机（前端/后端/容器）
#   SLB_BASE=http://12.244.66.225 ./scripts/selfcheck-deploy.sh   # 额外查经 SLB 的访问
#   FRONTEND_PORT=3500 BACKEND_PORT=8500 ./scripts/selfcheck-deploy.sh   # 覆盖端口
#
# 判据速查：
#   404 = 路径没打通（SLB 剥了前缀 / 前端不是带 basePath 的产物）
#   500 = 路径通了、后端不可用（容器没起 / 主机名解析不到 / 端口错）
#
# 退出码：0 全部通过或有跳过项；1 存在失败项。

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

BASE_PATH="${BASE_PATH:-/Bocom_Video_Generation}"
SLB_BASE="${SLB_BASE:-}"

if [[ -f .env ]]; then
  FRONTEND_PORT="${FRONTEND_PORT:-$(grep -E '^[[:space:]]*FRONTEND_PORT=' .env | tail -1 | cut -d= -f2 | tr -d ' "')}"
  BACKEND_PORT="${BACKEND_PORT:-$(grep -E '^[[:space:]]*BACKEND_PORT=' .env | tail -1 | cut -d= -f2 | tr -d ' "')}"
fi
FRONTEND_PORT="${FRONTEND_PORT:-3500}"
BACKEND_PORT="${BACKEND_PORT:-8500}"

FE="http://127.0.0.1:${FRONTEND_PORT}"
BE="http://127.0.0.1:${BACKEND_PORT}"

pass=0; fail=0; skip=0
ok() { printf '  \033[32m[PASS]\033[0m %s\n' "$1"; pass=$((pass + 1)); }
ng() { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; fail=$((fail + 1)); }
sk() { printf '  \033[33m[SKIP]\033[0m %s\n' "$1"; skip=$((skip + 1)); }
hint() { printf '         ↳ %s\n' "$1"; }
httpcode() { curl -s -o /dev/null -m "${2:-15}" -w '%{http_code}' "$1"; }

echo "仓库根：$ROOT"
echo "前端：$FE  后端：$BE  前缀：$BASE_PATH"
[[ -n "$SLB_BASE" ]] && echo "SLB：$SLB_BASE"
echo

# ────────────────────────────────────────────────────────────────
echo "1) 容器状态"
if docker ps --format '{{.Names}}' | grep -qx 'video-claw-frontend'; then
  ok "video-claw-frontend 运行中"
else
  ng "video-claw-frontend 未运行"
  hint "docker compose up -d frontend（改过前端代码要先重新构建并组装 .next-runtime）"
fi

if docker ps --format '{{.Names}}' | grep -qx 'video-claw-backend'; then
  be_health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' video-claw-backend 2>/dev/null)
  ok "video-claw-backend 运行中（health=${be_health}）"
else
  be_state=$(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}}' video-claw-backend 2>/dev/null || echo '不存在')
  ng "video-claw-backend 未运行（${be_state}）"
  hint "所有 /api/*、/code/* 必然 500（前端 rewrite 目标 backend:${BACKEND_PORT} 不可达）"
  hint "docker compose up -d backend && docker compose logs --tail=50 backend"
fi

# ────────────────────────────────────────────────────────────────
echo
echo "2) 后端自身健康（不经前端）"
be_code=$(httpcode "$BE/api/health" 8)
if [[ "$be_code" == '200' ]]; then
  ok "GET ${BE}/api/health → 200"
elif [[ "$be_code" == '000' ]]; then
  ng "GET ${BE}/api/health → 连接失败"
  hint "后端没在 ${BACKEND_PORT} 监听，或宿主机端口未发布"
else
  ng "GET ${BE}/api/health → ${be_code}"
fi

# ────────────────────────────────────────────────────────────────
echo
echo "3) 前端产物是否为「带前缀」构建（basePath）"
if docker ps --format '{{.Names}}' | grep -qx 'video-claw-frontend'; then
  if docker exec video-claw-frontend sh -c "grep -rqs 'withBasePathUrl' /app/.next/static/chunks" 2>/dev/null; then
    ok "产物含媒体地址前缀逻辑（withBasePathUrl）"
  else
    ng "产物缺少媒体地址前缀逻辑：跑的是旧产物"
    hint "重新 npm run build + 清空式组装 .next-runtime（见 README 快速开始步骤 2）"
  fi
  if docker exec video-claw-frontend sh -c "grep -rqs '${BASE_PATH}' /app/.next/static/chunks" 2>/dev/null; then
    ok "产物中内联了前缀 ${BASE_PATH}"
  else
    ng "产物中找不到 ${BASE_PATH}：basePath 没生效或前缀不一致"
    hint "检查构建时 NEXT_PUBLIC_BASE_PATH（默认 ${BASE_PATH}）"
  fi
else
  sk "frontend 未运行，跳过产物检查"
fi

# ────────────────────────────────────────────────────────────────
echo
echo "4) 直连前端（应带前缀访问）"
root_code=$(httpcode "$FE/")
if [[ "$root_code" == '404' ]]; then
  ok "GET / → 404（符合 basePath 生效）"
elif [[ "$root_code" == '200' ]]; then
  ng "GET / → 200：产物是根路径构建，与 SLB 前缀方案不匹配"
  hint "重新构建时不要设置 NEXT_PUBLIC_BASE_PATH=（空）"
else
  ng "GET / → ${root_code}"
fi

page_code=$(httpcode "$FE${BASE_PATH}")
case "$page_code" in
  200|308) ok "GET ${BASE_PATH} → ${page_code}" ;;
  000) ng "GET ${BASE_PATH} → 连接失败（前端端口 ${FRONTEND_PORT} 未发布？）" ;;
  *) ng "GET ${BASE_PATH} → ${page_code}" ;;
esac

# ────────────────────────────────────────────────────────────────
echo
echo "5) 前端 → 后端转发（rewrite）"
# 注意：/api/health 不在前端 rewrite 白名单（只有 sessions/stages/models/config/tasks/...），
# 用它判断会永远 404；这里用 rewrite 覆盖到的 /api/identity/mode。
api_code=$(httpcode "$FE${BASE_PATH}/api/identity/mode" 30)
case "$api_code" in
  200) ok "GET ${BASE_PATH}/api/identity/mode → 200" ;;
  404) ng "GET ${BASE_PATH}/api/identity/mode → 404：路径没到 rewrite（前缀/SLB 不匹配）" ;;
  500) ng "GET ${BASE_PATH}/api/identity/mode → 500：路径通了但后端不可用" ;;
  *)   ng "GET ${BASE_PATH}/api/identity/mode → ${api_code}" ;;
esac

media_code=$(httpcode "$FE${BASE_PATH}/code/result" 30)
case "$media_code" in
  404) ok "GET ${BASE_PATH}/code/result → 404（后端静态目录可达，无目录索引）" ;;
  403|200) ok "GET ${BASE_PATH}/code/result → ${media_code}（后端可达）" ;;
  307|308) ok "GET ${BASE_PATH}/code/result → ${media_code}（后端静态挂载点重定向，可达）" ;;
  500) ng "GET ${BASE_PATH}/code/result → 500：后端不可达" ;;
  *)   ng "GET ${BASE_PATH}/code/result → ${media_code}" ;;
esac

# ────────────────────────────────────────────────────────────────
echo
echo "6) 前端日志：最近 5 分钟是否有后端不可达"
if docker ps --format '{{.Names}}' | grep -qx 'video-claw-frontend'; then
  log_hit=$(docker logs --since 5m video-claw-frontend 2>&1 | grep -oE 'ENOTFOUND [a-z]+|ECONNREFUSED [0-9.]+:[0-9]+' | sort -u | head -3)
  if [[ -z "$log_hit" ]]; then
    ok "无 ENOTFOUND / ECONNREFUSED"
  elif [[ "$api_code" == '200' ]]; then
    # 后端已恢复时，窗口内可能残留恢复前的报错，按历史记录处理
    ok "存在历史报错（$(echo "$log_hit" | tr '\n' ' ')），但当前接口已 200，按已恢复处理"
  else
    ng "日志出现后端不可达：$(echo "$log_hit" | tr '\n' ' ')"
    hint "ENOTFOUND=服务名解析不到（容器不在同一网络）；ECONNREFUSED=端口没监听"
  fi
else
  sk "frontend 未运行，跳过日志检查"
fi

# ────────────────────────────────────────────────────────────────
echo
echo "7) 经 SLB 访问（可选，需 SLB_BASE）"
if [[ -z "$SLB_BASE" ]]; then
  sk "未设置 SLB_BASE，跳过"
  hint "SLB_BASE=http://12.244.66.225 $0"
else
  slb_code=$(httpcode "${SLB_BASE}${BASE_PATH}" 20)
  case "$slb_code" in
    200|308) ok "GET ${SLB_BASE}${BASE_PATH} → ${slb_code}" ;;
    404) ng "GET ${SLB_BASE}${BASE_PATH} → 404：SLB 把前缀剥掉了"
         hint "location 去掉结尾斜杠，proxy_pass 末尾也不要带 /" ;;
    *)   ng "GET ${SLB_BASE}${BASE_PATH} → ${slb_code}" ;;
  esac
  slb_api=$(httpcode "${SLB_BASE}${BASE_PATH}/api/identity/mode" 30)
  case "$slb_api" in
    200) ok "经 SLB GET ${BASE_PATH}/api/identity/mode → 200" ;;
    404) ng "经 SLB GET ${BASE_PATH}/api/identity/mode → 404：前缀或 location 不匹配" ;;
    500) ng "经 SLB GET ${BASE_PATH}/api/identity/mode → 500：已转发到后端侧，但后端不可用" ;;
    *)   ng "经 SLB GET ${BASE_PATH}/api/identity/mode → ${slb_api}" ;;
  esac
fi

# ────────────────────────────────────────────────────────────────
echo
echo "8) 浏览器侧自检（手动，F12 控制台执行）"
echo "   performance.getEntriesByType('resource').map(r => r.name)"
echo "     .filter(n => n.includes('/code/') || n.includes('/result/'))"
echo "   → 全部应以 ${SLB_BASE:-<你的访问域名>}${BASE_PATH}/code/ 开头；出现根路径 /code/ 即为旧产物。"

echo
echo "─────────────────────────────"
printf '结果：通过 %d，失败 %d，跳过 %d\n' "$pass" "$fail" "$skip"
if (( fail > 0 )); then
  echo "存在失败项，按上面的 ↳ 提示处理。"
  exit 1
fi
echo "全部检查通过。"
