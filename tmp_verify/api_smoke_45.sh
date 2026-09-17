#!/bin/bash
# 任务 8.2 主流程冒烟：以自定义 mock 模型跑通 第4阶段（参考图）-> 第5阶段（首帧生视频）
set -e
ROOT=/home/wmd/projects/VideoClaw
BASE=http://localhost:8000
DIR=$ROOT/tmp_verify
PY=python3

curl -s "$BASE/api/config" > "$DIR/smoke_before.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "smoke_before.json")))["config"]
orig.setdefault("api_providers", {})["smoke-mock"] = {"protocol": "openai", "base_url": "http://127.0.0.1:18099/v1", "api_key": "", "enable_proxy": False, "name": "冒烟 Mock"}
models = orig.setdefault("custom_models", [])
models.append({"id": "m-smoke-llm", "provider": "smoke-mock", "model": "LLM1", "types": ["llm", "vlm"], "abilities": []})
models.append({"id": "m-smoke-img", "provider": "smoke-mock", "model": "IMG1", "types": ["t2i", "i2i"], "abilities": []})
models.append({"id": "m-smoke-vid", "provider": "smoke-mock", "model": "VID1", "types": ["video"], "abilities": ["text_to_video", "first_frame_i2v"]})
json.dump({"values": orig}, open(os.path.join(d, "smoke_setup.json"), "w"), ensure_ascii=False)
PY
curl -s -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/smoke_setup.json" > /dev/null
echo "== 0 ok: 冒烟模型已注册 =="

SID=$($PY - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
payload = {
    "idea": "冒烟回归", "style": "realistic", "video_ratio": "16:9", "video_resolution": "720P",
    "expand_idea": False,
    "llm_model": "m-smoke-llm", "vlm_model": "m-smoke-llm",
    "image_t2i_model": "m-smoke-img", "image_it2i_model": "m-smoke-img",
    "video_first_frame_model": "m-smoke-vid", "video_start_end_model": "m-smoke-vid",
    "video_reference_model": "m-smoke-vid", "video_generation_mode": "first_frame",
}
req = urllib.request.Request(base + "/api/project/start", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=10))["session_id"])
PY
)
echo "SESSION=$SID"

$PY - "$BASE" "$SID" <<'PY'
import json, sys, urllib.request
base, sid = sys.argv[1], sys.argv[2]
def patch(stage, body):
    req = urllib.request.Request(base + f"/api/project/{sid}/artifact/{stage}",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="PATCH")
    return json.load(urllib.request.urlopen(req, timeout=10))
seg = {
    "segment_id": "seg_01_01", "location": "", "characters": [], "total_duration": 5,
    "shots": [{"shot_id": "shot_01", "content": "镜头内容", "plot": "剧情描述", "visual_prompt": "画面描述", "duration": 5}],
}
patch("storyboard", {"episodes": [{"episode_number": 1, "act_title": "第一集", "segments": [seg]}]})
patch("reference_generation", {"scenes": [{
    "id": "seg_01_01", "name": "片段1", "index": 1, "description": "画面描述",
    "selected": "", "versions": [], "status": "pending", "episode": 1,
}]})
patch("video_generation", {"clips": [{
    "id": "seg_01_01", "name": "片段1", "index": 1, "description": "剧情描述", "duration": 5,
    "selected": "", "versions": [], "status": "pending", "episode": 1,
}]})
print("== 1 ok: 分镜/条目已注入 ==")
PY

for stage in reference_generation video_generation; do
  echo "== 执行 $stage =="
  $PY - "$BASE" "$SID" "$stage" <<'PY'
import json, sys, urllib.request
base, sid, stage = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(base + f"/api/project/{sid}/execute/{stage}", data=b"{}", headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req, timeout=180)
body = resp.read().decode("utf-8", "ignore")
assert '"type": "error"' not in body and "'type': 'error'" not in body, body[-500:]
print(f"== {stage} 流完成，长度 {len(body)} ==")
PY
done

$PY - "$ROOT" "$BASE" "$SID" <<'PY'
import json, sys, os, urllib.request
root, base, sid = sys.argv[1], sys.argv[2], sys.argv[3]
art = json.load(urllib.request.urlopen(base + f"/api/project/{sid}/artifact/reference_generation", timeout=10))["artifact"]
scene = (art.get("scenes") or [{}])[0]
assert scene.get("selected") and scene.get("versions"), scene
print("== 4 ok: 参考图已生成并选中 ->", scene["selected"])
v_art = json.load(urllib.request.urlopen(base + f"/api/project/{sid}/artifact/video_generation", timeout=10))["artifact"]
clip = (v_art.get("clips") or [{}])[0]
assert clip.get("selected"), clip
host_path = os.path.join(root, "data", clip["selected"].replace("code/", "code/", 1)) if clip["selected"].startswith("code/") else None
assert host_path and os.path.exists(host_path), (clip["selected"], host_path)
assert host_path.endswith(".mp4") and os.path.getsize(host_path) > 0, host_path
print("== 5 ok: 视频片段已生成并选中 ->", clip["selected"])
PY

echo "== 清理 =="
curl -s -X DELETE "$BASE/api/sessions/$SID" > /dev/null
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "smoke_before.json")))["config"]
json.dump({"values": orig}, open(os.path.join(d, "smoke_restore.json"), "w"), ensure_ascii=False)
PY
curl -s -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/smoke_restore.json" > /dev/null
curl -s "$BASE/api/config" > "$DIR/smoke_after.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
a = json.load(open(os.path.join(d, "smoke_before.json")))["config"]
b = json.load(open(os.path.join(d, "smoke_after.json")))["config"]
assert a == b, "恢复后配置不一致"
print("== 清理 ok: 配置恢复且语义一致 ==")
PY
echo "ALL PASS (smoke 4->5)"