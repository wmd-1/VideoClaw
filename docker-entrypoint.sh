#!/bin/sh
# Backend container entrypoint.
#
# 1. Make sure a persistent, editable config.yaml exists (under /app/data, which
#    is bind-mounted to the host so API keys survive restarts/redeploys).
# 2. Symlink it to /app/config.yaml (the path the backend reads/writes).
# 3. Hand off to the real command.
#
# 说明：server.host/port 不再由本脚本改写 config.yaml。对齐逻辑已上移到
# backend/config.py 的环境变量覆盖层（BACKEND_HOST/BACKEND_PORT，或显式
# VC_SERVER__HOST/VC_SERVER__PORT，优先级高于文件），因此文件中的注释与
# 自定义结构可完整保留。
set -e

USER_DATA=/app/data
mkdir -p "$USER_DATA"

USER_CFG="$USER_DATA/config.yaml"
if [ ! -f "$USER_CFG" ]; then
  cp /app/config.yaml.example "$USER_CFG"
  echo "[entrypoint] Created $USER_CFG from config.yaml.example."
  echo "[entrypoint] Edit it (API keys, models, ...) then:  docker compose restart backend"
fi

# Point /app/config.yaml at the persisted copy.
ln -sfn "$USER_CFG" /app/config.yaml

echo "[entrypoint] backend listening on ${BACKEND_HOST:-0.0.0.0}:${BACKEND_PORT:-8000} (server.* 由 config.py 环境变量层对齐)"
exec "$@"
