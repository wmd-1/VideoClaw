"""参考图阶段在 reference 模式下跳过（openspec: reference-mode-skip-scene-images）。

覆盖：agent 入口短路与产物形态、编排层物化短路、完成态按模式判定、快照晋升看
`skip` 标记、模式切换双向对齐与继续阻塞、图像依赖闸放行、跳过形态的上传防线。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

import asyncio
import io
import os
import uuid

import pytest
from fastapi import HTTPException

from api.dependencies import workflow_engine
from config import settings
from core import media_refs
from core.orchestrator import WorkflowStage, WorkflowState
from core.reference_skip import SKIP_REASON, build_reference_skip
from core.workflow_store import artifact_has_pending, build_stage_changes
from db.repositories.commits import load_committed

OWNER = "alice"
MODELS = {
    "llm_model": "test-llm",
    "vlm_model": "test-vlm",
    "image_t2i_model": "test-t2i",
    "image_it2i_model": "test-i2i",
    "video_first_frame_model": "test-video",
}


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    code_root = tmp_path / "code"
    for path in (
        code_root / "result",
        code_root / "temp",
        code_root / "work" / "executions",
        tmp_path / "sessions",
    ):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "CODE_DIR", str(code_root))
    monkeypatch.setattr(settings, "RESULT_DIR", str(code_root / "result"))
    monkeypatch.setattr(settings, "TEMP_DIR", str(code_root / "temp"))
    refs_root = code_root / "work" / "_refs"
    refs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(media_refs, "_REFS_ROOT", refs_root)
    monkeypatch.setattr(workflow_engine, "_session_dir", str(tmp_path / "sessions"))
    yield tmp_path


def _new_session(mode: str = "first_frame") -> str:
    sid = f"s{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(
        sid, {"idea": "参考图跳过测试", "video_generation_mode": mode}, owner=OWNER
    )
    return sid


def _run_reference_stage(sid: str, *, intervention=None, mode: str = "reference"):
    workflow_engine.update_session_meta(
        sid, {"video_generation_mode": mode}, ("video_generation_mode",)
    )
    state = workflow_engine.get_state(sid)
    return asyncio.run(
        workflow_engine.execute_stage(
            state,
            WorkflowStage.REFERENCE_GENERATION,
            {"session_id": sid, "video_generation_mode": mode, **MODELS},
            intervention=intervention,
        )
    )


# ─────────────── 1. agent 短路与产物形态（tasks 1.1 / 1.2 / 1.3） ───────────────


def test_reference_mode_returns_skip_payload_without_generation(monkeypatch):
    import models.image_client as image_client

    def boom(*_args, **_kwargs):
        raise AssertionError("reference 模式不应构造图像客户端")

    monkeypatch.setattr(image_client, "ImageClient", boom)
    from core.agents.reference_agent import ReferenceGeneratorAgent

    result = asyncio.run(
        ReferenceGeneratorAgent().process(
            {"session_id": "sid-skip", "video_generation_mode": "reference"}
        )
    )
    payload = result["payload"]
    assert result["stage_completed"] is True
    assert payload["scenes"] == []
    # 所有跳过信息都在单一嵌套对象内；顶层不新增标量键（既有 session_id 除外）
    assert set(payload) == {"session_id", "scenes", "skip"}
    assert payload["skip"]["mode"] == "reference"
    assert payload["skip"]["reason"] == SKIP_REASON
    assert payload["skip"]["note"]


def test_reference_mode_ignores_single_scene_regeneration(monkeypatch):
    """介入参数不改变跳过行为：仍返回同一形态且不构造图像客户端（tasks 1.2）。"""
    import models.image_client as image_client

    def boom(*_args, **_kwargs):
        raise AssertionError("介入路径也不得构造图像客户端")

    monkeypatch.setattr(image_client, "ImageClient", boom)
    from core.agents.reference_agent import ReferenceGeneratorAgent

    result = asyncio.run(
        ReferenceGeneratorAgent().process(
            {"session_id": "sid-skip", "video_generation_mode": "reference"},
            intervention={"regenerate_scenes": ["seg_1"]},
        )
    )
    assert result["stage_completed"] is True
    assert result["payload"]["scenes"] == []
    assert result["payload"]["skip"]["mode"] == "reference"


def test_non_reference_mode_does_not_short_circuit(monkeypatch):
    """模式为非 reference 时不走跳过分支（由旧路径的数据校验报错，而不是返回 skip）。"""
    from core.agents.reference_agent import ReferenceGeneratorAgent

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            ReferenceGeneratorAgent().process(
                {"session_id": "sid-noskip", "video_generation_mode": "first_frame"}
            )
        )
    assert "skip" not in str(excinfo.value)


def test_empty_artifact_is_written_with_skip_and_completed():
    sid = _new_session(mode="reference")
    _run_reference_stage(sid)

    artifact = workflow_engine.get_state(sid).artifacts["reference_generation"]
    assert artifact["scenes"] == []
    assert artifact["skip"]["mode"] == "reference"
    assert workflow_engine.get_state(sid).status["reference_generation"] == "completed"


def test_skip_artifact_promotes_to_checkpoint():
    sid = _new_session(mode="reference")
    _run_reference_stage(sid)

    snapshot = load_committed(sid)
    checkpoint = snapshot["checkpoints"].get("reference_generation") or {}
    assert checkpoint.get("skip", {}).get("mode") == "reference", snapshot["checkpoints"].keys()
    working = (snapshot["working"].get("reference_generation") or {}).get("working")
    assert not working, "跳过形态产物不应停留在工作层"


def test_skip_run_creates_no_image_files():
    """跳过路径零文件产出（tasks 1.3）：执行前后 Scenes 目录不应出现任何图片。"""
    sid = _new_session(mode="reference")
    _run_reference_stage(sid)
    scenes_dir = os.path.join(settings.CODE_DIR, "result", "image", str(sid), "Scenes")
    assert not os.path.isdir(scenes_dir) or not os.listdir(scenes_dir)


# ─────────────── 2. 编排层物化短路（tasks 2.1 / 2.2） ───────────────


def test_plan_targets_reference_stage_is_empty_in_reference_mode():
    from core.consumption import plan_targets

    artifacts = {
        "storyboard": {
            "episodes": [{"episode_number": 1, "segments": [
                {"segment_id": "seg_1", "characters": ["小明"], "shots": [{"content": "镜头"}]}
            ]}]
        },
        "character_design": {
            "characters": [{"id": "c1", "name": "小明", "selected": "/code/x.png"}],
            "settings": [],
        },
    }
    assert plan_targets("reference_generation", artifacts, mode="reference") == []
    # 其它模式保持原计划（不应误伤共享分支）
    assert plan_targets("reference_generation", artifacts, mode="first_frame")


def test_reference_stage_completes_without_character_assets():
    """角色/场景资产未产出时仍以跳过形态完成，不抛消费错误（tasks 2.2）。"""
    sid = _new_session(mode="reference")
    _run_reference_stage(sid)
    artifact = workflow_engine.get_state(sid).artifacts["reference_generation"]
    assert artifact["skip"]["mode"] == "reference"
    assert workflow_engine.get_state(sid).status["reference_generation"] == "completed"


def test_materialize_does_not_require_assets_in_reference_mode():
    from core.consumption import materialize_stage_inputs

    artifacts = {
        "storyboard": {"episodes": [{"segments": [{"segment_id": "seg_1"}]}]},
        "character_design": {"characters": [{"id": "c1", "selected": ""}], "settings": []},
    }
    result = materialize_stage_inputs("reference_generation", artifacts, object(), mode="reference")
    assert result.optional_issues == []
    assert result.pinned == []


def test_other_stage_plans_unchanged_in_reference_mode():
    """video_generation / prompt_rewrite 的消费计划不因本次短路而改变（不误改共享分支）。"""
    from core.consumption import plan_targets

    artifacts = {
        "storyboard": {
            "episodes": [{"episode_number": 1, "segments": [
                {"segment_id": "seg_1", "characters": ["小明"], "shots": [{"content": "镜头"}]}
            ]}]
        },
        "character_design": {
            "characters": [{"id": "c1", "name": "小明", "selected": "/code/x.png"}],
            "settings": [],
        },
    }
    assert plan_targets("video_generation", artifacts, mode="reference")
    assert plan_targets("prompt_rewrite", artifacts, mode="reference", grounding=True)


# ─────────────── 3. 完成态与晋升判据（tasks 4.1 / 4.2 / 4.4） ───────────────


def _status_for(mode: str, artifact: dict) -> str:
    state = WorkflowState("sid-status")
    state.meta = {"video_generation_mode": mode}
    state.artifacts["reference_generation"] = artifact
    workflow_engine._recalculate_all_statuses(state)
    return state.status["reference_generation"]


def test_status_completed_in_reference_mode_even_without_scenes():
    assert _status_for("reference", {"scenes": [], "skip": build_reference_skip()}) == "completed"


def test_status_completed_in_reference_mode_with_placeholder_entries():
    artifact = {"scenes": [{"id": "seg_1", "selected": "", "versions": []}], "skip": build_reference_skip()}
    assert _status_for("reference", artifact) == "completed"


def test_status_waiting_in_frame_mode_without_selection():
    assert _status_for("first_frame", {"scenes": []}) == "waiting"
    assert _status_for("first_frame", {"scenes": [{"id": "seg_1", "selected": ""}]}) == "waiting"
    assert _status_for("first_frame", {"scenes": [{"id": "seg_1", "selected": "/x.png"}]}) == "completed"


def test_status_matrix_six_combinations():
    """reference/非 reference × 空清单/仅占位/完整清单六种组合（tasks 4.1）。"""
    empty = {"scenes": []}
    placeholder = {"scenes": [{"id": "seg_1", "selected": "", "versions": []}]}
    full = {"scenes": [{"id": "seg_1", "selected": "/x.png", "versions": ["/x.png"]}]}
    skip = build_reference_skip()
    assert _status_for("reference", {**empty, "skip": skip}) == "completed"
    assert _status_for("reference", {**placeholder, "skip": skip}) == "completed"
    assert _status_for("reference", {**full, "skip": skip}) == "completed"
    assert _status_for("first_frame", empty) == "waiting"
    assert _status_for("first_frame", placeholder) == "waiting"
    assert _status_for("first_frame", full) == "completed"


def test_artifact_has_pending_reads_skip_marker_only():
    assert artifact_has_pending("reference_generation", {"scenes": [], "skip": build_reference_skip()}) is False
    assert artifact_has_pending("reference_generation", {"scenes": []}) is True
    assert artifact_has_pending(
        "reference_generation", {"scenes": [{"id": "a", "selected": ""}]}
    ) is True
    assert artifact_has_pending(
        "reference_generation", {"scenes": [{"id": "a", "selected": "p"}]}
    ) is False


def test_skip_marker_requires_object():
    """非对象/空对象不算有效跳过标记，不得据此放行晋升（防误写）。"""
    assert artifact_has_pending("reference_generation", {"scenes": [], "skip": "true"}) is True
    assert artifact_has_pending("reference_generation", {"scenes": [], "skip": True}) is True
    assert artifact_has_pending("reference_generation", {"scenes": [], "skip": {}}) is True
    assert artifact_has_pending(
        "reference_generation", {"scenes": [], "skip": build_reference_skip()}
    ) is False


def test_build_stage_changes_promotes_skip_artifact():
    changes = build_stage_changes(
        {}, {"reference_generation": {"scenes": [], "skip": build_reference_skip()}}
    )
    assert changes and changes[0]["promote"] is True
    # 无 skip 的空清单仍进工作层（不晋升）
    changes = build_stage_changes({}, {"reference_generation": {"scenes": []}})
    assert changes and "working" in changes[0] and "promote" not in changes[0]


# ─────────────── 4. 模式切换（tasks 4.5） ───────────────


def _patch_mode(sid: str, mode: str) -> dict:
    workflow_engine.update_session_meta(sid, {"video_generation_mode": mode}, ("video_generation_mode",))
    state = workflow_engine.get_state(sid)
    return {"status": state.status.get("reference_generation"), "artifact": state.artifacts.get("reference_generation")}


def test_switch_to_reference_builds_marker_and_completes():
    sid = _new_session(mode="first_frame")
    workflow_engine.update_artifact(sid, "reference_generation", {"scenes": []})
    assert workflow_engine.get_state(sid).status["reference_generation"] == "waiting"

    result = _patch_mode(sid, "reference")
    assert result["status"] == "completed"
    assert result["artifact"]["skip"]["mode"] == "reference"


def test_switch_away_clears_marker_and_falls_back_to_pending():
    sid = _new_session(mode="reference")
    _run_reference_stage(sid)
    assert _status(sid) == "completed"

    result = _patch_mode(sid, "first_frame")
    assert result["status"] == "waiting"
    assert "skip" not in (result["artifact"] or {})


def test_switch_back_without_rerun_rebuilds_marker_and_promotes():
    sid = _new_session(mode="reference")
    _run_reference_stage(sid)
    _patch_mode(sid, "first_frame")  # 切走：清标记、待生成
    result = _patch_mode(sid, "reference")  # 切回但未重跑
    assert result["status"] == "completed"
    assert result["artifact"]["skip"]["note"] == build_reference_skip()["note"]
    snapshot = load_committed(sid)
    assert (snapshot["checkpoints"].get("reference_generation") or {}).get("skip")


def test_switch_back_with_partial_selection_also_rebuilds_marker():
    sid = _new_session(mode="first_frame")
    workflow_engine.update_artifact(
        sid,
        "reference_generation",
        {"scenes": [
            {"id": "seg_1", "selected": "", "versions": []},
            {"id": "seg_2", "selected": "", "versions": []},
        ]},
    )
    # 只给一个分镜上传真实图片，形成“部分选中 + 占位”的产物形态
    workflow_engine.upload_artifact_image(
        sid, "reference_generation", "scenes", "seg_1",
        io.BytesIO(b"\x89PNG\r\n\x1a\n partial"), "a.png",
    )
    assert _status(sid) == "waiting"

    result = _patch_mode(sid, "reference")
    assert result["status"] == "completed"
    assert result["artifact"]["skip"]
    # 晋升与状态同源：跳过产物应落入正式快照，不停在工作层
    snapshot = load_committed(sid)
    assert (snapshot["checkpoints"].get("reference_generation") or {}).get("skip")
    assert not (snapshot["working"].get("reference_generation") or {}).get("working")


def test_switch_to_reference_without_artifact_does_not_create_one():
    sid = _new_session(mode="first_frame")
    result = _patch_mode(sid, "reference")
    assert result["artifact"] is None
    assert result["status"] == "pending"


def _status(sid: str) -> str:
    return workflow_engine.get_state(sid).status["reference_generation"]


def _stage_status(sid: str, stage: str) -> str:
    return workflow_engine.get_state(sid).status[stage]


def test_switch_mode_keeps_other_completed_stages():
    """模式切换触发的全量重算不改变已完成且产物完整的其他阶段（tasks 4.5）。"""
    sid = _new_session(mode="reference")
    workflow_engine.update_artifact(
        sid,
        "storyboard",
        {"episodes": [{"episode_number": 1, "segments": [
            {"segment_id": "seg_1", "shots": [{"content": "镜头"}]}
        ]}]},
    )
    assert _stage_status(sid, "storyboard") == "completed"
    _patch_mode(sid, "first_frame")
    assert _stage_status(sid, "storyboard") == "completed"


# ─────────────── 5. 继续阻塞（tasks 4.6） ───────────────


def test_continue_rejected_when_no_selection_and_not_reference_mode():
    sid = _new_session(mode="first_frame")
    workflow_engine.update_artifact(sid, "reference_generation", {"scenes": []})
    assert _status(sid) == "waiting"

    result = asyncio.run(workflow_engine.continue_workflow(sid, "reference_generation"))
    assert result["status"] == "error"
    assert result["message"] == "需要先生成分镜图"
    assert _status(sid) == "waiting", "拒绝后不得把阶段置为已完成"


def test_continue_passes_in_reference_mode():
    sid = _new_session(mode="reference")
    _run_reference_stage(sid)
    result = asyncio.run(workflow_engine.continue_workflow(sid, "reference_generation"))
    assert result["status"] == "ready"
    assert _status(sid) == "completed"


def test_continue_passes_in_frame_mode_with_selection():
    """非 reference 模式但有可用选中图时，继续正常通过（tasks 4.6）。"""
    sid = _new_session(mode="first_frame")
    workflow_engine.update_artifact(
        sid, "reference_generation", {"scenes": [{"id": "seg_1", "selected": "", "versions": []}]}
    )
    workflow_engine.upload_artifact_image(
        sid, "reference_generation", "scenes", "seg_1",
        io.BytesIO(b"\x89PNG\r\n\x1a\n selected"), "a.png",
    )
    assert _status(sid) == "completed"
    result = asyncio.run(workflow_engine.continue_workflow(sid, "reference_generation"))
    assert result["status"] == "ready"
    assert _status(sid) == "completed"


# ─────────────── 6. 图像依赖闸（tasks 3.1 / 3.2） ───────────────


def test_require_model_fields_allows_missing_it2i_in_reference_mode():
    from api.routers.workflow import _require_model_fields

    values = {
        "llm_model": "l",
        "vlm_model": "v",
        "image_t2i_model": "t",
        "video_reference_model": "vid",
        "video_first_frame_model": "vid",
        "video_generation_mode": "reference",
    }
    _require_model_fields(values)  # 不抛

    values["video_generation_mode"] = "first_frame"
    with pytest.raises(HTTPException) as excinfo:
        _require_model_fields(values)
    assert "image_it2i_model" in excinfo.value.detail

    # 文生图仍为必需
    with pytest.raises(HTTPException):
        _require_model_fields({
            "llm_model": "l", "vlm_model": "v", "video_first_frame_model": "vid",
            "video_generation_mode": "reference",
        })


def test_preflight_skips_image_models_only_for_reference_stage():
    from api.routers.workflow import _preflight_stage_models

    assert _preflight_stage_models(
        "reference_generation", {"video_generation_mode": "reference"}
    ) is None
    with pytest.raises(HTTPException) as excinfo:
        _preflight_stage_models("reference_generation", {"video_generation_mode": "first_frame"})
    assert excinfo.value.status_code == 409


# ─────────────── 7. 上传防线（tasks 6.1） ───────────────


def test_upload_rejects_unknown_scene_in_skip_form():
    sid = _new_session(mode="reference")
    _run_reference_stage(sid)
    artifact = workflow_engine.get_state(sid).artifacts["reference_generation"]
    assert artifact["skip"]

    class _File:
        def read(self, *_args):
            return b""

    with pytest.raises(ValueError) as excinfo:
        workflow_engine.upload_artifact_image(
            sid, "reference_generation", "scenes", "seg_missing", _File(), "x.png"
        )
    assert "不会新建条目" in str(excinfo.value)
    assert workflow_engine.get_state(sid).artifacts["reference_generation"]["scenes"] == []


def test_field_update_keeps_skip_marker():
    sid = _new_session(mode="reference")
    _run_reference_stage(sid)
    workflow_engine.update_artifact(
        sid, "reference_generation", {"segments": [{"segment_id": "seg_1", "visual_prompt": "描述"}]}
    )
    artifact = workflow_engine.get_state(sid).artifacts["reference_generation"]
    assert artifact.get("skip"), "非跳过相关的字段更新 MUST NOT 清除 skip 标记"
    assert _status(sid) == "completed"
