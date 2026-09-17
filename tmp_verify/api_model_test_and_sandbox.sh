#!/bin/bash
# 任务 3.2 / 3.3 HTTP 验证：模型连通测试端点 + 全端可用性（自定义模型注册→项目启动→沙盒出图）
# 依赖容器内 mock 服务（127.0.0.1:18099），针对既有 Docker 后端，不触碰镜像
set -e
BASE=http://localhost:8000
DIR=/home/wmd/projects/VideoClaw/tmp_verify
PY=python3
MOCK=http://127.0.0.1:18099/v1

# ── 0. 语义级备份 ──
curl -s "$BASE/api/config" > "$DIR/mt_before.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "mt_before.json")))["config"]
providers = orig.setdefault("api_providers", {})
providers["api-mock"] = {"protocol": "openai", "base_url": "http://127.0.0.1:18099/v1", "api_key": "", "enable_proxy": False, "name": "容器内 Mock"}
models = orig.setdefault("custom_models", [])
models.append({"id": "m-api-llm", "name": "Mock LLM", "provider": "api-mock", "model": "LLM1", "types": ["llm", "vlm"], "abilities": []})
models.append({"id": "m-api-t2i", "name": "Mock 图像", "provider": "api-mock", "model": "IMG1", "types": ["t2i", "i2i"], "abilities": []})
models.append({"id": "m-api-vid", "name": "Mock 视频", "provider": "api-mock", "model": "VID1", "types": ["video"], "abilities": ["text_to_video", "first_frame_i2v"]})
json.dump({"values": orig}, open(os.path.join(d, "mt_setup.json"), "w"), ensure_ascii=False)
PY
code=$(curl -s -o "$DIR/mt_setup_resp.json" -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/mt_setup.json")
[ "$code" = "200" ] || { echo "FAIL 注册测试模型 $code"; cat "$DIR/mt_setup_resp.json"; exit 1; }
curl -s "$BASE/api/config" > "$DIR/mt_after_setup.json"
echo "== 0 ok: 已注册 mock 供应商与 3 个模型 =="

test_case() {
  local name="$1" expect="$2" payload="$3"
  code=$(curl -s -o "$DIR/mt_case.json" -w "%{http_code}" -X POST "$BASE/api/models/test" -H 'Content-Type: application/json' -d "$payload")
  [ "$code" = "200" ] || { echo "FAIL $name：HTTP $code"; cat "$DIR/mt_case.json"; exit 1; }
  $PY - "$DIR/mt_case.json" "$expect" "$name" <<'PY'
import json, sys
body = json.load(open(sys.argv[1]))
expect = sys.argv[2] == "true"
name = sys.argv[3]
assert body.get("success") is expect, f"{name}: success={body.get('success')} reason={body.get('reason')}"
assert isinstance(body.get("elapsed_ms"), int) and body["elapsed_ms"] >= 0, f"{name}: elapsed_ms 缺失"
if expect:
    assert body.get("reason", "") == "", f"{name}: 成功时不应有 reason"
    print(f"== {name} ok: success=True elapsed={body['elapsed_ms']}ms detail={body.get('detail','')[:40]}")
else:
    assert body.get("reason"), f"{name}: 失败时应给出原因"
    print(f"== {name} ok: success=False reason={body['reason'][:60]}")
PY
}

# ── 1. 连通测试各类型 ──
test_case "llm 草稿测试" true '{"model": {"id": "draft-llm", "model": "LLM1"}, "provider": {"key": "draft-prov", "protocol": "openai", "base_url": "http://127.0.0.1:18099/v1", "api_key": ""}, "model_type": "llm"}'
test_case "vlm 已保存条目" true '{"model": {"id": "m-api-llm"}, "provider": "api-mock", "model_type": "vlm"}'
test_case "t2i 已保存条目" true '{"model": {"id": "m-api-t2i"}, "provider": "api-mock", "model_type": "t2i"}'
test_case "i2i 已保存条目" true '{"model": {"id": "m-api-t2i"}, "provider": "api-mock", "model_type": "i2i"}'
test_case "video 已保存条目" true '{"model": {"id": "m-api-vid"}, "provider": "api-mock", "model_type": "video"}'
test_case "错误 Base URL" false '{"model": {"id": "bad-llm"}, "provider": {"key": "bad-prov", "protocol": "openai", "base_url": "http://127.0.0.1:9/v1", "api_key": ""}, "model_type": "llm"}'

echo "== 1b. 非法 model_type -> 400 =="
code=$(curl -s -o "$DIR/mt_400.json" -w "%{http_code}" -X POST "$BASE/api/models/test" -H 'Content-Type: application/json' -d '{"model": {"id": "x"}, "provider": "api-mock", "model_type": "audio"}')
[ "$code" = "400" ] && echo "== 1b ok: HTTP 400 ==" || { echo "FAIL 1b: $code"; exit 1; }

# ── 2. 测试不持久化配置 ──
curl -s "$BASE/api/config" > "$DIR/mt_after_tests.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
a = json.load(open(os.path.join(d, "mt_after_setup.json")))["config"]
b = json.load(open(os.path.join(d, "mt_after_tests.json")))["config"]
assert a == b, "连通测试后配置发生了变化"
print("== 2 ok: 连通测试未持久化任何配置 ==")
PY

# ─ 3. 全端可用性：项目启动 + 会话级换模型 + 沙盒出图 ──
SID=$($PY - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
payload = {
    "idea": "自定义模型可用性验证",
    "style": "realistic",
    "video_ratio": "16:9",
    "video_resolution": "720P",
    "expand_idea": False,
    "llm_model": "m-api-llm",
    "vlm_model": "m-api-llm",
    "image_t2i_model": "m-api-t2i",
    "image_it2i_model": "m-api-t2i",
    "video_first_frame_model": "m-api-vid",
    "video_start_end_model": "m-api-vid",
    "video_reference_model": "m-api-vid",
    "video_generation_mode": "first_frame",
}
req = urllib.request.Request(base + "/api/project/start", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
resp = json.load(urllib.request.urlopen(req, timeout=10))
print(resp["session_id"])
PY
)
echo "== 3a ok: 项目启动 session=$SID =="

code=$(curl -s -o "$DIR/mt_patch.json" -w "%{http_code}" -X PATCH "$BASE/api/project/$SID/models" -H 'Content-Type: application/json' -d '{"image_t2i_model": "m-api-t2i"}')
$PY - "$DIR/mt_patch.json" "$code" <<'PY'
import json, sys
code, body = sys.argv[2], json.load(open(sys.argv[1]))
assert code == "200", (code, body)
assert body.get("meta", {}).get("image_t2i_model") == "m-api-t2i", body
print("== 3b ok: PATCH 会话模型接受自定义模型 id ==")
PY

code=$(curl -s -o "$DIR/mt_sandbox.json" -w "%{http_code}" -X POST "$BASE/api/sandbox/t2i" -H 'Content-Type: application/json' -d '{"model": "m-api-t2i", "prompt": "a red circle on white", "style": "realistic", "ratio": "16:9"}')
$PY - "$DIR/mt_sandbox.json" "$code" <<'PY'
import json, sys
code, body = sys.argv[2], json.load(open(sys.argv[1]))
assert code == "200" and body.get("success") is True, (code, body)
result = body.get("result") or []
assert result and str(result[0]).startswith("result/"), body
# 清理沙盒历史记录
import urllib.request
rid = body.get("record_id")
if rid:
    req = urllib.request.Request("http://localhost:8000/api/sandbox/history/" + rid, method="DELETE")
    urllib.request.urlopen(req, timeout=10)
print("== 3c ok: 沙盒 t2i 使用自定义模型出图 ->", result[0])
PY

echo "== 4. 清理：恢复配置 + 删除测试会话 =="
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "mt_before.json")))["config"]
json.dump({"values": orig}, open(os.path.join(d, "mt_restore.json"), "w"), ensure_ascii=False)
PY
code=$(curl -s -o "$DIR/mt_restore_resp.json" -w "%{http_code}" -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/mt_restore.json")
[ "$code" = "200" ] || { echo "FAIL 恢复配置 $code"; exit 1; }
curl -s -X DELETE "$BASE/api/sessions/$SID" > /dev/null
curl -s "$BASE/api/config" > "$DIR/mt_after_all.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
a = json.load(open(os.path.join(d, "mt_before.json")))["config"]
b = json.load(open(os.path.join(d, "mt_after_all.json")))["config"]
assert a == b, "恢复后配置与初始不一致"
print("== 4 ok: 配置与初始语义一致，测试会话已删除 ==")
PY
echo "ALL PASS (models test & availability)"