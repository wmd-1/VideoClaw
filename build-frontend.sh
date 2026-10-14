#!/usr/bin/env bash
# 构建前端镜像 video-claw-frontend:latest。
#
# 后端端口必须在构建期注入（Dockerfile.frontend 的 ARG BACKEND_PORT），因为
# standalone 产物会把两个值烤进构建输出，运行时环境变量改不了：
#   BACKEND_INTERNAL_URL      —— Next.js rewrites 代理目标（烤入服务端产物），
#                                 端口错了则经前端转发的 /api/* 全部 500
#   NEXT_PUBLIC_BACKEND_PORT  —— 浏览器 SSE 直连兜底端口（烤入客户端 chunk），
#                                 端口错了则实时推送连不上后端
#
# 端口来源（优先级从高到低）：
#   1) 环境变量 BACKEND_PORT（显式覆盖）
#   2) 仓库根 .env 的 BACKEND_PORT
# 两者都取不到时直接失败（exit 1），不再静默回退 8000：“值错但不报错”正是
# docs/handoffs/Handoff 5 记录的事故模式。
#
# 改了端口记得重新构建本镜像；运行时的 .next-runtime 组装另见 README
# 「快速开始」步骤 2（宿主机 npm run build 路径同样需要这两个变量）。
set -euo pipefail

cd "$(dirname "$0")"

PORT_SOURCE="环境变量"
BACKEND_PORT="${BACKEND_PORT:-}"
if [[ -z "$BACKEND_PORT" && -f .env ]]; then
  # `|| true` 必需：set -o pipefail 下 grep 无匹配会把非零状态传出命令替换，
  # 使 set -e 在下方显式空值判断分支之前就静默退出，丢失指引输出
  BACKEND_PORT="$(grep -E '^[[:space:]]*(export[[:space:]]+)?BACKEND_PORT=' .env | tail -1 | cut -d= -f2- || true)"
  # 去掉可能的引号与空白
  BACKEND_PORT="${BACKEND_PORT//[\"\'[:space:]]/}"
  PORT_SOURCE=".env"
fi
if [[ -z "$BACKEND_PORT" ]]; then
  {
    echo "错误：未能从环境变量或仓库根 .env 读到 BACKEND_PORT，已取消构建。"
    echo "修复：在仓库根 .env 里设置 BACKEND_PORT=<后端实际监听端口>（与 docker-compose.yml 一致），"
    echo "      或显式执行 BACKEND_PORT=<端口> ./build-frontend.sh"
    echo "后果：两个端口值会被烤进 standalone 产物，表现为页面正常但经前端转发的"
    echo "      /api/* 全部 500、SSE 连不上后端，且只能靠重新构建修复。"
    echo "参见：docs/handoffs/Handoff 5（同类事故的根因与排障顺序）"
  } >&2
  exit 1
fi

echo "==> 构建 video-claw-frontend:latest（BACKEND_PORT=${BACKEND_PORT}，来源：${PORT_SOURCE}）"
echo "    烤入值：BACKEND_INTERNAL_URL=http://backend:${BACKEND_PORT}、NEXT_PUBLIC_BACKEND_PORT=${BACKEND_PORT}"
exec docker build -f Dockerfile.frontend \
  --build-arg "BACKEND_PORT=${BACKEND_PORT}" \
  -t video-claw-frontend:latest .
