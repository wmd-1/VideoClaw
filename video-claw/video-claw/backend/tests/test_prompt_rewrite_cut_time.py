"""cut time 韧性：格式确定性规范化、镜头表提示注入与校验闭环边界。

覆盖 openspec change `stage-stop-and-rewrite-resilience` 的 h3-prompt-rewriter 增量需求。
"""
from __future__ import annotations

from core.agents.prompt_rewrite_agent import (
    PromptRewriteAgent,
    _normalize_cut_time_format,
    _shot_plan_text,
    _validate_h3_prompt,
)
from core.h3_modes import collect_segments


def _t2va(body: str, seconds: int = 8) -> str:
    return (
        "integrated_multimodal_description: "
        f"[Shot 1] A young man stands in a bank hall. {body} "
        f"The scene lasts {seconds} seconds.\n"
        "overall_soundscape: Quiet room tone with distant traffic.\n"
        "non_diegetic_music: N/A\n"
    )


def test_cut_time_variants_are_normalized_to_official_format():
    assert "[Shot 2] At 00:03.500" in _normalize_cut_time_format("[Shot 2] At 3.5s, the camera cuts to X")
    assert "[Shot 3] At 00:05.200" in _normalize_cut_time_format("[Shot 3] At 0:05.2 the shot changes")
    assert "[Shot 2] At 00:03.000" in _normalize_cut_time_format("[Shot 2] At 00:03, cut")
    assert "[Shot 2] At 00:03.500" in _normalize_cut_time_format("[Shot 2] At 00:03:500, cut")
    assert "[Shot 2] At 00:03.500" in _normalize_cut_time_format("[Shot 2] At 3.5 seconds, cut")


def test_normalization_keeps_first_shot_timestamp_and_missing_cut_points():
    first_shot = "[Shot 1] At 00:00.000 opens"
    assert _normalize_cut_time_format(first_shot) == first_shot
    missing = "[Shot 2] The camera cuts to a counter."
    assert _normalize_cut_time_format(missing) == missing


def test_normalized_cut_time_passes_validation():
    raw = _t2va("[Shot 2] At 3.5s, the camera cuts to a teller counter.")
    assert any("缺少严格递增的 cut time" in item for item in _validate_h3_prompt(raw, "T2VA", 8, picture_count=0))
    fixed = _normalize_cut_time_format(raw)
    assert _validate_h3_prompt(fixed, "T2VA", 8, picture_count=0) == []


def test_missing_cut_time_still_rejected_after_normalization():
    raw = _t2va("[Shot 2] The camera cuts to a counter without a timestamp.")
    fixed = _normalize_cut_time_format(raw)
    violations = _validate_h3_prompt(fixed, "T2VA", 8, picture_count=0)
    assert any("缺少严格递增的 cut time" in item for item in violations)


def test_first_shot_timestamp_still_rejected_after_normalization():
    raw = _t2va("[Shot 2] At 00:03.500, cut.").replace(
        "[Shot 1] A young man stands in a bank hall.",
        "[Shot 1] At 00:00.000, a young man stands in a bank hall.",
    )
    fixed = _normalize_cut_time_format(raw)
    violations = _validate_h3_prompt(fixed, "T2VA", 8, picture_count=0)
    assert any("首个镜头不得添加时间戳" in item for item in violations)


def test_shot_plan_lists_suggested_cut_points():
    segment = {
        "id": "seg_01", "duration": 10,
        "shots": [{"index": 1, "duration": 2}, {"index": 2, "duration": 3}, {"index": 3, "duration": 5}],
    }
    plan = _shot_plan_text(segment, 10)
    assert "本分镜共 3 个镜头" in plan
    assert "At 00:02.000" in plan
    assert "At 00:05.000" in plan
    assert "不得写时间戳" in plan


def test_shot_plan_omitted_when_durations_unavailable():
    assert _shot_plan_text({"duration": 8, "shots": [{"index": 1, "duration": 0}, {"index": 2, "duration": 0}]}, 8) == ""
    assert _shot_plan_text({"duration": 8, "shots": [{"index": 1, "duration": 8}]}, 8) == ""


def test_shot_plan_section_keeps_context_priority_order():
    sections = PromptRewriteAgent._render_sections(
        "AUTH\n", "CURRENT-SHOT", "【用户修改】\nUSER\n", [], "GROUNDING", shot_plan="SHOT-PLAN\n",
    )
    assert sections["shot_plan_section"] == "SHOT-PLAN\n"
    request = PromptRewriteAgent()._build_request_text(
        {"id": "s1", "duration": 8, "shot_content": "CURRENT-SHOT"}, "T2VA", sections,
    )
    assert request.index("【当前分镜】") < request.index("SHOT-PLAN")
    assert request.index("SHOT-PLAN") < request.index("GROUNDING")
    assert request.index("GROUNDING") < request.index("【用户修改】")


def test_collect_segments_exposes_shot_table_without_touching_existing_keys():
    storyboard = {
        "episodes": [{
            "episode_number": 1,
            "segments": [{
                "segment_id": "seg_01",
                "location": "银行大堂",
                "shots": [
                    {"content": "小陈站立", "duration": 2},
                    {"content": "小陈转身", "duration": 3},
                ],
            }],
        }],
    }
    segments = collect_segments(storyboard, model="m1", session_override=10)
    assert len(segments) == 1
    row = segments[0]
    assert row["shot_content"] == "小陈站立\n小陈转身"
    assert [shot["duration"] for shot in row["shots"]] == [2.0, 3.0]
    assert row["duration"] == 10
