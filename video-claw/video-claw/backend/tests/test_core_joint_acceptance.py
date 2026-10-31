"""Group 16: joint acceptance across workflow / pipeline / sandbox on the DB+MinIO core.

Only paid model outputs are faked; publication, commits, read models, the media
gateway and the cleaner are the production code. Runs inside the existing backend
container against `videoclaw_test` and the `videoclaw-test*` buckets only.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import threading
import time
import uuid

import pytest

import pipelines.storage as storage
from api.dependencies import workflow_engine
from config import settings
from core import artifact_publisher, media_refs
from core.agents.base_agent import AgentInterface
from core.orchestrator import WorkflowStage
from db import database
from db.repositories import FileRepo, SandboxRepo, TaskRepo
from db.repositories.commits import load_committed
from pipelines.runner import PIPELINE_REGISTRY, run_pipeline_task
from pipelines.utils import artifact
from storage import get_storage

OWNER = "alice"


def headers(login: str = OWNER) -> dict:
    return {"X-User-Login-Name": login}


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    code_root = tmp_path / "code"
    for path in (code_root / "result", code_root / "temp", code_root / "work" / "executions",
                 code_root / "work" / "_refs", tmp_path / "sessions"):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "CODE_DIR", str(code_root))
    monkeypatch.setattr(settings, "RESULT_DIR", str(code_root / "result"))
    monkeypatch.setattr(settings, "TEMP_DIR", str(code_root / "temp"))
    monkeypatch.setattr(media_refs, "_REFS_ROOT", code_root / "work" / "_refs")
    monkeypatch.setattr(workflow_engine, "_session_dir", str(tmp_path / "sessions"))
    assert settings.STORAGE_BUCKET.startswith("videoclaw-test")
    yield tmp_path


class ScriptedAgent(AgentInterface):
    def __init__(self, behavior):
        super().__init__(name="scripted")
        self.behavior = behavior

    async def process(self, input_data, intervention=None):
        result = self.behavior(self, input_data, intervention)
        if asyncio.iscoroutine(result):
            result = await result
        return result


def _write(path: str, content: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def _character_payload(agent, content: bytes, **fields):
    path = _write(os.path.join(agent.execution_root, "Assets", "characters", "c1.png"), content)
    item = {"id": "c1", "name": "小明", "description": "主角", "status": "done",
            "selected": path, "versions": agent._history_versions("characters", "c1") + [path]}
    item.update(fields)
    return {"payload": {"characters": [item], "settings": []}, "stage_completed": True}


def _run(stage: WorkflowStage, sid: str):
    state = workflow_engine.get_state(sid)
    return asyncio.run(workflow_engine.execute_stage(state, stage, {}, progress_callback=lambda *a, **k: None))


def _new_session() -> str:
    sid = f"s{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(sid, {"idea": "联合验收"}, owner=OWNER)
    return sid


def _upload(name: str, content: bytes) -> str:
    key = media_refs.owner_key(OWNER)
    _write(os.path.join(settings.TEMP_DIR, "users", key, name), content)
    return f"users/{key}/{name}"


def _db_dump() -> str:
    tables = {
        "sessions": "*",
        "session_stage_artifacts": "*",
        "session_stage_working": "*",
        "pipeline_tasks": "task_id, owner, pipeline, status, input_json, output_json, artifacts_json, error",
        "sandbox_records": "*",
        "sandbox_paths": "*",
        "saved_items": "*",
    }
    rows = []
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            for table, columns in tables.items():
                cur.execute(f"SELECT {columns} FROM {table}")
                rows.extend(cur.fetchall())
    return json.dumps(rows, ensure_ascii=False, default=str)


# ── 16.1 成功 → 失败重跑 → 中断 → DB-only 恢复（同名冲突 JSON 保留）──────────
def test_success_failed_rerun_interrupt_and_db_only_recovery(isolated_roots, monkeypatch, client):
    import core.persistence_service as persistence_service
    from db.repositories.commits import CommitConflict, commit_session_change

    sid = _new_session()
    (isolated_roots / "sessions" / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "owner": "bob", "artifacts": {"character_design": {"characters": []}}}),
        encoding="utf-8",
    )
    monkeypatch.setitem(workflow_engine.agent_factories, WorkflowStage.CHARACTER_DESIGN,
                        lambda: ScriptedAgent(lambda agent, *_: _character_payload(agent, b"good")))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    good = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]["selected"]

    real = persistence_service.commit_session_change

    def fail_finish(**kwargs):
        if any((c.get("execution_state") or {}).get("status") == "completed" for c in kwargs.get("stage_changes") or []):
            raise CommitConflict("injected finish conflict")
        return real(**kwargs)

    monkeypatch.setattr(persistence_service, "commit_session_change", fail_finish)
    monkeypatch.setitem(workflow_engine.agent_factories, WorkflowStage.CHARACTER_DESIGN,
                        lambda: ScriptedAgent(lambda agent, *_: _character_payload(agent, b"lost")))
    with pytest.raises(CommitConflict):
        _run(WorkflowStage.CHARACTER_DESIGN, sid)
    monkeypatch.setattr(persistence_service, "commit_session_change", real)

    # 外部进程遗留的 running 执行：重启恢复扫描中断它
    committed = load_committed(sid)
    commit_session_change(
        session_id=sid, owner=OWNER, expected_revision=committed["revision"],
        stage_changes=[{"stage": "video_generation",
                        "execution_state": {"action": "start", "execution_id": "ex-ghost", "boot_id": "old"}}],
        session_patch={"status_json": {**committed["status_json"], "video_generation": "running"}, "boot_id": "old"},
    )
    workflow_engine.sessions.clear()
    workflow_engine._persist.cache.clear()
    assert workflow_engine.load_sessions() >= 1
    status = client.get(f"/api/project/{sid}/status", headers=headers()).json()
    assert status["owner"] == OWNER  # 冲突 JSON（owner=bob）不覆盖 DB
    assert status["artifacts"]["character_design"]["characters"][0]["selected"] == good  # 最后成功层
    assert status["status"]["character_design"] == "error" and status["status"]["video_generation"] == "stopped"
    versions = status["artifacts"]["character_design"]["characters"][0]["versions"]
    assert versions == [good]  # 失败执行的结果不进正式层；未选历史不丢
    assert json.loads((isolated_roots / "sessions" / f"{sid}.json").read_text(encoding="utf-8"))["owner"] == "bob"


# ── 16.2 DB/API 不出现执行/缓存绝对路径与预签名 ─────────────────────────
def test_db_and_api_never_expose_work_cache_paths_or_presigns(isolated_roots, monkeypatch, client):
    from models import video_client

    sid = _new_session()
    monkeypatch.setitem(workflow_engine.agent_factories, WorkflowStage.CHARACTER_DESIGN,
                        lambda: ScriptedAgent(lambda agent, *_: _character_payload(agent, b"char")))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    workflow_engine.upload_artifact_image(
        session_id=sid, stage="character_design", item_type="characters", item_id="c1",
        file_obj=io.BytesIO(b"user-upload"), filename="pick.png",
    )

    async def pipeline(task_id, params):
        root = storage.task_output_dir(task_id)
        final = _write(os.path.join(root, "final.mp4"), b"final")
        items = [artifact(final, "video", "final")]
        for item in items:
            await storage.publish_artifact(task_id, item)
        return {"video_path": final, "input": params.get("image_path"), "dir": root}, items

    monkeypatch.setitem(PIPELINE_REGISTRY, "action_transfer", pipeline)
    task = storage.create_task("action_transfer", {"image_path": _upload("in.png", b"in")}, owner=OWNER)
    asyncio.run(run_pipeline_task(task["task_id"], "action_transfer", {}))

    captured = {}

    def fake_video(self, **kwargs):
        captured.update(kwargs)
        _write(kwargs["save_path"], b"video")
        return {"local_path": kwargs["save_path"]}

    monkeypatch.setattr(video_client.VideoClient, "generate_video", fake_video)
    body = client.post("/api/sandbox/video", headers=headers(),
                       json={"model": "m", "prompt": "p", "audio_url": _upload("a.mp3", b"aud")}).json()
    assert body["success"] is True, body
    presign = captured["audio_reference_url"]

    api = [
        client.get(f"/api/project/{sid}/status", headers=headers()).text,
        client.get(f"/api/project/{sid}/artifact/character_design", headers=headers()).text,
        client.get("/api/sessions", headers=headers()).text,
        client.get("/api/sandbox/history", headers=headers()).text,
        client.get(f"/api/sandbox/history/{body['record_id']}", headers=headers()).text,
        json.dumps({k: v for k, v in client.get(f"/api/tasks/{task['task_id']}", headers=headers()).json().items()
                    if k != "output_dir"}),  # output_dir 是既有内部字段（终态后已清理）
        json.dumps(body),
    ]
    forbidden = [
        artifact_publisher.work_roots_base(), "/work/executions", "_refs", settings.TEMP_DIR,
        "X-Amz-", presign.split("?")[0],
    ]
    for text in [_db_dump(), *api]:
        for marker in forbidden:
            assert marker not in text, (marker, text[:300])
    assert presign.startswith("http") and "X-Amz-" in presign  # 推理端拿到的是预签名


# ── 16.3 故障注入：上传/核验/DB/缓存安装 ──────────────────────────────────
def test_upload_and_verify_faults_leave_no_half_written_record(isolated_roots, monkeypatch, client):
    from models import video_client
    from storage import StorageError

    monkeypatch.setattr(video_client.VideoClient, "generate_video",
                        lambda self, **kw: _write(kw["save_path"], b"v") and {})
    real_upload = type(get_storage()).upload

    def broken_upload(self, *args, **kwargs):
        raise StorageError("injected upload failure")

    monkeypatch.setattr(type(get_storage()), "upload", broken_upload)
    body = client.post("/api/sandbox/video", headers=headers(), json={"model": "m", "prompt": "p"}).json()
    assert body["success"] is False and "upload" in body["error"]
    monkeypatch.setattr(type(get_storage()), "upload", real_upload)
    monkeypatch.setattr(artifact_publisher, "_verified", lambda *a, **k: False)
    body = client.post("/api/sandbox/video", headers=headers(), json={"model": "m", "prompt": "p"}).json()
    assert body["success"] is False and "unverified" in body["error"]
    assert SandboxRepo().list(OWNER) == []
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM artifact_files")
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT state, bucket, object_key FROM object_operations")
            ops = list(cur.fetchall())
    assert ops and {op["state"] for op in ops} == {"done"}  # 只补偿本批，且确认已删除
    assert all(get_storage().stat(op["object_key"], bucket=op["bucket"]) is None for op in ops)


def test_db_failure_after_publish_compensates_pipeline_artifact(isolated_roots, monkeypatch):
    task = storage.create_task("standard", {}, owner=OWNER)
    storage.mark_running(task["task_id"])
    path = _write(os.path.join(storage.task_output_dir(task["task_id"]), "img.png"), b"img")

    def broken_commit(**kwargs):
        raise RuntimeError("injected db failure")

    monkeypatch.setattr(storage, "commit_task_artifact", broken_commit)
    with pytest.raises(RuntimeError, match="injected db failure"):
        storage.append_artifact(task["task_id"], artifact(path, "image", "img"))
    assert storage.load_task(task["task_id"])["artifacts"] == []
    assert FileRepo().list_by_task(task["task_id"]) == []
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state, bucket, object_key FROM object_operations WHERE resource_id = %s",
                        (task["task_id"],))
            ops = list(cur.fetchall())
    assert [op["state"] for op in ops] == ["done"]
    assert get_storage().stat(ops[0]["object_key"], bucket=ops[0]["bucket"]) is None


def test_cache_install_failure_after_db_commit_does_not_lose_the_commit(monkeypatch):
    sid = _new_session()
    real_install = workflow_engine._install
    calls = {"n": 0}

    def flaky_install(snapshot):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected install failure")
        return real_install(snapshot)

    monkeypatch.setattr(workflow_engine, "_install", flaky_install)
    workflow_engine.update_artifact(sid, "script_generation", {"title": "已提交", "episodes": []})
    monkeypatch.setattr(workflow_engine, "_install", real_install)
    assert workflow_engine.get_state(sid).artifacts["script_generation"]["title"] == "已提交"
    assert load_committed(sid)["checkpoints"]["script_generation"]["title"] == "已提交"


def test_blocking_model_interleaved_with_real_patch_and_parallel_session(monkeypatch, client):
    first_sid, other_sid = _new_session(), _new_session()
    started, gate = threading.Event(), threading.Event()
    block = {"on": False}

    def behavior(agent, _input, _intervention):
        if block["on"] and first_sid in agent.execution_root:
            async def blocked():
                started.set()
                await asyncio.to_thread(gate.wait, 15)
                return _character_payload(agent, b"late-model", name="模型名", description="模型描述")
            return blocked()
        return _character_payload(agent, f"v-{uuid.uuid4().hex}".encode())

    monkeypatch.setitem(workflow_engine.agent_factories, WorkflowStage.CHARACTER_DESIGN, lambda: ScriptedAgent(behavior))
    _run(WorkflowStage.CHARACTER_DESIGN, first_sid)
    _run(WorkflowStage.CHARACTER_DESIGN, first_sid)
    v1, v2 = workflow_engine.get_state(first_sid).artifacts["character_design"]["characters"][0]["versions"]
    block["on"] = True

    errors = []
    worker = threading.Thread(target=lambda: _safe(errors, _run, WorkflowStage.CHARACTER_DESIGN, first_sid))
    worker.start()
    assert started.wait(10)
    # 模型阻塞期间用户经真实 PATCH 改名并把选择从 v2 改回 v1
    patched = client.patch(
        f"/api/project/{first_sid}/artifact/character_design", headers=headers(),
        json={"characters": [{"id": "c1", "name": "用户改名", "selected": v1}]},
    )
    assert patched.status_code == 200, patched.text
    _run(WorkflowStage.CHARACTER_DESIGN, other_sid)  # 另一会话在第一个被阻塞期间独立完成
    assert workflow_engine.get_state(other_sid).status["character_design"] in ("completed", "waiting")
    gate.set()
    worker.join(20)
    assert not worker.is_alive() and errors == []
    item = workflow_engine.get_state(first_sid).artifacts["character_design"]["characters"][0]
    assert item["name"] == "用户改名" and item["selected"] == v1  # 用户字段优先
    assert item["versions"][:2] == [v1, v2] and len(item["versions"]) == 3  # 晚到的模型结果只作为新版本
    assert not workflow_engine._active_sessions


def _safe(errors, fn, *args):
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - 转交主线程断言
        errors.append(exc)


# ── 16.4 清理：父删除后重启仍可定位、失败退避 ────────────────────────────
def test_cleanup_after_parent_delete_survives_restart_and_backs_off(isolated_roots, monkeypatch):
    from core.object_operations import ObjectCleanupWorker

    sid = _new_session()
    monkeypatch.setitem(workflow_engine.agent_factories, WorkflowStage.CHARACTER_DESIGN,
                        lambda: ScriptedAgent(lambda agent, *_: _character_payload(agent, b"to-clean")))
    _run(WorkflowStage.CHARACTER_DESIGN, sid)
    public = workflow_engine.get_state(sid).artifacts["character_design"]["characters"][0]["selected"]
    row = FileRepo().get_by_public_path(public)
    assert workflow_engine.delete_session(sid)
    real_delete = type(get_storage()).delete
    monkeypatch.setattr(type(get_storage()), "delete", lambda self, *a, **k: None)  # 适配器吞掉删除
    counts = ObjectCleanupWorker(boot_id="restarted").run_once()  # 新进程扫描，父行早已删除
    assert counts["failed"] >= 1
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state, attempts, last_error, next_retry_at FROM object_operations "
                        "WHERE object_key = %s", (row["object_key"],))
            op = cur.fetchone()
    assert op["state"] == "delete_pending" and op["attempts"] == 1 and "still present" in op["last_error"]
    monkeypatch.setattr(type(get_storage()), "delete", real_delete)
    ObjectCleanupWorker(boot_id="restarted").run_once(now=int(op["next_retry_at"]) + 1)
    assert get_storage().stat(str(row["object_key"]), bucket=str(row["bucket"])) is None
    assert TaskRepo().list(OWNER) == []


def test_prune_refs_skips_recently_used_and_pinned_entries(isolated_roots):
    from core.resource_claims import CLAIMS, local_identity

    refs = media_refs._REFS_ROOT / media_refs.owner_key(OWNER)
    old = _write(str(refs / "old.bin"), b"o")
    pinned = _write(str(refs / "pinned.bin"), b"p")
    recent = _write(str(refs / "recent.bin"), b"r")
    past = time.time() - 8 * 24 * 3600
    for path in (old, pinned):
        os.utime(path, (past, past))
    assert CLAIMS.pin(local_identity(pinned))
    try:
        assert media_refs.prune_refs(max_bytes=0) == 1  # 在用（pin）与近 10 分钟访问的条目都不删
    finally:
        CLAIMS.unpin(local_identity(pinned))
    assert not os.path.exists(old) and os.path.exists(pinned) and os.path.exists(recent)
    assert media_refs.prune_refs(max_bytes=0) == 1 and os.path.exists(recent)


def test_startup_prunes_only_empty_execution_dirs(isolated_roots):
    from core.object_operations import prune_empty_execution_dirs

    base = artifact_publisher.work_roots_base()
    os.makedirs(os.path.join(base, "k1", "sessions", "s1", "e1"))
    kept = _write(os.path.join(base, "k2", "tasks", "t1", "t1", "f.bin"), b"x")
    assert prune_empty_execution_dirs() == 4  # e1/s1/sessions/k1，只 rmdir 空目录
    assert not os.path.exists(os.path.join(base, "k1")) and os.path.exists(kept)
