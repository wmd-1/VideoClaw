# -*- coding: utf-8 -*-
"""对真实导入会话执行分镜阶段，验证标注消费与 placeholder 覆盖。"""

import json
import os
import urllib.request

BASE = os.environ.get("C2_BASE_URL", "http://localhost:8500")
SESSION = os.environ.get("C2_SESSION", "1792690246271")


def get_json(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=60))


request = urllib.request.Request(
    f"{BASE}/api/project/{SESSION}/execute/storyboard",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=900) as response:
    stream = response.read().decode("utf-8", errors="replace")
print("sse_tail", stream.strip().splitlines()[-1][:160])

status = get_json(f"/api/project/{SESSION}/status")
artifact = status.get("artifacts", {}).get("storyboard", {})
episodes = artifact.get("episodes") or []
segments = [seg for ep in episodes for seg in (ep.get("segments") or [])]
shots = [shot for seg in segments for shot in (seg.get("shots") or [])]
texts = "\n".join(
    str(shot.get(key) or "")
    for shot in shots
    for key in ("content", "plot", "visual_prompt", "description")
)
all_text = texts + "\n" + "\n".join(
    str(seg.get(key) or "") for seg in segments for key in ("description", "visual_prompt")
)

checks = {
    "storyboard_completed": status.get("status", {}).get("storyboard") == "completed",
    "segments_present": bool(segments),
    "shots_present": bool(shots),
    "no_act_heading_as_dialogue": "【第一幕" not in all_text and "【第二幕" not in all_text,
    "no_duration_as_dialogue": "视频时长" not in all_text,
    "placeholder_covered": "演示录屏预留位" in all_text or "展示银行对公风控AI产品主界面" in all_text,
}
print(json.dumps(checks, ensure_ascii=False, indent=2))
print("segments", len(segments), "shots", len(shots))
if not all(checks.values()):
    raise SystemExit(1)
print("STORYBOARD LIVE ALL PASS")
