#!/bin/sh
# Backend container entrypoint.
#
# 1. Make sure a persistent, editable config.yaml exists (under /app/data, which
#    is bind-mounted to the host so API keys survive restarts/redeploys).
# 2. Symlink it to /app/config.yaml (the path the backend reads/writes).
# 3. Force server.host=0.0.0.0 (overridable via BACKEND_HOST) so the service is
#    reachable from the frontend container on the compose network.
# 4. Hand off to the real command.
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

# Rewrite server.host/port so the container is reachable on the network.
# Use the venv interpreter (system python has no PyYAML).
/app/.venv/bin/python - <<'PY'
import os
import yaml

path = "/app/config.yaml"
with open(path, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}

srv = data.setdefault("server", {})
srv["host"] = os.environ.get("BACKEND_HOST", "0.0.0.0")
try:
    srv["port"] = int(os.environ.get("BACKEND_PORT", srv.get("port", 8000)))
except (TypeError, ValueError):
    pass

with open(path, "w", encoding="utf-8") as f:
    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
PY

echo "[entrypoint] backend listening on ${BACKEND_HOST:-0.0.0.0}:${BACKEND_PORT:-8000}"
exec "$@"
