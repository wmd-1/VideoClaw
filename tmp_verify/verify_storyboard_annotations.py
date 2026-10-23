# -*- coding: utf-8 -*-
"""C2 分镜消费导入标注验证（容器内执行，9 项断言）。"""

import re

from core.agents.storyboard_agent import StoryboardAgent

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


agent = StoryboardAgent()
characters = [{"name": "小陈"}, {"name": "小周"}]
settings = [{"name": "支行办公室"}, {"name": "审批中心"}]
lines = [
    "【第一幕：开场】",
    "视频时长：约90秒",
    "小陈：交行客户经理，深色西装",
    "小陈（叹气）：材料太多了。",
    "[演示录屏预留位]",
    "小周：终于完成了。",
]
content = "\n".join(lines)
annotations = [
    {"line": 0, "type": "heading", "unit": "act", "title": "第一幕"},
    {"line": 1, "type": "meta"},
    {"line": 2, "type": "cast_block"},
    {"line": 3, "type": "dialogue", "speaker": "小陈"},
    {"line": 4, "type": "placeholder", "kind": "screen_recording"},
    {"line": 5, "type": "dialogue", "speaker": "小周"},
]
scene_spans = [
    {"setting": "支行办公室", "from_line": 0, "to_line": 3},
    {"setting": "审批中心", "from_line": 4, "to_line": 5},
]

annotated, units = agent._annotate_episode_script(
    content,
    characters,
    settings,
    annotations=annotations,
    scene_spans=scene_spans,
    line_offset=0,
    baseline_line_count=len(lines),
)
texts = [unit["text"] for unit in units]
skipped = [lines[0], lines[1], lines[2]]
placeholder_units = [unit for unit in units if unit["text"] == lines[4]]
dialogue_speakers = {unit["speaker"] for unit in units if unit["is_dialogue"]}
placeholder_marked = any(
    re.match(r"^\[2秒\]\[U\d+\] \[演示录屏预留位\]", line)
    for line in annotated.split("\n")
)
check(
    "① 跳过类不进对话单元，占位行非对话且带时长/U 编号，speaker 来自标注",
    all(text not in texts for text in skipped)
    and placeholder_units
    and placeholder_units[0]["is_dialogue"] is False
    and dialogue_speakers == {"小陈", "小周"}
    and placeholder_marked,
    f"speakers={dialogue_speakers} marked={placeholder_marked}",
)

first_unit = next(unit for unit in units if unit["text"] == lines[3])
second_unit = next(unit for unit in units if unit["text"] == lines[5])
check("② scene_spans 按行区间切换场景", first_unit["scene_key"] != second_unit["scene_key"])

_, fallback_units = agent._annotate_episode_script(
    content,
    characters,
    settings,
    annotations=annotations,
    scene_spans=scene_spans,
    line_offset=0,
    baseline_line_count=1,
)
fallback_speakers = {unit["speaker"] for unit in fallback_units if unit["is_dialogue"]}
_, plain_units = agent._annotate_episode_script(content, characters, settings)
plain_speakers = {unit["speaker"] for unit in plain_units if unit["is_dialogue"]}
check(
    "③ 标注越界或无标注时整体回退既有正则",
    fallback_speakers == plain_speakers and "【第一幕" in plain_speakers,
    f"fallback={fallback_speakers} plain={plain_speakers}",
)

budget_units = [{"duration": 60}, {"duration": 60}, {"duration": 60}]
agent._apply_duration_budget(budget_units, 90, 1)
total = sum(unit["duration"] for unit in budget_units)
check(
    "④ 时长预算压缩后不超过目标 1.2 倍且无低于 2 秒单元",
    total <= 108 and all(unit["duration"] >= 2 for unit in budget_units),
    str(budget_units),
)

check(
    "⑤ 无标注旧产物行为不变（元信息仍按旧正则进入台词）",
    "视频时长" in plain_speakers
    and "视频时长" not in {unit["speaker"] for unit in units},
    f"plain={plain_speakers}",
)

fallback_content = "\n".join(
    [
        "第1集-第1场 日 内 支行办公室",
        "人物：小陈",
        "小陈：材料太多了。",
        "第1集-第2场 日 内 审批中心",
        "人物：小周",
        "小周：完成了。",
    ]
)
fallback_annotations = [
    {"line": 0, "type": "heading", "unit": "episode", "title": "第1集"},
    {"line": 1, "type": "cast_block"},
    {"line": 2, "type": "dialogue", "speaker": "小陈"},
    {"line": 3, "type": "heading", "unit": "scene", "title": "第2场"},
    {"line": 4, "type": "cast_block"},
    {"line": 5, "type": "dialogue", "speaker": "小周"},
]
_, fallback_scene_units = agent._annotate_episode_script(
    fallback_content,
    characters,
    settings,
    annotations=fallback_annotations,
    scene_spans=[],
    line_offset=0,
    baseline_line_count=6,
)
first_scene_unit = next(unit for unit in fallback_scene_units if unit["text"].startswith("小陈"))
second_scene_unit = next(unit for unit in fallback_scene_units if unit["text"].startswith("小周"))
alias_header_settings = [{"name": "支行办公室", "aliases": ["小陈办公室"]}]
alias_header_content = "\n".join(
    [
        "第1集-第1场 日 内 小陈办公室",
        "人物：小陈",
        "小陈：材料太多了。",
    ]
)
_, alias_header_units = agent._annotate_episode_script(
    alias_header_content,
    [{"name": "小陈"}],
    alias_header_settings,
    annotations=[{"line": 0, "type": "heading", "unit": "scene", "title": "第1场"}],
    scene_spans=[],
    line_offset=0,
    baseline_line_count=3,
)
check(
    "⑥ scene_spans 缺失时回退场景头解析，别名场景头落规范名",
    first_scene_unit["scene_key"] != second_scene_unit["scene_key"]
    and bool(alias_header_units)
    and alias_header_units[0].get("location") == "支行办公室",
    str(alias_header_units[:1]),
)

alias_settings = [{"name": "支行办公室", "aliases": ["小陈办公室"]}]
alias_content = "小陈：我在小陈办公室整理材料。"
_, alias_units = agent._annotate_episode_script(
    alias_content,
    [{"name": "小陈"}],
    alias_settings,
    annotations=[{"line": 0, "type": "dialogue", "speaker": "小陈"}],
    scene_spans=[],
    line_offset=0,
    baseline_line_count=1,
)
check(
    "⑦ 场景别名参与匹配且落盘规范名",
    bool(alias_units) and alias_units[0].get("location") == "支行办公室",
    str(alias_units[:1]),
)

scene_one_ids = [unit["unit_id"] for unit in units if unit["scene_key"] == 0]
scene_two_ids = [unit["unit_id"] for unit in units if unit["scene_key"] == 1]
placeholder_id = placeholder_units[0]["unit_id"]
plan_ok = False
missing_rejected = False
try:
    agent._validate_segment_plan(
        1,
        [
            {"segment_number": 1, "unit_ids": scene_one_ids},
            {"segment_number": 2, "unit_ids": scene_two_ids},
        ],
        units,
    )
    plan_ok = True
except ValueError:
    plan_ok = False
try:
    agent._validate_segment_plan(
        1,
        [
            {"segment_number": 1, "unit_ids": scene_one_ids},
            {"segment_number": 2, "unit_ids": [item for item in scene_two_ids if item != placeholder_id]},
        ],
        units,
    )
except ValueError:
    missing_rejected = True
check("⑧ 分段规划必须覆盖 placeholder", plan_ok and missing_rejected)

alias_segments = agent._build_segments_by_regex(
    1,
    "第1集-第1场 日 内 小陈办公室\n小陈：材料太多了。\n",
    [{"name": "小陈"}],
    alias_settings,
)
check(
    "⑨ 兜底分段场景头同样落规范名",
    bool(alias_segments)
    and all(segment.get("location") == "支行办公室" for segment in alias_segments),
    str(alias_segments),
)

print(f"\nTOTAL PASS={PASS} FAIL={len(FAILED)}")
if FAILED:
    print("FAILED:", FAILED)
    raise SystemExit(1)
print("ALL PASS")
