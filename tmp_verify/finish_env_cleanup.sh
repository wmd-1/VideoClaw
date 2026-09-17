#!/bin/bash
# 任务 7.3 收尾：补跑 4c 文件原值校验 + 步骤 5 清理恢复
set -e
ROOT=/home/wmd/projects/VideoClaw
BASE=http://localhost:8000
DIR=$ROOT/tmp_verify
PY=python3
CONF=$ROOT/data/config/config.yaml

$PY - "$DIR/config.yaml.snapshot" "$CONF" <<'PY'
import sys, re
snapshot = open(sys.argv[1], encoding="utf-8").read()
current = open(sys.argv[2], encoding="utf-8").read()
def image_t2i(text):
    m = re.search(r"image_t2i:\s*(\S+)", text)
    return m.group(1) if m else None
assert image_t2i(snapshot) == image_t2i(current), (image_t2i(snapshot), image_t2i(current))
print("== 4c ok: 被覆盖字段文件原值未变：image_t2i =", image_t2i(current))
PY

echo "== 5. 清理：恢复 .env -> 重建后端 -> 校验 env 覆盖清空 =="
cp "$DIR/dotenv.backup" "$ROOT/.env"
docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" up -d --force-recreate backend 2>&1 | tail -1
sleep 10
curl -s "$BASE/api/config" > "$DIR/env_after.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
env = json.load(open(f"{d}/env_after.json"))["env_overrides"]
assert env == {"fields": [], "providers": [], "models": []}, env
config = json.load(open(f"{d}/env_after.json"))["config"]
assert "env_srv" not in config["api_providers"], "env 供应商未清除"
print("== 5 ok: 恢复后 env_overrides 为空、env 条目已消失 ==")
PY
docker cp "$DIR/mock_server.py" video-claw-backend:/tmp/mock_server.py
docker exec -d video-claw-backend /app/.venv/bin/python /tmp/mock_server.py
sleep 2
echo "ALL PASS (env injection)"