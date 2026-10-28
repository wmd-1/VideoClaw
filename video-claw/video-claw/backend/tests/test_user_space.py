"""Focused A1–A28 / P1–P20 regression tests for user-space isolation.

Run only inside the existing backend container:
  docker exec -i -e PYTHONPATH=/app -w /app video-claw-backend \
    /app/.venv/bin/python -m pytest backend/tests -q
"""
from __future__ import annotations

import hashlib
import os

from fastapi.testclient import TestClient

from api.dependencies import workflow_engine
from config import settings
from core.identity import owner_key
from core.media_refs import to_public
from core.space_service import list_saved, public_works, save_work
from db.repositories import FileRepo, StageArtifactRepo
from storage import get_storage


def headers(login: str) -> dict[str, str]:
    return {"X-User-Login-Name": login}


def make_file_row(session_id: str, path: str, owner: str = "alice", stage: str = "script_generation") -> dict:
    public = to_public(path)
    row = {
        "file_id": hashlib.sha256((session_id + public).encode()).hexdigest()[:32],
        "owner": owner,
        "session_id": session_id,
        "stage": stage,
        "kind": "video" if path.endswith(".mp4") else "file",
        "storage_kind": "local",
        "object_key": public.lstrip("/"),
        "public_path": public,
        "local_path": path,
        "media_type": "video/mp4" if path.endswith(".mp4") else "application/octet-stream",
        "size_bytes": os.path.getsize(path),
    }
    FileRepo().upsert_many([row])
    return row


def test_identity_guip_single_redaction_and_admin(client, monkeypatch):
    # A1: guip without identity is rejected on all owner-scoped lists.
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/sandbox/history").status_code == 401
    # A16/A18: empty and path-bearing logins are invalid.
    assert client.get("/api/sessions", headers=headers("")).status_code == 401
    assert client.get("/api/sessions", headers=headers("bad/name")).status_code == 401
    # A2: single mode is local without a header.
    monkeypatch.setattr(settings, "USERS_MODE", "single")
    assert client.get("/api/sessions").status_code == 200
    monkeypatch.setattr(settings, "USERS_MODE", "guip")

    # A9: non-admin gets redacted config and cannot write.
    monkeypatch.setattr(settings, "USERS_ADMIN_LOGINS", ["admin"])
    body = client.get("/api/config", headers=headers("alice")).json()
    assert body["config"]["database"]["dsn"] == "***"
    assert client.put("/api/config", headers=headers("alice"), json={"values": {}}).status_code == 403
    assert client.post("/api/models/test", headers=headers("alice"), json={}).status_code == 403

    # A14: configured trusted proxy CIDR fails closed for an untrusted TestClient host.
    monkeypatch.setattr(settings, "USERS_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    assert client.get("/api/sessions", headers=headers("alice")).status_code == 401
    monkeypatch.setattr(settings, "USERS_TRUSTED_PROXY_CIDRS", "")


def test_sessions_owner_isolation_and_delete(client, session_dir):
    workflow_engine.create_session("alice_sid", {"idea": "alice"}, owner="alice")
    assert [row["id"] for row in client.get("/api/sessions", headers=headers("alice")).json()["sessions"]] == ["alice_sid"]
    assert client.get("/api/sessions", headers=headers("bob")).json()["sessions"] == []
    assert client.get("/api/project/alice_sid/status", headers=headers("bob")).status_code == 404
    assert client.patch("/api/project/alice_sid/models", headers=headers("bob"), json={}).status_code == 404
    assert client.post("/api/project/alice_sid/stop", headers=headers("bob")).status_code == 404
    assert client.delete("/api/sessions/alice_sid", headers=headers("bob")).status_code == 404
    assert client.delete("/api/sessions/alice_sid", headers=headers("alice")).status_code == 200


def test_pipeline_task_owner_and_running_delete(client, monkeypatch):
    import pipelines.storage as storage

    root = os.path.join(settings.CODE_DIR, "data", "pytest-tasks")
    result = os.path.join(settings.RESULT_DIR, "pytest-tasks")
    os.makedirs(result, exist_ok=True)
    monkeypatch.setattr(storage, "TASK_DATA_DIR", root)
    monkeypatch.setattr(storage, "TASK_RESULT_DIR", result)
    task = storage.create_task("standard", {}, owner="alice")
    assert [row["task_id"] for row in client.get("/api/tasks", headers=headers("alice")).json()["tasks"]] == [task["task_id"]]
    assert client.get("/api/tasks", headers=headers("bob")).json()["tasks"] == []
    assert client.get(f"/api/tasks/{task['task_id']}", headers=headers("bob")).status_code == 404
    assert client.delete(f"/api/tasks/{task['task_id']}", headers=headers("bob")).status_code == 404
    assert client.delete(f"/api/tasks/{task['task_id']}", headers=headers("alice")).status_code == 409
    storage.update_task(task["task_id"], status="completed")
    assert client.delete(f"/api/tasks/{task['task_id']}", headers=headers("alice")).status_code == 200


def test_sandbox_history_owner(client, monkeypatch):
    import api.routers.sandbox as sandbox

    history = os.path.join(settings.CODE_DIR, "data", "pytest-sandbox.json")
    if os.path.exists(history):
        os.remove(history)
    monkeypatch.setattr(sandbox, "SANDBOX_HISTORY_FILE", history)
    record_id = sandbox._add_record("llm", "m", {}, {}, owner="alice")
    assert len(client.get("/api/sandbox/history", headers=headers("alice")).json()["records"]) == 1
    assert client.get("/api/sandbox/history", headers=headers("bob")).json()["records"] == []
    response = client.delete(f"/api/sandbox/history/{record_id}", headers=headers("bob")).json()
    assert response["success"] is False
    assert len(client.get("/api/sandbox/history", headers=headers("alice")).json()["records"]) == 1


def test_media_range_owner_missing_and_data_404(client, session_dir, result_file):
    workflow_engine.create_session("media_sid", {}, owner="alice")
    row = make_file_row("media_sid", result_file)
    url = f"{row['public_path']}?user_login=alice"
    assert client.get(url).status_code == 200
    response = client.get(url, headers={"Range": "bytes=2-5"})
    assert response.status_code == 206 and response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/16"
    assert client.head(url).status_code == 200
    assert client.head(url).content == b""
    assert client.get(url, headers={"Range": "bytes=99-"}).status_code == 416
    assert client.get(f"{row['public_path']}?user_login=bob").status_code == 404
    assert client.get("/code/data/sessions/x.json?user_login=alice").status_code == 404
    os.remove(result_file)
    assert client.get(url).status_code == 404
    assert FileRepo().get(row["file_id"])["missing_at"] is not None


def test_workflow_canonicalizes_selected_and_rejects_foreign_paths(client, session_dir, result_file):
    workflow_engine.create_session("alice_sid", {}, owner="alice")
    workflow_engine.create_session("bob_sid", {}, owner="bob")
    row = make_file_row("alice_sid", result_file, owner="alice", stage="video_generation")
    body = {"clips": [{"id": "c1", "selected": row["public_path"]}]}
    response = client.patch("/api/project/alice_sid/artifact/video_generation", headers=headers("alice"), json=body)
    assert response.status_code == 200
    assert response.json()["artifact"]["clips"][0]["selected"] == row["public_path"]
    # A23/A25: a foreign public/absolute path is rejected for Bob.
    foreign = {"clips": [{"id": "c1", "selected": row["public_path"]}]}
    assert client.patch("/api/project/bob_sid/artifact/video_generation", headers=headers("bob"), json=foreign).status_code == 404
    # A24: foreign temp owner path is rejected before reading.
    tmp = os.path.join(settings.TEMP_DIR, "users", owner_key("bob"), "x.docx")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    open(tmp, "wb").write(b"x")
    assert client.patch(
        "/api/project/alice_sid/artifact/video_generation",
        headers=headers("alice"),
        json={"clips": [{"id": "c1", "selected": tmp}]},
    ).status_code == 400


def test_works_saved_copy_source_delete_and_owner(client, session_dir, result_file):
    workflow_engine.create_session("work_sid", {"idea": "WORK"}, owner="alice")
    row = make_file_row("work_sid", result_file, owner="alice", stage="post_production")
    StageArtifactRepo().complete(
        "work_sid",
        "post_production",
        {"final_videos": [{"episode": 1, "path": row["public_path"], "name": "第1集"}]},
        [],
        None,
    )
    works = public_works("alice")
    assert works["total"] == 1
    work_id = works["items"][0]["work_id"]
    saved = save_work("alice", work_id)
    saved_id = saved["saved_id"]
    assert saved["media"][0]["url"] == f"/api/space/saved/{saved_id}/media"
    assert list_saved("alice")["total"] == 1
    assert client.get(f"/api/space/saved/{saved_id}/media?user_login=bob").status_code == 404
    assert client.get(f"/api/space/saved/{saved_id}/media?user_login=alice", headers={"Range": "bytes=1-3"}).status_code == 206
    workflow_engine.delete_session("work_sid")
    assert client.get(f"/api/space/saved/{saved_id}/media?user_login=alice").status_code == 200
    assert list_saved("alice")["items"][0]["source_deleted"] is True
    assert client.delete(f"/api/space/saved/{saved_id}", headers=headers("alice")).status_code == 200
    assert client.get(f"/api/space/saved/{saved_id}/media?user_login=alice").status_code == 404


def test_checkpoint_keeps_last_completed_payload(session_dir):
    workflow_engine.create_session("checkpoint_sid", {}, owner="alice")
    repo = StageArtifactRepo()
    repo.complete("checkpoint_sid", "script_generation", {"title": "old"}, [], None)
    repo.mark_running("checkpoint_sid", "script_generation", "boot", None)
    repo.fail("checkpoint_sid", "script_generation", "boom")
    assert repo.list_checkpoints("checkpoint_sid") == {"script_generation": {"title": "old"}}
    repo.import_checkpoint("checkpoint_sid", "script_generation", {"title": "ignored"})
    assert repo.list_checkpoints("checkpoint_sid") == {"script_generation": {"title": "old"}}


def test_legacy_import_owner_and_idempotence(tmp_path, monkeypatch, session_dir):
    import json

    from db.legacy_import import _import_sessions
    from db.repositories import SessionRepo

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "legacy_sid.json").write_text(
        json.dumps({
            "session_id": "legacy_sid",
            "current_stage": "script_generation",
            "artifacts": {"script_generation": {"title": "legacy"}},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "SESSION_DIR", str(legacy))
    first, _ = _import_sessions("local", False)
    second, _ = _import_sessions("local", False)
    row = SessionRepo().get("legacy_sid")
    assert first == second == 1
    assert row["owner"] == "local"
    assert StageArtifactRepo().list_checkpoints("legacy_sid")["script_generation"] == {"title": "legacy"}


def test_saved_limit_idempotence_and_foreign_owner(session_dir, result_file):
    import pytest

    from core.space_service import delete_saved

    workflow_engine.create_session("limit_sid", {}, owner="alice")
    row = make_file_row("limit_sid", result_file, owner="alice", stage="post_production")
    StageArtifactRepo().complete(
        "limit_sid", "post_production",
        {"final_videos": [{"episode": 1, "path": row["public_path"]}]}, [], None,
    )
    work_id = public_works("alice")["items"][0]["work_id"]
    first = save_work("alice", work_id)
    second = save_work("alice", work_id)
    assert first["saved_id"] == second["saved_id"]
    delete_saved("alice", first["saved_id"])
    settings.USERS_SAVED_LIMIT = 0
    with pytest.raises(OverflowError):
        save_work("alice", work_id)
    settings.USERS_SAVED_LIMIT = 50
    with pytest.raises(KeyError):
        save_work("bob", work_id)


def test_ensure_local_concurrency_and_remote_forms(session_dir, result_file, monkeypatch):
    import threading
    import pytest
    import core.media_refs as refs
    from core.identity import CurrentUser, owner_key

    class FakeStorage:
        def __init__(self):
            self.count = 0
            self.lock = threading.Lock()

        def download(self, key, path):
            with self.lock:
                self.count += 1
            with open(path, "wb") as handle:
                handle.write(b"concurrent")
            return path

    workflow_engine.create_session("refs_sid", {}, owner="alice")
    public = to_public(result_file)
    FileRepo().upsert_many([{
        "file_id": "refs-file", "owner": "alice", "session_id": "refs_sid",
        "stage": "script_generation", "kind": "file", "storage_kind": "minio",
        "object_key": "result/refs/object", "public_path": public,
        "local_path": None, "media_type": "application/octet-stream",
    }])
    user = CurrentUser(login="alice", owner_key=owner_key("alice"))
    fake = FakeStorage()
    monkeypatch.setattr(refs, "get_storage", lambda: fake)
    target = refs._ref_target(public, user.owner_key)
    if target.exists():
        target.unlink()
    results = []
    threads = [threading.Thread(target=lambda: results.append(refs.ensure_local(public, user))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert fake.count == 1
    assert len(set(results)) == 1 and os.path.isfile(results[0])
    assert refs.canonicalize("https://cdn.example.com/a.png", user).startswith("https://")
    assert refs.materialize("https://cdn.example.com/a.png", user) == "https://cdn.example.com/a.png"


def test_stale_running_and_owner_none(session_dir):
    import pytest
    from db.repositories import SessionRepo

    workflow_engine.create_session("stale_sid", {}, owner="alice")
    StageArtifactRepo().mark_running("stale_sid", "script_generation", "old-boot")
    assert StageArtifactRepo().sweep_stale_running("new-boot") == 1
    assert StageArtifactRepo().get("stale_sid", "script_generation")["status"] == "interrupted"
    with pytest.raises(ValueError):
        SessionRepo().list(None)


def test_startup_does_not_batch_download_and_missing_object(session_dir, result_file, monkeypatch):
    import pytest
    import core.media_refs as refs
    from core.identity import CurrentUser, owner_key
    from storage import ObjectMissing

    calls = {"download": 0}
    class Fake:
        def download(self, key, path):
            calls["download"] += 1
            raise ObjectMissing(key)

    workflow_engine.create_session("startup_sid", {}, owner="alice")
    public = to_public(result_file)
    FileRepo().upsert_many([{
        "file_id": "startup-file", "owner": "alice", "session_id": "startup_sid",
        "stage": "script_generation", "kind": "file", "storage_kind": "minio",
        "object_key": "result/startup/object", "public_path": public,
        "local_path": None, "media_type": "application/octet-stream",
    }])
    original_ensure = refs.ensure_local
    monkeypatch.setattr(refs, "ensure_local", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("startup download")))
    workflow_engine.load_sessions()
    monkeypatch.setattr(refs, "ensure_local", original_ensure)
    user = CurrentUser(login="alice", owner_key=owner_key("alice"))
    monkeypatch.setattr(refs, "get_storage", lambda: Fake())
    target = refs._ref_target(public, user.owner_key)
    if target.exists():
        target.unlink()
    with pytest.raises(refs.MediaMissingError):
        refs.ensure_local(public, user)
    assert FileRepo().get("startup-file")["missing_at"] is not None
