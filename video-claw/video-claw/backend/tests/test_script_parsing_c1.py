"""C1（WF-01/WF-02）：旁白标注与英文场志/人物清单归一。"""
from __future__ import annotations

from core.agents.storyboard_agent import StoryboardAgent
from core.script_import import _cast_section, annotate_lines, extract_cast


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
