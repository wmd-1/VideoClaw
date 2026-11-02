"""Q1 接线回归：视频侧当前指纹与 H3 侧同源，解析结果只读、可落产物。"""
from __future__ import annotations

import copy

from core.agents.prompt_rewrite_agent import PromptRewriteAgent
from core.agents.video_agent import VideoDirectorAgent
from core.h3_modes import (
    collect_segments,
    fingerprint_parts,
    resolve_grounding_model,
    resolve_style_key,
)

MODEL = "m1"
STORYBOARD = {"episodes": [{"episode_number": 1, "segments": [
    {"segment_id": "seg_01_01", "location": "支行办公室", "characters": ["小陈"],
     "shots": [{"content": "镜头A", "duration": 8}]},
    {"segment_id": "seg_01_02", "location": "支行办公室", "characters": ["小陈"],
     "shots": [{"content": "镜头B", "duration": 8}]},
]}]}

ARTIFACTS = {
    "character_design": {
        "characters": [{"id": "c1", "name": "小陈", "selected": "/code/result/_media/c1.png",
                        "description": "银行职员"}],
        "settings": [{"id": "s1", "name": "支行办公室", "selected": "/code/result/_media/s1.png"}],
    },
    "reference_generation": {"scenes": [
        {"id": "seg_01_01", "selected": "/code/result/_media/f1.png"},
        {"id": "seg_01_02", "selected": "/code/result/_media/f2.png"},
    ]},
}


def _setup():
    h3_segments = collect_segments(STORYBOARD, model=MODEL)
    h3_by_id = {item["id"]: item for item in h3_segments}
    scene_map = {item["id"]: item for item in ARTIFACTS["reference_generation"]["scenes"]}
    style = resolve_style_key({}, {})
    grounding = resolve_grounding_model({}, {})
    return h3_segments, h3_by_id, scene_map, style, grounding


def _fingerprint(seg, h3_segments, scene_map, style, grounding):
    return fingerprint_parts(
        seg, "I2VA", h3_segments, scene_map, ARTIFACTS,
        style=style, grounding_model=grounding,
    )


def test_video_fingerprint_matches_h3_agent():
    h3_segments, h3_by_id, scene_map, style, grounding = _setup()
    seg = h3_by_id["seg_01_01"]
    video_fp, _ = _fingerprint(seg, h3_segments, scene_map, style, grounding)
    h3_fp = PromptRewriteAgent()._input_fingerprint(
        seg, "I2VA", h3_segments, scene_map, ARTIFACTS, {}, {},
        grounding_model=grounding,
    )
    assert video_fp == h3_fp, "跨模块指纹必须一致，否则会恒 stale"


def test_fresh_h3_used_and_item_not_modified():
    h3_segments, h3_by_id, scene_map, style, grounding = _setup()
    seg = h3_by_id["seg_01_01"]
    fingerprint, dimensions = _fingerprint(seg, h3_segments, scene_map, style, grounding)
    h3_item = {"id": "seg_01_01", "status": "done", "rewritten_prompt": "H3 正文",
               "input_fingerprint": fingerprint, "input_dimensions": dimensions}
    snapshot = copy.deepcopy(h3_item)

    result = VideoDirectorAgent()._prompt_resolution(
        "seg_01_01", "first_frame", h3_by_id, h3_segments, scene_map, ARTIFACTS,
        style, grounding, {"description": "分镜描述"}, {"items": [h3_item]},
    )
    assert result["text"] == "H3 正文"
    assert result["source"] == "h3"
    assert result["freshness"] == "fresh"
    assert h3_item == snapshot, "视频阶段只能读，不得修改 H3 条目"


def test_stale_h3_falls_back_to_description_with_reason():
    h3_segments, h3_by_id, scene_map, style, grounding = _setup()
    seg = h3_by_id["seg_01_01"]
    stale_fp, stale_dims = _fingerprint({**seg, "duration": 12}, h3_segments, scene_map, style, grounding)
    h3_item = {"id": "seg_01_01", "status": "done", "rewritten_prompt": "H3 正文",
               "input_fingerprint": stale_fp, "input_dimensions": stale_dims}

    result = VideoDirectorAgent()._prompt_resolution(
        "seg_01_01", "first_frame", h3_by_id, h3_segments, scene_map, ARTIFACTS,
        style, grounding, {"description": "分镜描述"}, {"items": [h3_item]},
    )
    assert result["text"] == "分镜描述"
    assert result["source"] == "description"
    assert result["freshness"] == "stale"
    assert result["stale_reasons"] == ["duration_changed"]


def test_missing_prompt_rewrite_artifact_reports_absent():
    h3_segments, h3_by_id, scene_map, style, grounding = _setup()
    result = VideoDirectorAgent()._prompt_resolution(
        "seg_01_01", "first_frame", h3_by_id, h3_segments, scene_map, ARTIFACTS,
        style, grounding, {"description": "分镜描述"}, {},
    )
    assert result["text"] == "分镜描述"
    assert result["stale_reasons"] == ["h3_absent"]


VIDEO_SEGMENT = {
    "segment_id": "seg_01_01", "segment_name": "第1集-片段1", "total_duration": 8,
    "shots": [{"content": "走进办公室", "duration": 8}],
}


def test_final_prompt_description_uses_full_assembly():
    """description 分支不得退化成一句分镜描述：风格/人物/负向约束必须保留。"""
    agent = VideoDirectorAgent()
    resolution = {"text": "走进办公室", "source": "description", "freshness": "stale",
                  "stale_reasons": ["duration_changed"]}
    prompt = agent._final_prompt(
        VIDEO_SEGMENT, "anime style,", ARTIFACTS["character_design"],
        video_data={"description": "走进办公室"}, prompt_rewrite_art=None,
        seg_id="seg_01_01", resolution=resolution,
    )
    assert "风格控制：" in prompt
    assert "人物列表：" in prompt
    assert prompt == agent._assemble_prompt(
        VIDEO_SEGMENT, "anime style,", ARTIFACTS["character_design"],
        video_data={"description": "走进办公室"},
    )


def test_final_prompt_h3_and_manual_are_verbatim():
    agent = VideoDirectorAgent()
    for source, text in (("h3", "H3 完整正文"), ("manual", "人工完整正文")):
        prompt = agent._final_prompt(
            VIDEO_SEGMENT, "anime style,", ARTIFACTS["character_design"],
            video_data={"description": "描述"}, prompt_rewrite_art=None,
            seg_id="seg_01_01",
            resolution={"text": text, "source": source, "freshness": "fresh",
                        "stale_reasons": []},
        )
        assert prompt == text
