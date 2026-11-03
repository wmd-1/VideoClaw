"""产物重建继承：跳过重生成的片段不得丢掉选中与解析链字段。"""
from __future__ import annotations

from core.agents.video_agent import VideoDirectorAgent

SEGMENTS = [{
    "segment_id": "s1", "episode_number": 1, "segment_number": 1,
    "total_duration": 8, "shots": [{"content": "走进办公室", "duration": 8}],
}]


def _agent(monkeypatch, versions):
    agent = VideoDirectorAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, segment_id: list(versions))
    return agent


def _clip(payload):
    return payload["payload"]["clips"][0]


def test_selected_prefers_existing_when_no_new_version(monkeypatch):
    agent = _agent(monkeypatch, ["v1.mp4", "v2.mp4"])
    prev = {"id": "s1", "selected": "v1.mp4", "versions": ["v1.mp4", "v2.mp4"]}
    clip = _clip(agent._build_payload("sid", SEGMENTS, [prev]))
    assert clip["selected"] == "v1.mp4"


def test_new_version_becomes_selected(monkeypatch):
    """本轮新增版本必须成为选中，否则“重生成成功但看起来没变化”。"""
    agent = _agent(monkeypatch, ["v1.mp4", "v2.mp4"])
    prev = {"id": "s1", "selected": "v1.mp4", "versions": ["v1.mp4"]}
    clip = _clip(agent._build_payload("sid", SEGMENTS, [prev]))
    assert clip["selected"] == "v2.mp4"


def test_skipped_segment_keeps_prompt_fields(monkeypatch):
    agent = _agent(monkeypatch, ["v1.mp4"])
    prev = {
        "id": "s1", "selected": "v1.mp4",
        "prompt_source": {"source": "h3", "freshness": "fresh", "stale_reasons": []},
        "submitted_prompt": "old prompt",
        "duration_adjustment": ["时长夹取"],
        "manual_prompt": "人工正文",
        "manual_prompt_fingerprint": "fp",
        "manual_prompt_dimensions": {"duration": "x"},
        "resolved_fingerprint": "fp-resolved",
        "resolved_dimensions": {"duration": "d1"},
    }
    clip = _clip(agent._build_payload("sid", SEGMENTS, [prev]))
    assert clip["prompt_source"]["source"] == "h3"
    assert clip["submitted_prompt"] == "old prompt"
    assert clip["duration_adjustment"] == ["时长夹取"]
    assert clip["manual_prompt"] == "人工正文"
    assert clip["manual_prompt_fingerprint"] == "fp"
    assert clip["manual_prompt_dimensions"] == {"duration": "x"}
    assert clip["resolved_fingerprint"] == "fp-resolved"
    assert clip["resolved_dimensions"] == {"duration": "d1"}


def test_new_resolution_overrides_previous(monkeypatch):
    agent = _agent(monkeypatch, ["v1.mp4"])
    agent._prompt_sources = {"s1": {
        "source": "description", "freshness": "stale",
        "stale_reasons": ["duration_changed"], "submitted_prompt": "new prompt",
        "fingerprint": "fp-new", "dimensions": {"duration": "d2"},
    }}
    prev = {"id": "s1", "selected": "v1.mp4",
            "prompt_source": {"source": "h3", "freshness": "fresh", "stale_reasons": []},
            "submitted_prompt": "old prompt"}
    clip = _clip(agent._build_payload("sid", SEGMENTS, [prev]))
    assert clip["prompt_source"]["source"] == "description"
    assert clip["submitted_prompt"] == "new prompt"
    assert clip["resolved_fingerprint"] == "fp-new"
    assert clip["resolved_dimensions"] == {"duration": "d2"}


def test_submitted_prompt_is_truncated_at_1000(monkeypatch):
    agent = _agent(monkeypatch, [])
    agent._prompt_sources = {"s1": {
        "source": "h3", "freshness": "fresh", "stale_reasons": [],
        "submitted_prompt": "x" * 1500,
    }}
    clip = _clip(agent._build_payload("sid", SEGMENTS, []))
    assert len(clip["submitted_prompt"]) == 1000
    assert clip["submitted_prompt_truncated"] is True


def test_effective_prompt_is_not_persisted(monkeypatch):
    """重复数据不落产物：预填按 source 从 manual_prompt / H3 条目 / description 取。"""
    agent = _agent(monkeypatch, [])
    agent._prompt_sources = {"s1": {
        "source": "h3", "freshness": "fresh", "stale_reasons": [],
        "text": "y" * 2000, "submitted_prompt": "y" * 2000,
    }}
    clip = _clip(agent._build_payload("sid", SEGMENTS, []))
    assert "effective_prompt" not in clip
    assert len(clip["submitted_prompt"]) == 1000


def test_empty_submitted_prompt_clears_previous(monkeypatch):
    agent = _agent(monkeypatch, ["v1.mp4"])
    agent._prompt_sources = {"s1": {
        "source": "description", "freshness": "stale",
        "stale_reasons": ["duration_changed"], "text": "描述", "submitted_prompt": "",
    }}
    prev = {"id": "s1", "selected": "v1.mp4", "versions": ["v1.mp4"],
            "submitted_prompt": "old", "submitted_prompt_truncated": True}
    clip = _clip(agent._build_payload("sid", SEGMENTS, [prev]))
    assert clip["submitted_prompt"] == ""
    assert "submitted_prompt_truncated" not in clip
