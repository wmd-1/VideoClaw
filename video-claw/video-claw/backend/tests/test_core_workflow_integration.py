"""Group 14: workflow fully on the DB/MinIO core (no JSON, immutable publication).

Exercises the real engine → consumption → publication → commit → read-model chain
with fake agents (no paid model calls). Run inside the existing backend container
against `videoclaw_test` and the `videoclaw-test*` buckets only.
"""
from __future__ import annotations

import asyncio
import json
import os
import types
import uuid

import pytest

import core.persistence_service as persistence_service
from api.dependencies import workflow_engine
from config import settings
from core import media_refs
from core.agents.base_agent import AgentInterface
from core.media_refs import ResolvedMedia
from core.object_operations import ObjectCleanupWorker
from core.orchestrator import WorkflowStage
from db import database
from db.repositories import FileRepo, StageArtifactRepo
from db.repositories.commits import CommitConflict, commit_session_change, load_committed, load_working
from storage import get_storage

OWNER = "alice"
TEST_BUCKET_PREFIX = "videoclaw-test"


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    code_root = tmp_path / "code"
    for path in (code_root / "result", code_root / "temp", code_root / "work" / "executions", tmp_path / "sessions"):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "CODE_DIR", str(code_root))
    monkeypatch.setattr(settings, "RESULT_DIR", str(code_root / "result"))
    monkeypatch.setattr(settings, "TEMP_DIR", str(code_root / "temp"))
    refs_root = code_root / "work" / "_refs"
    refs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(media_refs, "_REFS_ROOT", refs_root)
    monkeypatch.setattr(workflow_engine, "_session_dir", str(tmp_path / "sessions"))
    assert settings.STORAGE_BUCKET.startswith(TEST_BUCKET_PREFIX)
    yield tmp_path


@pytest.fixture(autouse=True)
def cleanup_objects():
    """对象一律经 object_operations 登记后才上传；收尾按登记删除测试桶对象并显式报告失败。"""
    yield
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT bucket, object_key FROM object_operations WHERE target_kind = 'minio'")
            targets = {(str(row["bucket"]), str(row["object_key"])) for row in cur.fetchall()}
    failures = []
    for bucket, key in sorted(targets):
        if not bucket.startswith(TEST_BUCKET_PREFIX):
            failures.append((bucket, key, "refusing to touch a non-test bucket"))
            continue
        get_storage().delete(key, bucket=bucket)
        if get_storage().stat(key, bucket=bucket) is not None:
            failures.append((bucket, key, "still present"))
    if failures:
        raise AssertionError(f"test cleanup incomplete: {failures}")


class ScriptedAgent(AgentInterface):
    """测试替身：只替换模型产出，执行根/版本历史/进度回调都走真实编排器。"""

    def __init__(self, behavior):
        super().__init__(name="scripted")
        self.behavior = behavior

    async def process(self, input_data, intervention=None):
        return self.behavior(self, input_data, intervention)


def _use_agent(monkeypatch, stage: WorkflowStage, behavior) -> None:
    monkeypatch.setitem(workflow_engine.agent_factories, stage, lambda: ScriptedAgent(behavior))


def _run(stage: WorkflowStage, sid: str, *, intervention=None, events=None, input_data=None):
    def collect(phase, step, percent, data=None):
        if events is not None and data:
            events.append(data)

    state = workflow_engine.get_state(sid)
    return asyncio.run(workflow_engine.execute_stage(
        state, stage, dict(input_data or {}), progress_callback=collect, intervention=intervention,
    ))


def _new_session() -> str:
    sid = f"s{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(sid, {"idea": "测试"}, owner=OWNER)
    return sid


def _write(agent: ScriptedAgent, rel: str, content: bytes) -> str:
    path = os.path.join(agent.execution_root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def _bytes(public: str) -> bytes:
    row = FileRepo().get_by_public_path(public)
    assert row and row["storage_kind"] == "minio", public
    target = os.path.join(settings.TEMP_DIR, f"dl-{uuid.uuid4().hex}")
    get_storage().download(str(row["object_key"]), target, bucket=str(row["bucket"]))
    try:
        with open(target, "rb") as handle:
            return handle.read()
    finally:
        os.remove(target)


def _character_agent(content: bytes):
    def behavior(agent, _input, _intervention):
        path = _write(agent, "Assets/characters/c1.png", content)
        versions = agent._history_versions("characters", "c1") + [path]
        return {
            "payload": {
                "session_id": "ignored",
                "characters": [{"id": "c1", "name": "小明", "description": "主角",
                                "selected": path, "versions": versions, "status": "done"}],
                "settings": [],
            },
            "stage_completed": True,
        }

    return behavior


def _storyboard(sid: str) -> None:
    workflow_engine.update_artifact(sid, "storyboard", {"episodes": [{
        "episode_number": 1,
        "segments": [
            {"segment_id": "seg_01_01", "characters": ["小明"], "location": "",
             "shots": [{"plot": "开场", "duration": 5}]},
            {"segment_id": "seg_01_02", "characters": ["小明"], "location": "",
             "shots": [{"plot": "收尾", "duration": 5}]},
        ],
    }]})


# ── 14.1 / 14.6 DB-only read model ───────────────────────────
def test_db_is_the_only_authority_and_json_is_neither_read_nor_written(isolated_roots, client):
    sid = _new_session()
    session_dir = isolated_roots / "sessions"
    # 同名 JSON 声称另一个 owner 与另一份正文：既不覆盖 DB，也不被隐式导入
    (session_dir / f"{sid}.json").write_text(json.dumps({
        "session_id": sid, "owner": "bob", "artifacts": {"script_generation": {"title": "JSON 版本"}},
    }), encoding="utf-8")
    ghost = f"g{uuid.uuid4().hex[:10]}"
    (session_dir / f"{ghost}.json").write_text(json.dumps({"session_id": ghost, "owner": OWNER}), encoding="utf-8")
    workflow_engine.update_artifact(sid, "script_generation", {"title": "DB 版本", "episodes": []})
    workflow_engine.sessions.clear()

    headers = {"X-User-Login-Name": OWNER}
    status = client.get(f"/api/project/{sid}/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["artifacts"]["script_generation"]["title"] == "DB 版本"
    assert client.get(f"/api/project/{sid}/status", headers={"X-User-Login-Name": "bob"}).status_code == 404
    assert client.get(f"/api/project/{ghost}/status", headers=headers).status_code == 404
    listing = client.get("/api/sessions", headers=headers).json()["sessions"]
    assert [item["id"] for item in listing] == [sid] and listing[0]["title"] == "DB 版本"
    assert sorted(os.listdir(session_dir)) == sorted([f"{sid}.json", f"{ghost}.json"])
    assert json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))["owner"] == "bob"  # 未被改写


def test_legacy_imported_meta_shell_reads_as_flat_params(client):
    """legacy 导入行 meta_json 是 {project_params, model_fallback, boot_id} 外壳；读模型展开为扁平参数。"""
    from db.repositories import SessionRepo

    sid = f"l{uuid.uuid4().hex[:10]}"
    params = {"idea": "介绍交通银行", "style": "realistic", "video_generation_mode": "first_frame"}
    assert SessionRepo().insert_if_absent({
        "session_id": sid, "owner": OWNER, "status": "idle", "current_stage": "script_generation",
        "status_json": {"script_generation": "waiting"}, "stage_progress": {}, "project_params": params,
    })
    assert SessionRepo().get(sid)["meta_json"]["project_params"] == params  # 存储形态保持不变
    headers = {"X-User-Login-Name": OWNER}
    snapshot = client.get(f"/api/project/{sid}/status", headers=headers).json()
    assert snapshot["meta"] == params
    listing = {item["id"]: item for item in client.get("/api/sessions", headers=headers).json()["sessions"]}
    assert listing[sid]["idea"] == "介绍交通银行" and listing[sid]["style"] == "realistic"
    # 下一次 meta 变更按扁平口径写回，不丢旧参数
    workflow_engine.update_session_meta(sid, {"style": "anime"}, ("style",))
    stored = SessionRepo().get(sid)["meta_json"]
    assert stored["idea"] == "介绍交通银行" and stored["style"] == "anime" and "project_params" not in stored


def test_create_session_never_rewrites_another_owners_session():
    sid = _new_session()
    with pytest.raises(CommitConflict):
        workflow_engine.create_session(sid, {"idea": "别人的想法"}, owner="bob")
    workflow_engine.sessions.clear()
    state = workflow_engine.get_state(sid)
    assert state.owner == OWNER and state.meta["idea"] == "测试"


def test_status_read_does_not_download_objects(monkeypatch, client):
    sid = _new_session()
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"one"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    workflow_engine.sessions.clear()

    def no_download(*_args, **_kwargs):
        raise AssertionError("status/list must not download objects")

    monkeypatch.setattr(media_refs, "ensure_local", no_download)
    headers = {"X-User-Login-Name": OWNER}
    assert client.get(f"/api/project/{sid}/status", headers=headers).status_code == 200
    assert client.get("/api/sessions", headers=headers).status_code == 200


# ── 14.2 publication + immutable reruns + MinIO-only consumption ─────
def test_stage_outputs_are_published_and_reruns_never_overwrite(monkeypatch):
    sid = _new_session()
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"first-render"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    first = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]
    assert first["selected"].startswith("/code/result/_media/") and first["versions"] == [first["selected"]]
    assert StageArtifactRepo().get(sid, "character_design")["version"] == 1

    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"second-render"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    second = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]
    assert second["versions"][0] == first["selected"] and len(second["versions"]) == 2
    assert second["selected"] != first["selected"]  # 同名 c1.png 重跑得到新对象
    assert _bytes(first["selected"]) == b"first-render"  # 旧检查点媒体不被覆盖
    assert _bytes(second["selected"]) == b"second-render"
    assert StageArtifactRepo().get(sid, "character_design")["version"] == 2
    payload = json.dumps(load_committed(sid))
    assert "/work/executions/" not in payload and "_refs" not in payload  # DB 不出现工作/缓存路径


def test_minio_only_inputs_are_materialized_for_the_consumer(monkeypatch):
    sid = _new_session()
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"char-bytes"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    public = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]["selected"]
    # 执行终态后工作根已清理：本地工作副本不存在，只剩 MinIO 对象
    assert not os.path.exists(FileRepo().get_by_public_path(public)["local_path"])
    _storyboard(sid)
    seen = {}

    def reference(agent, input_data, _intervention):
        selected = input_data["_session_artifacts"]["character_design"]["characters"][0]["selected"]
        seen["type"] = type(selected)
        with open(selected, "rb") as handle:
            seen["bytes"] = handle.read()
        out = _write(agent, "Scenes/seg_01_01.jpg", b"scene-1")
        return {"payload": {"scenes": [
            {"id": "seg_01_01", "selected": out, "versions": [out], "status": "done"},
            {"id": "seg_01_02", "selected": "", "versions": [], "status": "failed"},
        ]}, "stage_completed": True}

    _use_agent(monkeypatch, WorkflowStage.REFERENCE_GENERATION, reference)
    _run(WorkflowStage.REFERENCE_GENERATION, sid)
    assert seen == {"type": ResolvedMedia, "bytes": b"char-bytes"}
    state = workflow_engine.get_state(sid)
    scenes = {s["id"]: s for s in state.artifacts["reference_generation"]["scenes"]}
    assert scenes["seg_01_01"]["selected"].startswith("/code/result/_media/")
    # 仍有未选片段：只写工作层，不伪造 completed 检查点
    assert "reference_generation" in state.working_stages
    assert StageArtifactRepo().get(sid, "reference_generation") is None


def test_unreadable_explicit_selection_fails_before_the_model_runs(monkeypatch):
    sid = _new_session()
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"x"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    public = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]["selected"]
    row = FileRepo().get_by_public_path(public)
    assert not os.path.exists(row["local_path"])
    get_storage().delete(str(row["object_key"]), bucket=str(row["bucket"]))
    _storyboard(sid)
    called = []
    _use_agent(monkeypatch, WorkflowStage.REFERENCE_GENERATION, lambda *a: called.append(1) or {"payload": {}})
    with pytest.raises(Exception, match="必需媒体不可用"):
        _run(WorkflowStage.REFERENCE_GENERATION, sid)
    assert called == []
    state = workflow_engine.get_state(sid)
    assert state.status["reference_generation"] == "error"
    assert FileRepo().get_by_public_path(public)["missing_at"] is not None
    # 前端可定位清单：哪个条目、哪个原因、被哪些片段使用；错误文案写明去哪个阶段处理
    [issue] = state.stage_progress["reference_generation"]["missing_inputs"]
    assert (issue["source_stage"], issue["container"], issue["item_id"], issue["reason"]) == (
        "character_design", "characters", "c1", "missing")
    assert issue["consumers"] == ["seg_01_01", "seg_01_02"] and "/" not in issue["message"]
    error = state.executions["reference_generation"]["error"]
    assert "角色 c1" in error and "「角色/场景设计」" in error


# ── 14.3 item events → execution layer, restart preview ─────
def test_item_events_commit_to_execution_layer_before_being_forwarded(monkeypatch):
    sid = _new_session()
    _storyboard(sid)
    workflow_engine.update_artifact(sid, "reference_generation", {"scenes": [
        {"id": "seg_01_01", "selected": "", "versions": []}, {"id": "seg_01_02", "selected": "", "versions": []},
    ]})
    events = []

    def video(agent, _input, _intervention):
        clip = _write(agent, "clips/seg_01_01.mp4", b"clip-1")
        agent._report_progress("视频生成", "完成", 50, data={"asset_complete": {
            "type": "clips", "id": "seg_01_01", "status": "done", "selected": clip, "versions": [clip],
        }})
        execution = load_working(sid, "video_generation")["execution_json"]
        active = execution["active_items"]["clips:seg_01_01"]
        assert active["seq"] == 1 and active["selected"].startswith("/code/result/_media/")
        raise RuntimeError("模型中途失败")

    _use_agent(monkeypatch, WorkflowStage.VIDEO_GENERATION, video)
    with pytest.raises(RuntimeError, match="模型中途失败"):
        _run(WorkflowStage.VIDEO_GENERATION, sid, events=events)
    forwarded = events[0]["asset_complete"]
    assert forwarded["selected"].startswith("/code/result/_media/")  # 事件只带已提交的 public_path
    assert _bytes(forwarded["selected"]) == b"clip-1"

    snapshot = workflow_engine.get_status_snapshot(sid)
    assert snapshot["status"]["video_generation"] == "error" and "模型中途失败" in snapshot["error"]
    preview = snapshot["execution_preview"]["video_generation"]
    assert preview["status"] == "failed" and preview["items"][0]["selected"] == forwarded["selected"]
    assert "video_generation" not in StageArtifactRepo().list_checkpoints(sid)  # 未完成执行不覆盖成功层

    # 重试：上次已发布的版本作为候选回到 Agent 的历史，不重复生成也不丢失
    seen = {}

    def retry(agent, _input, _intervention):
        seen["history"] = agent._history_versions("clips", "seg_01_01")
        return {"payload": {"clips": [
            {"id": "seg_01_01", "selected": seen["history"][-1], "versions": seen["history"], "status": "done"},
        ]}, "stage_completed": True}

    _use_agent(monkeypatch, WorkflowStage.VIDEO_GENERATION, retry)
    _run(WorkflowStage.VIDEO_GENERATION, sid)
    assert seen["history"] == [forwarded["selected"]]
    execution = load_working(sid, "video_generation")["execution_json"]
    assert execution["status"] == "completed" and execution["version_candidates"] == {}
    assert workflow_engine.get_status_snapshot(sid)["execution_preview"] == {}


def test_restart_interrupts_foreign_running_execution(monkeypatch):
    sid = _new_session()
    commit_session_change(
        session_id=sid, owner=OWNER, expected_revision=load_committed(sid)["revision"],
        stage_changes=[{"stage": "video_generation",
                        "execution_state": {"action": "start", "execution_id": "ex-old", "boot_id": "old-boot"}}],
        session_patch={"status_json": {**load_committed(sid)["status_json"], "video_generation": "running"},
                       "current_stage": "video_generation", "boot_id": "old-boot"},
    )
    commit_session_change(
        session_id=sid, owner=OWNER, expected_revision=load_committed(sid)["revision"],
        stage_changes=[{"stage": "video_generation", "execution": {
            "execution_id": "ex-old", "active_items": {"clips:seg_01_01": {"seq": 1, "status": "done", "selected": ""}},
        }}],
    )
    workflow_engine.sessions.clear()
    assert workflow_engine.load_sessions() >= 1  # 启动恢复扫描
    snapshot = workflow_engine.get_status_snapshot(sid)
    assert snapshot["status"]["video_generation"] == "stopped"
    assert snapshot["stage_progress"]["video_generation"]["interrupted_by_restart"] is True
    assert snapshot["execution_preview"]["video_generation"]["status"] == "interrupted"
    assert load_working(sid, "video_generation")["execution_json"]["active_items"] == {}


# ── 14.4 edits, uploads, previews, cross-stage sync ─────
def test_cross_stage_patch_and_image_upload_commit_together(monkeypatch):
    sid = _new_session()
    _storyboard(sid)
    workflow_engine.update_artifact(sid, "video_generation", {"clips": [
        {"id": "seg_01_01", "description": "旧", "duration": 5, "selected": "", "versions": []},
    ]})
    workflow_engine.update_artifact(sid, "reference_generation", {"scenes": [
        {"id": "seg_01_01", "description": "旧", "selected": "", "versions": []},
        {"id": "seg_01_02", "description": "旧", "selected": "", "versions": []},
    ]})
    before = load_committed(sid)["revision"]
    workflow_engine.update_artifact(sid, "storyboard", {"episodes": [{
        "episode_number": 1,
        "segments": [{"segment_id": "seg_01_01", "visual_prompt": "新画面",
                      "shots": [{"plot": "新的剧情", "duration": 7}]}],
    }]})
    committed = load_committed(sid)
    assert committed["revision"] == before + 1  # 跨阶段同步与源阶段同一事务
    state = workflow_engine.get_state(sid)
    clip = next(c for c in state.artifacts["video_generation"]["clips"] if c["id"] == "seg_01_01")
    assert clip["description"] == "新的剧情" and clip["duration"] == 7
    scene = next(s for s in state.artifacts["reference_generation"]["scenes"] if s["id"] == "seg_01_01")
    assert scene["description"] == "新画面"

    import io

    result = workflow_engine.upload_artifact_image(
        session_id=sid, stage="reference_generation", item_type="scenes", item_id="seg_01_01",
        file_obj=io.BytesIO(b"user-upload"), filename="pick.png",
    )
    assert result["path"].startswith("/code/result/_media/")
    assert _bytes(result["path"]) == b"user-upload"
    row = FileRepo().get_by_public_path(result["path"])
    assert row["session_id"] == sid and row["owner"] == OWNER
    with pytest.raises(Exception):  # 未登记的结果文件不能凭路径进入产物
        workflow_engine.update_artifact(sid, "reference_generation", {"seg_01_02": f"/code/result/image/{sid}/x.png"})


def test_revision_preview_lives_in_preview_layer_and_can_be_cancelled(monkeypatch):
    from core import script_revision

    sid = _new_session()
    base = {"episodes": [{"episode_number": 1, "content": "正文"}], "characters": [], "settings": []}
    workflow_engine.update_artifact(sid, "script_generation", base)
    version = StageArtifactRepo().get(sid, "script_generation")["version"]
    preview = {"instruction": "改", "scope": "all", "base_version": script_revision.content_fingerprint(base),
               "episodes": [{"episode_number": 1, "content": "修订"}], "characters": [], "settings": []}

    def revise(_agent, input_data, _intervention):
        payload = {k: v for k, v in input_data.items() if k in ("episodes", "characters", "settings")}
        return {"payload": {**payload, "pending_revision": preview}, "requires_intervention": True}

    _use_agent(monkeypatch, WorkflowStage.SCRIPT_GENERATION, revise)
    state = workflow_engine.get_state(sid)
    asyncio.run(workflow_engine.execute_stage(state, WorkflowStage.SCRIPT_GENERATION, dict(base),
                                              intervention={"action": "revise_script"}))
    working = load_working(sid, "script_generation")
    assert working["preview_json"]["pending_revision"]["instruction"] == "改"
    assert StageArtifactRepo().get(sid, "script_generation")["version"] == version  # 预览不晋升
    artifact = workflow_engine.get_artifact_snapshot(sid, "script_generation")
    assert artifact["pending_revision"]["instruction"] == "改" and artifact["episodes"] == base["episodes"]
    workflow_engine.update_artifact(sid, "script_generation", {"pending_revision": None})
    assert load_working(sid, "script_generation")["preview_json"] is None
    assert "pending_revision" not in workflow_engine.get_artifact_snapshot(sid, "script_generation")


# ── 14.5 user edits during a run win ─────
def test_user_selection_during_run_is_not_overwritten(monkeypatch):
    sid = _new_session()
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"v1"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"v2"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    versions = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]["versions"]

    def rerun(agent, input_data, intervention):
        # 模型运行期间用户把选择改回 v1
        workflow_engine.update_artifact(sid, "character_design", {"characters": [{"id": "c1", "selected": versions[0]}]})
        return _character_agent(b"v3")(agent, input_data, intervention)

    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, rerun)
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    item = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]
    assert item["selected"] == versions[0]  # 用户字段优先
    assert len(item["versions"]) == 3  # 晚到模型结果只作为新版本


def test_background_regeneration_takes_new_selection(monkeypatch):
    """Q8 正向：未手改时，后台显式重生成的新图应成为 selected（旧行为会保留 v1）。"""
    sid = _new_session()
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"v1"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    first = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]

    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"v2"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid, intervention={"regenerate_characters": ["c1"]})

    item = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]
    assert item["selected"] != first["selected"]
    assert item["selected"] == item["versions"][-1]
    assert len(item["versions"]) == 2
    assert _bytes(item["selected"]) == b"v2"


def test_background_regeneration_keeps_in_run_user_selection(monkeypatch):
    """Q8 安全阀：后台重生成期间用户手选旧版本，晚到结果不得覆盖（事件层 + 收尾层）。"""
    sid = _new_session()
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"v1"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"v2"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    versions = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]["versions"]

    # 观察事件层单独落库后的条目（不经覆盖层）：用 committed snapshot 而非 get_state
    seen: dict = {}
    original_commit = workflow_engine._commit_item_event

    def spy_commit(run, asset):
        result = original_commit(run, asset)
        seen["calls"] = seen.get("calls", 0) + 1
        # 每次事件后都刷新，断言挂在最后一次上（不假设恰好只发一条）
        seen["after_event"] = load_committed(sid)["checkpoints"]["character_design"]["characters"][0]
        return result

    monkeypatch.setattr(workflow_engine, "_commit_item_event", spy_commit)

    def rerun(agent, input_data, intervention):
        # 后台重生成进行中，用户把选择改回第一版
        workflow_engine.update_artifact(sid, "character_design", {"characters": [{"id": "c1", "selected": versions[0]}]})
        result = _character_agent(b"v3")(agent, input_data, intervention)
        # 真实 agent 会随进度上报条目完成事件；这里复现同一路径以覆盖事件合入层
        payload_item = result["payload"]["characters"][0]
        agent._report_progress("角色设计", "完成", 100, data={"asset_complete": {
            "type": "characters", "id": "c1", "status": "done",
            "selected": payload_item["selected"], "versions": payload_item["versions"],
        }})
        return result

    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, rerun)
    _run(WorkflowStage.CHARACTER_DESIGN, sid, intervention={"regenerate_characters": ["c1"]})

    assert seen.get("calls", 0) >= 1, "条目完成事件未进入事件合入层，用例未覆盖真实链路"
    after_event = seen["after_event"]
    # 必须同时断言“合入确实生效”（挡把 _merge_asset_update 换成 no-op）与“保留了用户值”（挡丢基线）
    assert len(after_event["versions"]) == 3, "事件合入未生效（no-op 实现也会让旧断言通过）"
    assert after_event["selected"] == versions[0], "事件合入层未按基线保留用户手选"

    item = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]
    assert item["selected"] == versions[0]  # 手改优先于晚到的本轮结果
    assert len(item["versions"]) == 3  # 新版本仍然保留


# ── 14.6 failure keeps the old success layer; delete queues cleanup ─────
def test_finish_commit_failure_keeps_checkpoint_and_compensates(monkeypatch):
    sid = _new_session()
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"good"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    good = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]["selected"]
    real_commit = persistence_service.commit_session_change

    def fail_on_finish(**kwargs):
        if any((c.get("execution_state") or {}).get("action") == "finish"
               and (c.get("execution_state") or {}).get("status") == "completed"
               for c in kwargs.get("stage_changes") or []):
            raise CommitConflict("simulated conflict at finish")
        return real_commit(**kwargs)

    monkeypatch.setattr(persistence_service, "commit_session_change", fail_on_finish)
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"lost"))
    with pytest.raises(CommitConflict):
        _run(WorkflowStage.CHARACTER_DESIGN, sid)
    monkeypatch.setattr(persistence_service, "commit_session_change", real_commit)
    state = workflow_engine.get_state(sid)
    assert state.artifacts["character_design"]["characters"][0]["selected"] == good
    assert state.status["character_design"] == "error"
    assert StageArtifactRepo().get(sid, "character_design")["version"] == 1
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM object_operations WHERE resource_id = %s ORDER BY created_at", (sid,))
            states = [row["state"] for row in cur.fetchall()]
    assert states == ["retained", "done"]  # 新对象已补偿删除，旧对象保留


def test_delete_session_queues_object_cleanup(monkeypatch, client):
    sid = _new_session()
    _use_agent(monkeypatch, WorkflowStage.CHARACTER_DESIGN, _character_agent(b"to-delete"))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    public = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]["selected"]
    row = FileRepo().get_by_public_path(public)
    response = client.delete(f"/api/sessions/{sid}", headers={"X-User-Login-Name": OWNER})
    assert response.status_code == 200
    assert FileRepo().get_by_public_path(public) is None
    assert workflow_engine.get_state(sid) is None
    counts = ObjectCleanupWorker(boot_id="other").run_once()
    assert counts["deleted"] >= 1
    assert get_storage().stat(str(row["object_key"]), bucket=str(row["bucket"])) is None
    assert not os.path.exists(row["local_path"])
