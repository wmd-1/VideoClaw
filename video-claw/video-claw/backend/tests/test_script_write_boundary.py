# -*- coding: utf-8 -*-
"""剧本写入白名单、modified_script 收窄与内部 meta 过滤。"""
from __future__ import annotations

import asyncio
import copy
import uuid

import pytest

from api.dependencies import workflow_engine
from core import script_import, script_revision, workflow_store
from core.agents.base_agent import AgentInterface
from core.agents.script_agent import ScriptWriterAgent
from core.orchestrator import WorkflowStage
from core.script_errors import InternalMetaWriteError, ScriptFieldNotWritableError


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
            "description_source": "uploaded",
            "description_owner": "user",
        }],
        "settings": [{
            "setting_id": "set_old",
            "name": "办公室",
            "description": "旧场景",
            "description_source": "inferred",
            "description_owner": "model",
        }],
        "meta": {"original_text": "服务端原文", "film_type": "介绍"},
        "line_annotations": [{"line": 0, "type": "dialogue"}],
        "scene_spans": [{"line": 0, "setting": "办公室"}],
    }


def test_patch_rejects_unknown_top_level_fields_without_mutating_input():
    current = _script()
    body = {"title": "新标题", "meta": {"original_text": "伪造"}}
    before = copy.deepcopy(body)

    with pytest.raises(ScriptFieldNotWritableError) as exc_info:
        script_revision.sanitize_script_patch(current, body)

    assert exc_info.value.code == "script_field_not_writable"
    assert exc_info.value.fields == ["meta"]
    assert body == before


def test_nested_cleaning_preserves_server_fields_and_recomputes_owner():
    current = _script()
    cleaned, ignored = script_revision.sanitize_script_patch(current, {
        "characters": [{
            "character_id": "char_old",
            "name": "老张",
            "description": "用户新描述",
            "species": "人类",
            "description_owner": "model",
            "description_source": "inferred",
            "asset_kind": "human",
            "role": "主角",
        }],
        "settings": [{
            "setting_id": "set_old",
            "name": "办公室",
            "description": "旧场景",
            "mood": "紧张",
            "description_owner": "user",
        }],
        "episodes": [{
            "episode_number": 1,
            "act_title": "第一集",
            "content": "新正文",
            "line_start": 999,
            "line_end": 999,
            "episode_id": "forged",
        }],
    })

    character = cleaned["characters"][0]
    assert character["character_id"] == "char_old"
    assert character["description_source"] == "uploaded"
    assert character["description_owner"] == "user"
    assert "asset_kind" not in character and "role" not in character
    setting = cleaned["settings"][0]
    assert setting["setting_id"] == "set_old"
    assert setting["description_owner"] == "model"
    assert setting["mood"] == "紧张"
    episode = cleaned["episodes"][0]
    assert "line_start" not in episode and "line_end" not in episode
    assert "episode_id" not in episode
    assert "characters[0].description_owner" in ignored
    assert "characters[0].description_source" in ignored
    assert "characters[0].asset_kind" in ignored
    assert "characters[0].role" in ignored
    assert "episodes[0].line_start" in ignored


def test_new_cast_item_gets_stable_id_and_user_owner():
    cleaned, ignored = script_revision.sanitize_script_patch(_script(), {
        "characters": [{"name": "新人物", "description": "新描述", "species": "人类"}],
    })

    item = cleaned["characters"][0]
    assert item["character_id"] == script_import.stable_id("char", "新人物")
    assert item["description_owner"] == "user"
    assert ignored == []


def test_modified_script_merges_only_whitelist_and_reports_ignored_fields():
    current = _script()
    current["pending_revision"] = {"instruction": "旧预览"}
    modified = copy.deepcopy(current)
    modified["title"] = "新标题"
    modified["episodes"][0]["content"] = "新正文"
    modified["meta"]["original_text"] = "客户端伪造原文"
    modified["line_annotations"] = [{"line": 999, "type": "forged"}]
    modified["scene_spans"] = [{"line": 999, "setting": "伪造"}]
    modified["summary"] = "客户端摘要"
    modified["overall_style"] = "客户端风格"

    result = asyncio.run(ScriptWriterAgent().process(
        {
            "session_id": "sid",
            "_session_artifacts": {"script_generation": current},
        },
        {"modified_script": modified},
    ))

    payload = result["payload"]
    assert payload["title"] == "新标题"
    assert payload["episodes"][0]["content"] == "新正文"
    assert payload["meta"]["original_text"] == "服务端原文"
    assert payload["line_annotations"] == current["line_annotations"]
    assert payload["scene_spans"] == current["scene_spans"]
    assert "summary" not in payload and "overall_style" not in payload
    assert "pending_revision" not in payload
    ignored = set(result["ignored_fields"])
    assert {"meta", "line_annotations", "scene_spans", "summary", "overall_style"} <= ignored


def test_legacy_meta_shell_expands_and_keeps_internal_keys():
    meta = {
        "project_params": {"idea": "创意", "style": "anime", "models": {"llm": "m1"}},
        "model_fallback": {"llm": "m2"},
        "_cast_reparse": {"status": "pending", "attempt": 1},
        "_cast_suggestions": {"base_version": "v1", "items": []},
    }

    flat = workflow_store.session_meta(meta)

    assert flat["idea"] == "创意"
    assert flat["style"] == "anime"
    assert flat["models"] == {"llm": "m1"}
    assert flat["model_fallback"] == {"llm": "m2"}
    assert flat["_cast_reparse"] == meta["_cast_reparse"]
    assert flat["_cast_suggestions"] == meta["_cast_suggestions"]
    public = workflow_store.public_session_meta(flat)
    assert "_cast_reparse" not in public and "_cast_suggestions" not in public


def _session():
    sid = f"sw{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(sid, {"idea": "写入边界", "llm_model": "fake"}, owner="alice")
    return sid


def test_prepare_filters_internal_meta_and_rejects_client_internal_keys():
    sid = _session()

    def apply(state):
        state.meta["_cast_reparse"] = {"status": "pending", "attempt": 1}
        state.meta["_cast_suggestions"] = {"base_version": "v1", "items": []}

    workflow_engine._mutate(sid, apply)
    _, stage_input = workflow_engine.prepare_stage_execution(sid, "script_generation", {})
    assert "_cast_reparse" not in stage_input
    assert "_cast_suggestions" not in stage_input
    assert stage_input["idea"] == "写入边界"

    _, intervention_input = workflow_engine.prepare_intervention_execution(
        sid, "script_generation", {"action": "modified_script", "modified_script": {}}
    )
    assert "_cast_reparse" not in intervention_input
    assert "_cast_suggestions" not in intervention_input

    with pytest.raises(InternalMetaWriteError):
        workflow_engine.prepare_stage_execution(sid, "script_generation", {"_cast_reparse": {}})
    with pytest.raises(InternalMetaWriteError):
        workflow_engine.prepare_intervention_execution(
            sid, "script_generation", {"_cast_suggestions": {}}
        )


def test_execute_stage_filters_internal_meta_but_keeps_runtime_keys(monkeypatch):
    sid = _session()
    captured = {}

    class CaptureAgent(AgentInterface):
        async def process(self, input_data, intervention=None):
            captured.update(input_data)
            return {"payload": {"episodes": [], "characters": [], "settings": []}}

    def apply(state):
        state.meta["_cast_reparse"] = {"status": "pending", "attempt": 1}
        state.meta["_cast_suggestions"] = {"base_version": "v1", "items": []}

    workflow_engine._mutate(sid, apply)
    monkeypatch.setitem(workflow_engine.agent_factories, WorkflowStage.SCRIPT_GENERATION, CaptureAgent)
    state = workflow_engine.get_state(sid)
    asyncio.run(workflow_engine.execute_stage(state, WorkflowStage.SCRIPT_GENERATION, {}))

    assert "_cast_reparse" not in captured["_session_meta"]
    assert "_cast_suggestions" not in captured["_session_meta"]
    assert captured["_session_meta"]["idea"] == "写入边界"
    assert "_session_artifacts" in captured
    assert "_session_owner" in captured
    assert "_runtime_map" in captured


def test_update_artifact_api_rejects_mixed_fields_without_partial_write(client):
    sid = _session()
    workflow_engine.update_artifact(sid, "script_generation", {"title": "旧标题"})
    headers = {"X-User-Login-Name": "alice"}

    response = client.patch(
        f"/api/project/{sid}/artifact/script_generation",
        json={"title": "不应落盘", "meta": {"original_text": "伪造"}},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "script_field_not_writable"
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation")["title"] == "旧标题"


def test_update_artifact_returns_nested_ignored_fields_and_keeps_preview_clear():
    sid = _session()
    workflow_engine.update_artifact(sid, "script_generation", {
        "title": "标题",
        "characters": [{"name": "老张", "description": "旧描述"}],
        "episodes": [{"episode_number": 1, "content": "正文"}],
    })

    result = workflow_engine.update_artifact(sid, "script_generation", {
        "characters": [{
            "name": "老张",
            "description": "新描述",
            "description_owner": "model",
            "asset_kind": "human",
        }],
        "pending_review": None,
    })

    item = result["artifact"]["characters"][0]
    assert item["description_owner"] == "user"
    assert "asset_kind" not in item
    assert "characters[0].description_owner" in result["ignored_fields"]
    assert "characters[0].asset_kind" in result["ignored_fields"]
    assert "pending_review" not in result["artifact"]
