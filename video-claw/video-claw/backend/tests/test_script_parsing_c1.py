"""C1（WF-01/WF-02）：旁白标注与英文场志/人物清单归一。"""
from __future__ import annotations

import asyncio
import re

import pytest

from core.agents.storyboard_agent import StoryboardAgent
from core.script_import import (
    _cast_section,
    annotate_lines,
    detect_candidates,
    extract_cast,
)


def test_narration_labels_are_annotated_as_narration():
    lines = ["旁白：很久以前。", "Narration: Once upon a time.", "VO: 画外音", "Alice: Hello."]
    annotations = annotate_lines(lines, [])
    by_line = {item["line"]: item for item in annotations}
    assert by_line[0]["type"] == "narration"
    assert by_line[1]["type"] == "narration"
    assert by_line[2]["type"] == "narration"
    assert by_line[3]["type"] == "dialogue"


def test_english_scene_header_is_normalized():
    parts = StoryboardAgent._parse_scene_header_parts(
        "Episode 1 - Scene 1 Day INT Office", ["Office"]
    )
    assert parts == {
        "location": "Office",
        "scene_time": "日",
        "scene_space": "内",
        "scene_context": "Episode 1 - Scene 1 Day INT Office",
    }
    assert StoryboardAgent._parse_scene_header_parts(
        "1-2 Night EXT Street", ["Office"]
    )["scene_time"] == "夜"
    assert StoryboardAgent._parse_scene_header_parts(
        "Episode 1, Scene 2 Dawn INT./EXT. Rooftop", ["Office"]
    )["scene_space"] == "内/外"
    assert StoryboardAgent._parse_scene_header_parts(
        "Scene 3 Evening EXT. Park", ["Office"]
    )["scene_time"] == "傍晚"


def test_narration_becomes_unit_without_visible_characters():
    script = "\n".join([
        "Episode 1 - Scene 1 Day INT Office",
        "Characters: Alice",
        "<action>Alice enters.</action>",
        "Narration: Alice was alone.",
        "Alice: Hello.",
    ])
    annotated, units = StoryboardAgent._annotate_episode_script(
        script,
        characters=[{"name": "Alice"}],
        settings=[{"name": "Office"}],
    )
    narration_units = [unit for unit in units if "Narration:" in unit["text"]]
    assert len(narration_units) == 1, "旁白必须成为单元"
    assert narration_units[0]["characters"] == [], "旁白不得自动创建可见人物"
    assert narration_units[0]["is_dialogue"] is False
    assert narration_units[0]["is_narration"] is True, "旁白必须有显式标记"
    action_units = [unit for unit in units if "Alice enters" in unit["text"]]
    assert action_units and action_units[0]["is_narration"] is False
    assert all("Characters:" not in unit["text"] for unit in units), "人物清单不得进入单元"
    assert "Narration: Alice was alone." in annotated


def test_every_non_empty_line_has_explicit_type():
    lines = [
        "Episode 1 - Scene 1 Day INT Office",
        "Characters: Alice",
        "",
        "<action>Alice enters.</action>",
        "Narration: Once.",
        "Alice: Hi",
        "THE END",
    ]
    annotations = annotate_lines(lines, [])
    non_empty = [index for index, line in enumerate(lines) if line.strip()]
    assert [item["line"] for item in annotations] == non_empty
    allowed = {
        "placeholder", "scene_desc", "narration", "subtitle", "end_card",
        "heading", "cast_block", "meta", "action", "dialogue",
    }
    assert {item["type"] for item in annotations} <= allowed
    assert all(item.get("type") for item in annotations), "每行必须显式归位"


def test_english_meta_and_cast_lines_are_not_dialogue():
    lines = [
        "Title: The Bank",
        "Duration: 60s",
        "Genre: Drama",
        "Style: Noir",
        "Characters: Alice, Bob",
    ]
    annotations = annotate_lines(lines, [], cast_range=(4, 4))
    by_line = {item["line"]: item for item in annotations}
    assert [by_line[index]["type"] for index in range(4)] == ["meta"] * 4
    assert by_line[4]["type"] == "cast_block"


def test_cast_section_does_not_swallow_following_dialogue():
    lines = ["Characters: Alice, Bob", "", "Alice: Hi"]
    assert _cast_section(lines) == (0, 0), "行内名单应视为单行段"
    cast = extract_cast(lines)
    assert [item["name"] for item in cast] == ["Alice", "Bob"]
    assert all(item["description"] == "" for item in cast)


def test_single_english_name_is_parsed():
    assert _cast_section(["Characters: Alice"]) == (0, 0)
    cast = extract_cast(["Characters: Alice"])
    assert [item["name"] for item in cast] == ["Alice"]


def test_inline_chinese_enumeration_list_is_parsed():
    cast = extract_cast(["Characters: 小明、小红"])
    assert [item["name"] for item in cast] == ["小明", "小红"]


def test_meta_cast_section_allows_blank_between_items():
    lines = ["角色设定：", "", "小陈：三十岁", "", "小周：二十八岁", "", "【第一幕】"]
    assert _cast_section(lines) == (0, 4)
    cast = {item["name"]: item["description"] for item in extract_cast(lines)}
    assert cast == {"小陈": "三十岁", "小周": "二十八岁"}


def test_chinese_cast_list_allows_blank_before_first_item():
    lines = ["人物：", "", "小明：三十岁", "小红：二十八岁"]
    assert _cast_section(lines) == (0, 3)
    cast = {item["name"]: item["description"] for item in extract_cast(lines)}
    assert cast == {"小明": "三十岁", "小红": "二十八岁"}


def test_chinese_cast_list_stops_at_blank_after_items():
    lines = ["人物：", "小红：28岁", "小明：30岁", "", "小红：你好"]
    assert _cast_section(lines) == (0, 2)
    cast = {item["name"]: item["description"] for item in extract_cast(lines)}
    assert cast == {"小红": "28岁", "小明": "30岁"}, "空行后的对白不得成为角色描述"


def test_repeated_name_ends_cast_section():
    lines = ["人物：", "小明：三十岁", "小明：你好"]
    assert _cast_section(lines) == (0, 1), "重复名字应视为对白并结束角色段"
    cast = extract_cast(lines)
    assert [(item["name"], item["description"]) for item in cast] == [("小明", "三十岁")]


def test_inline_cast_item_continues_scanning_following_items():
    lines = ["角色设定：小陈：三十岁", "小红：二十八岁"]
    assert _cast_section(lines) == (0, 1)
    cast = {item["name"]: item["description"] for item in extract_cast(lines)}
    assert cast == {"小陈": "三十岁", "小红": "二十八岁"}


def test_units_keep_source_order_and_line_numbers():
    script_lines = [
        "Episode 1 - Scene 1 Day INT Office",
        "Characters: Alice",
        "<action>Alice enters.</action>",
        "旁白：从前有座山。",
        "Alice: Hello.",
        "Episode 1 - Scene 2 Night EXT Street",
        "Alice: Bye.",
    ]
    _, units = StoryboardAgent._annotate_episode_script(
        "\n".join(script_lines),
        characters=[{"name": "Alice"}],
        settings=[{"name": "Office"}],
    )
    assert [unit["unit_id"] for unit in units] == [
        f"U{index:03d}" for index in range(1, len(units) + 1)
    ]
    line_numbers = [unit["line_number"] for unit in units]
    assert line_numbers == sorted(line_numbers), "单元必须保持源顺序"
    scene_keys = [unit["scene_key"] for unit in units]
    assert scene_keys == sorted(scene_keys), "场景边界不得回退"
    assert len(set(scene_keys)) >= 2, "第二个场景头必须切场景"
    dialogue_texts = [unit["text"] for unit in units if unit["is_dialogue"]]
    assert "Alice: Hello." in dialogue_texts and "Alice: Bye." in dialogue_texts
    for unit in units:
        source = script_lines[unit["line_number"] - 1]
        assert unit["text"] in source, "单元正文必须可回溯到源行（允许去标签）"


def _front_matter_annotations(lines):
    return {
        item["line"]: item
        for item in annotate_lines(lines, detect_candidates(lines), _cast_section(lines))
    }


def test_front_matter_title_cast_and_synopsis_are_not_body_text():
    """片名/人物设定/剧情梗概属于剧本头，必须标为标题或元信息而不是动作。"""
    lines = [
        "《惠民贷风波》",
        "",
        "人物设定",
        "李大海 - 45岁，小微企业主，惠民贷借款人",
        "张晓敏 - 李大海的妻子",
        "",
        "剧情梗概",
        "李大海因经营困难导致惠民贷逾期，与银行产生纠纷。",
        "",
        "第一场：逾期焦虑",
        "李大海：我还不上了。",
    ]
    by_line = _front_matter_annotations(lines)
    assert by_line[0] == {
        "line": 0,
        "type": "heading",
        "unit": "title",
        "title": "《惠民贷风波》",
    }
    assert [by_line[index]["type"] for index in (2, 3, 4)] == ["cast_block"] * 3
    assert [by_line[index]["type"] for index in (6, 7)] == ["meta"] * 2
    assert by_line[9]["type"] == "heading"
    assert by_line[10]["type"] == "dialogue"


def test_front_matter_label_line_is_title_not_dialogue():
    """「带你了解：……」是片头副标题，不是角色台词。"""
    lines = [
        "# 原剧本",
        "带你了解：银行卡限额与管控的那些事儿",
        "视频时长：约60秒",
        "",
        "角色设定：",
        "李阿姨：着急用卡转账的普通市民",
        "",
        "【第一幕：卡突然被管控了】",
        "李阿姨快步走进银行大厅。",
    ]
    by_line = _front_matter_annotations(lines)
    assert by_line[1]["type"] == "heading", "片头副标题不得进入对话单元"
    assert by_line[1]["unit"] == "title"
    assert by_line[5]["type"] == "cast_block"
    assert by_line[8]["type"] == "action"


def test_dash_cast_items_are_parsed():
    lines = ["人物设定", "李大海 - 45岁，小微企业主", "张晓敏 - 李大海的妻子"]
    assert _cast_section(lines) == (0, 2), "破折号条目应被识别为角色段"
    cast = {item["name"]: item["description"] for item in extract_cast(lines)}
    assert cast == {"李大海": "45岁，小微企业主", "张晓敏": "李大海的妻子"}


def test_cast_section_stops_at_scene_heading():
    lines = ["人物设定", "李大海 - 45岁", "", "第一场：逾期焦虑", "李大海：我还不上了。"]
    assert _cast_section(lines) == (0, 1), "场标题不得被吞进角色段"


def test_unstructured_script_keeps_opening_dialogue():
    """无幕/场/集标记时不得把开头台词当片头标题。"""
    lines = ["小李：你好。", "小王：你也好。"]
    annotations = annotate_lines(lines, detect_candidates(lines))
    assert [item["type"] for item in annotations] == ["dialogue", "dialogue"]


def test_bracket_scene_marker_is_scene_desc():
    """「[场景：…]」是场景志，不是台词，也不得成为动作单元。"""
    lines = ["【第一幕：开场】", "[场景：李大海家中，客厅]", "李大海：我还不上了。"]
    annotations = annotate_lines(lines, detect_candidates(lines), _cast_section(lines))
    by_line = {item["line"]: item for item in annotations}
    assert by_line[1]["type"] == "scene_desc"
    assert by_line[2]["type"] == "dialogue"

    by_line[1]["duration"] = 8  # 场景志的停留建议必须并入下一个单元
    _, units = StoryboardAgent._annotate_episode_script(
        "\n".join(lines),
        characters=[{"name": "李大海"}],
        settings=[],
        annotations=annotations,
        scene_spans=[],
        line_offset=0,
        baseline_line_count=len(lines),
    )
    assert [unit["text"] for unit in units] == ["李大海：我还不上了。"], "场景志不得自成单元"
    assert units[0]["duration"] >= 8, "场景志时长必须合并到下一个单元"


def test_cast_section_stops_at_bracket_scene_marker():
    lines = ["人物设定", "李大海 - 45岁", "[场景：李大海家中，客厅]", "李大海：我还不上了。"]
    assert _cast_section(lines) == (0, 1), "方括号场景志不得被吞进角色段"
    assert {item["name"] for item in extract_cast(lines)} == {"李大海"}


def test_front_matter_heading_does_not_split_scene():
    """片名/副标题标为 heading 后，不得被场志解析器当成场景切换。"""
    for text in ("《惠民贷风波》", "带你了解：银行卡限额与管控的那些事儿"):
        assert StoryboardAgent._parse_scene_header_parts(text, []) is None

    lines = ["《惠民贷风波》", "带你了解：银行卡限额与管控的那些事儿", "第一场：逾期焦虑", "李大海：我还不上了。"]
    _, units = StoryboardAgent._annotate_episode_script(
        "\n".join(lines),
        characters=[{"name": "李大海"}],
        settings=[],
        annotations=annotate_lines(lines, detect_candidates(lines), _cast_section(lines)),
        scene_spans=[],
        line_offset=0,
        baseline_line_count=len(lines),
    )
    assert len(units) == 1
    assert units[0]["scene_key"] == 0, "片头标题不得新增场景"
    assert units[0]["location"] == ""


def test_storyboard_units_skip_front_matter():
    script_lines = [
        "《惠民贷风波》",
        "",
        "人物设定",
        "李大海 - 45岁，小微企业主",
        "张晓敏 - 李大海的妻子",
        "",
        "剧情梗概",
        "李大海因经营困难导致惠民贷逾期。",
        "",
        "第一场：逾期焦虑",
        "李大海：我还不上了。",
    ]
    lines = script_lines
    _, units = StoryboardAgent._annotate_episode_script(
        "\n".join(lines),
        characters=[{"name": "李大海"}, {"name": "张晓敏"}],
        settings=[],
        annotations=annotate_lines(lines, detect_candidates(lines), _cast_section(lines)),
        scene_spans=[],
        line_offset=0,
        baseline_line_count=len(lines),
    )
    texts = [unit["text"] for unit in units]
    assert all("惠民贷风波" not in text for text in texts), "片名不得生成镜头"
    assert all("人物设定" not in text for text in texts), "人物设定不得生成镜头"
    assert all("剧情梗概" not in text for text in texts), "剧情梗概不得生成镜头"
    assert any("我还不上了" in text for text in texts), "正文台词必须保留"


def test_segment_plan_and_shots_exclude_front_matter(monkeypatch):
    """正常规划路径：片头行不带单元编号，进不了片段。

    `shots` 断言基于确定性设计（`_fallback_design_segment`），只证明片头文本
    不在任何 plan item 中；模型写镜头文案时主动复述片头不在覆盖范围内。
    """
    script_lines = [
        "《惠民贷风波》",
        "",
        "人物设定",
        "李大海 - 45岁，小微企业主",
        "张晓敏 - 李大海的妻子",
        "",
        "剧情梗概",
        "李大海因经营困难导致惠民贷逾期。",
        "",
        "第一场：逾期焦虑",
        "李大海：我还不上了。",
    ]
    lines = script_lines
    annotations = annotate_lines(lines, detect_candidates(lines), _cast_section(lines))
    annotated_script, units = StoryboardAgent._annotate_episode_script(
        "\n".join(lines),
        characters=[{"name": "李大海"}, {"name": "张晓敏"}],
        settings=[],
        annotations=annotations,
        scene_spans=[],
        line_offset=0,
        baseline_line_count=len(lines),
    )
    assert units

    agent = StoryboardAgent()
    tagged = [
        line for line in annotated_script.split("\n")
        if re.search(r"\[\d+秒\]\[U\d{3,}\]", line)
    ]
    assert len(tagged) == len(units), "只有单元行才带时长与编号"
    assert not any("惠民贷风波" in line or "人物设定" in line for line in tagged)

    async def fake_plan(prompt, llm_model, sid, *, label, max_retries=3):
        # 正常规划路径：模型按带时长剧本里的编号全量分组
        return [{"unit_ids": [unit["unit_id"] for unit in units]}]

    monkeypatch.setattr(agent, "_query_json_array_with_retries", fake_plan)
    plans = asyncio.run(
        agent._plan_episode_segments(
            1, "第1集", annotated_script, units, [{"name": "李大海"}], [], "fake-model", "sid-test",
        )
    )
    assert len(plans) == 1
    plan_texts = [item["text"] for item in plans[0]["items"]]
    assert not any("惠民贷风波" in text for text in plan_texts)
    assert not any("人物设定" in text for text in plan_texts)
    assert not any("剧情梗概" in text for text in plan_texts)

    segments = [StoryboardAgent._fallback_design_segment(1, plan) for plan in plans]
    shot_texts = [shot["content"] for segment in segments for shot in segment["shots"]]
    assert shot_texts
    assert not any("惠民贷风波" in text for text in shot_texts), "片名不得进入镜头文案"
    assert not any("人物设定" in text for text in shot_texts), "人物设定不得进入镜头文案"
    assert all("我还不上了" in text for text in shot_texts)


def test_segment_plan_rejects_plan_missing_a_unit(monkeypatch):
    """防线负例：漏掉正文单元的规划必须被拒绝，回退后仍全量覆盖。"""
    script_lines = [
        "《惠民贷风波》",
        "",
        "人物设定",
        "李大海 - 45岁，小微企业主",
        "",
        "第一场：逾期焦虑",
        "李大海：我还不上了。",
        "李大海：真的还不上了。",
    ]
    lines = script_lines
    annotations = annotate_lines(lines, detect_candidates(lines), _cast_section(lines))
    annotated_script, units = StoryboardAgent._annotate_episode_script(
        "\n".join(lines),
        characters=[{"name": "李大海"}],
        settings=[],
        annotations=annotations,
        scene_spans=[],
        line_offset=0,
        baseline_line_count=len(lines),
    )
    assert len(units) == 2

    with pytest.raises(ValueError):
        StoryboardAgent._validate_segment_plan(
            1,
            [{"segment_number": 1, "unit_ids": [units[0]["unit_id"]]}],
            units,
        )

    agent = StoryboardAgent()
    calls = 0

    async def partial_plan(prompt, llm_model, sid, *, label, max_retries=3):
        nonlocal calls
        calls += 1
        return [{"segment_number": 1, "unit_ids": [units[0]["unit_id"]]}]

    monkeypatch.setattr(agent, "_query_json_array_with_retries", partial_plan)
    plans = asyncio.run(
        agent._plan_episode_segments(
            1, "第1集", annotated_script, units, [{"name": "李大海"}], [], "fake-model", "sid-test",
        )
    )
    planned_ids = [item["unit_id"] for plan in plans for item in plan["items"]]
    assert calls == 3, "规划共 3 次 attempt（首次 + 2 次重试）后才回退"
    assert planned_ids == [unit["unit_id"] for unit in units], "漏单元只能回退到全量覆盖"
