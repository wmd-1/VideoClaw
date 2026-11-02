"""Q1 回归：有效提示词解析单点（优先级 / 两类 stale 分流 / 结构化原因）。"""
from __future__ import annotations

from core.prompt_fingerprint import build_payload, dimension_hashes, sha_of
from core.prompt_source import resolve_effective_prompt

SEGMENT = {"id": "s1", "shot_content": "分镜描述正文", "duration": 8}


def _current(**overrides):
    payload = build_payload(
        shot_content=SEGMENT["shot_content"],
        duration=SEGMENT["duration"],
        mode="I2VA",
        references=[("reference_generation", "scenes", "/code/result/_media/f1.png")],
        style="写实",
        facts=[],
        grounding_model="vlm-a",
    )
    payload.update(overrides)
    return sha_of(payload), dimension_hashes(payload)


def _h3(**overrides):
    fingerprint, dimensions = _current()
    item = {
        "id": "s1",
        "status": "done",
        "rewritten_prompt": "H3 正文",
        "input_fingerprint": fingerprint,
        "input_dimensions": dimensions,
        "grounding": {"model": "vlm-a", "described": True},
    }
    item.update(overrides)
    return item


def test_manual_fresh_wins_over_h3():
    fingerprint, dimensions = _current()
    result = resolve_effective_prompt(
        SEGMENT, _h3(), {"manual_prompt": "人工正文", "manual_prompt_fingerprint": fingerprint},
        current_fingerprint=fingerprint, current_dimensions=dimensions,
    )
    assert result == {"text": "人工正文", "source": "manual", "freshness": "fresh",
                      "stale_reasons": []}


def test_manual_stale_still_used():
    fingerprint, dimensions = _current()
    result = resolve_effective_prompt(
        SEGMENT, _h3(), {"manual_prompt": "人工正文", "manual_prompt_fingerprint": "old"},
        current_fingerprint=fingerprint, current_dimensions=dimensions,
    )
    assert result["text"] == "人工正文"
    assert result["source"] == "manual"
    assert result["freshness"] == "stale"
    assert result["stale_reasons"] == ["fingerprint_changed"]


def test_h3_fresh_used_without_manual():
    fingerprint, dimensions = _current()
    result = resolve_effective_prompt(
        SEGMENT, _h3(), {"description": "分镜描述正文"},
        current_fingerprint=fingerprint, current_dimensions=dimensions,
    )
    assert result == {"text": "H3 正文", "source": "h3", "freshness": "fresh",
                      "stale_reasons": []}


def test_h3_stale_falls_back_to_description_with_reasons():
    fingerprint, dimensions = _current()
    stale_fp, stale_dims = _current(duration=12)
    result = resolve_effective_prompt(
        SEGMENT, _h3(input_fingerprint=stale_fp, input_dimensions=stale_dims),
        {"description": "分镜描述正文"},
        current_fingerprint=fingerprint, current_dimensions=dimensions,
    )
    assert result["text"] == "分镜描述正文"
    assert result["source"] == "description"
    assert result["freshness"] == "stale"
    assert result["stale_reasons"] == ["duration_changed"]


def test_h3_missing_fingerprint_is_unknown_and_not_used():
    fingerprint, dimensions = _current()
    result = resolve_effective_prompt(
        SEGMENT, _h3(input_fingerprint="", input_dimensions={}),
        {"description": "分镜描述正文"},
        current_fingerprint=fingerprint, current_dimensions=dimensions,
    )
    assert result["source"] == "description"
    assert result["freshness"] == "unknown"
    assert result["stale_reasons"] == ["fingerprint_missing"]


def test_legacy_item_without_manual_is_unknown():
    fingerprint, dimensions = _current()
    result = resolve_effective_prompt(
        SEGMENT, {}, {"description": "用户以前编辑过的正文"},
        current_fingerprint=fingerprint, current_dimensions=dimensions,
    )
    assert result["source"] == "description"
    assert result["freshness"] == "unknown"


def test_grounding_model_change_is_reported():
    fingerprint, dimensions = _current()
    stale_fp, stale_dims = _current(grounding_model="vlm-b")
    result = resolve_effective_prompt(
        SEGMENT, _h3(input_fingerprint=stale_fp, input_dimensions=stale_dims), {"description": "d"},
        current_fingerprint=fingerprint, current_dimensions=dimensions,
    )
    assert "grounding_model_changed" in result["stale_reasons"]


def test_missing_current_dimensions_does_not_claim_all_changed():
    fingerprint, _ = _current()
    stale_fp, _ = _current(duration=12)
    result = resolve_effective_prompt(
        SEGMENT, _h3(input_fingerprint=stale_fp), {"description": "d"},
        current_fingerprint=fingerprint, current_dimensions=None,
    )
    assert result["stale_reasons"] == ["fingerprint_changed"]


def test_old_item_without_dimensions_reports_changed_not_missing():
    fingerprint, dimensions = _current()
    stale_fp, _ = _current(duration=12)
    result = resolve_effective_prompt(
        SEGMENT, _h3(input_fingerprint=stale_fp, input_dimensions={}), {"description": "d"},
        current_fingerprint=fingerprint, current_dimensions=dimensions,
    )
    assert result["stale_reasons"] == ["fingerprint_changed"]


def test_h3_item_not_done_reports_not_ready():
    fingerprint, dimensions = _current()
    result = resolve_effective_prompt(
        SEGMENT, _h3(status="failed"), {"description": "d"},
        current_fingerprint=fingerprint, current_dimensions=dimensions,
    )
    assert result["source"] == "description"
    assert result["stale_reasons"] == ["h3_not_ready"]
