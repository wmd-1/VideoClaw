"""5.5 回归：服务可用时视频阶段产物与模型选择行为不变。

覆盖：
- `_build_payload` 正常路径的字段集、selected/versions、stage_completed；
- 无失败标记时不出现 error/error_type；
- `_select_video_model` 的按模式选路与旧会话回退。
"""
from __future__ import annotations

import pytest

from core.agents.video_agent import VideoDirectorAgent

SEGMENTS = [{
    "segment_id": "s1", "episode_number": 1, "segment_number": 1,
    "total_duration": 8, "shots": [{"content": "镜头"}],
}]

PREV = {
    "id": "s1", "status": "done", "selected": "v1.mp4",
    "versions": ["v1.mp4", "v2.mp4"],
    "prompt_source": {"source": "h3", "freshness": "fresh", "stale_reasons": []},
    "submitted_prompt": "上次提交正文",
}


def _agent(monkeypatch, versions):
    agent = VideoDirectorAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, segment_id: list(versions))
    return agent


def test_available_service_keeps_normal_payload_shape(monkeypatch):
    agent = _agent(monkeypatch, ["v1.mp4", "v2.mp4"])
    result = agent._build_payload("sid", SEGMENTS, [PREV])
    clip = result["payload"]["clips"][0]

    assert result["stage_completed"] is True
    assert result["payload"]["session_id"] == "sid"
    assert clip["id"] == "s1"
    assert clip["name"] == "第1集-片段1"
    assert clip["episode"] == 1 and clip["index"] == 1
    assert clip["duration"] == 8
    assert clip["versions"] == ["v1.mp4", "v2.mp4"]
    assert clip["selected"] == "v1.mp4", "版本数未增长时沿用已存选中"
    assert clip["status"] == "done"
    # 服务可用（无 marker）时不得带失败字段
    assert "error" not in clip
    assert "error_type" not in clip
    # 未重算的解析链字段沿用已存
    assert clip["prompt_source"]["source"] == "h3"
    assert clip["submitted_prompt"] == "上次提交正文"


def test_this_run_resolution_overrides_carried_fields(monkeypatch):
    agent = _agent(monkeypatch, ["v1.mp4", "v2.mp4"])
    agent._prompt_sources["s1"] = {
        "source": "description", "freshness": "stale",
        "stale_reasons": ["duration_changed"], "submitted_prompt": "本轮正文",
        "fingerprint": "fp", "dimensions": {"duration": "d1"},
    }
    clip = agent._build_payload("sid", SEGMENTS, [PREV])["payload"]["clips"][0]
    assert clip["prompt_source"]["source"] == "description"
    assert clip["submitted_prompt"] == "本轮正文"
    assert clip["resolved_fingerprint"] == "fp"


def test_failure_marker_only_applies_to_failed_clips(monkeypatch):
    agent = _agent(monkeypatch, [])
    failed = agent._build_payload(
        "sid", SEGMENTS, [], failure_markers={
            "s1": {"error_type": "model_unavailable", "error": "模型不可用"},
        },
    )["payload"]["clips"][0]
    assert failed["status"] == "failed"
    assert failed["error_type"] == "model_unavailable"
    assert failed["error"] == "模型不可用"

    agent = _agent(monkeypatch, ["v1.mp4"])
    done = agent._build_payload(
        "sid", SEGMENTS, [], failure_markers={
            "s1": {"error_type": "model_unavailable", "error": "模型不可用"},
        },
    )["payload"]["clips"][0]
    assert done["status"] == "done"
    assert "error_type" not in done


def test_select_video_model_mode_routing_and_legacy_fallback():
    assert VideoDirectorAgent._select_video_model(
        {"video_generation_mode": "first_frame", "video_first_frame_model": "m1"}, {}
    ) == ("first_frame", "m1")
    assert VideoDirectorAgent._select_video_model(
        {"video_generation_mode": "start_end_frame", "video_start_end_model": "m2"}, {}
    ) == ("start_end_frame", "m2")
    assert VideoDirectorAgent._select_video_model(
        {"video_generation_mode": "reference", "video_reference_model": "m3"}, {}
    ) == ("reference", "m3")
    # 旧会话只有 video_model
    assert VideoDirectorAgent._select_video_model({}, {"video_model": "legacy"}) == (
        "first_frame", "legacy",
    )
    with pytest.raises(ValueError, match="video_model"):
        VideoDirectorAgent._select_video_model({}, {})


def test_regenerate_success_branch_runs_end_to_end(monkeypatch):
    """5.5：stub `_generate_one` 返回成功，真实走 process 的成功分支。"""
    import models.config_model as config_model

    monkeypatch.setattr(config_model, "get_max_concurrency", lambda model, enabled: 1)
    agent = VideoDirectorAgent()
    calls = []

    def fake_generate(*args, **kwargs):
        calls.append(args[1])
        return args[1], "v1.mp4", None, None

    monkeypatch.setattr(agent, "_prompt_resolution", lambda *a, **k: {
        "source": "description", "freshness": "fresh", "stale_reasons": [],
    })
    monkeypatch.setattr(agent, "_final_prompt", lambda *a, **k: "最终提示词")
    monkeypatch.setattr(agent, "_generate_one", fake_generate)
    monkeypatch.setattr(agent, "_list_versions", lambda sid, seg_id: ["v1.mp4"])
    monkeypatch.setattr(agent, "_report_progress", lambda *a, **k: None)

    artifacts = {
        "storyboard": {"episodes": [{"episode_number": 1, "segments": [SEGMENTS[0]]}]},
        "reference_generation": {"scenes": [{"id": "s1", "selected": ""}]},
        "character_design": {"characters": [], "settings": []},
        "video_generation": {"clips": [PREV]},
    }
    import asyncio
    result = asyncio.run(agent.process({
        "session_id": "sid",
        "video_generation_mode": "first_frame",
        "video_first_frame_model": "m1",
        "style": "realistic",
        "_session_meta": {"video_first_frame_model": "m1", "style": "realistic"},
        "_session_artifacts": artifacts,
    }, intervention={"regenerate_clips": ["s1"]}))

    clip = result["payload"]["clips"][0]
    assert calls == ["s1"], "成功分支未被实际执行"
    assert result["stage_completed"] is True
    assert clip["status"] == "done"
    assert clip["selected"] == "v1.mp4"
    assert clip["versions"] == ["v1.mp4"]
    assert "error_type" not in clip and "error" not in clip
