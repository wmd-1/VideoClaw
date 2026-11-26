"""H3 /continue 完整性门控与失败条目显式放行。"""
from __future__ import annotations

import asyncio
import copy
import uuid

import pytest

from api.dependencies import workflow_engine
from config import Config
from core.agents.prompt_rewrite_agent import PromptRewriteAgent
from core.orchestrator import WorkflowStage
from core.prompt_rewrite_quality import assess_prompt_rewrite


@pytest.fixture(autouse=True)
def _enable_h3(monkeypatch):
    monkeypatch.setattr(Config, "H3_REWRITE_ENABLED", True)


def _storyboard():
    return {"episodes": [{"episode_number": 1, "segments": [
        {"segment_id": "seg_01_01"},
        {"segment_id": "seg_01_02"},
    ]}]}


def _session(items, *, storyboard_status="completed"):
    sid = f"prc{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(sid, {"idea": "h3 continue", "llm_model": "fake"}, owner="alice")

    def seed(state):
        state.artifacts["storyboard"] = _storyboard()
        state.artifacts["prompt_rewrite"] = {"items": copy.deepcopy(items)}
        state.current_stage = WorkflowStage.PROMPT_REWRITE
        state.status["storyboard"] = storyboard_status
        state.status["prompt_rewrite"] = "waiting"

    workflow_engine._mutate(sid, seed)
    return sid


def _post(client, sid, **body):
    return client.post(
        f"/api/project/{sid}/continue",
        json={"stage": "prompt_rewrite", **body},
        headers={"X-User-Login-Name": "alice"},
    )


def test_assessment_separates_missing_incomplete_and_failed():
    result = assess_prompt_rewrite(_storyboard(), {"items": [
        {"id": "seg_01_01", "status": "pending"},
        {"id": "seg_01_02", "status": "failed"},
    ]})
    assert result["hasMissingItems"] is False
    assert result["hasIncompleteItems"] is True
    assert result["hasFailedItems"] is True
    assert result["canContinue"] is False

    best_effort = assess_prompt_rewrite(_storyboard(), {"items": [
        {"id": "seg_01_01", "status": "done", "best_effort": True},
        {"id": "seg_01_02", "status": "done"},
    ]})
    assert best_effort["hasFailedItems"] is False
    assert best_effort["canContinue"] is True


@pytest.mark.parametrize("items,storyboard_status,code", [
    ([{"id": "seg_01_01", "status": "done"}], "completed", "prompt_rewrite_incomplete"),
    ([{"id": "seg_01_01", "status": "done"}, {"id": "seg_01_02", "status": "pending"}], "completed", "prompt_rewrite_incomplete"),
    ([{"id": "seg_01_01", "status": "done"}, {"id": "seg_01_02", "status": "done"}], "reconfirm", "prompt_rewrite_incomplete"),
])
def test_continue_rejects_missing_incomplete_or_reconfirm(client, items, storyboard_status, code):
    sid = _session(items, storyboard_status=storyboard_status)
    before = copy.deepcopy(workflow_engine.get_state(sid).status)
    response = _post(client, sid)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    assert workflow_engine.get_state(sid).status == before


def test_continue_requires_explicit_failed_acceptance(client):
    sid = _session([
        {"id": "seg_01_01", "status": "done", "rewritten_prompt": "ok"},
        {"id": "seg_01_02", "status": "failed", "error": "boom"},
    ])
    response = _post(client, sid)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "prompt_rewrite_failed_items"
    assert detail["assessment"]["failed_ids"] == ["seg_01_02"]

    accepted = _post(client, sid, allow_failed_items=True)
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "ready"
    item = workflow_engine.get_artifact_snapshot(sid, "prompt_rewrite")["items"][1]
    assert item["status"] == "failed"


def test_continue_allows_best_effort_done_without_confirmation(client):
    sid = _session([
        {"id": "seg_01_01", "status": "done", "best_effort": True},
        {"id": "seg_01_02", "status": "done"},
    ])
    response = _post(client, sid)
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_process_partial_preserves_done_items_and_only_rewrites_targets(monkeypatch):
    agent = PromptRewriteAgent()
    storyboard = _storyboard()
    previous = {"items": [
        {"id": "seg_01_01", "status": "done", "rewritten_prompt": "用户手工编辑"},
        {"id": "seg_01_02", "status": "failed", "rewritten_prompt": ""},
    ]}
    segments = [{"id": "seg_01_01"}, {"id": "seg_01_02"}]
    calls = []

    monkeypatch.setattr(agent, "check_enabled", lambda: None)
    monkeypatch.setattr(agent, "_merge_session_params", lambda value: value)
    monkeypatch.setattr(agent, "_session_artifacts", lambda value: {"storyboard": storyboard, "prompt_rewrite": previous})
    monkeypatch.setattr(agent, "_session_artifact", lambda value, key: storyboard if key == "storyboard" else previous)
    monkeypatch.setattr(agent, "_session_meta", lambda value: {})
    monkeypatch.setattr(agent, "_duration_contract", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_collect_segments", lambda *a, **k: segments)
    monkeypatch.setattr(agent, "_report_progress", lambda *a, **k: None)
    monkeypatch.setattr("core.agents.prompt_rewrite_agent.VideoDirectorAgent._select_video_model", lambda *a, **k: ("", ""))

    async def fake_rewrite(targets, input_data, prev_items_by_id, **kwargs):
        calls.extend(seg["id"] for seg in targets)
        return {seg["id"]: {"id": seg["id"], "status": "done", "rewritten_prompt": "new"} for seg in targets}

    monkeypatch.setattr(agent, "_rewrite_segments", fake_rewrite)
    result = asyncio.run(agent.process({"video_generation_mode": "first_frame"}))
    assert calls == ["seg_01_02"]
    assert result["payload"]["items"][0]["rewritten_prompt"] == "用户手工编辑"
    assert result["payload"]["items"][1]["rewritten_prompt"] == "new"


def test_continue_rejects_non_boolean_allow_failed_items(client):
    sid = _session([
        {"id": "seg_01_01", "status": "done"},
        {"id": "seg_01_02", "status": "done"},
    ])
    response = _post(client, sid, allow_failed_items="true")
    assert response.status_code == 400
