"""WF-04：读时派生的 needs_regeneration（不落库，按当前 artifacts+meta 重算）。"""
from __future__ import annotations

import copy

from core.h3_modes import collect_segments, current_fingerprint, resolve_grounding_model
from core.prompt_freshness import annotate_regeneration

STORYBOARD = {"episodes": [{"episode_number": 1, "segments": [
    {"segment_id": "seg_01_01", "location": "会议室", "characters": ["小陈"],
     "total_duration": 8, "shots": [{"content": "镜头A", "duration": 8}]},
]}]}

ARTIFACTS = {
    "storyboard": STORYBOARD,
    "character_design": {
        "characters": [{"id": "c1", "name": "小陈", "selected": "", "description": "银行职员"}],
        "settings": [{"id": "s1", "name": "会议室", "selected": "", "description": "会议室"}],
    },
    "reference_generation": {"scenes": [{"id": "seg_01_01", "selected": ""}]},
}
META = {"video_first_frame_model": "m1", "video_generation_mode": "first_frame", "style": "realistic"}


def _current(meta=None, duration=8, style="realistic"):
    meta = meta or META
    segments = collect_segments(STORYBOARD, model="m1")
    scene_map = {item["id"]: item for item in ARTIFACTS["reference_generation"]["scenes"]}
    segment = {**segments[0], "duration": duration}
    _mode, fingerprint, dimensions = current_fingerprint(
        "seg_01_01", meta.get("video_generation_mode") or "first_frame",
        [segment], scene_map, ARTIFACTS, style=style,
        grounding_model=resolve_grounding_model({}, meta),
    )
    return fingerprint, dimensions


def _artifacts(items=None, clips=None):
    artifacts = copy.deepcopy(ARTIFACTS)
    artifacts["prompt_rewrite"] = {"items": items or []}
    artifacts["video_generation"] = {"clips": clips or []}
    return artifacts


def _h3(fingerprint, dimensions, **overrides):
    item = {"id": "seg_01_01", "status": "done", "rewritten_prompt": "H3 正文",
            "input_fingerprint": fingerprint, "input_dimensions": dimensions}
    item.update(overrides)
    return item


def _clip(**overrides):
    clip = {"id": "seg_01_01", "description": "描述", "versions": [], "selected": ""}
    clip.update(overrides)
    return clip


def test_fresh_h3_needs_no_regeneration():
    fingerprint, dimensions = _current()
    artifacts = _artifacts(items=[_h3(fingerprint, dimensions)])
    annotate_regeneration(artifacts, META)
    item = artifacts["prompt_rewrite"]["items"][0]
    assert item["needs_regeneration"] is False
    assert item["regeneration_reasons"] == []


def test_changed_duration_marks_h3_with_reason():
    fingerprint, dimensions = _current()
    stale_fp, stale_dims = _current(duration=10)
    artifacts = _artifacts(items=[_h3(stale_fp, stale_dims)])
    annotate_regeneration(artifacts, META)
    item = artifacts["prompt_rewrite"]["items"][0]
    assert item["needs_regeneration"] is True
    assert item["regeneration_reasons"] == ["duration_changed"]


def test_style_change_marks_h3_even_without_artifact_patch():
    fingerprint, dimensions = _current()
    artifacts = _artifacts(items=[_h3(fingerprint, dimensions)])
    annotate_regeneration(artifacts, {**META, "style": "anime"})
    item = artifacts["prompt_rewrite"]["items"][0]
    assert item["needs_regeneration"] is True
    assert item["regeneration_reasons"] == ["style_changed"]


def test_missing_fingerprint_is_not_fresh():
    artifacts = _artifacts(items=[_h3("", {})])
    annotate_regeneration(artifacts, META)
    item = artifacts["prompt_rewrite"]["items"][0]
    assert item["needs_regeneration"] is True
    assert item["regeneration_reasons"] == ["fingerprint_missing"]


def test_not_done_h3_reports_not_ready():
    artifacts = _artifacts(items=[_h3("", {}, status="failed")])
    annotate_regeneration(artifacts, META)
    assert artifacts["prompt_rewrite"]["items"][0]["regeneration_reasons"] == ["h3_not_ready"]


def test_manual_fresh_but_old_generation_needs_regeneration():
    """人工正文新鲜但视频用旧正文生成：clip 仍需重生成（语义只看生成指纹）。"""
    fingerprint, dimensions = _current()
    old_fp, _ = _current(duration=10)
    artifacts = _artifacts(clips=[_clip(
        manual_prompt="人工正文", manual_prompt_fingerprint=fingerprint,
        manual_prompt_dimensions=dimensions, resolved_fingerprint=old_fp,
        prompt_source={"source": "manual", "freshness": "fresh", "stale_reasons": []},
    )])
    annotate_regeneration(artifacts, META)
    clip = artifacts["video_generation"]["clips"][0]
    assert clip["needs_regeneration"] is True
    assert clip["regeneration_reasons"] == ["fingerprint_changed"]


def test_manual_stale_but_video_matches_current_is_fresh_for_video():
    """人工正文 stale 只影响徽标；视频已经用了当前正文时，不应要求重生成。"""
    fingerprint, _ = _current()
    stale_fp, stale_dims = _current(duration=10)
    artifacts = _artifacts(clips=[_clip(
        manual_prompt="人工正文", manual_prompt_fingerprint=stale_fp,
        manual_prompt_dimensions=stale_dims, resolved_fingerprint=fingerprint,
        prompt_source={"source": "manual", "freshness": "stale",
                       "stale_reasons": ["duration_changed"]},
    )])
    annotate_regeneration(artifacts, META)
    clip = artifacts["video_generation"]["clips"][0]
    assert clip["needs_regeneration"] is False
    assert clip["regeneration_reasons"] == []


def test_clip_without_resolved_fingerprint_is_not_fresh():
    artifacts = _artifacts(clips=[_clip()])
    annotate_regeneration(artifacts, META)
    clip = artifacts["video_generation"]["clips"][0]
    assert clip["needs_regeneration"] is True
    assert clip["regeneration_reasons"] == ["fingerprint_missing"]


def test_clip_without_resolved_reuses_prompt_source_reasons():
    """旧产物：缺 resolved_fingerprint 但 prompt_source 已给出原因，不得退回 fingerprint_missing。"""
    artifacts = _artifacts(clips=[_clip(prompt_source={
        "source": "description", "freshness": "stale",
        "stale_reasons": ["shot_changed", "duration_changed"],
    })])
    annotate_regeneration(artifacts, META)
    clip = artifacts["video_generation"]["clips"][0]
    assert clip["needs_regeneration"] is True
    assert clip["regeneration_reasons"] == ["shot_changed", "duration_changed"]


def test_clip_with_matching_resolved_fingerprint_is_fresh():
    fingerprint, _ = _current()
    artifacts = _artifacts(clips=[_clip(resolved_fingerprint=fingerprint)])
    annotate_regeneration(artifacts, META)
    assert artifacts["video_generation"]["clips"][0]["needs_regeneration"] is False
