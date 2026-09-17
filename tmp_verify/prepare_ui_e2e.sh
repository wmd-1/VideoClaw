#!/bin/bash
# 准备任务 6.3 浏览器端到端场景：注册 mock 供应商/模型并构造会发生模型不可用预检的会话
set -e
BASE=http://localhost:8000
DIR=/home/wmd/projects/VideoClaw/tmp_verify
PY=python3

curl -s "$BASE/api/config" > "$DIR/ui_before.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "ui_before.json")))["config"]
orig.setdefault("api_providers", {})["ui-mock"] = {"protocol": "openai", "base_url": "http://127.0.0.1:18099/v1", "api_key": "", "enable_proxy": False, "name": "UI 验证 Mock"}
models = orig.setdefault("custom_models", [])
models.append({"id": "m-ui-img", "name": "UI 验证图像", "provider": "ui-mock", "model": "IMG1", "types": ["t2i", "i2i"], "abilities": []})
models.append({"id": "m-ui-llm", "name": "UI 验证LLM", "provider": "ui-mock", "model": "LLM1", "types": ["llm", "vlm"], "abilities": []})
models.append({"id": "m-ui-vid", "name": "UI 验证视频", "provider": "ui-mock", "model": "VID1", "types": ["video"], "abilities": ["text_to_video", "first_frame_i2v"]})
json.dump({"values": orig}, open(os.path.join(d, "ui_setup.json"), "w"), ensure_ascii=False)
PY
curl -s -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/ui_setup.json" > /dev/null

SID=$($PY - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
payload = {
    "idea": "UI 兜底验证",
    "style": "realistic",
    "video_ratio": "16:9",
    "video_resolution": "720P",
    "expand_idea": False,
    "llm_model": "m-ui-llm",
    "vlm_model": "m-ui-llm",
    "image_t2i_model": "unregistered-ui-zzz",
    "image_it2i_model": "m-ui-img",
    "video_first_frame_model": "m-ui-vid",
    "video_start_end_model": "m-ui-vid",
    "video_reference_model": "m-ui-vid",
    "video_generation_mode": "first_frame",
}
req = urllib.request.Request(base + "/api/project/start", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=10))["session_id"])
PY
)
echo "SESSION_ID=$SID"
echo "$SID" > "$DIR/ui_session_id.txt"

$PY - "$BASE" "$SID" <<'PY'
import json, sys, urllib.request
base, sid = sys.argv[1], sys.argv[2]
payload = {"characters": [
    {"character_id": "char_ui", "name": "星河旅人", "description": "披银色风衣的年轻旅者"},
], "settings": [{"setting_id": "set_ui", "name": "星港大厅", "description": "穹顶透光的未来候车厅"}]}
urllib.request.urlopen(urllib.request.Request(
    base + f"/api/project/{sid}/artifact/script_generation",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="PATCH",
), timeout=10)
# 触发一次预检 409：服务端将种子化第 2 阶段条目（供弹窗「上传图片」兜底）
req = urllib.request.Request(base + f"/api/project/{sid}/execute/character_design", data=b"{}", headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=10)
except urllib.error.HTTPError as exc:
    print("PRECHECK_HTTP", exc.code)
art = json.load(urllib.request.urlopen(base + f"/api/project/{sid}/artifact/character_design", timeout=10))["artifact"]
print("SEEDED", [c.get("id") for c in art.get("characters", [])])
PY
echo "READY"