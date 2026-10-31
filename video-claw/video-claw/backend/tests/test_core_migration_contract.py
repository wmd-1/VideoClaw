"""Group 17: legacy migration CLI contract (D18) — parameter matrix and exit codes.

Not a migration run: everything happens in the isolated `videoclaw_test` DB, the
`videoclaw-test*` buckets and tmp CODE/RESULT/SESSION/TASK roots. Objects uploaded by
the legacy tool are not registered in object_operations, so each test removes the
keys it created explicitly and asserts the bucket is clean.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from config import settings
from core.media_refs import owner_key, to_local
from db import database, legacy_import
from db.legacy_import import main
from db.repositories import FileRepo, OwnerRepo, SessionRepo
from storage import get_storage


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    code_root = tmp_path / "code"
    for path in (code_root / "result", tmp_path / "sessions", tmp_path / "tasks"):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "CODE_DIR", str(code_root))
    monkeypatch.setattr(settings, "RESULT_DIR", str(code_root / "result"))
    monkeypatch.setattr(settings, "SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(settings, "TASK_DIR", str(tmp_path / "tasks"))
    assert settings.STORAGE_BUCKET.startswith("videoclaw-test")
    yield tmp_path


@pytest.fixture
def created_keys():
    keys: list = []
    yield keys
    leftovers = []
    for key in keys:
        get_storage().delete(key, bucket=settings.STORAGE_BUCKET)
        if get_storage().stat(key, bucket=settings.STORAGE_BUCKET) is not None:
            leftovers.append(key)
    assert leftovers == [], f"test objects left behind: {leftovers}"


def _legacy_session(root, sid: str, owner, name: str = "asset.png", content: bytes = b"legacy-bytes") -> str:
    public = f"/code/result/image/{sid}/{name}"
    local = to_local(public)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "wb") as handle:
        handle.write(content)
    data = {"session_id": sid, "current_stage": "character_design", "status": {"character_design": "completed"},
            "meta": {"idea": "legacy"}, "artifacts": {"character_design": {"characters": [{"id": "c1", "selected": public}]}}}
    if owner is not None:
        data["owner"] = owner
    (root / "sessions" / f"{sid}.json").write_text(json.dumps(data), encoding="utf-8")
    return public


def _legacy_key(owner: str, public: str) -> str:
    digest = hashlib.sha1(public.encode("utf-8")).hexdigest()[:16]
    return f"{owner_key(owner)}/legacy/{digest}/{os.path.basename(public)}"


def _counts() -> dict:
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            result = {}
            for table in ("sessions", "session_stage_artifacts", "artifact_files", "pipeline_tasks", "sandbox_records"):
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                result[table] = cur.fetchone()["n"]
            return result


ILLEGAL = [
    ["--files"],
    ["--all-files"],
    ["--files", "--all-files", "--confirm-all-files"],
    ["--confirm-all-files"],
    ["--files-owner", "alice"],
    ["--all-files", "--confirm-all-files", "--files-owner", "alice"],
    ["--reassign-from", "local"],
    ["--to", "alice"],
    ["--reassign-from", "local", "--to", "alice", "--files", "--files-owner", "alice"],
    ["--reassign-from", "local", "--to", "alice", "--all-files", "--confirm-all-files"],
    ["--reassign-from", "local", "--to", "alice", "--fallback-owner", "x"],
    ["--reassign-from", "local", "--to", "alice", "--files-owner", "x"],
    ["--reassign-from", "local", "--to", "alice", "--confirm-all-files"],
]


@pytest.mark.parametrize("argv", ILLEGAL, ids=[" ".join(a) for a in ILLEGAL])
def test_illegal_combinations_are_refused_before_any_write(isolated_roots, monkeypatch, argv):
    _legacy_session(isolated_roots, "s_illegal", "local")
    monkeypatch.setattr(legacy_import, "init_db", lambda: pytest.fail("init_db must not run"))
    monkeypatch.setattr(legacy_import, "get_storage", lambda: pytest.fail("storage must not be touched"))
    before = _counts()
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
    assert _counts() == before


@pytest.mark.parametrize("argv", [
    ["--dry-run"],
    ["--files", "--files-owner", "alice", "--dry-run"],
    ["--all-files", "--confirm-all-files", "--dry-run"],
    ["--reassign-from", "local", "--to", "alice", "--dry-run"],
])
def test_dry_run_without_schema_exits_2_without_ddl(monkeypatch, argv):
    monkeypatch.setattr(legacy_import, "_schema_ready", lambda: False)
    monkeypatch.setattr(legacy_import, "init_db", lambda: pytest.fail("dry-run must not create schema"))
    assert main(argv) == 2


def test_dry_runs_never_write_rows_or_objects(isolated_roots, monkeypatch):
    _legacy_session(isolated_roots, "s_dry", None)

    class NoWrites:
        def __getattr__(self, name):
            if name in ("upload", "delete", "copy"):
                raise AssertionError(f"dry-run called storage.{name}")
            return getattr(get_storage(), name)

    monkeypatch.setattr(legacy_import, "get_storage", lambda: NoWrites())
    monkeypatch.setattr(legacy_import, "init_db", lambda: pytest.fail("dry-run must not run init_db"))
    before = _counts()
    assert main(["--fallback-owner", "local", "--dry-run"]) == 0
    assert main(["--files", "--fallback-owner", "local", "--dry-run"]) == 0
    assert main(["--all-files", "--confirm-all-files", "--fallback-owner", "local", "--dry-run"]) == 0
    assert main(["--reassign-from", "local", "--to", "alice", "--dry-run"]) == 0
    assert _counts() == before


def test_claim_then_files_for_the_claimed_owner(isolated_roots, created_keys):
    public = _legacy_session(isolated_roots, "s_claim", None)
    assert main(["--fallback-owner", "local"]) == 0  # 空库：先导入元数据（缺 owner 由 fallback 补）
    assert SessionRepo().get("s_claim")["owner"] == "local"
    assert main(["--reassign-from", "local", "--to", "alice"]) == 0  # 再单独认领
    assert SessionRepo().get("s_claim")["owner"] == "alice"
    created_keys.append(_legacy_key("alice", public))
    assert main(["--files", "--files-owner", "alice"]) == 0  # 认领后再迁文件，文件归属跟父资源
    row = FileRepo().get_by_public_path(public)
    assert row["owner"] == "alice" and row["storage_kind"] == "minio"
    assert get_storage().stat(row["object_key"], bucket=settings.STORAGE_BUCKET)["size"] == len(b"legacy-bytes")
    assert OwnerRepo().count_owner("local")["sessions"] == 0


def test_owner_conflict_and_unverified_upload_exit_1_with_real_compensation(isolated_roots, monkeypatch, created_keys):
    conflict = _legacy_session(isolated_roots, "s_conflict", "alice", name="conflict.png")
    assert main([]) == 0
    # 同一路径已被另一 owner 的会话索引：父资源与文件行归属不一致
    SessionRepo().insert_if_absent({"session_id": "s_bob", "owner": "bob", "status": "idle", "current_stage": None,
                                    "status_json": {}, "stage_progress": {}, "project_params": {}})
    FileRepo().upsert_many([{
        "file_id": "f_conflict", "owner": "bob", "session_id": "s_bob", "stage": "character_design",
        "kind": "legacy", "storage_kind": "local", "object_key": conflict.lstrip("/"), "public_path": conflict,
        "local_path": to_local(conflict), "media_type": "image/png", "size_bytes": 1,
    }])
    assert main(["--files", "--files-owner", "alice", "--dry-run"]) == 1  # 预览也报冲突
    assert main(["--files", "--files-owner", "alice"]) == 1
    assert FileRepo().get_by_public_path(conflict)["owner"] == "bob"  # 不隐式选边

    fresh = _legacy_session(isolated_roots, "s_verify", "alice", name="verify.png", content=b"verify-bytes")
    assert main([]) == 0
    key = _legacy_key("alice", fresh)
    created_keys.append(key)
    real_verify = legacy_import._verify_object

    def reject_new_upload(object_key, local, bucket=None):
        if object_key == key:
            assert get_storage().stat(key, bucket=settings.STORAGE_BUCKET) is not None  # 真实对象已写入
            return False, "size mismatch (injected)"
        return real_verify(object_key, local, bucket)

    monkeypatch.setattr(legacy_import, "_verify_object", reject_new_upload)
    assert main(["--files", "--files-owner", "alice"]) == 1
    assert get_storage().stat(key, bucket=settings.STORAGE_BUCKET) is None  # 真实 MinIO 补偿删除
    row = FileRepo().get_by_public_path(fresh)
    assert row["storage_kind"] == "local" and row["owner"] == "alice"  # 保留可读 local 行，可重试


def test_minio_missing_recover_dry_run_then_actual_clears_flag(isolated_roots, created_keys):
    public = _legacy_session(isolated_roots, "s_missing", "alice", content=b"recover-bytes")
    key = _legacy_key("alice", public)
    created_keys.append(key)
    assert main(["--files", "--fallback-owner", "alice"]) == 0
    get_storage().delete(key, bucket=settings.STORAGE_BUCKET)  # 远端对象丢失
    assert main(["--files", "--files-owner", "alice"]) == 1
    assert FileRepo().get_by_public_path(public)["missing_at"] is not None
    get_storage().upload(to_local(public), key, "image/png", bucket=settings.STORAGE_BUCKET)  # 运维恢复对象
    assert main(["--files", "--files-owner", "alice", "--dry-run"]) == 0
    assert FileRepo().get_by_public_path(public)["missing_at"] is not None  # 只读预览不清标记
    assert main(["--files", "--files-owner", "alice"]) == 0
    row = FileRepo().get_by_public_path(public)
    assert row["missing_at"] is None and row["object_key"] == key
