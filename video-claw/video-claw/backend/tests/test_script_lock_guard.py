# -*- coding: utf-8 -*-
"""剧本锁、下游就绪与提交级守卫。"""
from __future__ import annotations

import asyncio
import copy
import json
import queue
import uuid

import pytest

from api.dependencies import workflow_engine
from api.services.project_helpers import stream_workflow_task
from core import script_guard, script_revision
from core.agents.base_agent import AgentInterface
from core.orchestrator import WorkflowStage, WorkflowState, _StageRun, get_stage_order
from core.reference_skip import build_reference_skip
from core.script_errors import CastReparseNotReadyError, ScriptLockedError


def _script():
    return {
        "title": "旧标题",
        "logline": "旧梗概",
        "episodes": [{"episode_number": 1, "act_title": "第一集", "content": "旧正文"}],
        "characters": [{
            "character_id": "char_old",
            "name": "老张",
            "description": "旧描述",
            "species": "人类",
            "description_owner": "user",
        }],
        "settings": [{
            "setting_id": "set_old",
            "name": "办公室",
            "description": "旧场景",
            "description_owner": "model",
        }],
        "meta": {"original_text": "服务端原文"},
    }


def _session():
    sid = f"sl{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(sid, {"idea": "锁守卫", "llm_model": "fake"}, owner="alice")
    script = _script()
    meta = script.pop("meta")
    workflow_engine.update_artifact(sid, "script_generation", script)
    _mutate(sid, lambda state: state.artifacts["script_generation"].__setitem__("meta", meta))
    return sid


def _mutate(sid, fn):
    workflow_engine._mutate(sid, fn)


def _lock_at_character(sid):
    _mutate(sid, lambda state: setattr(state, "current_stage", WorkflowStage.CHARACTER_DESIGN))


def _set_pending(sid):
    def apply(state):
        artifact = state.artifacts.get("script_generation") or {}
        state.meta["_cast_reparse"] = {
            "status": "pending",
            "attempt": 1,
            "base_version": script_revision.structure_fingerprint(artifact, scope="body"),
        }

    _mutate(sid, apply)


def _headers():
    return {"X-User-Login-Name": "alice"}


@pytest.mark.parametrize(
    "stage,artifact,mode,expected",
    [
        ("character_design", {"characters": [], "settings": []}, "first_frame", False),
        ("character_design", {"characters": [{"id": "c1"}]}, "first_frame", True),
        ("storyboard", {"episodes": [{"episode_number": 1, "segments": []}]}, "first_frame", False),
        ("storyboard", {"episodes": [{"episode_number": 1, "segments": [{"segment_id": "s1"}]}]}, "first_frame", True),
        ("reference_generation", {"scenes": [], "skip": build_reference_skip()}, "reference", True),
        ("reference_generation", {"scenes": [], "skip": build_reference_skip()}, "first_frame", False),
        ("reference_generation", {"scenes": [{"id": "s1"}]}, "first_frame", True),
        ("prompt_rewrite", {"items": []}, "first_frame", False),
        ("prompt_rewrite", {"items": [{"id": "s1"}]}, "first_frame", True),
        ("video_generation", {"clips": []}, "first_frame", False),
        ("video_generation", {"clips": [{"id": "s1"}]}, "first_frame", True),
        ("post_production", {"final_videos": [], "final_video": ""}, "first_frame", False),
        ("post_production", {"final_video": "final.mp4"}, "first_frame", True),
        ("future_stage", {"items": []}, "first_frame", False),
        ("future_stage", {"items": [{"id": "x"}]}, "first_frame", True),
    ],
)
def test_meaningful_artifact_matrix(stage, artifact, mode, expected):
    assert script_guard.meaningful_artifact(stage, artifact, video_generation_mode=mode) is expected


def test_script_locked_uses_stage_execution_and_real_artifacts():
    state = WorkflowState("pure")
    assert script_guard.script_locked(state) is False
    state.current_stage = WorkflowStage.CHARACTER_DESIGN
    assert script_guard.script_locked(state) is True

    state = WorkflowState("pure")
    state.artifacts["character_design"] = {"characters": [], "settings": []}
    state.artifacts["storyboard"] = {"episodes": [{"episode_number": 1, "segments": []}]}
    assert script_guard.script_locked(state) is False
    state.executions["storyboard"] = {"status": "completed", "execution_id": "e1"}
    assert script_guard.script_locked(state) is True


def test_script_locked_optional_stage_uses_full_order(monkeypatch):
    monkeypatch.setattr(
        "core.orchestrator.get_stage_order",
        lambda: [stage for stage in get_stage_order() if stage != WorkflowStage.PROMPT_REWRITE],
    )
    state = WorkflowState("pure")
    state.current_stage = WorkflowStage.PROMPT_REWRITE
    assert script_guard.script_locked(state) is True


def test_cast_reparse_ready_matrix_and_accepted_residue():
    artifact = _script()
    version = script_revision.structure_fingerprint(artifact, scope="body")
    state = WorkflowState("pure")
    state.artifacts["script_generation"] = artifact
    assert script_guard.cast_reparse_ready(state) is True

    cases = [
        ({"status": "done", "base_version": version}, True),
        ({"status": "done", "base_version": "old"}, False),
        ({"status": "accepted_stale", "accepted_base_version": version}, True),
        ({"status": "accepted_stale", "accepted_base_version": "old"}, False),
        ({"status": "failed", "base_version": version}, False),
        ({"status": "stopped", "base_version": version}, False),
        ({"status": "pending", "base_version": version}, False),
        ({"status": "running", "base_version": version}, False),
        ({"status": "superseded", "base_version": version}, False),
        ({"status": "done", "base_version": version, "accepted_base_version": version}, False),
    ]
    for record, expected in cases:
        state.meta["_cast_reparse"] = record
        assert script_guard.cast_reparse_ready(state) is expected


def test_update_artifact_lock_allows_display_fields_and_preview_clear():
    sid = _session()

    def add_preview(state):
        state.artifacts["script_generation"]["pending_review"] = {"base_version": "v1"}
        state.artifacts["script_generation"]["pending_revision"] = {"base_version": "v1"}

    _mutate(sid, add_preview)
    _lock_at_character(sid)
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")

    with pytest.raises(ScriptLockedError):
        workflow_engine.update_artifact(sid, "script_generation", {"episodes": before["episodes"]})
    result = workflow_engine.update_artifact(sid, "script_generation", {
        "title": "新标题",
        "logline": "新梗概",
        "pending_review": None,
        "pending_revision": None,
    })
    assert result["artifact"]["title"] == "新标题"
    assert result["artifact"]["logline"] == "新梗概"
    assert result["artifact"]["episodes"] == before["episodes"]
    assert result["artifact"]["characters"] == before["characters"]
    assert "pending_review" not in result["artifact"]
    assert "pending_revision" not in result["artifact"]


def test_patch_api_maps_lock_to_stable_409(client):
    sid = _session()
    _lock_at_character(sid)
    response = client.patch(
        f"/api/project/{sid}/artifact/script_generation",
        json={"episodes": [{"episode_number": 1, "content": "不应落盘"}]},
        headers=_headers(),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "script_locked"
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation")["episodes"][0]["content"] == "旧正文"


@pytest.mark.parametrize(
    "modifications",
    [
        {"action": "revise_script", "instruction": "改"},
        {"action": "apply_revision"},
        {"action": "review_script"},
        {"action": "apply_script_review", "selected_issue_ids": []},
        {"action": "rollback_script_review"},
        {"action": "smart_continue"},
        {"action": "confirm_continue"},
        {"action": "modified_script", "modified_script": {}},
    ],
)
def test_locked_script_interventions_are_rejected_before_stream(modifications):
    sid = _session()
    _lock_at_character(sid)
    with pytest.raises(ScriptLockedError):
        workflow_engine.prepare_intervention_execution(sid, "script_generation", modifications)


def test_delete_continue_is_allowed_only_for_preview_only_payload():
    sid = _session()
    _lock_at_character(sid)
    workflow_engine.prepare_intervention_execution(sid, "script_generation", {"action": "delete_continue"})
    with pytest.raises(ScriptLockedError):
        workflow_engine.prepare_intervention_execution(sid, "script_generation", {
            "action": "delete_continue",
            "episodes": [],
        })


def test_prepare_stage_guards_run_before_seeding(monkeypatch):
    sid = _session()
    _lock_at_character(sid)
    called = False

    def forbidden(*_args):
        nonlocal called
        called = True
        raise AssertionError("锁态不应种子化")

    monkeypatch.setattr(workflow_engine, "_ensure_stage_seeded", forbidden)
    with pytest.raises(ScriptLockedError):
        workflow_engine.prepare_stage_execution(sid, "script_generation", {})
    assert called is False

    sid = _session()
    _set_pending(sid)
    with pytest.raises(CastReparseNotReadyError):
        workflow_engine.prepare_stage_execution(sid, "character_design", {})
    assert "character_design" not in workflow_engine.get_state(sid).artifacts


def test_begin_stage_rechecks_lock_and_ready_without_execution():
    sid = _session()
    _lock_at_character(sid)
    run = _StageRun(session_id=sid, stage=WorkflowStage.SCRIPT_GENERATION,
                    execution_id="exec-locked", background=False, intervention=None)
    with pytest.raises(ScriptLockedError):
        workflow_engine._begin_stage(run)
    assert "script_generation" not in workflow_engine.get_state(sid).executions

    sid = _session()
    _set_pending(sid)
    run = _StageRun(session_id=sid, stage=WorkflowStage.CHARACTER_DESIGN,
                    execution_id="exec-pending", background=False, intervention=None)
    with pytest.raises(CastReparseNotReadyError):
        workflow_engine._begin_stage(run)
    assert "character_design" not in workflow_engine.get_state(sid).executions


def test_finish_stage_rechecks_cross_lock_and_keeps_artifact():
    sid = _session()
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    run = _StageRun(session_id=sid, stage=WorkflowStage.SCRIPT_GENERATION,
                    execution_id="exec-cross-lock", background=False, intervention=None)
    workflow_engine._begin_stage(run)
    _lock_at_character(sid)
    payload = copy.deepcopy(before)
    payload["episodes"][0]["content"] = "迟到结果"
    with pytest.raises(ScriptLockedError):
        workflow_engine._finish_stage(run, {"stage_completed": True}, payload, None)
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation") == before


def test_finish_stage_lock_compensates_prepublished_media(monkeypatch):
    sid = _session()
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    run = _StageRun(session_id=sid, stage=WorkflowStage.SCRIPT_GENERATION,
                    execution_id="exec-lock-published", background=False, intervention=None)
    workflow_engine._begin_stage(run)
    _lock_at_character(sid)
    payload = copy.deepcopy(before)
    payload["episodes"][0]["content"] = "迟到结果"
    published = {
        "operation_ids": ["op-lock"],
        "objects": [{"operation_id": "op-lock", "bucket": "videoclaw-test", "object_key": "locked.bin"}],
    }
    compensated = []

    def abort(result):
        compensated.append(copy.deepcopy(result))
        return []

    monkeypatch.setattr("core.persistence_service.abort_publication", abort)
    with pytest.raises(ScriptLockedError):
        workflow_engine._finish_stage(run, {"stage_completed": True}, payload, published)
    assert compensated == [published]
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation") == before


def test_finish_stage_lock_keeps_error_when_compensation_fails(monkeypatch):
    sid = _session()
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    run = _StageRun(session_id=sid, stage=WorkflowStage.SCRIPT_GENERATION,
                    execution_id="exec-lock-comp-fail", background=False, intervention=None)
    workflow_engine._begin_stage(run)
    _lock_at_character(sid)
    payload = copy.deepcopy(before)
    payload["episodes"][0]["content"] = "迟到结果"
    published = {
        "operation_ids": ["op-lock-fail"],
        "objects": [{"operation_id": "op-lock-fail", "bucket": "videoclaw-test", "object_key": "locked.bin"}],
    }

    def abort(_result):
        raise RuntimeError("补偿存储不可用")

    monkeypatch.setattr("core.persistence_service.abort_publication", abort)
    with pytest.raises(ScriptLockedError) as excinfo:
        workflow_engine._finish_stage(run, {"stage_completed": True}, payload, published)
    assert isinstance(excinfo.value.compensation_error, RuntimeError)
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation") == before


def test_locked_delete_continue_finish_only_removes_preview():
    sid = _session()

    def add_preview(state):
        state.artifacts["script_generation"]["pending_revision"] = {"base_version": "v1"}

    _mutate(sid, add_preview)
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    run = _StageRun(session_id=sid, stage=WorkflowStage.SCRIPT_GENERATION,
                    execution_id="exec-delete", background=False,
                    intervention={"action": "delete_continue"})
    workflow_engine._begin_stage(run)
    _lock_at_character(sid)
    payload = copy.deepcopy(before)
    payload.pop("pending_revision", None)
    state = workflow_engine._finish_stage(run, {"stage_completed": True}, payload, None)
    assert state.artifacts["script_generation"]["episodes"] == before["episodes"]
    assert state.artifacts["script_generation"]["characters"] == before["characters"]
    assert "pending_revision" not in state.artifacts["script_generation"]


def test_stage2_writeback_is_internal_exemption_and_keeps_body():
    sid = _session()
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    run = _StageRun(session_id=sid, stage=WorkflowStage.CHARACTER_DESIGN,
                    execution_id="exec-char", background=False, intervention=None)
    workflow_engine._begin_stage(run)
    character_id = before["characters"][0].get("character_id") or before["characters"][0]["id"]
    payload = {
        "characters": [{
            "id": character_id,
            "name": "老张",
            "description": "阶段2新描述",
            "species": "人类",
            "description_owner": "user",
            "selected": "",
            "versions": [],
        }],
        "settings": [],
    }
    state = workflow_engine._finish_stage(run, {"stage_completed": True}, payload, None)
    script = state.artifacts["script_generation"]
    assert script["characters"][0]["description"] == "阶段2新描述"
    assert script["characters"][0]["description_owner"] == "user"
    assert script["episodes"] == before["episodes"]
    assert script_guard.script_locked(state) is True


def test_cast_reparse_writeback_is_internal_exemption_without_pending():
    sid = _session()
    _lock_at_character(sid)
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    workflow_engine.apply_cast_reparse_writeback(sid, {
        "characters": [{
            "character_id": before["characters"][0].get("character_id") or before["characters"][0]["id"],
            "name": "老张",
            "description": "重解析新描述",
            "description_owner": "model",
        }],
    })
    state = workflow_engine.get_state(sid)
    script = state.artifacts["script_generation"]
    assert script["characters"][0]["description"] == "重解析新描述"
    assert script["episodes"] == before["episodes"]
    assert "_cast_reparse" not in state.meta


def test_downstream_entry_checks_ready_not_lock():
    sid = _session()
    _lock_at_character(sid)
    workflow_engine.prepare_stage_execution(sid, "storyboard", {})
    workflow_engine.prepare_intervention_execution(sid, "storyboard", {
        "action": "regenerate_episodes",
        "regenerate_episodes": [1],
    })
    run = _StageRun(session_id=sid, stage=WorkflowStage.STORYBOARD,
                    execution_id="exec-ready", background=False, intervention=None)
    workflow_engine._begin_stage(run)
    assert workflow_engine.get_state(sid).executions["storyboard"]["execution_id"] == "exec-ready"

    sid = _session()
    _set_pending(sid)
    with pytest.raises(CastReparseNotReadyError):
        workflow_engine.prepare_intervention_execution(sid, "storyboard", {
            "action": "regenerate_episodes",
            "regenerate_episodes": [1],
        })


def test_continue_script_requires_ready_but_downstream_continue_ignores_lock():
    sid = _session()
    _set_pending(sid)
    _mutate(sid, lambda state: setattr(state, "current_stage", WorkflowStage.SCRIPT_GENERATION))
    with pytest.raises(CastReparseNotReadyError):
        workflow_engine._continue_workflow(sid, "script_generation")

    sid = _session()
    _lock_at_character(sid)
    _mutate(sid, lambda state: state.artifacts.__setitem__("character_design", {
        "characters": [{"id": "char_old", "selected": "https://example.invalid/portrait.png", "versions": []}],
        "settings": [],
    }))
    result = workflow_engine._continue_workflow(sid, "character_design")
    assert result["status"] == "ready"
    assert result["next_stage"] == "storyboard"


def test_unready_execute_api_returns_409_without_seed_or_execution(client):
    sid = _session()
    _set_pending(sid)
    response = client.post(f"/api/project/{sid}/execute/character_design", json={}, headers=_headers())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "blocked_by_cast_reparse"
    state = workflow_engine.get_state(sid)
    assert "character_design" not in state.artifacts
    assert "character_design" not in state.executions


def test_execute_and_intervention_api_return_409_before_sse(client):
    sid = _session()
    _lock_at_character(sid)
    execute = client.post(f"/api/project/{sid}/execute/script_generation", json={}, headers=_headers())
    assert execute.status_code == 409
    assert execute.json()["detail"]["code"] == "script_locked"
    assert "script_generation" not in workflow_engine.get_state(sid).executions

    intervene = client.post(
        f"/api/project/{sid}/intervene",
        json={"stage": "script_generation", "modifications": {"action": "review_script"}},
        headers=_headers(),
    )
    assert intervene.status_code == 409
    assert intervene.json()["detail"]["code"] == "script_locked"


def test_stream_emits_stable_code_for_commit_time_lock(monkeypatch):
    sid = _session()
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")

    class LockingAgent(AgentInterface):
        async def process(self, input_data, intervention=None):
            _lock_at_character(sid)
            payload = copy.deepcopy(before)
            payload["episodes"][0]["content"] = "不应落盘"
            return {"payload": payload, "stage_completed": True}

    monkeypatch.setitem(workflow_engine.agent_factories, WorkflowStage.SCRIPT_GENERATION, LockingAgent)

    class FakeRequest:
        async def is_disconnected(self):
            return False

    async def collect():
        return [
            chunk async for chunk in stream_workflow_task(
                request=FakeRequest(),
                workflow_engine=workflow_engine,
                state=workflow_engine.get_state(sid),
                stage="script_generation",
                input_data={"session_id": sid},
                cancellation_check=lambda: False,
                progress_callback=lambda *_args, **_kwargs: None,
                progress_events=queue.Queue(),
                event_trigger=asyncio.Event(),
            )
        ]

    chunks = asyncio.run(collect())
    events = [json.loads(chunk) for chunk in chunks]
    error = next(event for event in events if event.get("type") == "error")
    assert error["code"] == "script_locked"
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation") == before
