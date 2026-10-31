"""Core storage primitives (group 13, second correction round).

Covers: operation-log state machine (no target reassignment, in-transaction delete
registration, exact references, pins, bounded local cleanup), commit primitive
(files<->operations correspondence, mandatory CAS, per-item execution sequencing,
consistent snapshots, failure compensation incl. unknown outcome), publication
(execution root layout, legacy reuse, runtime reverse mapping, frozen sources),
consumption plans (video modes, optional degradation, unavailable vs missing) and
the staging lifecycle.

Business paths are NOT enabled. Run inside the existing backend container against
`videoclaw_test` and the `videoclaw-test*` buckets only.
"""
from __future__ import annotations

import os
import time
import types
import uuid

import psycopg
import pytest

import core.persistence_service as persistence_service
from config import settings
from core import media_refs
from core.artifact_publisher import (
    PublicationError,
    PublicationManifest,
    abort_publication,
    build_file_rows,
    execution_work_root,
    new_execution_id,
    publish,
)
from core.consumption import (
    ConsumptionError,
    materialize_stage_inputs,
    plan_targets,
    reverse_map,
)
from core.media_refs import ResolvedMedia, to_local
from core.object_operations import (
    ObjectCleanupWorker,
    cleanup_execution_root,
    register_delete_snapshot,
    register_resource_delete,
    register_saved_delete,
)
from core.persistence_service import CommitOutcomeUnknown, PersistenceService, SnapshotCache
from core.reference_values import (
    PresignTTLTooLong,
    StagingUnavailable,
    build_media_reference,
    needs_staging,
    presign_ttl,
    reference_kind,
    release_staging,
    stage_for_http,
)
from core.resource_claims import CLAIMS, TargetBeingDeleted, local_identity, object_identity
from db import database
from db.repositories import FileRepo, SavedRepo, SessionRepo, StageArtifactRepo
from db.repositories.commits import (
    CommitConflict,
    CommitParentMissing,
    CommitStale,
    CommitValidationError,
    _insert_new_files,
    commit_session_change,
    commit_session_progress,
    load_committed,
    load_working,
    merge_execution,
    rotate_execution,
    session_revision,
)
from db.repositories.operations import (
    CLAIM_LEASE_MS,
    LOCAL_BUCKET,
    ObjectOperationRepo,
    ObjectTargetConflict,
    OperationOwnershipError,
)
from models.media_types import MediaReference, UnsupportedReference, select_for_protocol
from storage import StorageError, get_storage

OWNER = "alice"
OWNER_KEY = media_refs.owner_key(OWNER)
TEST_BUCKET_PREFIX = "videoclaw-test"


def _user(login: str = OWNER) -> types.SimpleNamespace:
    return types.SimpleNamespace(login=login, owner_key=media_refs.owner_key(login), is_admin=False)


def _now() -> int:
    return int(time.time() * 1000)


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    code_root = tmp_path / "code"
    result_root = code_root / "result"
    temp_root = code_root / "temp"
    work_root = code_root / "work" / "executions"
    for path in (result_root, temp_root, work_root, tmp_path / "sessions"):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "CODE_DIR", str(code_root))
    monkeypatch.setattr(settings, "RESULT_DIR", str(result_root))
    monkeypatch.setattr(settings, "TEMP_DIR", str(temp_root))
    monkeypatch.setattr(settings, "SESSION_DIR", str(tmp_path / "sessions"))
    refs_root = code_root / "work" / "_refs"
    refs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(media_refs, "_REFS_ROOT", refs_root)
    assert settings.STORAGE_BUCKET.startswith(TEST_BUCKET_PREFIX)
    yield result_root


@pytest.fixture(autouse=True)
def cleanup_objects():
    """对象一律先登记（object_operations prepared 或此清单）后创建；收尾按登记删除并显式报告失败。"""
    created: list[tuple[str, str]] = []
    yield created
    targets = set(created)
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT bucket, object_key FROM object_operations WHERE target_kind = 'minio'")
            targets.update((str(row["bucket"]), str(row["object_key"])) for row in cur.fetchall())
    failures = []
    for bucket, key in sorted(targets):
        if not bucket.startswith(TEST_BUCKET_PREFIX):
            failures.append((bucket, key, "refusing to touch a non-test bucket"))
            continue
        try:
            get_storage().delete(key, bucket=bucket)
            if get_storage().stat(key, bucket=bucket) is not None:
                failures.append((bucket, key, "still present"))
        except Exception as exc:
            failures.append((bucket, key, str(exc)))
    if failures:
        raise AssertionError(f"test cleanup incomplete: {failures}")


# ── helpers ──────────────────────────────────────────────
def _ensure_session(session_id: str, owner: str = OWNER) -> None:
    SessionRepo().insert_if_absent({
        "session_id": session_id,
        "owner": owner,
        "status": "idle",
        "current_stage": "init",
        "status_json": {},
        "stage_progress": {},
        "project_params": {},
    })


def _sid() -> str:
    sid = f"s-{uuid.uuid4().hex[:8]}"
    _ensure_session(sid)
    return sid


def _commit(sid: str, **kwargs):
    return commit_session_change(session_id=sid, owner=OWNER, expected_revision=session_revision(sid), **kwargs)


def _manifest(session_id: str, *, stage: str = "character_design", execution_id: str = "",
              runtime_map=None) -> PublicationManifest:
    return PublicationManifest(
        owner=OWNER, owner_key=OWNER_KEY, resource_kind="sessions", resource_id=session_id,
        stage=stage, execution_id=execution_id or new_execution_id(), runtime_map=runtime_map,
    )


def _write(path: str, content: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def _write_work(manifest: PublicationManifest, name: str, content: bytes = b"work-bytes") -> str:
    return _write(os.path.join(manifest.work_root, name), content)


def _write_result(rel: str, content: bytes = b"result-bytes") -> tuple[str, str]:
    public = "/code/result/" + rel.lstrip("/")
    return _write(to_local(public), content), public


def _write_temp(owner_key: str, rel: str, content: bytes = b"upload") -> tuple[str, str]:
    public = f"users/{owner_key}/{rel}"
    return _write(os.path.realpath(os.path.join(settings.TEMP_DIR, public)), content), public


def _publish_one(session_id: str, *, stage: str = "character_design", content: bytes = b"x" * 24):
    manifest = _manifest(session_id, stage=stage)
    manifest.add(_write_work(manifest, "shot.png", content), kind="image")
    result = publish(manifest, boot_id="boot-a")
    return manifest, result, result["published"][0]


def _commit_published(sid: str, result, *, stage: str = "character_design", checkpoint=None):
    rows = build_file_rows(result, owner=OWNER, resource_kind="sessions", resource_id=sid, stage=stage)
    return _commit(
        sid, files=rows, retained_operation_ids=result["operation_ids"],
        stage_changes=[{"stage": stage, "promote": True, "checkpoint": checkpoint or {"ok": True}}],
    )


def _legacy_row(sid: str, *, owner: str = OWNER, content: bytes = b"legacy") -> tuple[str, str]:
    local, public = _write_result(f"tests/{uuid.uuid4().hex}/legacy.png", content)
    FileRepo().upsert_many([{
        "file_id": f"f-{uuid.uuid4().hex}", "owner": owner, "session_id": sid,
        "task_id": None, "record_id": None, "stage": "reference_generation", "kind": "image",
        "storage_kind": "local", "bucket": None, "object_key": public.lstrip("/"),
        "public_path": public, "local_path": local, "media_type": "image/png",
        "size_bytes": len(content),
    }])
    return local, public


def _register_orphan(*, boot_id: str, content: bytes = b"orphan") -> tuple[str, str]:
    """先登记 prepared，再上传（与发布层同序）。"""
    key = f"tests-cleanup/{uuid.uuid4().hex}.bin"
    local, _ = _write_result(f"tests/{uuid.uuid4().hex}/orphan.bin", content)
    op_id = ObjectOperationRepo().register_prepared(
        owner=OWNER, target_kind="minio", bucket=settings.STORAGE_BUCKET, object_key=key,
        public_path=f"/code/result/_media/{key}", local_paths=[],
        domain="sessions", resource_id="s-x", stage="character_design",
        execution_id=new_execution_id(), boot_id=boot_id, reason="publish",
    )
    get_storage().upload(local, key, "application/octet-stream", bucket=settings.STORAGE_BUCKET)
    return op_id, key


def _tx():
    return database.transaction()


def _state(op_id: str) -> str:
    return str((ObjectOperationRepo().get(op_id) or {}).get("state"))


def _exists(key: str, bucket: str | None = None) -> bool:
    return get_storage().stat(key, bucket=bucket or settings.STORAGE_BUCKET) is not None


# ── 13.1 migration markers ─────────────────────────────────
def test_migration_runner_records_per_file_markers():
    database.init_db()
    database.init_db()
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            names = [row["name"] for row in cur.fetchall()]
    assert "001_init.sql" in names and "002_core_storage.sql" in names
    assert len(names) == len(set(names))
    assert database.schema_version() == "2"


# ── 13.6 operation log state machine ──────────────────────
def test_prepared_target_is_never_reassigned():
    repo = ObjectOperationRepo()
    ident = dict(
        owner=OWNER, target_kind="minio", bucket=settings.STORAGE_BUCKET,
        object_key=f"tests-ops/{uuid.uuid4().hex}.bin", domain="sessions", resource_id="s-1",
        stage="character_design", execution_id="ex-1", file_id="f-1",
    )
    op = repo.register_prepared(**ident)
    assert repo.register_prepared(**ident) == op  # 同一 prepared 身份幂等
    for changed in ({"execution_id": "ex-2"}, {"owner": "bob"}, {"file_id": "f-2"}):
        with pytest.raises(ObjectTargetConflict):
            repo.register_prepared(**{**ident, **changed})
    with _tx() as conn, conn.cursor() as cur:
        assert repo.mark_retained([op], cur=cur) == 1
    with pytest.raises(ObjectTargetConflict):
        repo.register_prepared(**ident)  # retained 目标不可再次作为新上传
    with pytest.raises(ValueError):
        repo.register_prepared(**{**ident, "object_key": "x", "target_kind": "local"})
    with pytest.raises(ValueError):
        repo.register_prepared(**{**ident, "bucket": LOCAL_BUCKET})
    with pytest.raises(ValueError):
        repo.register_prepared(owner=OWNER, target_kind="local", bucket=LOCAL_BUCKET,
                               object_key="/code/result/a.png", local_paths=[])


def test_claim_for_delete_is_a_lease():
    repo = ObjectOperationRepo()
    op = repo.register_prepared(owner=OWNER, target_kind="minio", bucket=settings.STORAGE_BUCKET,
                                object_key=f"tests-ops/{uuid.uuid4().hex}.bin")
    assert repo.claim_for_delete(op, now=1_000) is True
    assert repo.claim_for_delete(op, now=1_000) is False  # 租约内第二个删除者拿不到
    assert repo.claim_for_delete(op, now=1_000 + CLAIM_LEASE_MS) is True  # 删除者崩溃后租约到期
    kept = repo.register_prepared(owner=OWNER, target_kind="minio", bucket=settings.STORAGE_BUCKET,
                                  object_key=f"tests-ops/{uuid.uuid4().hex}.bin")
    with _tx() as conn, conn.cursor() as cur:
        repo.mark_retained([kept], cur=cur)
    assert repo.claim_for_delete(kept) is False  # retained 永不被补偿/清理认领


def test_register_delete_rules(cleanup_objects):
    sid = _sid()
    _, result, item = _publish_one(sid)
    _commit_published(sid, result)
    target = dict(owner=OWNER, target_kind="minio", bucket=item["bucket"], object_key=item["object_key"])
    with pytest.raises(ValueError):
        register_delete_snapshot(cur=None, **target)  # 只能在父删除事务内登记
    with _tx() as conn, conn.cursor() as cur:
        with pytest.raises(ValueError):  # 必须声明删除方资源，空 scope 不能绕过归属检查
            register_delete_snapshot(cur=cur, **target)
    cache = str(media_refs._ref_target(item["public_path"], OWNER_KEY))
    with _tx() as conn, conn.cursor() as cur:
        with pytest.raises(OperationOwnershipError):
            register_delete_snapshot(cur=cur, **{**target, "owner": "bob"}, domain="sessions", resource_id=sid)
    with _tx() as conn, conn.cursor() as cur:
        with pytest.raises(OperationOwnershipError):  # 其他资源不能登记删除别人拥有的对象
            register_delete_snapshot(cur=cur, **target, domain="sessions", resource_id="s-other")
    assert _state(item["operation_id"]) == "retained"
    with _tx() as conn, conn.cursor() as cur:
        first = register_delete_snapshot(cur=cur, **target, domain="sessions", resource_id=sid,
                                         local_paths=[cache])
        again = register_delete_snapshot(cur=cur, **target, domain="sessions", resource_id=sid)
    assert first["claimed"] is True and first["state"] == "delete_pending"
    assert again["claimed"] is False and again["operation_id"] == first["operation_id"]
    op = ObjectOperationRepo().get(first["operation_id"])
    assert item["local_path"] in op["local_paths_json"] and cache in op["local_paths_json"]
    # 文件行仍在（父还没删）时清理器只推迟；父删除级联后才删对象
    assert ObjectCleanupWorker(boot_id="boot-new").run_once()["deferred"] == 1
    assert _exists(item["object_key"])
    with _tx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE session_id = %s", (sid,))
    later = _now() + 5 * 60_000
    assert ObjectCleanupWorker(boot_id="boot-new").run_once(now=later)["deleted"] == 1
    assert _state(item["operation_id"]) == "done" and not _exists(item["object_key"])
    with _tx() as conn, conn.cursor() as cur:  # done 可以被同一资源重新登记（删除幂等）
        reopened = register_delete_snapshot(cur=cur, **target, domain="sessions", resource_id=sid)
    assert reopened["claimed"] is True and _state(item["operation_id"]) == "delete_pending"


def test_resource_delete_registration_survives_parent_cascade(cleanup_objects):
    sid = _sid()
    manifest, result, item = _publish_one(sid)
    _commit_published(sid, result)
    legacy_local, _ = _legacy_row(sid)
    cache = _write(str(media_refs._ref_target(item["public_path"], OWNER_KEY)), b"cached")
    assert os.path.isfile(item["local_path"])
    with _tx() as conn, conn.cursor() as cur:
        registered = register_resource_delete(cur, owner=OWNER, domain="sessions", resource_id=sid)
        cur.execute("DELETE FROM sessions WHERE session_id = %s", (sid,))
    assert len(registered) == 2 and FileRepo().list_by_session(sid) == []
    minio_op = ObjectOperationRepo().get(item["operation_id"])
    assert minio_op["state"] == "delete_pending"
    assert set(minio_op["local_paths_json"]) == {item["local_path"], cache}
    counts = ObjectCleanupWorker(boot_id="boot-new").run_once()
    assert counts["deleted"] == 2 and counts["failed"] == 0
    assert not _exists(item["object_key"])
    assert not os.path.exists(legacy_local) and not os.path.exists(cache)
    assert not os.path.exists(item["local_path"])
    assert os.path.isdir(manifest.work_root)  # 只删登记文件，不递归清执行根


def test_saved_copy_deletion_is_registered_under_saved_scope():
    saved = {"item_id": f"saved-{uuid.uuid4().hex[:8]}", "owner": OWNER,
             "bucket": settings.STORAGE_SAVED_BUCKET, "object_key": f"{OWNER_KEY}/saved/x.png"}
    with _tx() as conn, conn.cursor() as cur:
        result = register_saved_delete(cur, owner=OWNER, saved=saved)
        with pytest.raises(OperationOwnershipError):
            register_saved_delete(cur, owner="bob", saved=saved)
    op = ObjectOperationRepo().get(result["operation_id"])
    assert (op["domain"], op["resource_id"], op["state"]) == ("saved", saved["item_id"], "delete_pending")


def test_cleanup_deletes_orphan_prepared_from_previous_boot_only():
    old_op, old_key = _register_orphan(boot_id="boot-old")
    live_op, live_key = _register_orphan(boot_id="boot-current")
    counts = ObjectCleanupWorker(boot_id="boot-current").run_once()
    assert counts["deleted"] == 1
    assert not _exists(old_key) and _state(old_op) == "done"
    assert _exists(live_key) and _state(live_op) == "prepared"  # 当前进程在途发布不清


def test_cleanup_uses_exact_object_reference():
    sid = _sid()
    unrelated_op, unrelated_key = _register_orphan(boot_id="boot-old")
    referenced_op, referenced_key = _register_orphan(boot_id="boot-old")
    saved_op, saved_key = _register_orphan(boot_id="boot-old")
    base = {"owner": OWNER, "session_id": sid, "task_id": None, "record_id": None,
            "stage": "character_design", "kind": "artifact", "storage_kind": "minio",
            "bucket": settings.STORAGE_BUCKET, "local_path": None,
            "media_type": "application/octet-stream", "size_bytes": 6}
    FileRepo().upsert_many([
        # 同 public_path 但指向别的 key：不是该对象的引用
        {**base, "file_id": f"f-{uuid.uuid4().hex}", "object_key": "another-key",
         "public_path": f"/code/result/_media/{unrelated_key}"},
        {**base, "file_id": f"f-{uuid.uuid4().hex}", "object_key": referenced_key,
         "public_path": f"/code/result/_media/elsewhere/{referenced_key}"},
    ])
    SavedRepo().insert({
        "item_id": f"saved-{uuid.uuid4().hex[:8]}", "owner": OWNER,
        "work_id": f"w-{uuid.uuid4().hex[:8]}", "domain": "image", "title": "t",
        "source_bucket": settings.STORAGE_BUCKET, "source_key": saved_key,
        "bucket": settings.STORAGE_BUCKET, "object_key": saved_key, "size_bytes": 6,
    })
    counts = ObjectCleanupWorker(boot_id="boot-current").run_once()
    assert counts["deleted"] == 1 and counts["deferred"] == 2
    assert not _exists(unrelated_key) and _state(unrelated_op) == "done"
    assert _exists(referenced_key) and _state(referenced_op) == "prepared"
    assert _exists(saved_key) and _state(saved_op) == "prepared"
    deferred = ObjectOperationRepo().get(referenced_op)
    assert int(deferred["next_retry_at"]) > _now()  # 推迟项不再占据队首


def test_cleanup_retries_with_backoff_and_keeps_locator():
    op_id, key = _register_orphan(boot_id="boot-old")
    before = _now()
    with pytest.MonkeyPatch.context() as patch:
        # 适配器吞删除异常时也必须靠 stat 复核发现对象仍在
        patch.setattr(type(get_storage()), "delete", lambda self, object_key, bucket=None: None)
        counts = ObjectCleanupWorker(boot_id="boot-new").run_once(now=before)
    assert counts["failed"] == 1 and _exists(key)
    op = ObjectOperationRepo().get(op_id)
    assert op["state"] == "delete_pending" and op["attempts"] == 1 and int(op["next_retry_at"]) > before
    assert (op["bucket"], op["object_key"]) == (settings.STORAGE_BUCKET, key) and op["last_error"]
    assert ObjectCleanupWorker(boot_id="boot-new").run_once(now=before)["scanned"] == 0
    later = int(op["next_retry_at"]) + 1
    assert ObjectCleanupWorker(boot_id="boot-new").run_once(now=later)["deleted"] == 1
    assert not _exists(key) and _state(op_id) == "done"


def test_cleanup_defers_pinned_object_and_pin_is_refused_while_claimed():
    op_id, key = _register_orphan(boot_id="boot-old")
    identity = object_identity(settings.STORAGE_BUCKET, key)
    assert CLAIMS.pin(identity)
    try:
        assert ObjectCleanupWorker(boot_id="boot-new").run_once()["deferred"] == 1
        assert _exists(key) and _state(op_id) == "prepared"
    finally:
        CLAIMS.unpin(identity)
    assert CLAIMS.claim(identity) is True
    try:
        assert CLAIMS.pin(identity) is False
        with pytest.raises(TargetBeingDeleted):
            with CLAIMS.pinned([identity]):
                pass
    finally:
        CLAIMS.release(identity)
    later = _now() + 5 * 60_000
    assert ObjectCleanupWorker(boot_id="boot-new").run_once(now=later)["deleted"] == 1


def test_local_cleanup_rules(tmp_path):
    result_root = os.path.realpath(settings.RESULT_DIR)
    inside = _write(os.path.join(result_root, "tests", "legacy.png"), b"x")
    outside = _write(str(tmp_path / "outside.txt"), b"keep")
    link = os.path.join(result_root, "tests", "link.png")
    os.symlink(outside, link)
    aux_in_result = _write(os.path.join(result_root, "tests", "aux.bin"), b"aux")
    with _tx() as conn, conn.cursor() as cur:
        legacy = register_delete_snapshot(cur=cur, owner=OWNER, target_kind="local", bucket=LOCAL_BUCKET,
                                          object_key=os.path.realpath(inside), local_paths=[inside],
                                          domain="sessions", resource_id="s-legacy")
        escaped = register_delete_snapshot(cur=cur, owner=OWNER, target_kind="local", bucket=LOCAL_BUCKET,
                                           object_key=os.path.realpath(link), local_paths=[link],
                                           domain="sessions", resource_id="s-legacy")
    orphan_op, orphan_key = _register_orphan(boot_id="boot-old")
    with _tx() as conn, conn.cursor() as cur:  # MinIO 目标附带 RESULT 路径：不随对象删除
        register_delete_snapshot(cur=cur, owner=OWNER, target_kind="minio", bucket=settings.STORAGE_BUCKET,
                                 object_key=orphan_key, local_paths=[aux_in_result],
                                 domain="sessions", resource_id="s-x")
    assert CLAIMS.pin(local_identity(inside))
    try:
        ObjectCleanupWorker(boot_id="boot-new").run_once()
        assert os.path.exists(inside) and _state(legacy["operation_id"]) == "delete_pending"
    finally:
        CLAIMS.unpin(local_identity(inside))
    later = _now() + 5 * 60_000
    ObjectCleanupWorker(boot_id="boot-new").run_once(now=later)
    assert not os.path.exists(inside) and _state(legacy["operation_id"]) == "done"
    assert os.path.exists(outside) and os.path.islink(link)  # symlink 指向结果根外：拒删
    escaped_op = ObjectOperationRepo().get(escaped["operation_id"])
    assert escaped_op["state"] == "delete_pending" and "outside result root" in escaped_op["last_error"]
    aux_op = ObjectOperationRepo().get(orphan_op)
    assert not _exists(orphan_key) and os.path.exists(aux_in_result)
    assert aux_op["state"] == "delete_pending" and "non-execution" in aux_op["last_error"]


def test_execution_root_cleanup_waits_for_in_flight_and_pins():
    sid = _sid()
    manifest = _manifest(sid)
    work_file = _write_work(manifest, "nested/a.png")
    repo = ObjectOperationRepo()
    op = repo.register_prepared(owner=OWNER, target_kind="minio", bucket=settings.STORAGE_BUCKET,
                                object_key=f"tests-ops/{uuid.uuid4().hex}.png",
                                execution_id=manifest.execution_id, local_paths=[work_file])
    args = (OWNER_KEY, "sessions", sid, manifest.execution_id)
    assert cleanup_execution_root(*args) is False  # 仍有在途 prepared
    assert repo.claim_for_delete(op) and repo.mark_done(op)
    assert CLAIMS.pin(local_identity(work_file))
    try:
        assert cleanup_execution_root(*args) is False  # 有 pin
    finally:
        CLAIMS.unpin(local_identity(work_file))
    assert cleanup_execution_root(*args) is True
    assert not os.path.exists(manifest.work_root)
    assert os.path.isdir(os.path.dirname(manifest.work_root))  # 只删本执行根


def test_symlinked_execution_namespace_is_never_trusted_or_removed(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_bytes(b"keep")
    execution_id = new_execution_id()
    root = execution_work_root(OWNER_KEY, "sessions", "s-link", execution_id)
    os.makedirs(os.path.dirname(root), exist_ok=True)
    os.symlink(str(victim), root)  # 执行根本身是链接
    assert cleanup_execution_root(OWNER_KEY, "sessions", "s-link", execution_id) is False
    with pytest.raises(PublicationError):
        _manifest("s-link", execution_id=execution_id)
    parent_link_root = execution_work_root(OWNER_KEY, "sessions", "s-parent", execution_id)
    os.symlink(str(victim), os.path.dirname(parent_link_root))  # 命名空间父级是链接
    os.makedirs(victim / execution_id)
    assert cleanup_execution_root(OWNER_KEY, "sessions", "s-parent", execution_id) is False
    assert (victim / "keep.txt").exists() and (victim / execution_id).is_dir()


# ── 13.7 commit / snapshot / cache ────────────────────────
def test_content_commit_requires_cas_and_valid_changes():
    sid = _sid()
    change = {"stage": "character_design", "promote": True, "checkpoint": {"a": 1}}
    with pytest.raises(CommitValidationError):
        commit_session_change(session_id=sid, owner=OWNER, expected_revision=None, stage_changes=[change])
    with pytest.raises(CommitConflict):
        commit_session_change(session_id=sid, owner=OWNER, expected_revision=5, stage_changes=[change])
    invalid = [
        [{"stage": "character_design", "promote": True}],  # 晋升必须带完整 payload
        [{"stage": "character_design", "checkpoint": {"a": 1}}],
        [{"stage": "character_design", "promote": True, "checkpoint": "x"}],
        [{"stage": "character_design", "checkpoint_payload": {}}],
        [change, change],
        [{"stage": "character_design", "working": {}, "clear_working": True}],
        [{"stage": "video_generation", "execution_state": {"action": "restart", "execution_id": "e"}}],
    ]
    for changes in invalid:
        with pytest.raises(CommitValidationError):
            _commit(sid, stage_changes=changes)
    with pytest.raises(CommitValidationError):
        _commit(sid, session_patch={"owner": "bob"})
    with pytest.raises(CommitValidationError):
        _commit(sid, session_patch={"unknown": 1})
    assert session_revision(sid) == 0 and StageArtifactRepo().list_checkpoints(sid) == {}


def test_commit_owner_and_parent_guards():
    sid = _sid()
    with pytest.raises(CommitConflict):
        commit_session_change(session_id=sid, owner="bob", expected_revision=0)
    with pytest.raises(CommitParentMissing):
        commit_session_change(session_id="missing", owner=OWNER, expected_revision=0)


def test_working_layer_is_not_a_checkpoint():
    sid = _sid()
    _commit(sid, stage_changes=[{"stage": "character_design", "working": {"characters": []}}])
    assert StageArtifactRepo().list_checkpoints(sid) == {}
    assert load_working(sid, "character_design")["working_json"] == {"characters": []}
    _commit(sid, stage_changes=[{"stage": "character_design", "promote": True,
                                 "checkpoint": {"characters": [{"id": "c1"}]}, "clear_working": True}])
    assert StageArtifactRepo().list_checkpoints(sid)["character_design"]["characters"][0]["id"] == "c1"
    assert load_working(sid, "character_design")["working_json"] is None


def test_commit_returns_complete_consistent_snapshot():
    sid = _sid()
    for stage, mark in (("character_design", "A"), ("reference_generation", "B")):
        _commit(sid, stage_changes=[{"stage": stage, "promote": True, "checkpoint": {"mark": mark}}])
    snapshot = _commit(sid, stage_changes=[{"stage": "character_design", "promote": True,
                                            "checkpoint": {"mark": "A2"}}])
    assert snapshot["checkpoints"] == {"character_design": {"mark": "A2"}, "reference_generation": {"mark": "B"}}
    assert snapshot["checkpoint_versions"] == {"character_design": 2, "reference_generation": 1}
    assert snapshot["revision"] == 3
    assert load_committed(sid) == snapshot


def test_files_and_operations_must_correspond(cleanup_objects):
    sid = _sid()
    manifest = _manifest(sid)
    manifest.add(_write_work(manifest, "a/shot.png", b"a" * 10))
    manifest.add(_write_work(manifest, "b/shot.png", b"b" * 12))
    result = publish(manifest, boot_id="boot-a")
    rows = build_file_rows(result, owner=OWNER, resource_kind="sessions", resource_id=sid,
                           stage="character_design")
    ops = result["operation_ids"]
    attempts = [
        dict(files=rows, retained_operation_ids=ops[:1]),  # 行多于操作
        dict(files=rows[:1], retained_operation_ids=ops),  # 操作多于行（未登记文件被保留）
        dict(files=[{**rows[0], "file_id": "forged"}, rows[1]], retained_operation_ids=ops),
        dict(files=[{**rows[0], "storage_kind": "local"}, rows[1]], retained_operation_ids=ops),
        dict(files=[{**rows[0], "public_path": "/code/result/x.png"}, rows[1]], retained_operation_ids=ops),
        dict(files=[{**rows[0], "owner": "bob"}, rows[1]], retained_operation_ids=ops),
    ]
    for kwargs in attempts:
        with pytest.raises(CommitValidationError):
            _commit(sid, **kwargs)
    other = _sid()
    with pytest.raises(CommitValidationError):  # 别的会话不能把本资源的发布操作改 retained
        _commit(other, files=[{**row, "session_id": other} for row in rows], retained_operation_ids=ops)
    assert {_state(op) for op in ops} == {"prepared"} and session_revision(sid) == 0
    committed = _commit(sid, files=rows, retained_operation_ids=ops)
    assert committed["revision"] == 1 and {_state(op) for op in ops} == {"retained"}
    assert {row["file_id"] for row in FileRepo().list_by_session(sid)} == {row["file_id"] for row in rows}
    with _tx() as conn, conn.cursor() as cur:  # 既有行只插不改绑
        with pytest.raises(CommitValidationError):
            _insert_new_files(cur, [{**rows[0], "session_id": other}])


def test_execution_layer_sequencing_and_terminal_states():
    sid = _sid()
    stage = "video_generation"

    def delta(execution_id: str, item: str, seq: int, **fields):
        return {"stage": stage, "execution": {"execution_id": execution_id,
                                              "active_items": {item: {"seq": seq, **fields}}}}

    _commit(sid, stage_changes=[{"stage": stage, "execution_state": {"action": "start", "execution_id": "ex-1"}}])
    _commit(sid, stage_changes=[delta("ex-1", "clips:c1", 1, status="running")])
    _commit(sid, stage_changes=[delta("ex-1", "clips:c2", 1, status="running")])  # 条目间可交错
    rev = session_revision(sid)
    for stale in (delta("ex-1", "clips:c1", 1, status="done"), delta("ex-0", "clips:c1", 9)):
        with pytest.raises(CommitStale):
            _commit(sid, stage_changes=[stale])
    assert session_revision(sid) == rev  # 重复/迟到事件不增版本
    with pytest.raises(CommitConflict):
        _commit(sid, stage_changes=[{"stage": stage, "execution_state": {"action": "start", "execution_id": "ex-2"}}])
    progress = commit_session_progress(session_id=sid, owner=OWNER, stage=stage, expected_execution_id="ex-1",
                                       session_patch={"stage_progress_json": {stage: 40}})
    assert progress["revision"] == rev and progress["stage_progress_json"] == {stage: 40}
    with pytest.raises(CommitStale):
        commit_session_progress(session_id=sid, owner=OWNER, stage=stage, expected_execution_id="ex-9",
                                session_patch={"stage_progress_json": {stage: 50}})
    with pytest.raises(CommitValidationError):
        commit_session_progress(session_id=sid, owner=OWNER, stage=stage, expected_execution_id="ex-1",
                                session_patch={"meta_json": {"idea": "x"}})
    _commit(sid, stage_changes=[{
        **delta("ex-1", "clips:c1", 2, status="done", selected="/code/result/_media/v1.mp4"),
        "execution": {"execution_id": "ex-1", "active_items": {"clips:c1": {"seq": 2, "status": "done"}},
                      "version_candidates": {"/code/result/_media/v1.mp4": {"item": "clips:c1"},
                                             "/code/result/_media/v0.mp4": {"item": "clips:c2"}}},
        "execution_state": {"action": "finish", "execution_id": "ex-1", "status": "completed",
                            "merged_candidates": ["/code/result/_media/v1.mp4"]},
    }])
    execution = load_working(sid, stage)["execution_json"]
    assert execution["status"] == "completed" and execution["active_items"] == {}
    assert set(execution["last_items"]) == {"clips:c1", "clips:c2"}
    assert list(execution["version_candidates"]) == ["/code/result/_media/v0.mp4"]  # 未合入候选保留
    with pytest.raises(CommitStale):
        _commit(sid, stage_changes=[delta("ex-1", "clips:c2", 5)])
    with pytest.raises(CommitStale):
        commit_session_progress(session_id=sid, owner=OWNER, stage=stage, expected_execution_id="ex-1",
                                session_patch={"stage_progress_json": {stage: 99}})
    _commit(sid, stage_changes=[{"stage": stage, "execution_state": {"action": "start", "execution_id": "ex-2"}}])
    restarted = load_working(sid, stage)
    assert restarted["execution_id"] == "ex-2" and restarted["execution_json"]["active_items"] == {}
    assert set(restarted["execution_json"]["last_items"]) == {"clips:c1", "clips:c2"}
    assert StageArtifactRepo().list_checkpoints(sid) == {}  # 执行层从不伪造 completed
    for reopened in ("ex-1", "ex-2"):  # 已结束/当前 execution 都不能被重复 start 重开
        with pytest.raises(CommitStale):
            _commit(sid, stage_changes=[{"stage": stage, "execution_state": {"action": "start",
                                                                              "execution_id": reopened}}])
    _commit(sid, stage_changes=[{"stage": stage, "execution_state": {
        "action": "finish", "execution_id": "ex-2", "status": "failed", "error": "boom"}}])
    with pytest.raises(CommitStale):
        _commit(sid, stage_changes=[{"stage": stage, "execution_state": {"action": "start", "execution_id": "ex-1"}}])
    assert load_working(sid, stage)["execution_json"]["finished_ids"] == ["ex-1"]
    with pytest.raises(CommitStale):
        merge_execution({}, {"execution_id": "ex-1", "active_items": {"x": {"seq": 1}}})
    assert rotate_execution({"active_items": {"a": {}}})["last_items"] == {"a": {}}


def test_cache_monotonic_progress_and_snapshot_revision():
    sid = _sid()
    for stage, mark in (("character_design", "A"), ("reference_generation", "B")):
        _commit(sid, stage_changes=[{"stage": stage, "promote": True, "checkpoint": {"mark": mark}}])
    calls = {"n": 0}

    def loader(session_id):
        calls["n"] += 1
        if calls["n"] == 1:  # 读计数之后、读快照之前另一提交推进了 revision
            _commit(session_id, stage_changes=[{"stage": "storyboard", "working": {"draft": 1}}])
        return load_committed(session_id)

    service = PersistenceService(loader=loader)
    snap = service.load(sid)
    assert snap["revision"] == 3 and service.cache.get(sid)[0] == 3  # 用快照自身 revision 安装
    assert service.load(sid) is snap and calls["n"] == 1
    committed = service.commit(session_id=sid, owner=OWNER, expected_revision=3, stage_changes=[
        {"stage": "video_generation", "execution_state": {"action": "start", "execution_id": "ex-1"}},
    ])
    assert service.load(sid) is committed and calls["n"] == 1
    service.commit_progress(session_id=sid, owner=OWNER, stage="video_generation",
                            expected_execution_id="ex-1", session_patch={"stage_progress_json": {"v": 40}})
    cached = service.load(sid)
    assert cached["stage_progress_json"] == {"v": 40} and calls["n"] == 1
    assert committed["stage_progress_json"] == {}  # 不原地修改既有快照
    cache = SnapshotCache()
    assert cache.install("s1", 2, {"v": 2, "updated_at": 5}) is True
    assert cache.install("s1", 1, {"v": 1, "updated_at": 9}) is False
    assert cache.install("s1", 2, {"v": 2, "updated_at": 5}) is False
    assert cache.install("s1", 2, {"v": 2, "updated_at": 6}) is True
    with pytest.raises(CommitParentMissing):
        service.load("missing")


def test_definite_failure_compensates_and_keeps_error_type(cleanup_objects):
    sid = _sid()
    _, result, item = _publish_one(sid)
    service = PersistenceService()
    with pytest.raises(CommitConflict):
        service.commit(published=result, session_id=sid, owner="bob", expected_revision=0)
    assert not _exists(item["object_key"]) and _state(item["operation_id"]) == "done"
    sid_broken = _sid()
    _, broken_result, broken_item = _publish_one(sid_broken)

    def compensation_db_down(published):
        raise psycopg.OperationalError("db down during compensation")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(persistence_service, "abort_publication", compensation_db_down)
        with pytest.raises(CommitConflict) as excinfo:  # 补偿失败不得替换原冲突类型
            service.commit(published=broken_result, session_id=sid_broken, owner=OWNER, expected_revision=7)
    assert isinstance(excinfo.value.compensation_error, psycopg.OperationalError)
    assert _state(broken_item["operation_id"]) == "prepared"  # 留在日志中，重启后由清理器处理
    sid2 = _sid()
    _, result2, item2 = _publish_one(sid2)
    rows = build_file_rows(result2, owner=OWNER, resource_kind="sessions", resource_id=sid2,
                           stage="character_design")
    service.commit(published=result2, session_id=sid2, owner=OWNER, expected_revision=0,
                   files=rows, retained_operation_ids=result2["operation_ids"])
    assert abort_publication(result2) == []  # retained 永不被补偿
    assert _exists(item2["object_key"]) and _state(item2["operation_id"]) == "retained"


def test_unknown_commit_outcome_is_resolved_from_operation_states(monkeypatch, cleanup_objects):
    real_commit = persistence_service.commit_session_change

    def committed_then_lost(**kwargs):
        real_commit(**kwargs)
        raise psycopg.OperationalError("connection lost after COMMIT")

    def lost_before_commit(**kwargs):
        raise psycopg.OperationalError("connection lost before COMMIT")

    def kwargs_for(sid, result):
        rows = build_file_rows(result, owner=OWNER, resource_kind="sessions", resource_id=sid,
                               stage="character_design")
        return dict(session_id=sid, owner=OWNER, expected_revision=0, files=rows,
                    retained_operation_ids=result["operation_ids"])

    service = PersistenceService()
    sid = _sid()
    _, result, item = _publish_one(sid)
    monkeypatch.setattr(persistence_service, "commit_session_change", committed_then_lost)
    snapshot = service.commit(published=result, **kwargs_for(sid, result))
    assert snapshot["revision"] == 1 and _exists(item["object_key"])  # 已提交：不补偿
    assert _state(item["operation_id"]) == "retained"

    sid2 = _sid()
    _, result2, item2 = _publish_one(sid2)
    monkeypatch.setattr(persistence_service, "commit_session_change", lost_before_commit)
    with pytest.raises(psycopg.OperationalError):
        service.commit(published=result2, **kwargs_for(sid2, result2))
    assert not _exists(item2["object_key"]) and _state(item2["operation_id"]) == "done"

    sid3 = _sid()
    _, result3, item3 = _publish_one(sid3)
    monkeypatch.setattr(persistence_service, "publication_outcome", lambda published: "unknown")
    with pytest.raises(CommitOutcomeUnknown) as excinfo:
        service.commit(published=result3, **kwargs_for(sid3, result3))
    assert excinfo.value.objects and _exists(item3["object_key"])  # 无法判定：不删，留清理器
    assert _state(item3["operation_id"]) == "prepared"


# ── 13.2/13.3 publication ─────────────────────────────────
def test_execution_root_layout_and_segment_guard():
    sid, execution_id = "s-layout", new_execution_id()
    root = execution_work_root(OWNER_KEY, "sessions", sid, execution_id)
    assert root.endswith(os.path.join("work", "executions", OWNER_KEY, "sessions", sid, execution_id))
    for bad in (("sessions", "../x"), ("sessions", "a/b"), ("other", sid)):
        with pytest.raises(PublicationError):
            execution_work_root(OWNER_KEY, bad[0], bad[1], execution_id)
    manifest = _manifest("s-other")
    assert manifest.work_root == execution_work_root(OWNER_KEY, "sessions", "s-other", manifest.execution_id)


def test_manifest_trust_boundary():
    sid = _sid()
    manifest = _manifest(sid)
    work_file = _write_work(manifest, "out.png")
    assert manifest.add(work_file) == work_file
    assert manifest.add(work_file) == work_file and len(manifest.entries) == 1  # 同 realpath 去重
    _, result_public = _write_result(f"tests/{uuid.uuid4().hex}/legacy.png")
    assert manifest.add(result_public) == result_public  # legacy：只复用，不上传
    _, temp_public = _write_temp(OWNER_KEY, f"{uuid.uuid4().hex}/up.png")
    assert manifest.add(temp_public) == temp_public
    sibling = _manifest(sid)  # 同 owner、同会话的另一个执行根也不是本次输出
    other_file = _write_work(sibling, "thumb.png")
    for bad in ("/etc/passwd", "relative/out.png", other_file, f"users/{'b' * 16}/x/up.png",
                f"users/{OWNER_KEY}/../../etc/passwd", os.path.join(settings.CODE_DIR, "work", "_refs", "x.png")):
        with pytest.raises(PublicationError):
            manifest.add(bad)


def test_same_basename_from_two_dirs_are_distinct_objects(cleanup_objects):
    sid = _sid()
    manifest = _manifest(sid)
    manifest.add(_write_work(manifest, "a/shot.png", b"1"))
    manifest.add(_write_work(manifest, "b/shot.png", b"22"))
    result = publish(manifest)
    keys = {item["object_key"] for item in result["published"]}
    assert len(keys) == 2 and {item["size_bytes"] for item in result["published"]} == {1, 2}


def test_publish_uploads_verifies_and_retains(cleanup_objects):
    sid = _sid()
    _, result, item = _publish_one(sid)
    assert item["public_path"] == "/code/result/_media/" + item["object_key"]
    assert len(item["file_id"]) == 32 and item["file_id"] in item["object_key"]
    assert item["object_key"].startswith(f"{OWNER_KEY}/sessions/{sid}/character_design/{result['execution_id']}/")
    stat = get_storage().stat(item["object_key"], bucket=item["bucket"])
    assert stat and int(stat["size"]) == 24
    committed = _commit_published(sid, result)
    assert committed["revision"] == 1 and _state(item["operation_id"]) == "retained"
    ObjectCleanupWorker(boot_id="other").run_once()
    assert _state(item["operation_id"]) == "retained"
    again = _manifest(sid, stage="reference_generation")
    again.add(item["public_path"])
    reused = publish(again)
    assert reused["published"][0]["reused"] is True and reused["operation_ids"] == []
    assert build_file_rows(reused, owner=OWNER, resource_kind="sessions", resource_id=sid,
                           stage="reference_generation") == []
    with pytest.raises(PublicationError):  # 结果不能被别的资源拿去写行
        build_file_rows(result, owner=OWNER, resource_kind="sessions", resource_id="s-other", stage="x")


def test_legacy_indexed_rows_are_reused_unindexed_or_foreign_refused(cleanup_objects):
    sid = _sid()
    _, legacy_public = _legacy_row(sid)
    manifest = _manifest(sid, stage="video_generation")
    manifest.add(legacy_public)
    reused = publish(manifest)["published"][0]
    assert reused["reused"] and reused["storage_kind"] == "local" and reused["public_path"] == legacy_public
    bob_sid = f"s-{uuid.uuid4().hex[:8]}"
    _ensure_session(bob_sid, owner="bob")
    _, bob_public = _legacy_row(bob_sid, owner="bob")
    _, unindexed = _write_result(f"tests/{uuid.uuid4().hex}/{sid}/never-indexed.png")  # 路径含 sid 也不放行
    for public in (bob_public, unindexed):
        refused = _manifest(sid)
        refused.add(public)
        with pytest.raises(PublicationError):
            publish(refused)
    relative = _manifest(sid, stage="video_generation")  # legacy 相对形态先归一再按索引复用
    for form in ("result/" + legacy_public[len("/code/result/"):], legacy_public.lstrip("/")):
        assert relative.add(form) == legacy_public
    assert len(relative.entries) == 1 and publish(relative)["published"][0]["reused"] is True


def test_runtime_values_map_back_to_canonical(cleanup_objects):
    sid = _sid()
    _, result, item = _publish_one(sid)
    _commit_published(sid, result, checkpoint={"characters": [{"id": "c1", "selected": item["public_path"]}]})
    artifacts = {"character_design": {"characters": [{"id": "c1", "selected": item["public_path"]}]}}
    with materialize_stage_inputs("character_design", artifacts, _user(), target_ids=["c1"]) as inputs:
        resolved = inputs.runtime["character_design"]["characters"][0]["selected"]
        assert isinstance(resolved, ResolvedMedia) and str(resolved) != item["public_path"]
        manifest = _manifest(sid, stage="reference_generation", runtime_map=inputs.runtime_map)
        assert manifest.add(resolved) == item["public_path"]
        assert manifest.add(str(resolved)) == item["public_path"]
        assert publish(manifest)["published"][0]["reused"] is True
        bare = _manifest(sid, stage="reference_generation")
        with pytest.raises(PublicationError):  # 没有运行映射的缓存路径不得入 DB
            bare.add(str(resolved))


def test_source_change_during_upload_is_rejected_and_compensated(monkeypatch, cleanup_objects):
    sid = _sid()
    manifest = _manifest(sid)
    path = _write_work(manifest, "grow.png", b"12345")
    manifest.add(path)
    real_upload = type(get_storage()).upload

    def upload_then_append(self, local_path, object_key, media_type, bucket=None):
        outcome = real_upload(self, local_path, object_key, media_type, bucket=bucket)
        with open(local_path, "ab") as handle:
            handle.write(b"late-write")
        return outcome

    monkeypatch.setattr(type(get_storage()), "upload", upload_then_append)
    with pytest.raises(PublicationError, match="source changed"):
        publish(manifest, boot_id="boot-a")
    states = {row["state"] for row in _all_ops()}
    assert states == {"done"}


def test_publisher_does_not_upload_after_parent_delete_registered(monkeypatch, cleanup_objects):
    sid = _sid()
    manifest = _manifest(sid)
    manifest.add(_write_work(manifest, "late.png"))
    real_register = ObjectOperationRepo.register_prepared
    uploads = []

    def register_then_parent_deleted(self, **kwargs):
        op_id = real_register(self, **kwargs)
        with _tx() as conn, conn.cursor() as cur:  # 真实父删除链：登记本资源全部对象后删父行
            registered = register_resource_delete(cur, owner=OWNER, domain="sessions", resource_id=sid)
            cur.execute("DELETE FROM sessions WHERE session_id = %s", (sid,))
        assert [item["operation_id"] for item in registered] == [op_id]
        return op_id

    monkeypatch.setattr(ObjectOperationRepo, "register_prepared", register_then_parent_deleted)
    monkeypatch.setattr(type(get_storage()), "upload", lambda self, *a, **k: uploads.append(a))
    with pytest.raises(PublicationError, match="no longer prepared"):
        publish(manifest)
    assert uploads == []
    assert {row["state"] for row in _all_ops()} == {"done"}


def test_staging_rows_of_deleted_resource_keep_their_window():
    repo = ObjectOperationRepo()
    sid = _sid()
    staging = repo.register_prepared(owner=OWNER, target_kind="minio", bucket=settings.STORAGE_BUCKET,
                                     object_key=f"{OWNER_KEY}/sessions/{sid}/staging/e/{uuid.uuid4().hex}.png",
                                     domain="sessions", resource_id=sid, reason="staging",
                                     not_before=_now() + 3_600_000)
    with _tx() as conn, conn.cursor() as cur:
        assert register_resource_delete(cur, owner=OWNER, domain="sessions", resource_id=sid) == []
    assert _state(staging) == "prepared"


def test_publication_outcome_only_trusts_all_prepared_or_all_retained():
    from core.artifact_publisher import publication_outcome

    repo = ObjectOperationRepo()
    ops = [repo.register_prepared(owner=OWNER, target_kind="minio", bucket=settings.STORAGE_BUCKET,
                                  object_key=f"tests-ops/{uuid.uuid4().hex}.bin") for _ in range(2)]
    assert publication_outcome({"operation_ids": ops}) == "rolled_back"
    assert repo.claim_for_delete(ops[0])
    assert publication_outcome({"operation_ids": ops}) == "unknown"  # 可能是提交后又被删除
    assert publication_outcome({"operation_ids": ["op_missing"]}) == "unknown"


def test_publish_failure_compensates_only_claimed_objects(monkeypatch, cleanup_objects):
    sid = _sid()
    manifest = _manifest(sid)
    manifest.add(_write_work(manifest, "a.png"))
    manifest.add(_write_work(manifest, "b.png"))
    calls = {"n": 0}
    real_upload = type(get_storage()).upload

    def flaky(self, local_path, object_key, media_type, bucket=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise StorageError("boom")
        return real_upload(self, local_path, object_key, media_type, bucket=bucket)

    monkeypatch.setattr(type(get_storage()), "upload", flaky)
    with pytest.raises(PublicationError):
        publish(manifest, boot_id="boot-a")
    ops = _all_ops()
    assert len(ops) == 2 and {row["state"] for row in ops} == {"done"}
    assert not any(_exists(row["object_key"]) for row in ops)


def _all_ops() -> list:
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM object_operations ORDER BY created_at")
            return list(cur.fetchall())


# ── 13.4 consumption plan ─────────────────────────────────
def _story(scene_selected: dict, *, versions: dict | None = None, characters=None, settings_items=None,
           segment_extra: dict | None = None) -> dict:
    ids = list(scene_selected)
    return {
        "storyboard": {"episodes": [{"episode_number": 1, "segments": [
            {"segment_id": seg_id, **((segment_extra or {}).get(seg_id) or {})} for seg_id in ids
        ]}]},
        "reference_generation": {"scenes": [
            {"id": seg_id, "selected": scene_selected[seg_id], "versions": (versions or {}).get(seg_id, [])}
            for seg_id in ids
        ]},
        "character_design": {"characters": characters or [], "settings": settings_items or []},
        "video_generation": {"clips": []},
    }


def _published_public(sid: str, content: bytes = b"img") -> str:
    _, result, item = _publish_one(sid, content=content)
    _commit_published(sid, result)
    return item["public_path"]


def _plan(stage, artifacts, **kwargs):
    return [(t.source_stage, t.container, t.item_id, t.required, t.fallback_versions, t.label)
            for t in plan_targets(stage, artifacts, **kwargs)]


def test_video_frame_modes_plan():
    artifacts = _story({"s1": "", "s2": "", "s3": ""})
    assert _plan("video_generation", artifacts, mode="first_frame", target_ids=["s2"]) == [
        ("reference_generation", "scenes", "s2", True, True, "first_frame"),
    ]
    assert _plan("video_generation", artifacts, mode="start_end_frame", target_ids=["s1", "s3"]) == [
        ("reference_generation", "scenes", "s1", True, True, "first_frame"),
        ("reference_generation", "scenes", "s2", True, True, "last_frame"),  # 非末片尾帧必需
        ("reference_generation", "scenes", "s3", True, True, "first_frame"),  # 末片无尾帧：沿用回退
    ]
    # 相邻片段：下一片首帧即上一片尾帧，同一字段只物化一次
    assert _plan("video_generation", artifacts, mode="start_end_frame") == [
        ("reference_generation", "scenes", "s1", True, True, "first_frame"),
        ("reference_generation", "scenes", "s2", True, True, "last_frame+first_frame"),
        ("reference_generation", "scenes", "s3", True, True, "last_frame+first_frame"),
    ]


def test_start_end_frame_batch_materializes_shared_frames_once(cleanup_objects):
    sid = _sid()
    frames = {seg: _published_public(sid, seg.encode()) for seg in ("s1", "s2", "s3")}
    artifacts = _story(frames)
    with materialize_stage_inputs("video_generation", artifacts, _user(), mode="start_end_frame") as inputs:
        scenes = {s["id"]: s for s in inputs.runtime["reference_generation"]["scenes"]}
        for seg, public in frames.items():
            assert isinstance(scenes[seg]["selected"], ResolvedMedia)
            assert open(str(scenes[seg]["selected"]), "rb").read() == seg.encode()
            assert reverse_map(str(scenes[seg]["selected"]), inputs.runtime_map) == public
        assert len(inputs.pinned) == 3 and inputs.optional_issues == []


def test_video_reference_mode_matches_segment_assets_by_name():
    artifacts = _story(
        {"s1": ""},
        characters=[{"id": "c1", "name": "小明", "selected": "/code/result/_media/c1.png"},
                    {"id": "c2", "name": "小红", "selected": ""}],
        settings_items=[{"id": "loc1", "name": "教室", "selected": ""}],
        segment_extra={"s1": {"characters": ["小明"], "location": "教室A"}},
    )
    assert _plan("video_generation", artifacts, mode="reference") == [
        ("character_design", "characters", "c1", True, False, "reference"),  # 显式选中：必需、不换版
        ("character_design", "settings", "loc1", False, True, "reference"),  # 未选中：可选、取已索引版本
    ]
    assert _plan("reference_generation", artifacts) == [
        ("character_design", "characters", "c1", True, False, "reference"),
        ("character_design", "settings", "loc1", False, True, "reference"),
    ]


def test_character_prompt_rewrite_and_post_plans():
    artifacts = _story({"s1": "", "s2": ""})
    artifacts["character_design"]["characters"] = [{"id": "c1", "selected": "x"}]
    assert plan_targets("character_design", artifacts) == []  # 生成不读历史
    assert _plan("character_design", artifacts, target_ids=["c1"]) == [
        ("character_design", "characters", "c1", True, False, "edit"),
    ]
    assert plan_targets("prompt_rewrite", artifacts, grounding=False) == []
    assert _plan("prompt_rewrite", artifacts, mode="start_end_frame", target_ids=["s1"], grounding=True) == [
        ("reference_generation", "scenes", "s1", False, False, "grounding"),
        ("reference_generation", "scenes", "s2", False, False, "grounding"),
    ]
    artifacts["video_generation"]["clips"] = [
        {"id": "seg_1_1", "episode": 1, "selected": "/code/result/_media/a.mp4"},
        {"id": "seg_2_1", "name": "第2集-片段1", "selected": ""},
    ]
    artifacts["post_production"] = {"clips": [{"id": "seg_1_1", "selected": "/code/result/wrong.mp4"}]}
    assert _plan("post_production", artifacts, episodes=[1]) == [
        ("video_generation", "clips", "seg_1_1", True, False, "clip"),
    ]
    # 重拼集内片段未选中：报清单并中止，不悄悄跳过后生成不完整成片（D14）
    with pytest.raises(ConsumptionError) as excinfo:
        materialize_stage_inputs("post_production", artifacts, _user(), episodes=[2])
    assert [(i.target.item_id, i.reason) for i in excinfo.value.issues] == [("seg_2_1", "empty")]
    # 未重拼的集不参与：第 2 集的空片段不影响只重拼第 1 集（a.mp4 不可读则另报 not_found）
    assert [t.item_id for t in plan_targets("post_production", artifacts, episodes=[1])] == ["seg_1_1"]
    artifacts["video_generation"]["clips"][1]["selected"] = "/code/result/_media/missing.mp4"
    with pytest.raises(ConsumptionError) as excinfo:  # 选中片段缺失：报清单并中止，不跳过
        materialize_stage_inputs("post_production", artifacts, _user(), episodes=[2])
    assert [(i.target.item_id, i.reason) for i in excinfo.value.issues] == [("seg_2_1", "not_found")]


def test_video_selected_or_latest_indexed_version_is_materialized(cleanup_objects):
    sid = _sid()
    first = _published_public(sid, b"first")
    second = _published_public(sid, b"second")
    artifacts = _story({"s1": first, "s2": ""}, versions={"s2": ["/code/result/_media/old.png", second]})
    with materialize_stage_inputs("video_generation", artifacts, _user(), mode="first_frame") as inputs:
        scenes = {s["id"]: s for s in inputs.runtime["reference_generation"]["scenes"]}
        assert open(str(scenes["s1"]["selected"]), "rb").read() == b"first"
        assert open(str(scenes["s2"]["selected"]), "rb").read() == b"second"  # 只取最新已索引版本
        assert reverse_map(str(scenes["s2"]["selected"]), inputs.runtime_map) == second
        assert scenes["s2"]["versions"][0] == "/code/result/_media/old.png"  # 历史版本不物化
        assert len(inputs.pinned) == 2
    assert artifacts["reference_generation"]["scenes"][1]["selected"] == ""  # 持久态不变


def test_explicit_selection_missing_is_not_swapped(cleanup_objects):
    sid = _sid()
    gone = _published_public(sid, b"gone")
    fallback = _published_public(sid, b"fallback")
    row = FileRepo().get_by_public_path(gone)
    get_storage().delete(str(row["object_key"]), bucket=str(row["bucket"]))
    artifacts = _story({"s1": gone}, versions={"s1": [fallback]})
    with pytest.raises(ConsumptionError) as excinfo:
        materialize_stage_inputs("video_generation", artifacts, _user(), mode="first_frame")
    assert [(i.target.item_id, i.reason) for i in excinfo.value.issues] == [("s1", "missing")]
    assert FileRepo().get_by_public_path(gone)["missing_at"] is not None


def test_optional_grounding_degrades_and_unavailable_is_distinguished(monkeypatch, cleanup_objects):
    sid = _sid()
    ok = _published_public(sid, b"ok")
    artifacts = _story({"s1": "/code/result/_media/not-indexed.png", "s2": ok})
    with materialize_stage_inputs("prompt_rewrite", artifacts, _user(), mode="start_end_frame",
                                  target_ids=["s1"], grounding=True) as inputs:
        scenes = {s["id"]: s for s in inputs.runtime["reference_generation"]["scenes"]}
        assert scenes["s1"]["selected"] == ""  # 可选降级：运行副本置空，消费方走 text_only
        assert isinstance(scenes["s2"]["selected"], ResolvedMedia)
        assert [(i.target.item_id, i.reason) for i in inputs.optional_issues] == [("s1", "not_found")]

    def unreachable(value, user):
        raise media_refs.MediaUnavailable("minio unreachable")

    monkeypatch.setattr(media_refs, "materialize", unreachable)
    with pytest.raises(ConsumptionError) as excinfo:
        materialize_stage_inputs("video_generation", _story({"s1": ok}), _user(), mode="first_frame")
    assert excinfo.value.unavailable and excinfo.value.issues[0].reason == "unavailable"
    with materialize_stage_inputs("prompt_rewrite", _story({"s1": ok}), _user(), grounding=True) as inputs:
        assert inputs.optional_issues[0].reason == "unavailable"


def test_materialized_files_are_pinned_until_released(cleanup_objects):
    sid = _sid()
    public = _published_public(sid)
    artifacts = {"character_design": {"characters": [{"id": "c1", "selected": public}]}}
    inputs = materialize_stage_inputs("character_design", artifacts, _user(), target_ids=["c1"])
    identity = inputs.pinned[0]
    assert CLAIMS.claim(identity) is False  # 使用期间清理器无法认领
    inputs.release()
    assert CLAIMS.claim(identity) is True
    CLAIMS.release(identity)


def test_explicit_inputs_are_materialized_or_kept_remote():
    _, temp_public = _write_temp(OWNER_KEY, f"{uuid.uuid4().hex}/voice.mp3", b"voice")
    with materialize_stage_inputs(
        "video_generation", {}, _user(),
        explicit={"audio_reference_url": temp_public, "reference_videos": ["https://cdn.example.com/v.mp4"]},
    ) as inputs:
        assert open(str(inputs.explicit["audio_reference_url"]), "rb").read() == b"voice"
        assert inputs.explicit["reference_videos"] == ["https://cdn.example.com/v.mp4"]
    with pytest.raises(ConsumptionError):
        materialize_stage_inputs("video_generation", {}, _user(),
                                 explicit={"audio_reference_url": f"users/{OWNER_KEY}/missing.mp3"})


# ── 13.5 references and staging ───────────────────────────
def test_protocol_scheme_filtering():
    remote = MediaReference(public_path="https://x/y.mp4", remote_uri="https://x/y.mp4")
    assert select_for_protocol("remote_uri", remote)["remote_uri"] == "https://x/y.mp4"
    assert select_for_protocol("http_uri", remote)["http_uri"] == "https://x/y.mp4"
    with pytest.raises(UnsupportedReference):
        select_for_protocol("file_or_multipart", remote)
    data_uri = MediaReference(remote_uri="data:image/png;base64,AAA")
    with pytest.raises(UnsupportedReference):
        select_for_protocol("http_uri", data_uri)
    assert select_for_protocol("remote_uri", data_uri)["remote_uri"].startswith("data:")
    with pytest.raises(UnsupportedReference):
        select_for_protocol("http_uri", MediaReference(remote_uri="oss://bucket/key"))


def test_presign_ttl_uses_real_video_timeout(monkeypatch):
    monkeypatch.setattr(settings, "TIMEOUT_VIDEO", 10800, raising=False)
    monkeypatch.setattr(settings, "TIMEOUT_IMAGE", 10800, raising=False)
    assert presign_ttl() >= 11100
    monkeypatch.setattr(settings, "TIMEOUT_VIDEO", 8 * 24 * 3600, raising=False)
    with pytest.raises(PresignTTLTooLong):
        presign_ttl()


def test_managed_reference_multipart_and_lazy_presign(cleanup_objects):
    sid = _sid()
    public = _published_public(sid)
    os.remove(FileRepo().get_by_public_path(public)["local_path"])  # MinIO-only
    ref = build_media_reference(public, _user(), need_local=True)
    assert ref.uri_candidates == () and ref.presign_factory is not None  # 准备阶段不预签
    assert ref.local_path and os.path.isfile(ref.local_path)
    assert reference_kind(ref) == "managed_http"
    assert select_for_protocol("http_uri", ref)["http_uri"].startswith("http")
    assert needs_staging(public, _user()) is False
    with pytest.raises(Exception):
        build_media_reference(public, _user("bob"))


def test_staging_lifecycle(monkeypatch, cleanup_objects):
    sid = _sid()
    legacy_local, legacy_public = _legacy_row(sid)
    execution_id = new_execution_id()
    assert needs_staging(legacy_public, _user()) is True
    seen = {}
    real_upload = type(get_storage()).upload

    def upload_after_registration(self, local_path, object_key, media_type, bucket=None):
        op = ObjectOperationRepo().get_by_target(bucket, object_key)
        seen.update(state=op["state"], not_before=int(op["next_retry_at"]), media_type=media_type)
        return real_upload(self, local_path, object_key, media_type, bucket=bucket)

    monkeypatch.setattr(type(get_storage()), "upload", upload_after_registration)
    before = _now()
    staged_ref, staged = stage_for_http(legacy_public, _user(), domain="sessions", resource_id=sid,
                                        execution_id=execution_id, boot_id="boot-a")
    assert seen["state"] == "prepared" and seen["not_before"] > before + presign_ttl() * 1000
    assert seen["media_type"] == "image/png"
    assert staged.object_key.startswith(f"{OWNER_KEY}/sessions/{sid}/staging/{execution_id}/")
    op = ObjectOperationRepo().get(staged.operation_id)
    assert op["public_path"] == "" and op["local_paths_json"] == [] and op["reason"] == "staging"
    assert FileRepo().list_by_session(sid)[0]["public_path"] == legacy_public  # 不新建文件行
    first_not_before = int(op["next_retry_at"])
    time.sleep(0.01)
    uri = select_for_protocol("http_uri", staged_ref)["http_uri"]
    assert uri.startswith("http") and int(ObjectOperationRepo().get(staged.operation_id)["next_retry_at"]) > first_not_before

    ObjectCleanupWorker(boot_id="boot-a").run_once()  # not-before 之前不清
    assert _exists(staged.object_key) and _state(staged.operation_id) == "prepared"
    far = _now() + 30 * 24 * 3600 * 1000
    # 扫描取得资格后、认领前又签发了一次：认领必须重新评估最新 not-before
    with pytest.MonkeyPatch.context() as patch:
        def renewed_between_scan_and_claim(self, op):
            ObjectOperationRepo().extend_not_before(staged.operation_id, far + 3_600_000)
            return False

        patch.setattr(ObjectCleanupWorker, "_has_business_reference", renewed_between_scan_and_claim)
        assert ObjectCleanupWorker(boot_id="boot-a").run_once(now=far)["skipped"] == 1
    assert _exists(staged.object_key) and _state(staged.operation_id) == "prepared"
    far += 2 * 3_600_000
    with CLAIMS.pinned([staged.identity]):  # 远端仍在取图：过了 not-before 也延后
        assert ObjectCleanupWorker(boot_id="boot-a").run_once(now=far)["deferred"] == 1
    assert _exists(staged.object_key)
    assert release_staging(staged, remote_done=False) is False and _state(staged.operation_id) == "prepared"
    assert release_staging(staged, remote_done=True) is True
    assert ObjectCleanupWorker(boot_id="boot-a").run_once()["deleted"] == 1
    assert not _exists(staged.object_key) and _state(staged.operation_id) == "done"
    assert staged_ref.candidates() == []  # 已清理：不再签发
    assert os.path.isfile(legacy_local)  # 源 legacy 文件不受暂存清理影响
    with pytest.raises(StagingUnavailable):
        stage_for_http(legacy_public, _user(), domain="sessions", resource_id="../x", execution_id=execution_id)


def test_staging_upload_failure_hands_object_to_cleaner(monkeypatch):
    sid = _sid()
    _, legacy_public = _legacy_row(sid)

    def broken(self, *args, **kwargs):
        raise StorageError("upload broke midway")

    monkeypatch.setattr(type(get_storage()), "upload", broken)
    with pytest.raises(StagingUnavailable):
        stage_for_http(legacy_public, _user(), domain="sessions", resource_id=sid, execution_id=new_execution_id())
    (op,) = _all_ops()
    assert op["state"] == "delete_pending" and int(op["next_retry_at"]) <= _now()
