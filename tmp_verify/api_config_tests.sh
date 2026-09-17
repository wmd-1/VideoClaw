#!/bin/bash
# 任务 1.2 / 1.4 HTTP 验证：配置接口硬校验、env_overrides 结构、保存往返恢复
# 针对项目既有 Docker 后端（localhost:8000），不触碰镜像
set -e
BASE=http://localhost:8000
DIR=/home/wmd/projects/VideoClaw/tmp_verify
CONF=/home/wmd/projects/VideoClaw/data/config/config.yaml
PY=python3

echo "== 0. 初始 GET（语义级备份）=="
curl -s "$BASE/api/config" > "$DIR/before.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
data = json.load(open(os.path.join(d, "before.json")))
assert "config" in data and "env_overrides" in data, "GET 缺少字段"
env = data["env_overrides"]
assert set(env.keys()) == {"fields", "providers", "models"}, env
json.dump(data["config"], open(os.path.join(d, "orig_config.json"), "w"), ensure_ascii=False)
print("== 0 ok: env_overrides 结构 =", env)
PY

echo "== 1. 非法 protocol -> 400 =="
code=$(curl -s -o "$DIR/r1.json" -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' \
  -d '{"values": {"api_providers": {"srv": {"protocol": "bad", "base_url": "http://x/v1"}}, "custom_models": []}}')
$PY - "$DIR/r1.json" "$code" <<'PY'
import json, sys
code, body = sys.argv[2], json.load(open(sys.argv[1]))
assert code == "400", code
assert "protocol" in body.get("detail", ""), body
print("== 1 ok:", body["detail"][:60])
PY

echo "== 2. 缺 provider -> 400 =="
code=$(curl -s -o "$DIR/r2.json" -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' \
  -d '{"values": {"custom_models": [{"id": "m-verify-1", "types": ["llm"]}]}}')
$PY - "$DIR/r2.json" "$code" <<'PY'
import json, sys
code, body = sys.argv[2], json.load(open(sys.argv[1]))
assert code == "400" and "缺少 provider" in body.get("detail", ""), body
print("== 2 ok:", body["detail"][:60])
PY

echo "== 3. 删除被引用供应商 -> 400 且提示模型 id =="
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
config = json.load(open(os.path.join(d, "orig_config.json")))
config.setdefault("custom_models", []).append(
    {"id": "m-verify-1", "name": "", "provider": "srv", "model": "", "types": ["llm"], "abilities": []}
)
config.get("api_providers", {}).pop("srv", None)  # 只留模型、不留供应商
json.dump({"values": config}, open(os.path.join(d, "delete_payload.json"), "w"), ensure_ascii=False)
PY
code=$(curl -s -o "$DIR/r3.json" -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/delete_payload.json")
$PY - "$DIR/r3.json" "$code" <<'PY'
import json, sys
code, body = sys.argv[2], json.load(open(sys.argv[1]))
detail = body.get("detail", "")
assert code == "400" and "引用的供应商不存在" in detail and "m-verify-1" in detail, body
print("== 3 ok:", detail[:80])
PY

echo "== 4. 合法供应商+模型 -> 200 且落盘 =="
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
config = json.load(open(os.path.join(d, "orig_config.json")))
config.setdefault("api_providers", {})["srv"] = {"protocol": "openai", "base_url": "http://127.0.0.1:1/v1", "api_key": "", "enable_proxy": False}
config.setdefault("custom_models", []).append(
    {"id": "m-verify-1", "name": "验证模型", "provider": "srv", "model": "M1", "types": ["llm"], "abilities": []}
)
json.dump({"values": config}, open(os.path.join(d, "valid_payload.json"), "w"), ensure_ascii=False)
PY
code=$(curl -s -o "$DIR/r4.json" -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/valid_payload.json")
$PY - "$DIR/r4.json" "$code" <<'PY'
import json, sys
code, body = sys.argv[2], json.load(open(sys.argv[1]))
assert code == "200", (code, body)
saved = body["config"]
assert any(m.get("id") == "m-verify-1" for m in saved.get("custom_models", [])), saved.get("custom_models")
assert saved["api_providers"]["srv"]["protocol"] == "openai"
print("== 4 ok: 保存响应包含自定义供应商与模型")
PY
grep -q "m-verify-1" "$CONF" && echo "== 4b ok: 配置文件已落盘 m-verify-1 =="

echo "== 5. 恢复原始配置 -> 200 且语义一致 =="
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "orig_config.json")))
json.dump({"values": orig}, open(os.path.join(d, "restore_payload.json"), "w"), ensure_ascii=False)
PY
code=$(curl -s -o "$DIR/r5.json" -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/restore_payload.json")
$PY - "$DIR/r5.json" "$code" <<'PY'
import json, sys
code, body = sys.argv[2], json.load(open(sys.argv[1]))
assert code == "200", (code, body)
print("== 5 ok: 恢复完成")
PY
curl -s "$BASE/api/config" > "$DIR/after.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
before = json.load(open(os.path.join(d, "before.json")))["config"]
after = json.load(open(os.path.join(d, "after.json")))["config"]
assert before == after, "恢复后配置与原始语义不一致"
print("== 5b ok: 恢复后 GET 与初始语义一致")
PY
if grep -q "m-verify-1" "$CONF"; then echo "FAIL: 恢复后文件仍含 m-verify-1"; exit 1; else echo "== 5c ok: 恢复后文件不再含测试模型 =="; fi
echo "ALL PASS (API)"