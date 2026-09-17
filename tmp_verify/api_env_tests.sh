#!/bin/bash
# 任务 7.3 容器内注入端到端验证：.env -> compose env_file -> 后端生效 -> 只读/不写回 -> 清理
set -e
ROOT=/home/wmd/projects/VideoClaw
BASE=http://localhost:8000
DIR=$ROOT/tmp_verify
PY=python3
CONF=$ROOT/data/config/config.yaml

echo "== 0. compose config 校验 + 镜像时间戳（重建前）=="
docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" config -q && echo "compose config ok"
IMG_BEFORE=$(docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' | grep video-claw-backend)
echo "IMG_BEFORE: $IMG_BEFORE"

echo "== 1. 备份 .env 并写入测试 VC_* 变量 =="
cp "$ROOT/.env" "$DIR/dotenv.backup"
cat >> "$ROOT/.env" <<'EOF'

# --- E2E verify (task 7.3, temporary) ---
VC_PROVIDER_ENV_SRV__PROTOCOL=sglang
VC_PROVIDER_ENV_SRV__BASE_URL=http://127.0.0.1:18099/v1
VC_CUSTOM_MODEL_9__ID=m-env-only
VC_CUSTOM_MODEL_9__PROVIDER=env_srv
VC_CUSTOM_MODEL_9__TYPES=t2i
VC_MODEL_IMAGE_T2I=m-env-only
EOF

echo "== 2. 重建后端容器（注入 env_file，不构建镜像）=="
docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" up -d --force-recreate backend 2>&1 | tail -2
sleep 10
docker compose -f "$ROOT/docker-compose.yml" --project-directory "$ROOT" ps backend --format "{{.Status}}"
IMG_AFTER=$(docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' | grep video-claw-backend)
[ "$IMG_BEFORE" = "$IMG_AFTER" ] && echo "== 2b ok: 镜像未被重建 ==" || { echo "FAIL 镜像被重建"; exit 1; }

echo "== 3. 重启容器内 mock 并验证注入结果 =="
docker cp "$DIR/mock_server.py" video-claw-backend:/tmp/mock_server.py
docker exec -d video-claw-backend /app/.venv/bin/python /tmp/mock_server.py
sleep 2
curl -s "$BASE/api/config" > "$DIR/env_get.json"
$PY - "$DIR" "$BASE" <<'PY'
import json, sys, os, urllib.request
d, base = sys.argv[1], sys.argv[2]
data = json.load(open(f"{d}/env_get.json"))
config = data["config"]
env = data["env_overrides"]
assert config["api_providers"].get("env_srv", {}).get("protocol") == "sglang", "env 供应商未生效"
assert config["models"]["image_t2i"] == "m-env-only", "VC_MODEL_* 覆盖未生效"
assert any(m.get("id") == "m-env-only" for m in config.get("custom_models", [])), "env 模型未注册"
assert "api_providers.env_srv.base_url" in env["fields"], env
assert "models.image_t2i" in env["fields"], env
assert "custom_models[m-env-only].types" in env["fields"], env
assert env["providers"] == ["env_srv"] and env["models"] == ["m-env-only"], env
print("== 3 ok: env_overrides =", {k: len(v) for k, v in env.items()})
models = json.load(urllib.request.urlopen(base + "/api/models?model_type=t2i", timeout=10))["models"]
hit = next((m for m in models if m["id"] == "m-env-only"), None)
assert hit and hit.get("provider_label") == "env_srv", models
print("== 3b ok: /api/models 包含 env 模型（provider_label=env_srv）==")
PY

echo "== 4. 保存不写回被覆盖字段 / env-only 条目不写回 =="
cp "$CONF" "$DIR/config.yaml.snapshot"
$PY - "$DIR" "$BASE" <<'PY'
import json, sys, os, urllib.request
d, base = sys.argv[1], sys.argv[2]
data = json.load(open(f"{d}/env_get.json"))["config"]
data["models"]["image_t2i"] = "wan2.7-image"           # 尝试通过 PUT 改写被 env 覆盖的字段
data["api_providers"]["env_srv"]["base_url"] = "http://page-edit:1/v1"   # 篡改 env-only 供应商
req = urllib.request.Request(base + "/api/config", data=json.dumps({"values": data}).encode(), headers={"Content-Type": "application/json"}, method="PUT")
resp = json.load(urllib.request.urlopen(req, timeout=10))
assert resp["config"]["models"]["image_t2i"] == "m-env-only", "PUT 后生效值应仍为 env"
print("== 4 ok: PUT 后生效值仍为 env 覆盖值 ==")
PY
grep -q "env_srv" "$CONF" && { echo "FAIL: env-only 供应商被写回文件"; exit 1; } || echo "== 4b ok: env-only 供应商未写回 config.yaml =="
$PY - "$DIR/config.yaml.snapshot" "$CONF" <<'PY'
import sys, re
snapshot_path, conf_path = sys.argv[1], sys.argv[2]
snapshot = open(snapshot_path, encoding="utf-8").read()
current = open(conf_path, encoding="utf-8").read()
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