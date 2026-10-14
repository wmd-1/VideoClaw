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
# 端口来源（优先级从高到低，日志会如实标注实际走到的来源）：
#   1) 环境变量 BACKEND_PORT（显式覆盖）
#   2) 仓库根 .env 的 BACKEND_PORT
#   3) 默认 8000（与 Dockerfile.frontend 的 ARG 默认值一致；走到此项会告警，
#      因为它正是 Handoff 5 记录的事故模式：值错但不报错）
#
# 改了端口记得重新构建本镜像；运行时的 .next-runtime 组装另见 README
# 「快速开始」步骤 2（宿主机 npm run build 路径同样需要这两个变量）。
set -euo pipefail

cd "$(dirname "$0")"

PORT_SOURCE="环境变量"
BACKEND_PORT="${BACKEND_PORT:-}"
if [[ -z "$BACKEND_PORT" && -f .env ]]; then
  BACKEND_PORT="$(grep -E '^[[:space:]]*(export[[:space:]]+)?BACKEND_PORT=' .env | tail -1 | cut -d= -f2-)"
  # 去掉可能的引号与空白
  BACKEND_PORT="${BACKEND_PORT//[\"\'[:space:]]/}"
  PORT_SOURCE=".env"
fi
if [[ -z "$BACKEND_PORT" ]]; then
  BACKEND_PORT=8000
  PORT_SOURCE="默认值"
  echo "警告：未从环境变量或 .env 读到 BACKEND_PORT，回退默认 8000。" >&2
  echo "      若后端实际端口不是 8000，两个端口值会被烤进 standalone 产物，" >&2
  echo "      表现为页面正常但经前端转发的 /api/* 全部 500、SSE 连不上后端。" >&2
  echo "      请确认仓库根 .env 的 BACKEND_PORT 与后端监听端口一致后再构建。" >&2
fi

echo "==> 构建 video-claw-frontend:latest（BACKEND_PORT=${BACKEND_PORT}，来源：${PORT_SOURCE}）"
exec docker build -f Dockerfile.frontend \
  --build-arg "BACKEND_PORT=${BACKEND_PORT}" \
  -t video-claw-frontend:latest .
