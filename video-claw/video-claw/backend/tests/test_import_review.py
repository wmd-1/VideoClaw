"""导入质检：只产出建议与行级时长，不改写正文。

只允许在既有后端容器内执行（详见 README「测试与校验」）。
"""
from __future__ import annotations

from core.agents.storyboard_agent import StoryboardAgent
from core.script_import import apply_import_review
from prompts.loader import load_prompt


def _annotations() -> list[dict]:
    return [
        {"line": 0, "type": "heading", "unit": "scene", "title": "第1场"},
        {"line": 1, "type": "action"},
        {"line": 2, "type": "dialogue", "speaker": "小陈"},
        {"line": 3, "type": "narration"},
    ]


def test_review_keeps_valid_lines_and_clips_durations():
    review = {
        "summary": "台词偏长，节奏偏快",
        "issues": [
            {"line": 2, "type": "timing", "severity": "high",
             "problem": "单句台词过长", "suggestion": "拆成两句"},
            {"line": 99, "type": "pacing", "severity": "low",
             "problem": "虚构行号", "suggestion": ""},
        ],
        "unit_durations": [
            {"line": 1, "seconds": 30},
            {"line": 2, "seconds": 9},
            {"line": 0, "seconds": 5},
            {"line": 88, "seconds": 4},
        ],
        "suggested_total_seconds": 99999,
        "duration_reason": "按台词字数估算",
    }
    original = _annotations()
    normalized, updated = apply_import_review(review, original)

    assert normalized is not None
    assert normalized["summary"] == "台词偏长，节奏偏快"
    assert [issue["line"] for issue in normalized["issues"]] == [2]
    assert normalized["unit_durations"] == [
        {"line": 1, "seconds": 15},
        {"line": 2, "seconds": 9},
    ]
    assert normalized["suggested_total_seconds"] == 7200

    by_line = {item["line"]: item for item in updated}
    assert by_line[1]["duration"] == 15, "超上限必须夹取到 15 秒"
    assert by_line[2]["duration"] == 9
    assert "duration" not in by_line[0], "标题行不得接受时长建议"
    assert original[1] == {"line": 1, "type": "action"}, "不得修改传入的标注"


def test_review_without_usable_fields_is_discarded():
    normalized, updated = apply_import_review(
        {"issues": [{"line": 99, "problem": "不存在行"}], "unit_durations": [{"line": 9, "seconds": 6}]},
        _annotations(),
    )
    assert normalized is None
    assert updated == _annotations()


def test_non_dict_review_is_ignored():
    normalized, updated = apply_import_review("not-json", _annotations())
    assert normalized is None
    assert updated == _annotations()


def test_zero_values_are_not_treated_as_valid_suggestions():
    normalized, _ = apply_import_review(
        {"suggested_total_seconds": 0, "unit_durations": [{"line": 1, "seconds": 0}]},
        _annotations(),
    )
    assert normalized is None


def test_annotation_duration_wins_over_length_estimate():
    script = "小陈走进办公室。\n小陈：材料放这儿。"
    annotations = [
        {"line": 0, "type": "action", "duration": 6},
        {"line": 1, "type": "dialogue", "speaker": "小陈", "duration": 9},
    ]
    _, units = StoryboardAgent._annotate_episode_script(
        script,
        characters=[{"name": "小陈"}],
        settings=[{"name": "办公室"}],
        annotations=annotations,
        scene_spans=[],
        line_offset=0,
        baseline_line_count=2,
    )
    assert [unit["duration"] for unit in units] == [6, 9]


def test_invalid_annotation_duration_falls_back_to_rule():
    _, units = StoryboardAgent._annotate_episode_script(
        "小陈：材料放这儿吧，麻烦你核对一下金额。",
        characters=[{"name": "小陈"}],
        settings=[],
        annotations=[{"line": 0, "type": "dialogue", "speaker": "小陈", "duration": "不是数字"}],
        scene_spans=[],
        line_offset=0,
        baseline_line_count=1,
    )
    assert units and units[0]["duration"] >= 2


def test_advisory_budget_keeps_units_intact():
    units = [{"duration": 10}, {"duration": 10}]
    notes: list[str] = []
    StoryboardAgent._apply_duration_budget(units, 10, 1, notes.append, advisory=True)
    assert [unit["duration"] for unit in units] == [10, 10]
    assert notes and "优先保留" in notes[0]


def test_non_advisory_budget_still_compresses():
    units = [{"duration": 10}, {"duration": 10}]
    StoryboardAgent._apply_duration_budget(units, 10, 1, None)
    assert sum(unit["duration"] for unit in units) < 20


def test_review_prompts_are_reachable_in_both_languages():
    for lang in ("zh", "en"):
        template = load_prompt("script", "import_review", lang)
        assert "{numbered_script}" in template
        assert "unit_durations" in template


IMPORT_SCRIPT = "\n".join([
    "人物：小陈",
    "小陈：材料放这儿吧，麻烦你核对一下金额。",
    "<action>小陈把材料放在桌上。</action>",
])


def test_import_pipeline_never_runs_quality_review(monkeypatch):
    """导入阶段不再触发质检：请求带 run_quality_check 时也只做导入解析。"""
    import asyncio
    import json

    import models.llm_client as llm_client
    from core.agents.script_agent import ScriptWriterAgent
    from models.file_reader import FileReader

    meta_reply = json.dumps(
        {
            "title": "测试剧本",
            "logline": "小陈交付材料。",
            "genre": ["职场"],
            "mood": "平稳",
            "film_type": "介绍",
            "subject_mode": "人物故事",
            "characters": [{"name": "小陈", "description": "青年男性，短发，深色西装。"}],
            "settings": [{"name": "办公室", "description": "内景，日光，办公桌与文件柜。"}],
        },
        ensure_ascii=False,
    )

    agent = ScriptWriterAgent()
    prompts: list[str] = []

    def fake_query(llm, prompt, *args):
        prompts.append(prompt)
        return meta_reply

    monkeypatch.setattr(
        ScriptWriterAgent, "_resolve_uploaded_document",
        staticmethod(lambda path, owner: "/tmp/fake-script.txt"),
    )
    monkeypatch.setattr(FileReader, "extract_text", staticmethod(lambda path: IMPORT_SCRIPT))
    monkeypatch.setattr(llm_client, "LLM", lambda: object())
    monkeypatch.setattr(agent, "_cancellable_query", fake_query)
    monkeypatch.setattr(agent, "_save_result", lambda *args, **kwargs: None)

    result = asyncio.run(agent._run_import_script({
        "session_id": "s1",
        "llm_model": "m1",
        "run_quality_check": True,
        "_session_owner": "alice",
        "_session_meta": {
            "file_path": "fake-script.txt",
            "language": "zh",
            "run_quality_check": True,
        },
    }))
    artifact = result["payload"]

    assert artifact["episodes"][0]["content"] == IMPORT_SCRIPT, "正文必须逐字保真"
    assert "import_review" not in artifact, "导入阶段不得再生成质检结论"
    assert "import_review_mode" not in artifact["meta"]
    assert artifact["meta"]["duration_budget"] == "advisory"
    assert "suggested_total_seconds" not in artifact["meta"]
    assert len(prompts) == 1, "只应调用元信息提取，不得调用质检"


def test_dialogue_duration_never_shorter_than_rule_estimate():
    script = "小陈：材料放这儿吧，麻烦你核对一下金额，然后签名确认。"
    annotations = [{"line": 0, "type": "dialogue", "speaker": "小陈", "duration": 2}]
    _, units = StoryboardAgent._annotate_episode_script(
        script,
        characters=[{"name": "小陈"}],
        settings=[],
        annotations=annotations,
        scene_spans=[],
        line_offset=0,
        baseline_line_count=1,
    )
    rule = StoryboardAgent._duration_for_script_unit(script, True)
    assert units[0]["duration"] >= rule
    assert units[0]["duration"] > 2, "质检建议不应把台词时长压到比字数估算更短"


def test_non_finite_duration_is_ignored_without_crashing():
    normalized, _ = apply_import_review(
        {
            "unit_durations": [{"line": 1, "seconds": float("inf")}],
            "suggested_total_seconds": float("inf"),
        },
        _annotations(),
    )
    assert normalized is None, "非有限值必须被完整丢弃，不能只做部分过滤"


def _stub_import(monkeypatch, reply):
    import models.llm_client as llm_client
    from core.agents.script_agent import ScriptWriterAgent
    from models.file_reader import FileReader

    agent = ScriptWriterAgent()
    prompts: list[str] = []

    def fake_query(llm, prompt, *args):
        prompts.append(prompt)
        return reply(prompt)

    monkeypatch.setattr(
        ScriptWriterAgent, "_resolve_uploaded_document",
        staticmethod(lambda path, owner: "/tmp/fake-script.txt"),
    )
    monkeypatch.setattr(FileReader, "extract_text", staticmethod(lambda path: IMPORT_SCRIPT))
    monkeypatch.setattr(llm_client, "LLM", lambda: object())
    monkeypatch.setattr(agent, "_cancellable_query", fake_query)
    monkeypatch.setattr(agent, "_save_result", lambda *args, **kwargs: None)
    return agent, prompts


def test_quality_check_off_by_default_for_short_script(monkeypatch):
    import asyncio

    agent, prompts = _stub_import(monkeypatch, lambda prompt: "{}")

    result = asyncio.run(agent._run_import_script({
        "session_id": "s3",
        "llm_model": "m1",
        "file_path": "fake-script.txt",
        "_session_owner": "alice",
        "_session_meta": {"language": "zh"},
    }))
    artifact = result["payload"]

    assert "import_review" not in artifact, "短剧本不得额外调用质检"
    assert "import_review_mode" not in artifact["meta"]
    assert len(prompts) == 1, "只应调用元信息提取"


def test_scene_desc_duration_merges_into_next_unit():
    script = "【画面描述】办公室全景。\n小陈：你好。"
    annotations = [
        {"line": 0, "type": "scene_desc", "duration": 5},
        {"line": 1, "type": "dialogue", "speaker": "小陈", "duration": 2},
    ]
    _, units = StoryboardAgent._annotate_episode_script(
        script,
        characters=[{"name": "小陈"}],
        settings=[],
        annotations=annotations,
        scene_spans=[],
        line_offset=0,
        baseline_line_count=2,
    )
    assert len(units) == 1, "画面描述不单独成镜头"
    assert units[0]["duration"] >= 5, "画面描述的停留建议必须由下一个单元消费"


def test_planning_hint_contains_duration_and_issues():
    hint = StoryboardAgent._build_planning_hint({
        "suggested_total_seconds": 120,
        "episode_count": 3,
        "line_start": 0,
        "line_end": 5,
        "review_issues": [{"line": 1, "problem": "台词偏长", "suggestion": "拆句"}],
    })
    assert "约 40 秒" in hint
    assert "台词偏长" in hint
    assert "第 2 行" not in hint, "单集正文无行号标注，hint 不应携带全局行号"


def test_planning_hint_fails_closed_without_episode_coordinates():
    hint = StoryboardAgent._build_planning_hint({
        "review_issues": [{"line": 1, "problem": "台词偏长", "suggestion": "拆句"}],
    })
    assert "台词偏长" not in hint, "坐标缺失时宁可不用问题清单"


def test_apply_preview_marks_import_review_stale():
    """AI 修订路径也必须让旧质检结论与建议总时长失效。"""
    from core.script_revision import apply_preview_to_artifact

    artifact = {
        "episodes": [{"episode_number": 1, "content": "旧正文"}],
        "characters": [],
        "settings": [],
        "import_review": {"summary": "旧结论", "suggested_total_seconds": 30},
        "meta": {"suggested_total_seconds": 30, "original_text": "旧正文"},
    }
    updated = apply_preview_to_artifact(
        artifact,
        {"episodes": [{"episode_number": 1, "content": "新正文"}], "summary": "修订"},
        instruction="改",
        scope="all",
        episode_number=None,
    )
    assert updated["episodes"][0]["content"] == "新正文"
    assert updated["import_review"]["stale"] is True
    assert "suggested_total_seconds" not in updated["meta"]
    assert artifact["import_review"].get("stale") is None, "不得改写传入产物"


def test_segmentation_prompt_includes_planning_hint():
    prompt = StoryboardAgent._build_segmentation_prompt(
        1,
        "第一集",
        "[5秒][U001] 小陈：你好。",
        [],
        [],
        planning_hint="本集建议总时长约 30 秒",
    )
    assert "本集建议总时长约 30 秒" in prompt


def test_narration_duration_never_below_rule_estimate():
    script = "旁白：这是一段大约三十多个字的旁白内容用来验证时长下限保护确实生效。"
    annotations = [{"line": 0, "type": "narration", "duration": 2}]
    _, units = StoryboardAgent._annotate_episode_script(
        script,
        characters=[],
        settings=[],
        annotations=annotations,
        scene_spans=[],
        line_offset=0,
        baseline_line_count=1,
    )
    rule = StoryboardAgent._duration_for_script_unit(script, True)
    assert units and units[0]["is_narration"] is True
    assert units[0]["duration"] >= rule > 2, "旁白的下限保护不能退化成固定 2 秒"


def test_scene_desc_duration_does_not_leak_across_scenes():
    # 原 bug 形态：画面描述后直接换场景，同场没有单元消费 pending
    script = "\n".join([
        "【画面描述】第一场办公室全景。",
        "【第二场】",
        "小周：嗯。",
    ])
    annotations = [
        {"line": 0, "type": "scene_desc", "duration": 9},
        {"line": 1, "type": "heading", "title": "第二场"},
        {"line": 2, "type": "dialogue", "speaker": "小周"},
    ]
    _, units = StoryboardAgent._annotate_episode_script(
        script,
        characters=[{"name": "小周"}],
        settings=[],
        annotations=annotations,
        scene_spans=[],
        line_offset=0,
        baseline_line_count=3,
    )
    by_line = {unit["line_number"]: unit for unit in units}
    assert by_line[3]["duration"] < 9, "第二场不得继承上一场的画面停留"


def test_review_context_ignores_stale_and_truncated():
    stale_issues, stale_total = StoryboardAgent._review_context(
        {"import_review": {"stale": True, "issues": [{"line": 0, "problem": "旧问题"}]}},
        {"suggested_total_seconds": 120},
    )
    assert stale_issues == []
    assert stale_total is None, "正文已改时旧建议总时长必须失效"

    issues, total = StoryboardAgent._review_context(
        {
            "import_review": {
                "issues": [{"line": 0, "problem": "台词偏长"}],
                "coverage": {"truncated": True},
            }
        },
        {"suggested_total_seconds": 120},
    )
    assert issues, "截断时问题清单仍可参考"
    assert total is None, "截断时建议总时长只代表节选，不得折算成每集目标"


def test_planning_hint_filters_issues_by_episode_range():
    hint = StoryboardAgent._build_planning_hint({
        "line_start": 10,
        "line_end": 19,
        "review_issues": [
            {"line": 12, "problem": "本集问题", "suggestion": "调整"},
            {"line": 25, "problem": "别集问题", "suggestion": "调整"},
        ],
    })
    assert "本集问题" in hint
    assert "别集问题" not in hint, "多集时不得把别集问题注入本集提示词"
