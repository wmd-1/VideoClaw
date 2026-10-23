# -*- coding: utf-8 -*-
"""C2 集成断言：真实导入产物 → 分镜标注消费 + 阶段 2 种子化。"""

import os

from core import script_import as si
from core.agents.storyboard_agent import StoryboardAgent
from core.orchestrator import WorkflowEngine, WorkflowStage

PASS = 0
FAILED = []


def check(name, condition, detail=""):
    global PASS
    if condition:
        PASS += 1
        print("PASS", name)
    else:
        FAILED.append(name)
        print("FAIL", name, detail)


FIXTURES = os.environ.get("C2_FIXTURES", "/tmp/c2fixtures")
raw = open(os.path.join(FIXTURES, "script_act_style.txt"), encoding="utf-8").read()
lines = si.split_lines(raw)
heading_line = next(
    index for index, line in enumerate(lines) if "第二幕" in line
)
artifact = si.build_import_artifact(
    raw,
    session_id="verify-c2-int",
    title="对公风控研判官——AI赋能全流程风控实战",
    inferred_settings=[
        {"name": "支行办公室", "description": "开放工位，日光灯，木质桌面"}
    ],
    setting_by_heading_line={heading_line: "支行办公室"},
)

agent = StoryboardAgent()
_, units = agent._annotate_episode_script(
    artifact["episodes"][0]["content"],
    artifact["characters"],
    artifact["settings"],
    annotations=artifact["line_annotations"],
    scene_spans=artifact["scene_spans"],
    line_offset=artifact["episodes"][0]["line_start"],
    baseline_line_count=len(artifact["meta"]["original_text"].split("\n")),
)
speakers = {unit["speaker"] for unit in units if unit["is_dialogue"]}
check(
    "分镜消费真实导入产物：幕标题/元信息不成为台词",
    "【第一幕" not in speakers
    and "【第二幕" not in speakers
    and "视频时长" not in speakers
    and "小陈" in speakers,
    str(sorted(speakers)),
)
check(
    "分镜消费真实导入产物：占位行不生成台词",
    any(unit["text"].startswith("[演示录屏预留位]") and not unit["is_dialogue"] for unit in units),
)

engine = WorkflowEngine()
sid = "verify_c2_seed"
state = engine.get_or_create_state(sid)
state.meta = {
    "idea": "",
    "user_textbox_input": "",
    "script_source": "upload",
    "file_path": "fixture.txt",
    "style": "realistic",
    "video_ratio": "16:9",
    "video_resolution": "720P",
    "llm_model": "mock-model",
    "vlm_model": "local-vlm",
    "image_t2i_model": "local-image-t2i",
    "image_it2i_model": "local-image-it2i",
    "video_first_frame_model": "m3",
    "video_model": "m3",
    "episodes": 1,
}
state.artifacts = {"script_generation": artifact}
state.status = {
    stage.value: "pending"
    for stage in WorkflowStage
    if stage not in (WorkflowStage.INIT, WorkflowStage.COMPLETED)
}
state.status["script_generation"] = "completed"
state.current_stage = WorkflowStage.CHARACTER_DESIGN
engine.save_session_to_disk(sid)

state2, _ = engine.prepare_stage_execution(sid, "character_design", {})
seeded = state2.artifacts.get("character_design") or {}
names = [item["name"] for item in seeded.get("characters") or []]
ids = [item["id"] for item in seeded.get("characters") or []]
script_ids = [item["character_id"] for item in artifact["characters"]]
check(
    "进入阶段 2 时按剧本种子化角色",
    {"小陈", "小周", "浙小控", "浙宝箱"} <= set(names)
    and ids == script_ids
    and all(item.get("status") == "pending" for item in seeded.get("characters") or []),
    f"names={names} ids={ids}",
)
setting_names = [item["name"] for item in seeded.get("settings") or []]
check(
    "进入阶段 2 时按剧本种子化场景",
    "支行办公室" in setting_names
    and all(item.get("status") == "pending" for item in seeded.get("settings") or []),
    str(setting_names),
)

print(f"\nTOTAL PASS={PASS} FAIL={len(FAILED)}")
if FAILED:
    print("FAILED:", FAILED)
    raise SystemExit(1)
print("ALL PASS")
