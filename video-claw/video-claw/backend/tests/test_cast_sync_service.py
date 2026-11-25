# -*- coding: utf-8 -*-
"""cast 重解析状态机、调度器、控制接口与安全合并。"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
import uuid

import pytest

from api.dependencies import workflow_engine
from core import script_guard, script_revision
from core.agents.script_agent import ScriptWriterAgent
from core.cast_sync_service import CastSyncService
from core.orchestrator import WorkflowStage, WorkflowState, _StageRun
from core.script_errors import CastReparseNotReadyError, ScriptFieldNotWritableError
from db.repositories.commits import CommitConflict
from db.repositories.sessions import SessionRepo


def _script(content="旧正文", *, owner="model"):
    return {
        "title": "标题",
        "logline": "梗概",
        "episodes": [{"episode_number": 1, "act_title": "第一集", "content": content}],
        "characters": [{
            "character_id": "char_old",
            "name": "老张",
            "description": "旧描述",
            "species": "人类",
            "description_owner": owner,
        }],
        "settings": [{
            "setting_id": "set_old",
            "name": "办公室",
            "description": "旧场景",
            "description_owner": "model",
        }],
    }


def _session(content="旧正文", *, owner="model"):
    sid = f"cs{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(sid, {"idea": "重解析", "llm_model": "fake"}, owner="alice")
    workflow_engine.update_artifact(sid, "script_generation", _script(content, owner=owner))

    def set_owner(state):
        state.artifacts["script_generation"]["characters"][0]["description_owner"] = owner

    workflow_engine._mutate(sid, set_owner)
    return sid


def _record(sid):
    return copy.deepcopy((workflow_engine.get_state(sid).meta or {}).get("_cast_reparse") or {})


def _version(sid):
    return script_revision.structure_fingerprint(
        workflow_engine.get_artifact_snapshot(sid, "script_generation"), scope="body"
    )


def _headers():
    return {"X-User-Login-Name": "alice"}


async def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等待状态超时")


def test_manual_body_change_registers_pending_atomically_and_noop_does_not():
    sid = _session()
    assert "_cast_reparse" not in workflow_engine.get_state(sid).meta

    workflow_engine.update_artifact(sid, "script_generation", {"title": "新标题"})
    assert "_cast_reparse" not in workflow_engine.get_state(sid).meta
    workflow_engine.update_artifact(sid, "script_generation", {
        "characters": [{"name": "老张", "description": "手改描述", "species": "人类"}],
    })
    assert "_cast_reparse" not in workflow_engine.get_state(sid).meta

    workflow_engine.update_artifact(sid, "script_generation", {
        "episodes": [{"episode_number": 1, "content": "新正文"}],
    })
    record = _record(sid)
    assert record["status"] == "pending"
    assert record["attempt"] == 1
    assert record["base_version"] == _version(sid)
    assert record["changed_episodes"] == [1]

    workflow_engine.update_artifact(sid, "script_generation", {
        "episodes": [{"episode_number": 1, "content": "新正文"}],
    })
    assert _record(sid) == record


def test_finish_stage_registers_pending_only_for_existing_body_change():
    sid = _session()
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    run = _StageRun(session_id=sid, stage=WorkflowStage.SCRIPT_GENERATION,
                    execution_id="exec-cast", background=False,
                    intervention={"action": "modified_script"})
    workflow_engine._begin_stage(run)
    payload = copy.deepcopy(before)
    payload["episodes"][0]["content"] = "AI 新正文"
    workflow_engine._finish_stage(run, {"stage_completed": True}, payload, None)
    assert _record(sid)["status"] == "pending"
    assert _record(sid)["reason"] == "modified_script"

    first = _session()
    run = _StageRun(session_id=first, stage=WorkflowStage.SCRIPT_GENERATION,
                    execution_id="exec-first", background=False, intervention=None)
    workflow_engine._begin_stage(run)
    workflow_engine._finish_stage(run, {"stage_completed": True}, _script(), None)
    assert "_cast_reparse" not in workflow_engine.get_state(first).meta


@pytest.mark.parametrize("scope,include_cast", [("single", True), ("all", False)])
def test_revision_apply_registers_pending_regardless_of_model_cast(scope, include_cast):
    sid = _session()
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    run = _StageRun(
        session_id=sid, stage=WorkflowStage.SCRIPT_GENERATION,
        execution_id=f"exec-revision-{scope}", background=False,
        intervention={"action": "apply_revision", "revision_scope": scope},
    )
    workflow_engine._begin_stage(run)
    payload = copy.deepcopy(before)
    payload["episodes"][0]["content"] = f"{scope} 修订后的正文"
    if include_cast:
        payload["characters"].append({
            "name": "修订新增角色", "description": "单集修订返回", "description_owner": "model",
        })
    workflow_engine._finish_stage(
        run, {"stage_completed": True, "revision_applied": True}, payload, None,
    )
    record = _record(sid)
    assert record["status"] == "pending"
    assert record["reason"] == "apply_revision"
    assert record["changed_episodes"] == [1]


def test_service_settles_continuous_saves_and_commits_latest_without_stage_execution():
    sid = _session()
    calls = []

    class FakeAgent:
        async def reparse_cast(self, script_artifact, changed_episodes, llm_model, cancellation_check):
            calls.append(copy.deepcopy(script_artifact))
            return {
                "characters": [{"name": "老张", "description": script_artifact["episodes"][0]["content"]}],
                "settings": [],
            }

    async def scenario():
        service = CastSyncService(workflow_engine, agent_factory=FakeAgent, settle_seconds=0.05)
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            workflow_engine.update_artifact(sid, "script_generation", {
                "episodes": [{"episode_number": 1, "content": "版本 A"}],
            })
            await asyncio.sleep(0.01)
            workflow_engine.update_artifact(sid, "script_generation", {
                "episodes": [{"episode_number": 1, "content": "版本 B"}],
            })
            await _wait_for(lambda: _record(sid).get("status") == "done")
        finally:
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0]["episodes"][0]["content"] == "版本 B"
    state = workflow_engine.get_state(sid)
    assert state.artifacts["script_generation"]["characters"][0]["description"] == "版本 B"
    assert state.status["script_generation"] != "running"
    assert "script_generation" not in state.executions


def test_service_allows_two_sessions_and_keeps_third_pending():
    sessions = [_session(f"正文 {index}") for index in range(3)]
    started = []
    active = 0
    max_active = 0
    gate = asyncio.Event()

    class FakeAgent:
        async def reparse_cast(self, script_artifact, changed_episodes, llm_model, cancellation_check):
            nonlocal active, max_active
            started.append(script_artifact["episodes"][0]["content"])
            active += 1
            max_active = max(max_active, active)
            try:
                await gate.wait()
            finally:
                active -= 1
            return {"characters": [], "settings": []}

    async def scenario():
        service = CastSyncService(workflow_engine, agent_factory=FakeAgent, settle_seconds=0.01)
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            for sid in sessions:
                workflow_engine.update_artifact(sid, "script_generation", {
                    "episodes": [{"episode_number": 1, "content": f"更新 {sid}"}],
                })
            await _wait_for(lambda: len(started) == 2)
            assert _record(sessions[2])["status"] == "pending"
            gate.set()
            await _wait_for(lambda: all(_record(sid).get("status") == "done" for sid in sessions))
        finally:
            gate.set()
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())
    assert max_active == 2
    assert len(started) == 3


def test_running_new_version_supersedes_old_result_and_single_session_never_overlaps():
    sid = _session()
    calls = []
    active = 0
    max_active = 0
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class FakeAgent:
        async def reparse_cast(self, script_artifact, changed_episodes, llm_model, cancellation_check):
            nonlocal active, max_active
            calls.append(script_artifact["episodes"][0]["content"])
            active += 1
            max_active = max(max_active, active)
            try:
                if len(calls) == 1:
                    first_started.set()
                    await release_first.wait()
                return {
                    "characters": [{"name": "老张", "description": script_artifact["episodes"][0]["content"]}],
                    "settings": [],
                }
            finally:
                active -= 1

    async def scenario():
        service = CastSyncService(workflow_engine, agent_factory=FakeAgent, settle_seconds=0.1)
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            workflow_engine.update_artifact(sid, "script_generation", {
                "episodes": [{"episode_number": 1, "content": "旧任务版本"}],
            })
            await first_started.wait()
            workflow_engine.update_artifact(sid, "script_generation", {
                "episodes": [{"episode_number": 1, "content": "最新版本"}],
            })
            release_first.set()
            await _wait_for(lambda: _record(sid).get("status") == "superseded")
            assert _record(sid)["attempt"] == 2
            await _wait_for(lambda: _record(sid).get("status") == "done")
        finally:
            release_first.set()
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())
    assert calls == ["旧任务版本", "最新版本"]
    assert max_active == 1
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation")["characters"][0]["description"] == "最新版本"
    assert _record(sid)["attempt"] == 2


def test_old_failure_after_new_version_marks_superseded_and_reruns():
    sid = _session()
    calls = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class FakeAgent:
        async def reparse_cast(self, script_artifact, changed_episodes, llm_model, cancellation_check):
            content = script_artifact["episodes"][0]["content"]
            calls.append(content)
            if len(calls) == 1:
                first_started.set()
                await release_first.wait()
                raise RuntimeError("旧任务失败")
            return {
                "characters": [{"name": "老张", "description": content}],
                "settings": [],
            }

    async def scenario():
        service = CastSyncService(workflow_engine, agent_factory=FakeAgent, settle_seconds=0.1)
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            workflow_engine.update_artifact(sid, "script_generation", {
                "episodes": [{"episode_number": 1, "content": "旧失败版本"}],
            })
            await first_started.wait()
            workflow_engine.update_artifact(sid, "script_generation", {
                "episodes": [{"episode_number": 1, "content": "最新成功版本"}],
            })
            release_first.set()
            await _wait_for(lambda: _record(sid).get("status") == "superseded")
            await _wait_for(lambda: _record(sid).get("status") == "done")
        finally:
            release_first.set()
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())
    assert calls == ["旧失败版本", "最新成功版本"]
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation")["characters"][0]["description"] == "最新成功版本"


def test_begin_version_drift_marks_superseded_without_model():
    sid = _session()
    workflow_engine.update_artifact(sid, "script_generation", {
        "episodes": [{"episode_number": 1, "content": "待校验版本"}],
    })
    record = _record(sid)

    def drift(state):
        episodes = state.artifacts["script_generation"]["episodes"]
        episodes[0]["content"] = "未经登记的正文漂移"
        episodes.append({"episode_number": 2, "act_title": "第二集", "content": "漂移新增集"})

    workflow_engine._mutate(sid, drift)
    assert workflow_engine.begin_cast_reparse(
        sid, "job-drift", record["attempt"], record["base_version"],
    ) is None
    drifted = _record(sid)
    assert drifted["status"] == "superseded"
    assert drifted["attempt"] == record["attempt"] + 1
    assert drifted["base_version"] == _version(sid)
    assert drifted["changed_episodes"] == [1]
    assert workflow_engine.promote_cast_reparse(
        sid, drifted["attempt"], drifted["base_version"],
    ) is True
    assert _record(sid)["status"] == "pending"


def test_job_exception_isolated_and_worker_continues():
    failed = _session("失败会话")
    succeeded = _session("成功会话")
    calls = []

    class FakeAgent:
        async def reparse_cast(self, script_artifact, changed_episodes, llm_model, cancellation_check):
            content = script_artifact["episodes"][0]["content"]
            calls.append(content)
            if content == f"更新 {failed}":
                raise RuntimeError("假模型失败")
            return {"characters": [], "settings": []}

    async def scenario():
        service = CastSyncService(workflow_engine, agent_factory=FakeAgent, settle_seconds=0.01)
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            for sid in (failed, succeeded):
                workflow_engine.update_artifact(sid, "script_generation", {
                    "episodes": [{"episode_number": 1, "content": f"更新 {sid}"}],
                })
            await _wait_for(lambda: _record(failed).get("status") == "failed")
            await _wait_for(lambda: _record(succeeded).get("status") == "done")
            assert len([task for task in service._workers if not task.done()]) == 2
        finally:
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())
    assert len(calls) == 2
    assert "假模型失败" in _record(failed)["error"]


def test_commit_conflict_recovers_pending_and_reschedules(monkeypatch):
    sid = _session()
    model_calls = []
    commit_calls = 0
    original_complete = workflow_engine.complete_cast_reparse

    class FakeAgent:
        async def reparse_cast(self, script_artifact, changed_episodes, llm_model, cancellation_check):
            model_calls.append(script_artifact["episodes"][0]["content"])
            return {"characters": [], "settings": []}

    def flaky_complete(*args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise CommitConflict("fake CAS conflict")
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(workflow_engine, "complete_cast_reparse", flaky_complete)

    async def scenario():
        service = CastSyncService(workflow_engine, agent_factory=FakeAgent, settle_seconds=0.01)
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            workflow_engine.update_artifact(sid, "script_generation", {
                "episodes": [{"episode_number": 1, "content": "冲突后重排"}],
            })
            await _wait_for(lambda: _record(sid).get("status") == "done")
        finally:
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())
    assert commit_calls == 2
    assert model_calls == ["冲突后重排", "冲突后重排"]
    assert _record(sid)["attempt"] == 1


def test_stop_marks_stopped_and_late_thread_result_does_not_commit():
    sid = _session()
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    started = threading.Event()
    release = threading.Event()

    class FakeAgent:
        async def reparse_cast(self, script_artifact, changed_episodes, llm_model, cancellation_check):
            started.set()
            await asyncio.to_thread(release.wait, 3.0)
            return {
                "characters": [{"name": "老张", "description": "迟到结果"}],
                "settings": [],
            }

    async def scenario():
        service = CastSyncService(workflow_engine, agent_factory=FakeAgent, settle_seconds=0.01)
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            workflow_engine.update_artifact(sid, "script_generation", {
                "episodes": [{"episode_number": 1, "content": "待重解析正文"}],
            })
            await _wait_for(lambda: _record(sid).get("status") == "running")
            await asyncio.to_thread(workflow_engine.stop_session, sid)
            assert _record(sid)["status"] == "stopped"
            release.set()
            await asyncio.sleep(0.1)
        finally:
            release.set()
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())
    assert started.is_set()
    assert _record(sid)["status"] == "stopped"
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation")["characters"] == before["characters"]
    assert workflow_engine.get_state(sid).status["script_generation"] != "running"


def test_stop_one_session_does_not_cancel_peer_and_supervisor_restores_worker():
    stopped, peer, queued = [_session(f"初始 {index}") for index in range(3)]
    started = []
    stopped_started = asyncio.Event()
    peer_started = asyncio.Event()
    release_peer = asyncio.Event()
    active = 0
    max_active = 0

    class FakeAgent:
        async def reparse_cast(self, script_artifact, changed_episodes, llm_model, cancellation_check):
            nonlocal active, max_active
            content = script_artifact["episodes"][0]["content"]
            started.append(content)
            active += 1
            max_active = max(max_active, active)
            try:
                if content == "停止 A":
                    stopped_started.set()
                    await asyncio.Event().wait()
                elif content == "继续 B":
                    peer_started.set()
                    await release_peer.wait()
                return {"characters": [], "settings": []}
            finally:
                active -= 1

    async def scenario():
        service = CastSyncService(
            workflow_engine, agent_factory=FakeAgent, settle_seconds=0.01, supervisor_interval=0.1,
        )
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            for sid, content in ((stopped, "停止 A"), (peer, "继续 B"), (queued, "排队 C")):
                workflow_engine.update_artifact(sid, "script_generation", {
                    "episodes": [{"episode_number": 1, "content": content}],
                })
            await _wait_for(stopped_started.is_set)
            await _wait_for(peer_started.is_set)
            assert _record(queued)["status"] == "pending"

            await asyncio.to_thread(workflow_engine.stop_session, stopped)
            assert _record(stopped)["status"] == "stopped"
            await _wait_for(lambda: len([task for task in service._workers if not task.done()]) == 2)
            await asyncio.sleep(0.05)
            assert _record(peer)["status"] == "running"
            assert not release_peer.is_set()

            release_peer.set()
            await _wait_for(lambda: _record(peer).get("status") == "done")
            await _wait_for(lambda: _record(queued).get("status") == "done")
        finally:
            release_peer.set()
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())
    assert len(started) == 3
    assert set(started) == {"停止 A", "继续 B", "排队 C"}
    assert max_active == 2
    assert _record(stopped)["status"] == "stopped"
    assert workflow_engine.get_state(stopped).status["script_generation"] != "running"


def test_retry_and_accept_stale_validation_and_audit(client):
    sid = _session()
    version = _version(sid)

    def seed_failed(state):
        state.meta["_cast_reparse"] = {
            "status": "failed", "attempt": 2, "base_version": version,
            "error": "失败", "active_job": None,
        }

    workflow_engine._mutate(sid, seed_failed)
    bad = client.post(
        f"/api/project/{sid}/cast-reparse/retry",
        json={"attempt": 1, "base_version": version}, headers=_headers(),
    )
    assert bad.status_code == 409
    assert bad.json()["detail"]["code"] == "cast_reparse_version_mismatch"

    accepted = client.post(
        f"/api/project/{sid}/cast-reparse/accept-stale",
        json={"accepted_attempt": 2, "accepted_base_version": version}, headers=_headers(),
    )
    assert accepted.status_code == 200
    record = accepted.json()["cast_reparse"]
    assert record["status"] == "accepted_stale"
    assert record["accepted_attempt"] == 2
    assert record["accepted_by"] == "alice"
    assert record["attempt"] == 2
    assert script_guard.cast_reparse_ready(workflow_engine.get_state(sid)) is True

    workflow_engine.update_artifact(sid, "script_generation", {
        "episodes": [{"episode_number": 1, "content": "接受后再次修改"}],
    })
    record = _record(sid)
    assert record["status"] == "pending"
    assert record["attempt"] == 3
    assert not any(key.startswith("accepted_") for key in record)

    current_version = _version(sid)

    def seed_failed_current(state):
        state.meta["_cast_reparse"] = {
            "status": "failed", "attempt": 2, "base_version": current_version,
            "error": "再次失败", "active_job": None,
            "summary": {"old": True}, "superseded_at": 1.0,
            "superseded_attempt": 1, "interrupted_by_restart": True,
        }
        state.meta["_cast_suggestions"] = {
            "base_version": current_version, "items": [{"id": "old-suggestion"}],
        }

    workflow_engine._mutate(sid, seed_failed_current)
    retried = client.post(
        f"/api/project/{sid}/cast-reparse/retry",
        json={"attempt": 2, "base_version": current_version}, headers=_headers(),
    )
    assert retried.status_code == 200
    assert retried.json()["cast_reparse"]["status"] == "pending"
    assert retried.json()["cast_reparse"]["attempt"] == 3
    retried_record = retried.json()["cast_reparse"]
    assert not any(key in retried_record for key in (
        "summary", "superseded_at", "superseded_attempt", "interrupted_by_restart",
    ))
    assert "_cast_suggestions" not in workflow_engine.get_state(sid).meta


def test_failed_blocks_downstream_until_retry_reparse_completes():
    sid = _session()
    version = _version(sid)

    def seed_failed(state):
        state.meta["_cast_reparse"] = {
            "status": "failed", "attempt": 1, "base_version": version,
            "error": "假模型失败", "active_job": None,
        }

    workflow_engine._mutate(sid, seed_failed)
    with pytest.raises(CastReparseNotReadyError):
        workflow_engine.prepare_stage_execution(sid, "character_design", {})
    assert "character_design" not in workflow_engine.get_state(sid).artifacts

    class FakeAgent:
        async def reparse_cast(self, *_args, **_kwargs):
            return {"characters": [], "settings": []}

    async def scenario():
        service = CastSyncService(workflow_engine, agent_factory=FakeAgent, settle_seconds=0.01)
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            workflow_engine.retry_cast_reparse(sid, attempt=1, base_version=version)
            await _wait_for(lambda: _record(sid).get("status") == "done")
            await asyncio.to_thread(workflow_engine.prepare_stage_execution, sid, "character_design", {})
        finally:
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())
    assert script_guard.cast_reparse_ready(workflow_engine.get_state(sid)) is True
    assert "character_design" in workflow_engine.get_state(sid).artifacts


def test_accept_stale_allows_stopped_current_version(client):
    sid = _session()
    version = _version(sid)

    def seed_stopped(state):
        state.meta["_cast_reparse"] = {
            "status": "stopped", "attempt": 3, "base_version": version, "active_job": None,
        }

    workflow_engine._mutate(sid, seed_stopped)
    response = client.post(
        f"/api/project/{sid}/cast-reparse/accept-stale",
        json={"accepted_attempt": 3, "accepted_base_version": version}, headers=_headers(),
    )
    assert response.status_code == 200
    record = response.json()["cast_reparse"]
    assert record["status"] == "accepted_stale"
    assert record["accepted_attempt"] == 3
    assert record["attempt"] == 3
    assert script_guard.cast_reparse_ready(workflow_engine.get_state(sid)) is True


def test_accept_stale_rejects_non_acceptable_states(client):
    sid = _session()
    version = _version(sid)
    for status in ("running", "pending", "done", "accepted_stale"):
        def seed(state, status=status):
            state.meta["_cast_reparse"] = {
                "status": status, "attempt": 1, "base_version": version,
                "active_job": "job" if status == "running" else None,
            }

        workflow_engine._mutate(sid, seed)
        response = client.post(
            f"/api/project/{sid}/cast-reparse/accept-stale",
            json={"accepted_attempt": 1, "accepted_base_version": version}, headers=_headers(),
        )
        assert response.status_code == 409
        assert _record(sid)["status"] == status


def test_controls_remain_available_when_script_locked(client):
    sid = _session()
    version = _version(sid)
    workflow_engine._mutate(sid, lambda state: setattr(state, "current_stage", WorkflowStage.CHARACTER_DESIGN))
    workflow_engine._mutate(sid, lambda state: state.meta.__setitem__("_cast_reparse", {
        "status": "running", "attempt": 1, "base_version": version, "active_job": "job",
    }))
    response = client.post(
        f"/api/project/{sid}/cast-reparse/stop",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["cast_reparse"]["status"] == "stopped"


def test_startup_scan_recovers_running_and_superseded_and_preserves_legacy_meta():
    ids = []
    for index, status in enumerate(("pending", "running", "superseded")):
        sid = f"recover{index}_{uuid.uuid4().hex[:6]}"
        ids.append(sid)
        state = WorkflowState(sid)
        state.owner = "alice"
        state.meta = {
            "project_params": {"idea": "历史创意", "style": "anime", "models": {"llm": "fake"}},
            "_cast_reparse": {"status": status, "attempt": 3, "base_version": "v3", "active_job": "old"},
        }
        SessionRepo().upsert(state)

    assert set(SessionRepo().list_cast_reparse_recoverable()) == set(ids)
    assert set(workflow_engine.recover_cast_reparse_sessions()) == set(ids)
    for sid in ids:
        state = workflow_engine.get_state(sid)
        record = state.meta["_cast_reparse"]
        assert record["status"] == "pending"
        assert record["attempt"] == 3
        assert state.meta["idea"] == "历史创意"
        assert state.meta["style"] == "anime"
        assert state.meta["models"] == {"llm": "fake"}
    assert workflow_engine.get_state(ids[1]).meta["_cast_reparse"]["interrupted_by_restart"] is True
    assert workflow_engine.get_state(ids[2]).meta["_cast_reparse"]["interrupted_by_restart"] is True


def test_internal_writeback_rejects_unknown_nested_fields_and_illegal_owner():
    sid = _session()
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    with pytest.raises(ScriptFieldNotWritableError):
        workflow_engine.apply_cast_reparse_writeback(sid, {
            "characters": [{"name": "老张", "description": "新", "role": "伪造"}],
        })
    with pytest.raises(ValueError, match="description_owner"):
        workflow_engine.apply_cast_reparse_writeback(sid, {
            "characters": [{"name": "老张", "description": "新", "description_owner": "admin"}],
        })
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation") == before


def test_safe_merge_protects_user_adds_new_and_suggests_removal_or_rename():
    current = _script("老张走进办公室。", owner="user")
    merged = script_revision.merge_cast_reparse_candidates(current, {
        "characters": [
            {"name": "老张", "description": "模型新描述"},
            {"name": "小李", "description": "新人物"},
        ],
        "settings": [{"name": "办公室", "description": "新场景"}],
    })
    old = next(item for item in merged["characters"] if item["name"] == "老张")
    new = next(item for item in merged["characters"] if item["name"] == "小李")
    assert old["description"] == "旧描述"
    assert old["description_owner"] == "user"
    assert old["character_id"] == "char_old"
    assert new["character_id"] == script_revision.item_id(new, "character_id", "char")
    assert new["description_owner"] == "model"
    assert any(item.get("reason") == "user_owned" for item in merged["suggestions"])

    removal = script_revision.merge_cast_reparse_candidates(
        _script("这里没有人物。", owner="model"),
        {"characters": [], "settings": []},
    )
    assert [item["name"] for item in removal["characters"]] == ["老张"]
    assert removal["remove_candidates"][0]["name"] == "老张"

    rename = script_revision.merge_cast_reparse_candidates(
        _script("张先生走进办公室。", owner="model"),
        {"characters": [{"name": "张先生", "description": "新描述"}], "settings": []},
    )
    assert [item["name"] for item in rename["characters"]] == ["老张"]
    assert any(item.get("kind") == "rename" for item in rename["suggestions"])


def test_reparse_cast_is_internal_agent_method_and_cancellable(monkeypatch):
    prompts = []

    class FakeLLM:
        def query(self, prompt, *args, **kwargs):
            prompts.append(prompt)
            return json.dumps({
                "characters": [{"name": "老张", "description": "新描述", "character_id": "forged"}],
                "settings": [],
            }, ensure_ascii=False)

    monkeypatch.setattr("models.llm_client.LLM", FakeLLM)
    result = asyncio.run(ScriptWriterAgent().reparse_cast(
        _script("第一集正文"), [1], "fake", lambda: False,
    ))
    assert result["characters"][0]["name"] == "老张"
    assert "character_id" not in result["characters"][0]
    assert "第一集正文" in prompts[0]

    with pytest.raises(RuntimeError, match="停止"):
        asyncio.run(ScriptWriterAgent().reparse_cast(
            _script(), [1], "fake", lambda: True,
        ))
    assert len(prompts) == 1


def test_reparse_cast_action_is_not_exposed_as_intervention(client):
    sid = _session()
    before = workflow_engine.get_state(sid).status
    response = client.post(
        f"/api/project/{sid}/intervene",
        json={"stage": "script_generation", "modifications": {"action": "reparse_cast"}},
        headers=_headers(),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "script_action_not_supported"
    assert workflow_engine.get_state(sid).status == before
    assert "script_generation" not in workflow_engine.get_state(sid).executions
