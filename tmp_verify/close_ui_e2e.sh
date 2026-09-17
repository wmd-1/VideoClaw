#!/bin/bash
# 任务 6.3 收尾：上传图片完成条目 -> 状态重算 -> 清理会话与配置
set -e
BASE=http://localhost:8000
DIR=/home/wmd/projects/VideoClaw/tmp_verify
PY=python3
SID=$(cat "$DIR/ui_session_id.txt")

$PY - "$DIR" <<'PY'
import base64, os, sys
d = sys.argv[1]
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
open(os.path.join(d, "ui_upload.png"), "wb").write(png)
PY

echo "== 上传角色图与场景图 =="
curl -s -X POST "$BASE/api/project/$SID/artifact/character_design/upload_image" -F "item_type=characters" -F "item_id=char_ui" -F "file=@$DIR/ui_upload.png" > "$DIR/ui_up1.json"
curl -s -X POST "$BASE/api/project/$SID/artifact/character_design/upload_image" -F "item_type=settings" -F "item_id=set_ui" -F "file=@$DIR/ui_upload.png" > "$DIR/ui_up2.json"

$PY - "$DIR" "$BASE" "$SID" <<'PY'
import json, sys, urllib.request
d, base, sid = sys.argv[1], sys.argv[2], sys.argv[3]
up1 = json.load(open(f"{d}/ui_up1.json"))
up2 = json.load(open(f"{d}/ui_up2.json"))
assert up1.get("status") == "ok" and up2.get("status") == "ok", (up1, up2)
art = json.load(urllib.request.urlopen(f"{base}/api/project/{sid}/artifact/character_design", timeout=10))["artifact"]
chars = art.get("characters") or []
sets = art.get("settings") or []
assert chars and "Assets/characters" in chars[0]["selected"], chars
assert sets and "Assets/settings" in sets[0]["selected"], sets
status = json.load(urllib.request.urlopen(f"{base}/api/project/{sid}/status", timeout=10))["status"]
assert status.get("character_design") == "completed", status
print("== ok: 上传后条目已选、阶段状态重算为 completed ==")
print("   char selected:", chars[0]["selected"])
print("   set  selected:", sets[0]["selected"])
PY

echo "== 清理：删除会话 + 恢复配置 =="
curl -s -X DELETE "$BASE/api/sessions/$SID" > /dev/null
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
orig = json.load(open(os.path.join(d, "ui_before.json")))["config"]
json.dump({"values": orig}, open(os.path.join(d, "ui_restore.json"), "w"), ensure_ascii=False)
PY
curl -s -X PUT "$BASE/api/config" -H 'Content-Type: application/json' --data @"$DIR/ui_restore.json" > /dev/null
curl -s "$BASE/api/config" > "$DIR/ui_after.json"
$PY - "$DIR" <<'PY'
import json, sys, os
d = sys.argv[1]
a = json.load(open(os.path.join(d, "ui_before.json")))["config"]
b = json.load(open(os.path.join(d, "ui_after.json")))["config"]
assert a == b, "恢复后配置不一致"
print("== ok: 配置已恢复且语义一致 ==")
PY
echo "ALL PASS (6.3 closure)"