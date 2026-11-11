"""关键帧对齐、镜头时间轴与参考标签的确定性校验。"""
from __future__ import annotations

import asyncio

from config import Config
from core.agents.prompt_rewrite_agent import (
    PromptRewriteAgent,
    _normalize_keyframe_spacing,
    _validate_h3_prompt,
)

BASE_FIELDS = (
    "integrated_multimodal_description: [Shot 1] A young man stands in a bank hall.\n"
    "overall_soundscape: Quiet interior room tone with distant footsteps.\n"
    "non_diegetic_music: Low strings fade in and last {duration} seconds.\n"
)


def _i2va(duration: int = 5, extra: str = "") -> str:
    return (
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
        + extra
        + BASE_FIELDS.format(duration=duration)
    )


def _fl2va(duration: int = 8, last_mark: str = "8.00") -> str:
    return (
        "How the reference pictures align with the target video — "
        "Picture 1 (from [Shot 1]) aligns with the 0.00-second mark of the target video; "
        f"Picture 2 (from [Shot 1]) aligns with the {last_mark}-second mark of the target video.\n\n"
        + BASE_FIELDS.format(duration=duration)
    )


def _l2va(duration: int = 6, mark: str = "6.00") -> str:
    return (
        "How the reference pictures align with the target video — "
        f"<Picture 1> (from [Shot 1]) aligns with the {mark}-second mark of the target video.\n\n"
        + BASE_FIELDS.format(duration=duration)
    )


def _t2va(duration: int = 5) -> str:
    return BASE_FIELDS.format(duration=duration)


def _has(violations, keyword: str) -> bool:
    return any(keyword in item for item in violations)


def test_official_sentences_pass_without_false_positive():
    assert _validate_h3_prompt(_i2va(), "I2VA", 5, picture_count=1) == []
    assert _validate_h3_prompt(_fl2va(), "FL2VA", 8, picture_count=2) == []
    assert _validate_h3_prompt(_l2va(), "L2VA", 6, picture_count=1) == []
    assert _validate_h3_prompt(_t2va(), "T2VA", 5, picture_count=0) == []


def test_i2va_requires_official_first_frame_sentence():
    wrong = _fl2va(duration=5, last_mark="5.00")
    violations = _validate_h3_prompt(wrong, "I2VA", 5, picture_count=1)
    assert _has(violations, "I2VA 首帧对齐声明")


def test_fl2va_last_frame_mark_must_equal_duration():
    violations = _validate_h3_prompt(_fl2va(last_mark="6.00"), "FL2VA", 8, picture_count=2)
    assert _has(violations, "8.00")


def test_alignment_mark_requires_two_decimals():
    violations = _validate_h3_prompt(_fl2va(last_mark="8.0"), "FL2VA", 8, picture_count=2)
    assert _has(violations, "两位小数")


def test_alignment_must_be_first_line_with_blank_line():
    text = _i2va().replace(
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.\n\n",
        "Style: live-action.\n"
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.\n",
    )
    violations = _validate_h3_prompt(text, "I2VA", 5, picture_count=1)
    assert _has(violations, "I2VA 首帧对齐声明")
    assert _has(violations, "缺少空行")


def test_first_shot_must_not_have_timestamp():
    text = _i2va(extra="").replace("[Shot 1] A young man", "[Shot 1] At 00:00.000, a young man")
    violations = _validate_h3_prompt(text, "I2VA", 5, picture_count=1)
    assert _has(violations, "首个镜头不得添加时间戳")


def test_cut_time_must_be_strictly_increasing_and_within_duration():
    increasing = _t2va(8).replace(
        "[Shot 1] A young man stands in a bank hall.",
        "[Shot 1] A young man stands in a bank hall. [Shot 2] At 00:03.500, the camera cuts to a counter. "
        "[Shot 3] At 00:06.000, the camera cuts to a clock.",
    )
    assert _validate_h3_prompt(increasing, "T2VA", 8, picture_count=0) == []

    decreasing = increasing.replace("At 00:06.000", "At 00:02.000")
    assert _has(_validate_h3_prompt(decreasing, "T2VA", 8, picture_count=0), "严格递增")

    out_of_range = increasing.replace("At 00:06.000", "At 00:09.000")
    assert _has(_validate_h3_prompt(out_of_range, "T2VA", 8, picture_count=0), "超出目标时长")


def test_picture_label_count_must_match_assets():
    violations = _validate_h3_prompt(_i2va(extra="<Picture 2> also appears. "), "I2VA", 5, picture_count=1)
    assert _has(violations, "超出实际素材数量")

    t2va_with_label = _t2va().replace("[Shot 1]", "[Shot 1] <Picture 1>")
    assert _has(_validate_h3_prompt(t2va_with_label, "T2VA", 5, picture_count=0), "超出实际素材数量")


def test_ref2va_defined_but_not_cited_is_allowed():
    text = (
        "subject_definitions:\n"
        "<Subject 1> is the woman in <Picture 2>.\n"
        "summary:\nA woman walks in the rain.\n"
        "retention_analysis:\n<Subject 1> appears throughout the video.\n"
        "detailed_description:\nThe woman walks in the rain for 8 seconds.\n"
        "overall_soundscape:\nRain and footsteps.\n"
        "non_diegetic_music:\nLow strings.\n"
    )
    assert _validate_h3_prompt(text, "Ref2VA", 8, picture_count=2) == []


def test_ref2va_undefined_label_is_rejected_and_base_modes_are_exempt():
    text = (
        "subject_definitions:\n"
        "<Subject 1> is the woman in <Picture 1>.\n"
        "summary:\nA woman walks in the rain.\n"
        "retention_analysis:\n<Subject 1> appears throughout the video.\n"
        "detailed_description:\nThe woman from <Picture 3> walks for 8 seconds.\n"
        "overall_soundscape:\nRain and footsteps.\n"
        "non_diegetic_music:\nLow strings.\n"
    )
    violations = _validate_h3_prompt(text, "Ref2VA", 8, picture_count=None)
    assert _has(violations, "未定义的参考标签")

    i2va_violations = _validate_h3_prompt(_i2va(), "I2VA", 5, picture_count=1)
    assert not _has(i2va_violations, "未定义的参考标签")


def test_keyframe_spacing_is_normalized_before_validation():
    broken = _i2va().replace("\n\nintegrated_multimodal_description", "\nintegrated_multimodal_description")
    assert _has(_validate_h3_prompt(broken, "I2VA", 5, picture_count=1), "缺少空行")
    fixed = _normalize_keyframe_spacing(broken, "I2VA")
    assert "\n\nintegrated_multimodal_description" in fixed
    assert _validate_h3_prompt(fixed, "I2VA", 5, picture_count=1) == []


def test_extra_alignment_mark_is_rejected():
    text = _l2va(mark="6.00").replace(
        "aligns with the 6.00-second mark",
        "aligns with the 6.00-second mark and the 3.00-second mark",
    )
    assert _has(_validate_h3_prompt(text, "L2VA", 6, picture_count=1), "允许值不一致")


def test_later_shot_without_cut_time_is_rejected():
    text = _t2va(8).replace(
        "[Shot 1] A young man stands in a bank hall.",
        "[Shot 1] A young man stands in a bank hall. [Shot 2] The camera cuts to a counter.",
    )
    assert _has(_validate_h3_prompt(text, "T2VA", 8, picture_count=0), "缺少严格递增的 cut time")


def test_l2va_production_input_uses_next_segment_frame(monkeypatch):
    """接线级：本分镜无 selected、下一分镜有 selected 时，resolver 落 L2VA 且按尾帧计 1 张图。"""
    agent = PromptRewriteAgent()
    seen = {}

    async def fake_rewrite_one(segment, mode, system_prompt, guide, llm_model, sections,
                               picture_count=None):
        seen[segment["id"]] = (mode, picture_count)
        return "改写结果", [], "", None

    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    monkeypatch.setattr(agent, "_report_progress", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_session_meta", lambda input_data: {"video_generation_mode": "start_end_frame"})
    monkeypatch.setattr(agent, "_session_artifacts", lambda input_data: {
        "reference_generation": {"scenes": [
            {"id": "seg_01", "selected": ""},
            {"id": "seg_02", "selected": "b.png"},
        ]},
    })
    monkeypatch.setattr(agent, "_load_guide", lambda mode: "")
    monkeypatch.setattr(agent, "_grounding_describe", lambda *a, **k: ("", []))
    monkeypatch.setattr(agent, "_rewrite_one", fake_rewrite_one)

    seg1 = {"id": "seg_01", "name": "s1", "index": 1, "episode": 1,
            "shot_content": "小陈站立", "duration": 5, "characters": [], "location": ""}
    seg2 = {"id": "seg_02", "name": "s2", "index": 2, "episode": 1,
            "shot_content": "小陈转身", "duration": 5, "characters": [], "location": ""}
    asyncio.run(agent._rewrite_segments(
        [seg1], {"video_generation_mode": "start_end_frame"}, {},
        ordered_segments=[seg1, seg2],
    ))

    assert seen == {"seg_01": ("L2VA", 1)}


def test_rewrite_one_normalizes_missing_blank_line(monkeypatch):
    """接线级：mock LLM 返回缺空行输出 → 产物由规范化补齐且零违规、不消耗重试。"""
    agent = PromptRewriteAgent()
    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    monkeypatch.setattr(Config, "H3_REWRITE_JUDGE_ENABLED", False)
    broken = _i2va().replace("\n\nintegrated_multimodal_description", "\nintegrated_multimodal_description")
    calls = []

    def fake_query(*args, **kwargs):
        calls.append(1)
        return broken

    monkeypatch.setattr(agent, "_query_llm", fake_query)
    sections = PromptRewriteAgent._render_sections("", "小陈站立", "", [], "")
    segment = {"id": "seg_01", "name": "s", "index": 1, "episode": 1,
               "shot_content": "小陈站立", "duration": 5, "characters": [], "location": ""}
    rewritten, violations, _, quality = asyncio.run(agent._rewrite_one(
        segment, "I2VA", "system", "guide", "fake-model", sections, picture_count=1,
    ))

    assert rewritten
    assert "\n\nintegrated_multimodal_description" in rewritten
    assert violations == []
    assert quality is None
    assert len(calls) == 1
