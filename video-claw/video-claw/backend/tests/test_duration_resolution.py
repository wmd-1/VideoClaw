"""Q2 回归：时长单一解析（会话覆盖 > 分镜 > 默认 → 模型能力夹取）。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

import pytest

import models.config_model as config_model
from core.agents.prompt_rewrite_agent import PromptRewriteAgent
from core.video_params import duration_range, resolve_effective_duration
from models.custom_video import CustomVideoClient

STORYBOARD = {"episodes": [{"episode_number": 1, "segments": [
    {"segment_id": "seg_01_01", "total_duration": 8, "characters": [],
     "shots": [{"content": "镜头", "duration": 8}]},
]}]}


def _patch_range(monkeypatch, low, high):
    monkeypatch.setattr(
        config_model, "get_model_config",
        lambda model: {"capabilities": {"duration": {"min": low, "max": high}}},
    )


def test_priority_session_override_over_segment():
    monkeypatch_free = resolve_effective_duration(8, 5, model="")
    assert monkeypatch_free == (5, [])


def test_falls_back_to_segment_then_default():
    assert resolve_effective_duration(6, None, model="") == (6, [])
    assert resolve_effective_duration(None, None, model="") == (8, [])


def test_clamps_to_model_capability_and_reports_adjustment(monkeypatch):
    _patch_range(monkeypatch, 5, 10)
    value, adjustments = resolve_effective_duration(8, 12, model="m1")
    assert value == 10
    assert adjustments and "会话覆盖" in adjustments[0]


def test_in_range_value_has_no_adjustment(monkeypatch):
    _patch_range(monkeypatch, 5, 10)
    assert resolve_effective_duration(8, 7, model="m1") == (7, [])


def test_invalid_capability_range_fails_loudly(monkeypatch):
    _patch_range(monkeypatch, 10, 5)
    with pytest.raises(ValueError, match="非法"):
        duration_range("m1")


def test_h3_segments_use_effective_duration_and_expose_adjustment(monkeypatch):
    _patch_range(monkeypatch, 5, 10)
    segments = PromptRewriteAgent._collect_segments(STORYBOARD, model="m1", session_override=12)
    segment = segments[0]
    assert segment["duration"] == 10
    assert segment["duration_adjustments"], "夹取必须留下可见 adjustment"


def test_h3_and_adapter_agree_on_final_duration(monkeypatch):
    """断言：H3 目标时长 == 适配器最终请求时长（不是两边各自自洽）。"""
    _patch_range(monkeypatch, 5, 10)
    segment = PromptRewriteAgent._collect_segments(
        STORYBOARD, model="m1", session_override=12
    )[0]

    client = CustomVideoClient(
        {"id": "m1", "protocol": "vllm-omni", "base_url": "http://127.0.0.1:1/v1",
         "capabilities": {"duration": {"min": 5, "max": 10}}},
        poll_interval=0.01, poll_timeout=1, timeout=1,
    )
    adapter_duration = client._clamp_duration(segment["duration"])
    assert adapter_duration == segment["duration"], "改写与适配器必须得到同一时长"


def test_h3_item_carries_duration_adjustment(monkeypatch):
    _patch_range(monkeypatch, 5, 10)
    segment = PromptRewriteAgent._collect_segments(
        STORYBOARD, model="m1", session_override=12
    )[0]
    item = PromptRewriteAgent._make_item(segment, "", "T2VA")
    assert item["duration"] == 10
    assert item["duration_adjustment"] == segment["duration_adjustments"]
