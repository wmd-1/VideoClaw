"""第六轮报告第四批回归：迁移工具 R06 / R17 / R18 / R19。

只允许在既有后端容器内执行：
  docker exec -i -e PYTHONPATH=/app -w /app video-claw-backend \\
    /app/.venv/bin/python -m pytest /tmp/vc_tests -q
"""
from __future__ import annotations

import json
import os

import pytest

from config import settings
from core.media_refs import to_local, to_public
from db import legacy_import
from db.legacy_import import _import_files, _import_sessions, main
from db.repositories import FileRepo, OwnerRepo, SessionRepo
from storage import StorageError, get_storage


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    """独占 CODE/RESULT/SESSION/TASK 根：不向业务挂载目录写任何文件。"""
    code_root = tmp_path / "code"
    result_root = code_root / "result"
    for path in (result_root, tmp_path / "sessions", tmp_path / "tasks"):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "CODE_DIR", str(code_root))
    monkeypatch.setattr(settings, "RESULT_DIR", str(result_root))
    monkeypatch.setattr(settings, "SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(settings, "TASK_DIR", str(tmp_path / "tasks"))
    yield result_root


def _write_media(name: str, content: bytes = b"legacy-bytes") -> tuple[str, str]:
    """在独占的 CODE/RESULT 根内造文件，并返回与 canonical 路径一致的 (local, public)。"""
    public = f"/code/result/tests/legacy-{name}/asset.bin"
    path = to_local(public)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path, public


def _records(session_id: str, owner: str, public: str):
    return [{
        "kind": "session",
        "id": session_id,
        "owner": owner,
        "data": {"artifacts": {"script_generation": {"image": public}}},
    }]


def _ensure_session(session_id: str, owner: str) -> None:
    """artifact_files 的父资源必须已存在（库内触发器校验 owner 一致性）。"""
    SessionRepo().insert_if_absent({
        "session_id": session_id,
        "owner": owner,
        "status": "completed",
        "current_stage": "script_generation",
        "status_json": {},
        "stage_progress": {},
        "project_params": {},
    })


def _write_session_json(tmp_path, monkeypatch, sid: str, owner: str | None, payload=None, meta=None):
    legacy = tmp_path / "legacy"
    legacy.mkdir(exist_ok=True)
    data = {"session_id": sid, "current_stage": "script_generation",
            "artifacts": {"script_generation": payload or {"title": "legacy"}}}
    if owner is not None:
        data["owner"] = owner
    if meta is not None:
        data["meta"] = meta
    (legacy / f"{sid}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(settings, "SESSION_DIR", str(legacy))
    return legacy


# ─────────────── R06：认领与导入分离、导入不覆盖既有行 ───────────────


def test_r06_import_does_not_roll_back_claim_or_metadata(tmp_path, monkeypatch):
    _write_session_json(tmp_path, monkeypatch, "legacy_sid", "local")
    first, _ = _import_sessions("local", False)
    assert first == 1
    assert SessionRepo().get("legacy_sid")["owner"] == "local"

    # 显式认领 local → alice
    OwnerRepo().reassign("local", "alice")
    assert SessionRepo().get("legacy_sid")["owner"] == "alice"

    # 业务侧更新元数据
    SessionRepo().upsert({
        "session_id": "legacy_sid",
        "owner": "alice",
        "status": "completed",
        "current_stage": "post_production",
        "status_json": {"script_generation": "completed"},
        "stage_progress": {},
        "project_params": {"idea": "新元数据"},
    })

    # 再次普通导入：owner 与元数据都不得回退
    _import_sessions("local", False)
    row = SessionRepo().get("legacy_sid")
    assert row["owner"] == "alice"
    assert row["status"] == "completed"
    assert row["current_stage"] == "post_production"
    assert row["meta_json"]["project_params"]["idea"] == "新元数据"


def test_r06_claim_dry_run_is_read_only(tmp_path, monkeypatch):
    """认领 dry-run：只读计数，不初始化 schema、不改归属。"""
    _write_session_json(tmp_path, monkeypatch, "legacy_sid", "local")
    _ensure_session("legacy_sid", "local")

    called = {"init_db": 0}
    real_init = legacy_import.init_db
    monkeypatch.setattr(legacy_import, "init_db", lambda: called.__setitem__("init_db", called["init_db"] + 1) or real_init())

    assert main(["--reassign-from", "local", "--to", "alice", "--dry-run"]) == 0
    assert called["init_db"] == 0, "dry-run 不得初始化 schema"
    assert SessionRepo().get("legacy_sid")["owner"] == "local"


def test_r06_claim_dry_run_requires_existing_schema(monkeypatch):
    monkeypatch.setattr(legacy_import, "_schema_ready", lambda: False)
    assert main(["--reassign-from", "local", "--to", "alice", "--dry-run"]) == 2


def test_r06_claim_mode_runs_alone(tmp_path, monkeypatch):
    _write_session_json(tmp_path, monkeypatch, "legacy_sid", "local")
    assert main(["--reassign-from", "local", "--to", "alice"]) == 0
    # 纯认领不触发导入
    assert SessionRepo().get("legacy_sid") is None

    with pytest.raises(SystemExit):
        main(["--reassign-from", "local", "--to", "alice", "--files"])
    with pytest.raises(SystemExit):
        main(["--files"])


# ─────────────── R17：文件迁移的 owner 范围与全量确认 ───────────────


def test_r17_files_scope_and_all_files_confirmation():
    alice_path, alice_public = _write_media("alice")
    bob_path, bob_public = _write_media("bob")
    _ensure_session("s_alice", "alice")
    _ensure_session("s_bob", "bob")
    records = _records("s_alice", "alice", alice_public) + _records("s_bob", "bob", bob_public)
    keys: list[str] = []
    try:
        counters = _import_files(records, False, target_owner="alice")
        assert counters["uploaded"] == 1 and counters["skipped_owner"] == 1
        assert FileRepo().get_by_public_path(alice_public) is not None
        assert FileRepo().get_by_public_path(bob_public) is None
        keys.append(FileRepo().get_by_public_path(alice_public)["object_key"])

        # 参数冲突/缺确认在写入前拒绝
        with pytest.raises(SystemExit):
            main(["--files", "--all-files", "--confirm-all-files"])
        with pytest.raises(SystemExit):
            main(["--all-files"])
        with pytest.raises(SystemExit):
            main(["--files"])

        # 全量：确认后两个 owner 都处理
        counters = _import_files(records, False, all_files=True)
        assert counters["uploaded"] == 1 and counters["skipped"] == 1
        bob_row = FileRepo().get_by_public_path(bob_public)
        assert bob_row is not None and bob_row["owner"] == "bob"
        keys.append(bob_row["object_key"])

        # dry-run 与执行范围一致，且不写库
        alice_row = FileRepo().get_by_public_path(alice_public)
        FileRepo().upsert_many([{**alice_row, "storage_kind": "local", "bucket": None, "object_key": "x"}])
        before = FileRepo().get_by_public_path(alice_public)
        dry = _import_files(records, True, target_owner="alice")
        assert dry["converted"] == 1 and dry["skipped_owner"] == 1
        assert FileRepo().get_by_public_path(alice_public) == before
    finally:
        for key in keys:
            try:
                get_storage().delete(key, bucket=settings.STORAGE_BUCKET)
            except Exception:
                pass
        for path in (alice_path, bob_path):
            try:
                os.remove(path)
            except OSError:
                pass


# ─────────────── R18：已有 local 行迁移后原位转 minio ───────────────


def test_r18_existing_local_row_is_converted_in_place():
    path, public = _write_media("convert")
    file_id = "legacy_convert"
    _ensure_session("s_convert", "alice")
    FileRepo().upsert_many([{
        "file_id": file_id, "owner": "alice", "session_id": "s_convert",
        "stage": "script_generation", "kind": "legacy", "storage_kind": "local",
        "bucket": None, "object_key": public.lstrip("/"), "public_path": public,
        "local_path": path, "media_type": "application/octet-stream",
        "size_bytes": os.path.getsize(path),
    }])
    records = _records("s_convert", "alice", public)
    try:
        counters = _import_files(records, False, target_owner="alice")
        assert counters["converted"] == 1
        row = FileRepo().get_by_public_path(public)
        assert row["file_id"] == file_id and row["storage_kind"] == "minio"
        assert row["bucket"] == settings.STORAGE_BUCKET and row["object_key"]
        assert get_storage().stat(row["object_key"], bucket=row["bucket"]) is not None

        # 重入：已完成 minio 记录幂等跳过，不产生重复行
        again = _import_files(records, False, target_owner="alice")
        assert again["skipped"] == 1 and again["converted"] == 0
        assert FileRepo().get_by_public_path(public)["file_id"] == file_id
    finally:
        row = FileRepo().get_by_public_path(public)
        if row and row.get("object_key"):
            try:
                get_storage().delete(row["object_key"], bucket=row.get("bucket"))
            except Exception:
                pass
        try:
            os.remove(path)
        except OSError:
            pass


# ─────────────── R19：首次上传失败保留可读的 local 行 ───────────────


class _FailingStorage:
    def upload(self, *args, **kwargs):
        raise StorageError("upload boom")


def test_r17_claimed_owner_drives_file_scope(tmp_path, monkeypatch):
    """认领后文件归属以 DB 为准：JSON 仍是 local，但 --files-owner alice 必须处理。"""
    legacy = _write_session_json(tmp_path, monkeypatch, "legacy_sid", "local")
    path, public = _write_media("claimed")
    data = json.loads((legacy / "legacy_sid.json").read_text(encoding="utf-8"))
    data["artifacts"] = {"script_generation": {"image": public}}
    (legacy / "legacy_sid.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    _ensure_session("legacy_sid", "local")
    _import_sessions("local", False)
    OwnerRepo().reassign("local", "alice")

    records = _records("legacy_sid", "local", public)  # 旧 JSON 仍是 local
    try:
        counters = _import_files(records, False, target_owner="alice")
        assert counters["uploaded"] == 1 and counters["skipped_owner"] == 0
        row = FileRepo().get_by_public_path(public)
        assert row["owner"] == "alice"
        assert row["storage_kind"] == "minio"

        # 已归 alice 的文件不能又被 --files-owner local 抓去转换
        again = _import_files(records, False, target_owner="local")
        assert again["skipped_owner"] == 1 and again["converted"] == 0
    finally:
        row = FileRepo().get_by_public_path(public)
        if row and row.get("object_key"):
            try:
                get_storage().delete(row["object_key"], bucket=row.get("bucket"))
            except Exception:
                pass
        try:
            os.remove(path)
        except OSError:
            pass


class _UnverifiedStorage:
    """upload 不报错，但对象不可见/大小不符；记录删除过的 key。"""

    def __init__(self, size=None):
        self.size = size
        self.deleted: list[str] = []
        self.uploaded: list[str] = []

    def upload(self, local, key, media_type=None, **kwargs):
        self.uploaded.append(key)
        return {"bucket": settings.STORAGE_BUCKET, "object_key": key}

    def stat(self, key, bucket=None):
        return None if self.size is None else {"size": self.size}

    def delete(self, key, bucket=None):
        self.deleted.append(key)
        return None


def test_r18_upload_result_is_verified_before_conversion(monkeypatch):
    """首次上传校验失败：补偿删除新对象 + 保留可读 local 行。"""
    path, public = _write_media("unverified")
    _ensure_session("s_unverified", "alice")
    records = _records("s_unverified", "alice", public)
    storage = _UnverifiedStorage()
    monkeypatch.setattr(legacy_import, "get_storage", lambda: storage)
    try:
        counters = _import_files(records, False, target_owner="alice")
        assert counters["failed"] == 1 and counters["converted"] == 0 and counters["uploaded"] == 0
        row = FileRepo().get_by_public_path(public)
        assert row["storage_kind"] == "local" and row["missing_at"] is None
        # 本次新上传对象被补偿删除，不留孤儿
        assert storage.uploaded == [storage.deleted[0]]
        assert storage.deleted and storage.uploaded[0] in storage.deleted
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_r18_unverifiable_existing_minio_row_keeps_remote_location(monkeypatch):
    """已有 minio 行不可核验：保留 bucket/key 并标记 missing，不降级、不盲删。"""
    path, public = _write_media("keepremote")
    _ensure_session("s_keepremote", "alice")
    remote_key = f"keepremote/{os.path.basename(path)}"
    FileRepo().upsert_many([{
        "file_id": "legacy_keepremote", "owner": "alice", "session_id": "s_keepremote",
        "stage": "script_generation", "kind": "legacy", "storage_kind": "minio",
        "bucket": settings.STORAGE_BUCKET, "object_key": remote_key, "public_path": public,
        "local_path": path, "media_type": "application/octet-stream",
        "size_bytes": os.path.getsize(path),
    }])
    records = _records("s_keepremote", "alice", public)
    storage = _UnverifiedStorage()
    monkeypatch.setattr(legacy_import, "get_storage", lambda: storage)
    try:
        counters = _import_files(records, False, target_owner="alice")
        assert counters["failed"] == 1 and counters["skipped"] == 0
        row = FileRepo().get_by_public_path(public)
        assert row["storage_kind"] == "minio"
        assert row["bucket"] == settings.STORAGE_BUCKET and row["object_key"] == remote_key
        assert row["missing_at"] is not None
        assert storage.deleted == []  # 不盲删已有对象
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_r18_dry_run_is_read_only_for_unverifiable_rows(monkeypatch):
    """dry-run 遇到不可核验的 minio 行：只计数，不改行、不删对象。"""
    path, public = _write_media("dryrun")
    _ensure_session("s_dryrun", "alice")
    remote_key = f"dryrun/{os.path.basename(path)}"
    FileRepo().upsert_many([{
        "file_id": "legacy_dryrun", "owner": "alice", "session_id": "s_dryrun",
        "stage": "script_generation", "kind": "legacy", "storage_kind": "minio",
        "bucket": settings.STORAGE_BUCKET, "object_key": remote_key, "public_path": public,
        "local_path": path, "media_type": "application/octet-stream",
        "size_bytes": os.path.getsize(path),
    }])
    before = FileRepo().get_by_public_path(public)
    records = _records("s_dryrun", "alice", public)
    storage = _UnverifiedStorage()
    monkeypatch.setattr(legacy_import, "get_storage", lambda: storage)
    try:
        counters = _import_files(records, True, target_owner="alice")
        assert counters["failed"] == 1
        assert FileRepo().get_by_public_path(public) == before
        assert storage.deleted == []
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class _ToggleStorage:
    """可切换“对象是否可用”的存储桩，用于“失败→恢复→重试”连续场景。"""

    def __init__(self, size: int, present: bool = False):
        self.size = size
        self.present = present

    def upload(self, local, key, media_type=None, **kwargs):
        self.present = True
        return {"bucket": settings.STORAGE_BUCKET, "object_key": key}

    def stat(self, key, bucket=None):
        return {"size": self.size} if self.present else None

    def delete(self, key, bucket=None):
        self.present = False
        return None


def test_r18_recovered_object_clears_missing_flag(monkeypatch):
    """失败置位 → 对象恢复 → dry-run 保持原标记 → 实际重试清除标记。"""
    path, public = _write_media("recover")
    _ensure_session("s_recover", "alice")
    remote_key = f"recover/{os.path.basename(path)}"
    FileRepo().upsert_many([{
        "file_id": "legacy_recover", "owner": "alice", "session_id": "s_recover",
        "stage": "script_generation", "kind": "legacy", "storage_kind": "minio",
        "bucket": settings.STORAGE_BUCKET, "object_key": remote_key, "public_path": public,
        "local_path": path, "media_type": "application/octet-stream",
        "size_bytes": os.path.getsize(path),
    }])
    records = _records("s_recover", "alice", public)
    storage = _ToggleStorage(size=os.path.getsize(path), present=False)
    monkeypatch.setattr(legacy_import, "get_storage", lambda: storage)
    try:
        # 1) 对象缺失：置 missing
        first = _import_files(records, False, target_owner="alice")
        assert first["failed"] == 1
        assert FileRepo().get_by_public_path(public)["missing_at"] is not None

        # 2) 对象恢复但只做 dry-run：标记保持
        storage.present = True
        dry = _import_files(records, True, target_owner="alice")
        assert dry["skipped"] == 1
        assert FileRepo().get_by_public_path(public)["missing_at"] is not None, "dry-run 不得清标记"

        # 3) 实际重试：核验通过后清 missing
        real = _import_files(records, False, target_owner="alice")
        assert real["skipped"] == 1 and real["failed"] == 0
        row = FileRepo().get_by_public_path(public)
        assert row["missing_at"] is None
        assert row["storage_kind"] == "minio" and row["object_key"] == remote_key
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_r18_owner_conflict_is_rejected_before_upload(monkeypatch):
    """父资源与文件行归属不一致：不隐式选边、不上传，计 conflict。"""
    path, public = _write_media("conflict")
    _ensure_session("s_conflict", "alice")
    row = {
        "file_id": "legacy_conflict", "owner": "bob", "session_id": "s_conflict",
        "stage": "script_generation", "kind": "legacy", "storage_kind": "local",
        "bucket": None, "object_key": public.lstrip("/"), "public_path": public,
        "local_path": path, "media_type": "application/octet-stream",
        "size_bytes": os.path.getsize(path), "missing_at": None,
    }
    monkeypatch.setattr(FileRepo, "get_by_public_path", lambda self, _p: dict(row))
    storage = _UnverifiedStorage()
    monkeypatch.setattr(legacy_import, "get_storage", lambda: storage)
    out = _import_files(_records("s_conflict", "alice", public), False, target_owner="bob")
    assert out["conflict"] == 1 and out["uploaded"] == 0 and out["converted"] == 0
    assert storage.uploaded == []
    try:
        os.remove(path)
    except OSError:
        pass


def test_r19_upload_failure_keeps_local_row_and_can_retry(monkeypatch):
    path, public = _write_media("fallback")
    _ensure_session("s_fallback", "alice")
    records = _records("s_fallback", "alice", public)
    monkeypatch.setattr(legacy_import, "get_storage", lambda: _FailingStorage())
    try:
        counters = _import_files(records, False, target_owner="alice")
        assert counters["failed"] == 1
        row = FileRepo().get_by_public_path(public)
        assert row is not None
        assert row["storage_kind"] == "local" and row["local_path"] == path
        assert row["missing_at"] is None

        # 存储恢复后重试：同一行升级为 minio
        monkeypatch.setattr(legacy_import, "get_storage", get_storage)
        retry = _import_files(records, False, target_owner="alice")
        assert retry["converted"] == 1
        row = FileRepo().get_by_public_path(public)
        assert row["storage_kind"] == "minio"
        assert get_storage().stat(row["object_key"], bucket=row["bucket"]) is not None
    finally:
        row = FileRepo().get_by_public_path(public)
        if row and row.get("storage_kind") == "minio" and row.get("object_key"):
            try:
                get_storage().delete(row["object_key"], bucket=row.get("bucket"))
            except Exception:
                pass
        try:
            os.remove(path)
        except OSError:
            pass
