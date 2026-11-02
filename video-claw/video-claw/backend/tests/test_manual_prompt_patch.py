"""Q1 第 4 步：PATCH 保存 manual_prompt 时即时算指纹；执行期保护清单包含该字段。"""
from __future__ import annotations

from types import SimpleNamespace

from core.h3_modes import collect_segments, current_fingerprint, resolve_grounding_model, resolve_style_key
from core.orchestrator import _USER_EDIT_FIELDS, WorkflowEngine

STORYBOARD = {"episodes": [{"episode_number": 1, "segments": [
    {"segment_id": "seg_01_01", "location": "支行办公室", "characters": ["小陈"],
     "shots": [{"content": "镜头A", "duration": 8}]},
]}]}

ARTIFACTS = {
    "storyboard": STORYBOARD,
    "character_design": {
        "characters": [{"id": "c1", "name": "小陈", "selected": "/code/result/_media/c1.png",
                        "description": "银行职员"}],
        "settings": [{"id": "s1", "name": "支行办公室", "selected": "/code/result/_media/s1.png"}],
    },
    "reference_generation": {"scenes": [
        {"id": "seg_01_01", "selected": "/code/result/_media/f1.png"},
    ]},
}


def test_manual_prompt_is_in_user_edit_fields():
    assert "manual_prompt" in _USER_EDIT_FIELDS


def test_patch_stamps_fingerprint_and_dimensions():
    state = SimpleNamespace(artifacts=ARTIFACTS, meta={"video_first_frame_model": "m1"})
    merged = {"id": "seg_01_01", "manual_prompt": "人工正文"}
    WorkflowEngine._stamp_manual_prompt(state, merged, "seg_01_01")

    h3_segments = collect_segments(STORYBOARD, model="m1")
    scene_map = {"seg_01_01": ARTIFACTS["reference_generation"]["scenes"][0]}
    _mode, fingerprint, dimensions = current_fingerprint(
        "seg_01_01", "first_frame", h3_segments, scene_map, ARTIFACTS,
        style=resolve_style_key({}, {}), grounding_model=resolve_grounding_model({}, {}),
    )
    assert merged["manual_prompt_fingerprint"] == fingerprint
    assert merged["manual_prompt_dimensions"] == dimensions
    assert dimensions, "必须有逐维度哈希，否则人工 stale 只能报“变了”"


def test_patch_stamp_failure_does_not_block_save():
    state = SimpleNamespace(artifacts=ARTIFACTS, meta={})  # 缺视频模型配置
    merged = {"id": "seg_01_01", "manual_prompt": "人工正文"}
    WorkflowEngine._stamp_manual_prompt(state, merged, "seg_01_01")
    assert merged["manual_prompt"] == "人工正文"
    assert "manual_prompt_fingerprint" not in merged
