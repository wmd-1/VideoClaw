# -*- coding: utf-8 -*-
"""cast provenance、安全合并与版本化建议层。"""
from __future__ import annotations

import copy
import time
import uuid

import pytest

from api.dependencies import workflow_engine
from core import script_revision
from core.agents.character_agent import CharacterDesignerAgent
from core.orchestrator import WorkflowStage, WorkflowState


def _script(content="老张走进办公室。", *, owner=None, source=None):
    character = {
        "character_id": "char_old",
        "name": "老张",
        "description": "旧描述",
        "species": "人类",
    }
    if owner is not None:
        character["description_owner"] = owner
    if source is not None:
        character["description_source"] = source
    return {
        "title": "标题",
        "logline": "梗概",
        "episodes": [{"episode_number": 1, "act_title": "第一集", "content": content}],
        "characters": [character],
        "settings": [{
            "setting_id": "set_old",
            "name": "办公室",
            "description": "旧场景",
            "description_owner": "model",
        }],
    }


def _session(script=None):
    sid = f"cp{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(sid, {"idea": "provenance", "llm_model": "fake"}, owner="alice")
    artifact = copy.deepcopy(script or _script())

    def seed(state):
        state.artifacts["script_generation"] = artifact

    workflow_engine._mutate(sid, seed)
    return sid


def _version(sid):
    return script_revision.structure_fingerprint(
        workflow_engine.get_artifact_snapshot(sid, "script_generation"), scope="body"
    )


def _headers():
    return {"X-User-Login-Name": "alice"}


def _seed_suggestions(sid, merged):
    record = {
        "base_version": _version(sid),
        "generated_at": time.time(),
        "items": [
            *copy.deepcopy(merged.get("suggestions") or []),
            *copy.deepcopy(merged.get("remove_candidates") or []),
        ],
    }

    def apply(state):
        state.meta["_cast_suggestions"] = record

    workflow_engine._mutate(sid, apply)
    return record


def test_owner_resolution_and_import_mapping_cover_all_initial_states():
    assert script_revision.resolve_description_owner({"description_owner": "user"}) == "user"
    assert script_revision.resolve_description_owner({"description_owner": "model"}) == "model"
    assert script_revision.resolve_description_owner({"description_source": "uploaded"}) == "user"
    assert script_revision.resolve_description_owner({"description_source": "inferred"}) == "model"
    assert script_revision.resolve_description_owner({}) == "model"
    assert script_revision.resolve_description_owner({"description_owner": "forged"}) == "model"

    from core.script_import import merge_characters, merge_settings

    characters = merge_characters(
        [{"name": "老张", "description": "原文"}],
        [{"name": "小李", "description": "补全"}],
    )
    assert [(item["description_source"], item["description_owner"]) for item in characters] == [
        ("uploaded", "user"),
        ("inferred", "model"),
    ]
    settings = merge_settings([{"name": "办公室", "description": "场景"}])
    assert settings[0]["description_source"] == "inferred"
    assert settings[0]["description_owner"] == "model"

    from core.script_import import build_import_artifact

    artifact = build_import_artifact(
        "人物设定：老张：原文\n第1集\n老张：你好。",
        session_id="import-owner",
        inferred_characters=[{"name": "小李", "description": "补全"}],
        inferred_settings=[{"name": "办公室", "description": "场景"}],
    )
    by_name = {item["name"]: item for item in artifact["characters"]}
    assert by_name["老张"]["description_owner"] == "user"
    assert by_name["小李"]["description_owner"] == "model"
    assert artifact["settings"][0]["description_owner"] == "model"


@pytest.mark.parametrize(
    "owner,source,expected_owner,should_update",
    [
        ("user", None, "user", False),
        (None, "uploaded", "user", False),
        (None, "inferred", "model", True),
        (None, None, "model", True),
        ("model", None, "model", True),
    ],
)
def test_reparse_merge_uses_owner_fallback_instead_of_protecting_every_legacy_item(
    owner, source, expected_owner, should_update
):
    current = _script(owner=owner, source=source)
    merged = script_revision.merge_cast_reparse_candidates(current, {
        "characters": [{"name": "老张", "description": "模型新描述"}],
        "settings": [],
    })
    character = merged["characters"][0]
    assert character["description_owner"] == expected_owner
    assert character["description"] == ("模型新描述" if should_update else "旧描述")
    assert bool(merged["suggestions"]) is (not should_update)


def test_script_patch_stamps_owner_by_description_diff_and_ignores_client_owner():
    current = _script(owner="model")
    unchanged, _ = script_revision.sanitize_script_patch(current, {
        "characters": [{
            "character_id": "char_old",
            "name": "老张",
            "description": "旧描述",
            "description_owner": "user",
        }],
    })
    assert unchanged["characters"][0]["description_owner"] == "model"

    changed, _ = script_revision.sanitize_script_patch(current, {
        "characters": [{
            "character_id": "char_old",
            "name": "老张",
            "description": "用户新描述",
            "description_owner": "model",
        }],
    })
    assert changed["characters"][0]["description_owner"] == "user"

    created, _ = script_revision.sanitize_script_patch(current, {
        "characters": [{"name": "小李", "description": "新人物", "description_owner": "model"}],
    })
    assert created["characters"][0]["name"] == "小李"
    assert created["characters"][0]["description_owner"] == "user"

    uploaded = _script(source="uploaded")
    mapped, _ = script_revision.sanitize_script_patch(uploaded, {
        "characters": [{"character_id": "char_old", "name": "老张", "description": "旧描述"}],
    })
    assert mapped["characters"][0]["description_owner"] == "user"


def test_description_owner_is_excluded_from_both_fingerprints_but_source_is_not():
    base = _script(owner="model", source="inferred")
    changed = copy.deepcopy(base)
    changed["characters"][0]["description_owner"] = "user"
    assert script_revision.content_fingerprint(base) == script_revision.content_fingerprint(changed)
    for scope in ("all", "cast", "body"):
        assert script_revision.structure_fingerprint(base, scope=scope) == script_revision.structure_fingerprint(
            changed, scope=scope
        )

    source_changed = copy.deepcopy(base)
    source_changed["characters"][0]["description_source"] = "uploaded"
    assert script_revision.content_fingerprint(base) != script_revision.content_fingerprint(source_changed)
    assert script_revision.structure_fingerprint(base, scope="cast") != script_revision.structure_fingerprint(
        source_changed, scope="cast"
    )


def test_ai_cast_replacement_preserves_assets_and_marks_only_confirmed_description_changes_model():
    current = [{
        "character_id": "char_old",
        "name": "老张",
        "description": "旧描述",
        "description_owner": "user",
        "selected": "portrait.png",
        "versions": ["portrait.png"],
    }]
    unchanged = script_revision.merge_model_cast_provenance(
        current, [{"name": "老张", "description": "旧描述"}],
        container="characters", id_key="character_id", prefix="char",
    )[0]
    assert unchanged["description_owner"] == "user"
    assert unchanged["selected"] == "portrait.png"

    changed = script_revision.merge_model_cast_provenance(
        current, [{"name": "老张", "description": "AI 新描述"}],
        container="characters", id_key="character_id", prefix="char",
    )[0]
    assert changed["description_owner"] == "model"
    assert changed["character_id"] == "char_old"
    assert changed["versions"] == ["portrait.png"]

    added = script_revision.merge_model_cast_provenance(
        current, [{"name": "小李", "description": "新人物"}],
        container="characters", id_key="character_id", prefix="char",
    )
    assert added[0]["name"] == "小李"
    assert added[0]["description_owner"] == "model"
    assert added[0]["character_id"]


def test_seed_and_rebuild_preserve_owner_and_existing_assets():
    state = WorkflowState("seed-owner")
    state.artifacts["script_generation"] = _script(owner="user")
    assert workflow_engine._seed_stage_items_from_script(state) is True
    seeded = state.artifacts["character_design"]["characters"][0]
    assert seeded["description_owner"] == "user"

    state.artifacts["character_design"]["characters"][0].update({
        "selected": "portrait.png",
        "versions": ["portrait.png"],
        "status": "done",
    })
    state.artifacts["script_generation"]["characters"][0]["description_owner"] = "model"
    assert workflow_engine._rebuild_stage_items_from_script(state) is True
    rebuilt = state.artifacts["character_design"]["characters"][0]
    assert rebuilt["description_owner"] == "model"
    assert rebuilt["selected"] == "portrait.png"
    assert rebuilt["versions"] == ["portrait.png"]


def test_stage2_patch_marks_manual_description_user_and_preserves_unchanged_owner():
    sid = _session()

    def seed_stage2(state):
        state.artifacts["character_design"] = {
            "characters": [{
                "id": "char_old",
                "name": "老张",
                "description": "旧描述",
                "species": "人类",
                "description_owner": "model",
                "selected": "",
                "versions": [],
            }],
            "settings": [],
        }

    workflow_engine._mutate(sid, seed_stage2)
    workflow_engine.update_artifact(sid, "character_design", {
        "characters": [{
            "id": "char_old",
            "name": "老张",
            "description": "阶段2手改",
            "description_owner": "model",
        }],
    })
    item = workflow_engine.get_artifact_snapshot(sid, "character_design")["characters"][0]
    assert item["description_owner"] == "user"

    workflow_engine.update_artifact(sid, "character_design", {
        "characters": [{
            "id": "char_old",
            "name": "老张",
            "description": "阶段2手改",
            "description_owner": "model",
        }],
    })
    item = workflow_engine.get_artifact_snapshot(sid, "character_design")["characters"][0]
    assert item["description_owner"] == "user"


def test_stage2_writeback_copies_owner_and_infers_missing_owner_from_description_diff():
    state = WorkflowState("writeback-owner")
    state.artifacts["script_generation"] = _script(owner="user")
    workflow_engine._writeback_character_cast_to_script(state, {
        "characters": [{
            "id": "char_old",
            "name": "老张",
            "description": "旧描述",
            "description_owner": "model",
        }],
        "settings": [],
    })
    assert state.artifacts["script_generation"]["characters"][0]["description_owner"] == "model"

    state.artifacts["script_generation"] = _script(owner="model")
    workflow_engine._writeback_character_cast_to_script(state, {
        "characters": [{"id": "char_old", "name": "老张", "description": "阶段2手改"}],
        "settings": [],
    })
    assert state.artifacts["script_generation"]["characters"][0]["description_owner"] == "user"


def test_prefer_user_edits_and_background_regeneration_keep_user_owner():
    base = {"characters": [{"id": "char_old", "description": "旧描述", "description_owner": "model"}]}
    latest = {"characters": [{"id": "char_old", "description": "用户改", "description_owner": "user"}]}
    payload = {"characters": [{"id": "char_old", "description": "模型改", "description_owner": "model"}]}
    merged = workflow_engine._prefer_user_edits("character_design", base, latest, payload)
    assert merged["characters"][0]["description"] == "用户改"
    assert merged["characters"][0]["description_owner"] == "user"

    existing = {"characters": [{
        "id": "char_old", "description": "用户改", "description_owner": "user", "versions": ["old.png"],
    }]}
    incoming = {"characters": [{
        "id": "char_old", "description": "模型改", "description_owner": "model", "versions": ["new.png"],
    }]}
    baseline = {"characters": {"char_old": {"description": "旧描述", "description_owner": "model"}}}
    merged = workflow_engine._merge_item_regeneration_payload(
        existing, incoming, ["characters", "settings"],
        baseline_fields=baseline, protected_fields=("description",),
    )
    assert merged["characters"][0]["description"] == "用户改"
    assert merged["characters"][0]["description_owner"] == "user"
    assert merged["characters"][0]["versions"] == ["old.png", "new.png"]


def test_character_agent_payload_and_preview_carry_owner(monkeypatch):
    script = _script(owner="user")
    data = CharacterDesignerAgent()._read_script_data({"_session_artifacts": {"script_generation": script}})
    assert data["characters"]["char_old"]["description_owner"] == "user"

    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda *_args: [])
    item = agent._build_asset_info(
        "sid", "characters", "char_old", "老张", "旧描述", description_owner="user"
    )
    assert item["description_owner"] == "user"
    preview = agent._build_preview("sid", data["characters"], data["settings"])
    assert preview["characters"][0]["description_owner"] == "user"


def test_suggestions_have_stable_ids_and_rename_does_not_also_suggest_removal():
    protected = script_revision.merge_cast_reparse_candidates(_script(owner="user"), {
        "characters": [{"name": "老张", "description": "新描述"}],
        "settings": [],
    })
    suggestion = protected["suggestions"][0]
    assert suggestion["id"].startswith("cast_sug_")
    assert suggestion["item_id"] == "char_old"
    assert protected["summary"]["suggested"] == 1

    renamed = script_revision.merge_cast_reparse_candidates(_script("张先生走进办公室。"), {
        "characters": [{"name": "张先生", "description": "新描述"}],
        "settings": [],
    })
    assert [item["kind"] for item in renamed["suggestions"]] == ["rename"]
    assert renamed["remove_candidates"] == []


def test_complete_projects_current_suggestions_and_new_body_invalidates_them():
    sid = _session(_script(owner="user"))
    workflow_engine.update_artifact(sid, "script_generation", {
        "episodes": [{"episode_number": 1, "content": "新正文"}],
    })
    record = copy.deepcopy(workflow_engine.get_state(sid).meta["_cast_reparse"])
    payload = workflow_engine.begin_cast_reparse(
        sid, "job-suggestions", record["attempt"], record["base_version"]
    )
    assert payload is not None
    before_fingerprint = script_revision.content_fingerprint(
        workflow_engine.get_artifact_snapshot(sid, "script_generation")
    )
    assert workflow_engine.complete_cast_reparse(
        sid, payload["job_id"], payload["attempt"], payload["base_version"],
        {"characters": [{"name": "老张", "description": "模型建议"}], "settings": []},
    ) == "done"

    snapshot = workflow_engine.get_status_snapshot(sid)
    assert snapshot["cast_reparse"]["status"] == "done"
    assert snapshot["cast_reparse_ready"] is True
    assert snapshot["cast_suggestions"][0]["reason"] == "user_owned"
    assert "_cast_reparse" not in snapshot["meta"]
    assert "_cast_suggestions" not in snapshot["meta"]
    assert "_cast_suggestions" not in snapshot["artifacts"]["script_generation"]
    assert script_revision.content_fingerprint(snapshot["artifacts"]["script_generation"]) == before_fingerprint

    workflow_engine.update_artifact(sid, "script_generation", {
        "episodes": [{"episode_number": 1, "content": "更新的正文"}],
    })
    snapshot = workflow_engine.get_status_snapshot(sid)
    assert snapshot["cast_suggestions"] == []
    assert snapshot["cast_reparse_ready"] is False
    assert "_cast_suggestions" not in workflow_engine.get_state(sid).meta


def test_status_routes_share_authoritative_control_projection(client):
    sid = _session(_script(owner="user"))
    merged = script_revision.merge_cast_reparse_candidates(
        workflow_engine.get_artifact_snapshot(sid, "script_generation"),
        {"characters": [{"name": "老张", "description": "模型建议"}], "settings": []},
    )
    suggestions = _seed_suggestions(sid, merged)
    version = _version(sid)

    def seed_done(state):
        state.meta["_cast_reparse"] = {
            "status": "done", "attempt": 1, "base_version": version,
            "active_job": None, "error": None, "summary": {},
        }

    workflow_engine._mutate(sid, seed_done)
    snapshots = []
    for suffix in ("status", "status/from_disk"):
        response = client.get(f"/api/project/{sid}/{suffix}", headers=_headers())
        assert response.status_code == 200
        snapshots.append(response.json())

    for snapshot in snapshots:
        assert snapshot["script_locked"] is False
        assert snapshot["cast_reparse"]["status"] == "done"
        assert snapshot["cast_reparse"]["base_version"] == version
        assert snapshot["cast_reparse_ready"] is True
        assert snapshot["cast_suggestions"] == suggestions["items"]
        assert "_cast_reparse" not in snapshot["meta"]
        assert "_cast_suggestions" not in snapshot["meta"]
    for key in ("script_locked", "cast_reparse", "cast_reparse_ready", "cast_suggestions"):
        assert snapshots[0][key] == snapshots[1][key]


def test_apply_suggestion_api_updates_owner_without_body_change_or_reparse(client):
    sid = _session(_script(owner="user"))
    merged = script_revision.merge_cast_reparse_candidates(
        workflow_engine.get_artifact_snapshot(sid, "script_generation"),
        {"characters": [{"name": "老张", "description": "采纳后的描述"}], "settings": []},
    )
    record = _seed_suggestions(sid, merged)
    before_version = _version(sid)

    response = client.post(
        f"/api/project/{sid}/cast-suggestions/apply",
        json={"ids": [record["items"][0]["id"]], "base_version": record["base_version"]},
        headers=_headers(),
    )
    assert response.status_code == 200
    artifact = response.json()["artifact"]
    assert artifact["characters"][0]["description"] == "采纳后的描述"
    assert artifact["characters"][0]["description_owner"] == "model"
    assert _version(sid) == before_version
    assert "_cast_reparse" not in workflow_engine.get_state(sid).meta
    assert "_cast_suggestions" not in workflow_engine.get_state(sid).meta


def test_apply_suggestion_is_rejected_when_script_locked_and_status_hides_suggestions(client):
    sid = _session(_script(owner="user"))
    merged = script_revision.merge_cast_reparse_candidates(
        workflow_engine.get_artifact_snapshot(sid, "script_generation"),
        {"characters": [{"name": "老张", "description": "锁后不应采纳"}], "settings": []},
    )
    record = _seed_suggestions(sid, merged)
    workflow_engine._mutate(sid, lambda state: setattr(state, "current_stage", WorkflowStage.CHARACTER_DESIGN))
    before = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    snapshot = workflow_engine.get_status_snapshot(sid)
    assert snapshot["script_locked"] is True
    assert snapshot["cast_suggestions"] == []

    response = client.post(
        f"/api/project/{sid}/cast-suggestions/apply",
        json={"ids": [record["items"][0]["id"]], "base_version": record["base_version"]},
        headers=_headers(),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "script_locked"
    assert workflow_engine.get_artifact_snapshot(sid, "script_generation") == before


@pytest.mark.parametrize("status", ["pending", "running", "superseded"])
def test_apply_suggestion_rejects_active_reparse(client, status):
    sid = _session(_script(owner="user"))
    merged = script_revision.merge_cast_reparse_candidates(
        workflow_engine.get_artifact_snapshot(sid, "script_generation"),
        {"characters": [{"name": "老张", "description": "运行中不应采纳"}], "settings": []},
    )
    record = _seed_suggestions(sid, merged)

    def seed_reparse(state):
        state.meta["_cast_reparse"] = {
            "status": status, "attempt": 1, "base_version": record["base_version"],
            "active_job": "job" if status == "running" else None,
        }

    workflow_engine._mutate(sid, seed_reparse)
    response = client.post(
        f"/api/project/{sid}/cast-suggestions/apply",
        json={"ids": [record["items"][0]["id"]], "base_version": record["base_version"]},
        headers=_headers(),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "reparse_busy"


def test_apply_suggestion_rejects_stale_version_and_unknown_id(client):
    sid = _session(_script(owner="user"))
    merged = script_revision.merge_cast_reparse_candidates(
        workflow_engine.get_artifact_snapshot(sid, "script_generation"),
        {"characters": [{"name": "老张", "description": "新描述"}], "settings": []},
    )
    record = _seed_suggestions(sid, merged)

    unknown = client.post(
        f"/api/project/{sid}/cast-suggestions/apply",
        json={"ids": ["missing"], "base_version": record["base_version"]},
        headers=_headers(),
    )
    assert unknown.status_code == 409
    assert unknown.json()["detail"]["code"] == "cast_suggestion_not_found"

    workflow_engine.update_artifact(sid, "script_generation", {
        "episodes": [{"episode_number": 1, "content": "建议失效"}],
    })
    stale = client.post(
        f"/api/project/{sid}/cast-suggestions/apply",
        json={"ids": [record["items"][0]["id"]], "base_version": record["base_version"]},
        headers=_headers(),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "cast_suggestions_stale"


def test_apply_rename_and_remove_suggestions_preserve_primary_key_rules():
    sid = _session(_script("张先生走进办公室。"))
    script = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    renamed = script_revision.merge_cast_reparse_candidates(script, {
        "characters": [{"name": "张先生", "description": "新描述"}],
        "settings": [],
    })
    record = _seed_suggestions(sid, renamed)
    result = workflow_engine.apply_cast_suggestions(
        sid, ids=[record["items"][0]["id"]], base_version=record["base_version"], user="alice"
    )
    assert result["artifact"]["characters"][0]["name"] == "张先生"
    assert result["artifact"]["characters"][0]["character_id"] == "char_old"

    sid = _session(_script("这里没有人物。"))
    script = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    removed = script_revision.merge_cast_reparse_candidates(script, {
        "characters": [], "settings": [],
    })
    record = _seed_suggestions(sid, removed)
    result = workflow_engine.apply_cast_suggestions(
        sid, ids=[record["items"][0]["id"]], base_version=record["base_version"], user="alice"
    )
    assert result["artifact"]["characters"] == []
