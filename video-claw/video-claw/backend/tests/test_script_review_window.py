# -*- coding: utf-8 -*-
"""质检应用/撤销与确认续写的统一提交漏斗回归。"""
from __future__ import annotations

import asyncio
import copy
import time
import uuid

from api.dependencies import workflow_engine
from core import script_revision
from core.agents.script_agent import ScriptWriterAgent
from core.cast_sync_service import CastSyncService
from core.orchestrator import WorkflowStage, _StageRun


def _script():
    return {
        "title": "导入剧本",
        "episodes": [{"episode_number": 1, "act_title": "第1集", "content": "第一行\n第二行\n第三行"}],
        "characters": [{
            "character_id": "char_old",
            "name": "小陈",
            "description": "青年",
            "description_owner": "user",
        }],
        "settings": [{
            "setting_id": "set_old",
            "name": "办公室",
            "description": "内景",
            "description_owner": "model",
        }],
        "line_annotations": [{"line": 0, "type": "action"}],
        "scene_spans": [{"line": 0, "setting": "办公室"}],
        "meta": {"language": "zh", "source_format": "episode"},
    }


def _session():
    sid = f"rw{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(sid, {"idea": "review window", "llm_model": "fake"}, owner="alice")
    artifact = _script()

    def seed(state):
        state.artifacts["script_generation"] = artifact

    workflow_engine._mutate(sid, seed)
    return sid


def _record(sid):
    return copy.deepcopy((workflow_engine.get_state(sid).meta or {}).get("_cast_reparse") or {})


def _run(sid, action):
    run = _StageRun(
        session_id=sid,
        stage=WorkflowStage.SCRIPT_GENERATION,
        execution_id=f"exec-{uuid.uuid4().hex[:8]}",
        background=False,
        intervention={"action": action},
    )
    workflow_engine._begin_stage(run)
    return run


def test_quality_apply_and_rollback_each_register_pending_in_same_finish_transaction():
    sid = _session()
    current = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    preview = {
        "summary": "一条建议",
        "base_version": script_revision.content_fingerprint(current),
        "issues": [{
            "id": "qr_1",
            "episode_number": 1,
            "line": 2,
            "problem": "生硬",
            "action": "replace",
            "original": "第二行",
            "text": "第二行（改）",
        }],
    }
    agent = ScriptWriterAgent()
    applied_result = asyncio.run(agent._run_apply_script_review(
        {
            "session_id": sid,
            "_session_artifacts": {"script_generation": current},
            "pending_review": preview,
        },
        {"selected_issue_ids": ["qr_1"]},
    ))
    apply_run = _run(sid, "apply_script_review")
    workflow_engine._finish_stage(apply_run, applied_result, applied_result["payload"], None)

    record = _record(sid)
    assert record["status"] == "pending"
    assert record["attempt"] == 1
    assert record["reason"] == "apply_script_review"
    assert workflow_engine.get_state(sid).status["script_generation"] == "completed"
    assert "character_design" not in workflow_engine.get_state(sid).artifacts
    assert "storyboard" not in workflow_engine.get_state(sid).artifacts

    applied = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    rollback_result = asyncio.run(agent._run_rollback_script_review(
        {"session_id": sid, "_session_artifacts": {"script_generation": applied}},
        {"action": "rollback_script_review"},
    ))
    rollback_run = _run(sid, "rollback_script_review")
    workflow_engine._finish_stage(rollback_run, rollback_result, rollback_result["payload"], None)

    record = _record(sid)
    assert record["status"] == "pending"
    assert record["attempt"] == 2
    assert record["reason"] == "rollback_script_review"
    restored = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    assert restored["episodes"][0]["content"] == "第一行\n第二行\n第三行"
    assert workflow_engine.get_state(sid).status["script_generation"] == "completed"


def test_quality_apply_reparse_appends_new_cast_with_primary_key():
    sid = _session()
    current = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    preview = {
        "summary": "一条建议",
        "base_version": script_revision.content_fingerprint(current),
        "issues": [{
            "id": "qr_1", "episode_number": 1, "line": 2, "problem": "生硬",
            "action": "replace", "original": "第二行", "text": "第二行（改）",
        }],
    }
    result = asyncio.run(ScriptWriterAgent()._run_apply_script_review(
        {
            "session_id": sid,
            "_session_artifacts": {"script_generation": current},
            "pending_review": preview,
        },
        {"selected_issue_ids": ["qr_1"]},
    ))

    class FakeAgent:
        async def reparse_cast(self, *_args, **_kwargs):
            return {
                "characters": [
                    {"name": "小陈", "description": "青年"},
                    {"name": "新角色", "description": "质检后重解析新增"},
                ],
                "settings": [],
            }

    async def scenario():
        service = CastSyncService(workflow_engine, agent_factory=FakeAgent, settle_seconds=0.01)
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            run = _run(sid, "apply_script_review")
            workflow_engine._finish_stage(run, result, result["payload"], None)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and _record(sid).get("status") != "done":
                await asyncio.sleep(0.02)
            assert _record(sid)["status"] == "done"
        finally:
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())
    characters = workflow_engine.get_artifact_snapshot(sid, "script_generation")["characters"]
    added = next(item for item in characters if item["name"] == "新角色")
    assert added["character_id"] == script_revision.item_id(added, "character_id", "char")
    assert added["description_owner"] == "model"
    assert workflow_engine.get_state(sid).status["script_generation"] == "completed"


def test_reparse_failure_after_quality_apply_does_not_change_script_stage():
    sid = _session()
    current = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    preview = {
        "summary": "一条建议",
        "base_version": script_revision.content_fingerprint(current),
        "issues": [{
            "id": "qr_1", "episode_number": 1, "line": 2, "problem": "生硬",
            "action": "replace", "original": "第二行", "text": "第二行（改）",
        }],
    }
    result = asyncio.run(ScriptWriterAgent()._run_apply_script_review(
        {
            "session_id": sid,
            "_session_artifacts": {"script_generation": current},
            "pending_review": preview,
        },
        {"selected_issue_ids": ["qr_1"]},
    ))

    class FailingAgent:
        async def reparse_cast(self, *_args, **_kwargs):
            raise RuntimeError("fake reparse failure")

    async def scenario():
        service = CastSyncService(workflow_engine, agent_factory=FailingAgent, settle_seconds=0.01)
        workflow_engine.cast_sync_service = service
        service.start()
        try:
            run = _run(sid, "apply_script_review")
            workflow_engine._finish_stage(run, result, result["payload"], None)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and _record(sid).get("status") != "failed":
                await asyncio.sleep(0.02)
            assert _record(sid)["status"] == "failed"
            assert workflow_engine.get_state(sid).status["script_generation"] == "completed"
        finally:
            await service.stop()
            workflow_engine.cast_sync_service = None

    asyncio.run(scenario())


def test_confirm_continue_updates_only_script_and_does_not_create_downstream_shells():
    sid = _session()
    current = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    new_episode = {"episode_number": 2, "act_title": "第2集", "content": "新的续写正文"}
    new_character = {
        "character_id": "char_new",
        "name": "小李",
        "description": "新人物",
        "description_owner": "model",
    }
    new_setting = {
        "setting_id": "set_new",
        "name": "街道",
        "description": "外景",
        "description_owner": "model",
    }
    payload = copy.deepcopy(current)
    payload["episodes"].append(new_episode)
    payload["characters"].append(new_character)
    payload["settings"].append(new_setting)
    payload["new_episodes"] = [new_episode]
    payload["new_characters"] = [new_character]
    payload["new_settings"] = [new_setting]
    result = {"payload": payload, "requires_intervention": False, "stage_completed": True}

    run = _run(sid, "confirm_continue")
    state = workflow_engine._finish_stage(run, result, payload, None)
    script = state.artifacts["script_generation"]
    assert [episode["episode_number"] for episode in script["episodes"]] == [1, 2]
    assert [item["name"] for item in script["characters"]] == ["小陈", "小李"]
    assert [item["name"] for item in script["settings"]] == ["办公室", "街道"]
    assert "new_episodes" not in script and "new_characters" not in script and "new_settings" not in script
    assert "character_design" not in state.artifacts
    assert "storyboard" not in state.artifacts
    assert state.status["character_design"] == "pending"
    assert state.status["storyboard"] == "pending"
    record = _record(sid)
    assert record["status"] == "pending"
    assert record["reason"] == "confirm_continue"
    assert record["changed_episodes"] == [2]
