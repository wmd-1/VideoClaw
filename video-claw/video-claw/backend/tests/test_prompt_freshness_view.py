"""视图层注入保护：get_status_snapshot 必须带 needs_regeneration，且绝不落库。"""
from __future__ import annotations

import uuid

from api.dependencies import workflow_engine

STORYBOARD = {"episodes": [{"episode_number": 1, "segments": [
    {"segment_id": "seg_01_01", "location": "会议室", "characters": ["小陈"],
     "total_duration": 8, "shots": [{"content": "镜头A", "duration": 8}]},
]}]}


def _seed(workflow_engine, sid):
    workflow_engine.create_session(sid, {
        "video_first_frame_model": "m1",
        "video_generation_mode": "first_frame",
        "style": "realistic",
    }, owner="alice")
    workflow_engine.update_artifact(sid, "storyboard", STORYBOARD)
    workflow_engine.update_artifact(sid, "character_design", {
        "characters": [{"id": "c1", "name": "小陈", "selected": "", "description": "银行职员"}],
        "settings": [{"id": "s1", "name": "会议室", "selected": "", "description": "会议室"}],
    })
    workflow_engine.update_artifact(sid, "reference_generation", {
        "scenes": [{"id": "seg_01_01", "selected": ""}],
    })
    # 故意写入过期指纹，制造 stale
    workflow_engine.update_artifact(sid, "prompt_rewrite", {
        "items": [{"id": "seg_01_01", "status": "done", "rewritten_prompt": "旧正文",
                   "input_fingerprint": "stale-fingerprint"}],
    })


def _sid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_status_snapshot_injects_needs_regeneration_but_never_persists():
    sid = _sid("freshness_view")
    _seed(workflow_engine, sid)

    snapshot = workflow_engine.get_status_snapshot(sid)
    item = snapshot["artifacts"]["prompt_rewrite"]["items"][0]
    assert item["needs_regeneration"] is True
    assert item["regeneration_reasons"], "stale 条目必须带结构化原因"

    # 派生只存在于视图副本；持久状态不得被写入
    state = workflow_engine.get_state(sid)
    persisted = state.artifacts["prompt_rewrite"]["items"][0]
    assert "needs_regeneration" not in persisted
    assert "regeneration_reasons" not in persisted


def test_status_map_uses_annotation_free_view():
    """仅取 status 的路径跳过派生，且不会反过来污染 artifacts。"""
    sid = _sid("freshness_view_map")
    _seed(workflow_engine, sid)

    status_map = workflow_engine.persist_session_snapshot(sid)
    assert status_map is not None
    state = workflow_engine.get_state(sid)
    assert "needs_regeneration" not in state.artifacts["prompt_rewrite"]["items"][0]
