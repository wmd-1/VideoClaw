#!/bin/bash
# 任务 4.1/4.2 HTTP 验证：执行入口 409 预检 + 种子化条目可见 + 无生成请求
set -e
BASE=http://localhost:8000
DIR=/home/wmd/projects/VideoClaw/tmp_verify
PY=python3

curl -s "$BASE/api/config" > "$DIR/pf_before.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "pf_before.json")))["config"]
orig.setdefault("api_providers", {})["api-mock"] = {"protocol": "openai", "base_url": "http://127.0.0.1:18099/v1", "api_key": "", "enable_proxy": False}
models = orig.setdefault("custom_models", [])
models.append({"id": "m-pf-llm", "provider": "api-mock", "model": "LLM1", "types": ["llm"], "abilities": []})
models.append({"id": "m-pf-img", "provider": "api-mock", "model": "IMG1", "types": ["t2i", "i2i"], "abilities": []})
models.append({"id": "m-pf-vid", "provider": "api-mock", "model": "VID1", "types": ["video"], "abilities": ["text_to_video", "first_frame_i2v"]})
json.dump({"values": orig}, open(os.path.join(d, "pf_setup.json"), "w"), ensure_ascii=False)
PY
code=$(curl -s -o "$DIR/pf_setup_resp.json" -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/pf_setup.json")
[ "$code" = "200" ] || { echo "FAIL 注册 $code"; exit 1; }
echo "== 0 ok: 测试模型已注册 =="

SID=$($PY - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
payload = {
    "idea": "预检验证", "style": "realistic", "video_ratio": "16:9", "video_resolution": "720P",
    "expand_idea": False,
    "llm_model": "m-pf-llm", "vlm_model": "m-pf-llm",
    "image_t2i_model": "definitely-unregistered-zzz", "image_it2i_model": "m-pf-img",
    "video_first_frame_model": "m-pf-vid", "video_start_end_model": "m-pf-vid",
    "video_reference_model": "m-pf-vid", "video_generation_mode": "first_frame",
}
req = urllib.request.Request(base + "/api/project/start", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=10))["session_id"])
PY
)
echo "== 1. 未注册模型执行第2阶段 -> 409 =="
MOCK_BEFORE=$(docker exec video-claw-backend /app/.venv/bin/python -c "import urllib.request,json;print(len(json.load(urllib.request.urlopen('http://127.0.0.1:18099/__records'))['records']))")
code=$(curl -s -o "$DIR/pf_409.json" -w "%{http_code}" -X POST "$BASE/api/project/$SID/execute/character_design" -H 'Content-Type: application/json' -d '{}')
$PY - "$DIR/pf_409.json" "$code" <<'PY'
import json, sys
code, body = sys.argv[2], json.load(open(sys.argv[1]))
detail = body.get("detail", {})
assert code == "409", (code, body)
assert detail.get("code") == "model_unavailable" and detail.get("error_code") == "not_registered", detail
assert detail.get("stage") == "character_design" and detail.get("field") == "image_t2i_model", detail
print("== 1 ok:", detail["reason"])
PY
MOCK_AFTER=$(docker exec video-claw-backend /app/.venv/bin/python -c "import urllib.request,json;print(len(json.load(urllib.request.urlopen('http://127.0.0.1:18099/__records'))['records']))")
[ "$MOCK_BEFORE" = "$MOCK_AFTER" ] && echo "== 1b ok: 409 未触发任何生成请求（记录数 $MOCK_AFTER）==" || { echo "FAIL: 409 仍产生请求"; exit 1; }

echo "== 2. 第4阶段同会话 -> 409 且指向同一字段 =="
code=$(curl -s -o "$DIR/pf_ref_409.json" -w "%{http_code}" -X POST "$BASE/api/project/$SID/execute/reference_generation" -H 'Content-Type: application/json' -d '{}')
$PY - "$DIR/pf_ref_409.json" "$code" <<'PY'
import json, sys
code, body = sys.argv[2], json.load(open(sys.argv[1]))
detail = body.get("detail", {})
assert code == "409" and detail.get("stage") == "reference_generation", (code, body)
print("== 2 ok:", detail.get("field"), detail.get("error_code"))
PY

echo "== 3. 内置缺凭据 -> 409 =="
curl -s -X PATCH "$BASE/api/project/$SID/models" -H 'Content-Type: application/json' -d '{"image_t2i_model": "doubao-seedream-5-0-260128"}' > /dev/null
code=$(curl -s -o "$DIR/pf_key_409.json" -w "%{http_code}" -X POST "$BASE/api/project/$SID/execute/character_design" -H 'Content-Type: application/json' -d '{}')
$PY - "$DIR/pf_key_409.json" "$code" <<'PY'
import json, sys
code, body = sys.argv[2], json.load(open(sys.argv[1]))
detail = body.get("detail", {})
assert code == "409" and detail.get("error_code") in ("missing_credentials", "not_registered"), (code, body)
print("== 3 ok:", detail.get("error_code"))
PY

echo "== 4. 种子化条目在 409 后可见 =="
$PY - "$BASE" "$SID" <<'PY'
import json, sys, urllib.request
base, sid = sys.argv[1], sys.argv[2]
# 注入剧本产物（仅用于验证种子化；真实流程由第 1 阶段产生）
payload = {"characters": [
    {"character_id": "char_seed", "name": "种子角色", "description": "验证用"},
], "settings": [{"setting_id": "set_seed", "name": "种子场景", "description": "验证用"}]}
req = urllib.request.Request(base + f"/api/project/{sid}/artifact/script_generation", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="PATCH")
urllib.request.urlopen(req, timeout=10)
# 再次触发预检（仍 409）后检查种子条目已可见
req = urllib.request.Request(base + f"/api/project/{sid}/execute/character_design", data=b"{}", headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=10)
except urllib.error.HTTPError as exc:
    assert exc.code == 409, exc.code
art = json.load(urllib.request.urlopen(base + f"/api/project/{sid}/artifact/character_design", timeout=10))["artifact"]
chars = art.get("characters") or []
assert any(c.get("id") == "char_seed" and c.get("status") == "pending" for c in chars), art
status = json.load(urllib.request.urlopen(base + f"/api/project/{sid}/status", timeout=10))
assert status["status"].get("character_design") == "pending", status["status"]
print("== 4 ok: 种子条目可见且阶段状态保持 pending ==")
PY

echo "== 5. 清理 =="
curl -s -X DELETE "$BASE/api/sessions/$SID" > /dev/null
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "pf_before.json")))["config"]
json.dump({"values": orig}, open(os.path.join(d, "pf_restore.json"), "w"), ensure_ascii=False)
PY
code=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/pf_restore.json")
[ "$code" = "200" ] || { echo "FAIL 恢复 $code"; exit 1; }
curl -s "$BASE/api/config" > "$DIR/pf_after.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
a = json.load(open(os.path.join(d, "pf_before.json")))["config"]
b = json.load(open(os.path.join(d, "pf_after.json")))["config"]
assert a == b, "恢复后配置不一致"
print("== 5 ok: 配置恢复且语义一致 ==")
PY
echo "ALL PASS (preflight & seeding API)"