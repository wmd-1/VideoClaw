#!/bin/bash
# 任务 1.5 HTTP 验证：注册自定义供应商+模型后，/api/models 各筛选包含自定义模型且带 provider_label
set -e
BASE=http://localhost:8000
DIR=/home/wmd/projects/VideoClaw/tmp_verify
PY=python3

curl -s "$BASE/api/config" > "$DIR/models_before.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "models_before.json")))["config"]
orig.setdefault("api_providers", {})["srv-verify"] = {
    "protocol": "vllm-omni", "base_url": "http://127.0.0.1:8091/v1", "api_key": "", "enable_proxy": False,
    "name": "验证供应商",
}
models = orig.setdefault("custom_models", [])
models.append({"id": "m-verify-t2i", "name": "验证文生图", "provider": "srv-verify", "model": "Qwen-Image", "types": ["t2i", "i2i"], "abilities": []})
models.append({"id": "m-verify-video", "name": "验证视频", "provider": "srv-verify", "model": "", "types": ["video"], "abilities": ["text_to_video", "first_frame_i2v"], "concurrency": 2})
models.append({"id": "m-verify-llm", "name": "验证LLM", "provider": "srv-verify", "model": "Qwen3", "types": ["llm"], "abilities": []})
json.dump({"values": orig}, open(os.path.join(d, "models_payload.json"), "w"), ensure_ascii=False)
PY
code=$(curl -s -o "$DIR/models_put.json" -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/models_payload.json")
[ "$code" = "200" ] || { echo "FAIL: 注册自定义模型失败 $code"; cat "$DIR/models_put.json"; exit 1; }
echo "== 注册成功（srv-verify + 3 个模型）=="

check_models() {
  local url="$1" expect_id="$2" label="$3"
  curl -s "$url" > "$DIR/models_q.json"
  $PY - "$DIR/models_q.json" "$expect_id" "$label" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
expect_id, label = sys.argv[2], sys.argv[3]
models = data.get("models", [])
hit = next((m for m in models if m.get("id") == expect_id), None)
assert hit is not None, f"{label}: 未包含 {expect_id}"
assert hit.get("provider_label") == "验证供应商", f"{label}: provider_label 缺失或错误 -> {hit.get('provider_label')}"
print(f"== {label} ok: {expect_id} provider_label=验证供应商")
PY
}

check_models "$BASE/api/models?model_type=t2i" "m-verify-t2i" "model_type=t2i"
check_models "$BASE/api/models?model_type=i2i" "m-verify-t2i" "model_type=i2i"
check_models "$BASE/api/models?model_type=llm" "m-verify-llm" "model_type=llm"
check_models "$BASE/api/models?model_type=video" "m-verify-video" "model_type=video"
check_models "$BASE/api/models?media_type=image" "m-verify-t2i" "media_type=image"
check_models "$BASE/api/models?media_type=video" "m-verify-video" "media_type=video"
check_models "$BASE/api/models?media_type=video&ability=first_frame_i2v&verified_only=true" "m-verify-video" "video+ability+verified"

echo "== 恢复原始配置 =="
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "models_before.json")))["config"]
json.dump({"values": orig}, open(os.path.join(d, "models_restore.json"), "w"), ensure_ascii=False)
PY
code=$(curl -s -o "$DIR/models_restore_resp.json" -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/models_restore.json")
[ "$code" = "200" ] || { echo "FAIL: 恢复失败 $code"; exit 1; }
curl -s "$BASE/api/config" > "$DIR/models_after.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
before = json.load(open(os.path.join(d, "models_before.json")))["config"]
after = json.load(open(os.path.join(d, "models_after.json")))["config"]
assert before == after, "恢复后配置与初始不一致"
print("== 恢复后语义一致 ok ==")
PY
echo "ALL PASS (models API)"