"""A3：视频资产状态与预览/payload 同口径。"""
from __future__ import annotations

from core.agents.video_agent import VideoDirectorAgent
from core.asset_quality import asset_status

SEGMENTS = [{
    "segment_id": "s1", "episode_number": 1, "segment_number": 1,
    "total_duration": 8, "shots": [{"content": "镜头"}],
}]


def test_video_preview_and_payload_share_status_rule(monkeypatch):
    agent = VideoDirectorAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, segment_id: [])

    preview = agent._build_preview("sid", SEGMENTS, [])[0]
    payload = agent._build_payload("sid", SEGMENTS, [])["payload"]["clips"][0]
    assert preview["status"] == "pending"
    assert payload["status"] == "failed"

    monkeypatch.setattr(agent, "_list_versions", lambda sid, segment_id: ["v1.mp4"])
    preview = agent._build_preview("sid", SEGMENTS, [])[0]
    payload = agent._build_payload("sid", SEGMENTS, [])["payload"]["clips"][0]
    assert preview["status"] == "done"
    assert payload["status"] == "done"


def test_status_helper_matches_video_semantics():
    assert asset_status(["v1.mp4"], stage_completed=True) == "done"
    assert asset_status([], stage_completed=True) == "failed"
    assert asset_status([]) == "pending"
