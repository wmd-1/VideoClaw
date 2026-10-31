"""Shared-cursor commit boundary (design D12/D13).

One transaction for checkpoint / working layer / execution layer / files / session
row / object operations. The commit returns a **complete** committed snapshot (all
checkpoints and working rows), because the cache install must never serve a partial
view.

Contracts enforced here (not left to callers):
- content commits always carry ``expected_revision`` (CAS) and bump the revision once;
- progress commits never touch content, never bump the revision, and only apply while
  the named stage still runs the expected execution;
- new file rows are insert-only MinIO rows that correspond 1:1 to this resource's
  prepared publish operations, which are flipped to ``retained`` in the same tx;
- execution deltas are per-item sequenced: duplicates, late events of a finished or
  superseded execution raise ``CommitStale`` and nothing is written.

Callers must not chain repository helpers for a business commit: helpers that open
their own transaction (e.g. FileRepo.upsert_many) are for single-table maintenance.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from db.database import transaction
from db.repositories._common import jsonb, now_ms
from db.repositories.operations import (
    ObjectOperationRepo,
    ObjectTargetConflict,
    OperationOwnershipError,
)

MEDIA_PUBLIC_ROOT = "/code/result/_media/"
EXECUTION_TERMINAL = ("completed", "failed", "interrupted")
FINISHED_IDS_KEPT = 50


class CommitConflict(RuntimeError):
    """Owner mismatch, revision CAS failure or execution ownership conflict."""


class CommitStale(CommitConflict):
    """Duplicate / out-of-order / late execution event: nothing was written."""


class CommitParentMissing(RuntimeError):
    """Parent row no longer exists; late callbacks must not resurrect it."""


class CommitValidationError(ValueError):
    """Malformed change, or file rows / operation ids that do not belong to this commit."""


_SESSION_PATCH_COLUMNS = {
    "status": "status",
    "current_stage": "current_stage",
    "status_json": "status_json",
    "stage_progress_json": "stage_progress_json",
    "meta_json": "meta_json",
    "boot_id": "boot_id",
}
_PROGRESS_PATCH_KEYS = {"status", "current_stage", "status_json", "stage_progress_json", "boot_id"}
_STAGE_CHANGE_KEYS = {
    "stage", "checkpoint", "promote", "working", "preview", "execution", "execution_state",
    "clear_working", "clear_preview", "base_version",
}
_EXECUTION_ACTIONS = ("start", "finish")


# ── 执行层纯函数 ─────────────────────────────────────────────
def rotate_execution(execution: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """旧 active 合入 last_items，清空 active（保留未合入的版本候选）。"""
    current = dict(execution or {})
    active = dict(current.get("active_items") or {})
    last = dict(current.get("last_items") or {})
    last.update(active)
    current["active_items"] = {}
    current["last_items"] = last
    current["version_candidates"] = dict(current.get("version_candidates") or {})
    return current


def start_execution(existing: Optional[Dict[str, Any]], execution_id: str, *, boot_id: str = "") -> Dict[str, Any]:
    """开始新执行；同一 execution_id 不能重开（重复 start 视为迟到事件）。"""
    if not execution_id:
        raise CommitValidationError("execution start requires execution_id")
    current = dict(existing or {})
    previous_id = str(current.get("execution_id") or "")
    finished_ids = [str(item) for item in current.get("finished_ids") or []]
    if execution_id == previous_id or execution_id in finished_ids:
        raise CommitStale(f"execution {execution_id} was already started on this stage")
    if current.get("status") == "running":
        raise CommitConflict(
            f"stage already runs execution {previous_id}; finish or sweep it first"
        )
    if previous_id:
        finished_ids = (finished_ids + [previous_id])[-FINISHED_IDS_KEPT:]
    started = rotate_execution(current)
    started.update({
        "execution_id": execution_id,
        "status": "running",
        "boot_id": boot_id,
        "last_seq": 0,
        "started_at": now_ms(),
        "finished_at": None,
        "error": None,
        "finished_ids": finished_ids,
    })
    return started


def merge_execution(existing: Optional[Dict[str, Any]], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """条目增量按 (execution, item) 事件序号幂等合并；重复/迟到抛 CommitStale。"""
    current = dict(existing or {})
    execution_id = str(incoming.get("execution_id") or "")
    if not execution_id:
        raise CommitValidationError("execution delta requires execution_id")
    if str(current.get("execution_id") or "") != execution_id:
        raise CommitStale(f"execution superseded: delta={execution_id} current={current.get('execution_id')}")
    if current.get("status") != "running":
        raise CommitStale(f"execution {execution_id} already {current.get('status')}")
    items = incoming.get("active_items") or {}
    if not isinstance(items, dict) or not items:
        raise CommitValidationError("execution delta requires active_items")
    active = dict(current.get("active_items") or {})
    top = int(current.get("last_seq") or 0)
    for key, item in items.items():
        if not isinstance(item, dict) or not isinstance(item.get("seq"), int):
            raise CommitValidationError(f"execution item {key} requires integer seq")
        previous = active.get(str(key)) or {}
        if int(item["seq"]) <= int(previous.get("seq") or 0):
            raise CommitStale(f"duplicate or out-of-order event for {key}: seq={item['seq']}")
        active[str(key)] = dict(item)
        top = max(top, int(item["seq"]))
    merged = dict(current)
    merged["active_items"] = active
    candidates = dict(current.get("version_candidates") or {})
    candidates.update(incoming.get("version_candidates") or {})
    merged["version_candidates"] = candidates
    merged["last_seq"] = top
    return merged


def finish_execution(
    existing: Optional[Dict[str, Any]],
    execution_id: str,
    status: str,
    *,
    error: Optional[str] = None,
    merged_candidates: Iterable[str] = (),
) -> Dict[str, Any]:
    """终态：active 移入 last_items；只移除已合入 payload 的候选。"""
    current = dict(existing or {})
    if status not in EXECUTION_TERMINAL:
        raise CommitValidationError(f"invalid terminal status: {status}")
    if str(current.get("execution_id") or "") != str(execution_id or ""):
        raise CommitStale(f"execution superseded: finish={execution_id} current={current.get('execution_id')}")
    if current.get("status") != "running":
        raise CommitStale(f"execution {execution_id} already {current.get('status')}")
    finished = rotate_execution(current)
    for key in merged_candidates:
        finished["version_candidates"].pop(str(key), None)
    finished.update({"status": status, "finished_at": now_ms(), "error": error})
    return finished


# ── 校验 ───────────────────────────────────────────────────
def _validate_changes(changes: List[Dict[str, Any]]) -> None:
    seen = set()
    for change in changes:
        if not isinstance(change, dict):
            raise CommitValidationError("stage change must be a dict")
        unknown = set(change) - _STAGE_CHANGE_KEYS
        if unknown:
            raise CommitValidationError(f"unknown stage change keys: {sorted(unknown)}")
        stage = str(change.get("stage") or "")
        if not stage:
            raise CommitValidationError("stage change requires stage")
        if stage in seen:
            raise CommitValidationError(f"duplicate stage change: {stage}")
        seen.add(stage)
        if bool(change.get("promote")) != ("checkpoint" in change):
            raise CommitValidationError(f"{stage}: checkpoint and promote must be given together")
        if change.get("promote") and not isinstance(change.get("checkpoint"), dict):
            raise CommitValidationError(f"{stage}: promoted checkpoint must be a payload dict")
        if "working" in change and change.get("clear_working"):
            raise CommitValidationError(f"{stage}: working and clear_working are exclusive")
        if "preview" in change and change.get("clear_preview"):
            raise CommitValidationError(f"{stage}: preview and clear_preview are exclusive")
        state = change.get("execution_state")
        if state is not None:
            if not isinstance(state, dict) or state.get("action") not in _EXECUTION_ACTIONS:
                raise CommitValidationError(f"{stage}: execution_state.action must be start|finish")
            if not state.get("execution_id"):
                raise CommitValidationError(f"{stage}: execution_state requires execution_id")


_PARENT_COLUMNS = {"sessions": "session_id", "tasks": "task_id", "sandbox": "record_id"}


def _validate_files_and_operations(
    cur: Any,
    *,
    domain: str,
    resource_id: str,
    owner: str,
    files: List[Dict[str, Any]],
    retained: List[str],
) -> None:
    """新文件行与本资源 prepared 发布操作一一对应（同 bucket/key/file_id/public_path）。"""
    parent = _PARENT_COLUMNS[domain]
    rows_by_target: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in files:
        file_id = str(row.get("file_id") or "")
        if str(row.get("owner") or "") != owner:
            raise CommitValidationError(f"file owner mismatch: {file_id}")
        if str(row.get(parent) or "") != resource_id:
            raise CommitValidationError(f"file parent mismatch: {file_id}")
        if any(row.get(column) for column in _PARENT_COLUMNS.values() if column != parent):
            raise CommitValidationError(f"file binds a different resource: {file_id}")
        if str(row.get("storage_kind") or "") != "minio":
            raise CommitValidationError(f"new business files must be MinIO rows: {file_id}")
        bucket, key, public = str(row.get("bucket") or ""), str(row.get("object_key") or ""), str(row.get("public_path") or "")
        if not file_id or not bucket or not key or public != MEDIA_PUBLIC_ROOT + key:
            raise CommitValidationError(f"file row is not an immutable published object: {file_id}")
        if (bucket, key) in rows_by_target:
            raise CommitValidationError(f"duplicate file target in commit: {bucket}/{key}")
        rows_by_target[(bucket, key)] = row
    try:
        operations = ObjectOperationRepo().verify_owned_prepared(
            retained, owner=owner, domain=domain, resource_id=resource_id, cur=cur,
        )
    except (OperationOwnershipError, ObjectTargetConflict) as exc:
        raise CommitValidationError(str(exc)) from exc
    ops_by_target = {(str(op["bucket"]), str(op["object_key"])): op for op in operations}
    if set(ops_by_target) != set(rows_by_target):
        raise CommitValidationError(
            "file rows and retained operations do not correspond: "
            f"rows={sorted(set(rows_by_target) - set(ops_by_target))} "
            f"ops={sorted(set(ops_by_target) - set(rows_by_target))}"
        )
    for target, op in ops_by_target.items():
        row = rows_by_target[target]
        if str(op.get("file_id") or "") != str(row["file_id"]) or str(op.get("public_path") or "") != str(row["public_path"]):
            raise CommitValidationError(f"file identity differs from its operation: {target[0]}/{target[1]}")


# ── 写入 ───────────────────────────────────────────────────
def _insert_new_files(cur: Any, files: List[Dict[str, Any]]) -> None:
    """只插入新行：既有 file_id/public_path 不得被提交改绑或覆盖。"""
    for row in files:
        cur.execute(
            """
            INSERT INTO artifact_files
              (file_id, owner, session_id, task_id, record_id, stage, kind,
               storage_kind, bucket, object_key, public_path, local_path,
               media_type, size_bytes, missing_at, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'minio', %s, %s, %s, %s, %s, %s, NULL, %s)
            ON CONFLICT DO NOTHING
            RETURNING file_id
            """,
            (
                str(row["file_id"]), str(row["owner"]), row.get("session_id") or None,
                row.get("task_id") or None, row.get("record_id") or None, row.get("stage"),
                str(row.get("kind") or "file"), str(row["bucket"]), str(row["object_key"]),
                str(row["public_path"]), row.get("local_path"), row.get("media_type"),
                row.get("size_bytes"), int(row.get("created_at") or now_ms()),
            ),
        )
        if cur.fetchone() is None:
            raise CommitValidationError(f"file row already exists (no rebinding): {row['file_id']}")


def _apply_session_patch(cur: Any, session_id: str, patch: Dict[str, Any], bump: int) -> None:
    if "owner" in patch:
        raise CommitValidationError("session_patch must not change owner")
    unknown = set(patch) - set(_SESSION_PATCH_COLUMNS)
    if unknown:
        raise CommitValidationError(f"unknown session_patch keys: {sorted(unknown)}")
    assignments: List[str] = []
    params: List[Any] = []
    for key, column in _SESSION_PATCH_COLUMNS.items():
        if key not in patch:
            continue
        assignments.append(f"{column} = %s")
        value = patch[key]
        params.append(jsonb(value) if key.endswith("_json") else value)
    assignments.append("content_revision = content_revision + %s")
    params.append(int(bump))
    assignments.append("updated_at = %s")
    params.append(now_ms())
    params.append(session_id)
    cur.execute(f"UPDATE sessions SET {', '.join(assignments)} WHERE session_id = %s", params)


def _read_working_row(cur: Any, session_id: str, stage: str) -> Dict[str, Any]:
    cur.execute(
        "SELECT * FROM session_stage_working WHERE session_id = %s AND stage = %s FOR UPDATE",
        (session_id, stage),
    )
    return cur.fetchone() or {}


def _upsert_working(cur: Any, session_id: str, stage: str, columns: Dict[str, Any]) -> None:
    if not columns:
        return
    columns = {**columns, "updated_at": now_ms()}
    names = list(columns)
    insert_columns = ["session_id", "stage", *names]
    placeholders = ", ".join(["%s"] * len(insert_columns))
    updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in names)
    cur.execute(
        f"""INSERT INTO session_stage_working ({', '.join(insert_columns)})
            VALUES ({placeholders})
            ON CONFLICT (session_id, stage) DO UPDATE SET {updates}""",
        [session_id, stage, *[columns[name] for name in names]],
    )


def _apply_stage_change(cur: Any, session_id: str, change: Dict[str, Any]) -> None:
    stage = str(change["stage"])
    existing = _read_working_row(cur, session_id, stage)
    columns: Dict[str, Any] = {}
    for key, column in (("working", "working_json"), ("preview", "preview_json")):
        if key in change:
            columns[column] = jsonb(change[key])
        if change.get(f"clear_{key}"):
            columns[column] = None
    if "base_version" in change:
        columns["base_version"] = int(change["base_version"])

    execution = existing.get("execution_json")
    changed = False
    state = change.get("execution_state") or {}
    if state.get("action") == "start":
        execution = start_execution(execution, str(state["execution_id"]), boot_id=str(state.get("boot_id") or ""))
        changed = True
    if "execution" in change:
        execution = merge_execution(execution, dict(change["execution"] or {}))
        changed = True
    if state.get("action") == "finish":
        execution = finish_execution(
            execution, str(state["execution_id"]), str(state.get("status") or ""),
            error=state.get("error"), merged_candidates=state.get("merged_candidates") or (),
        )
        changed = True
    if changed:
        columns["execution_json"] = jsonb(execution)
        columns["execution_id"] = str((execution or {}).get("execution_id") or "") or None

    _upsert_working(cur, session_id, stage, columns)
    if change.get("promote"):
        _promote_checkpoint(cur, session_id, stage, change["checkpoint"])


def _promote_checkpoint(cur: Any, session_id: str, stage: str, payload: Dict[str, Any]) -> None:
    ts = now_ms()
    cur.execute(
        """INSERT INTO session_stage_artifacts
             (session_id, stage, status, payload_json, version, completed_at, updated_at)
           VALUES (%s, %s, 'completed', %s, 1, %s, %s)
           ON CONFLICT (session_id, stage) DO UPDATE SET
             status = 'completed',
             payload_json = EXCLUDED.payload_json,
             error = NULL,
             version = session_stage_artifacts.version + 1,
             completed_at = EXCLUDED.completed_at,
             updated_at = EXCLUDED.updated_at""",
        (session_id, stage, jsonb(payload), ts, ts),
    )


# ── 快照 ───────────────────────────────────────────────────
def _snapshot_conn(cur: Any, session_id: str) -> Dict[str, Any]:
    cur.execute(
        """SELECT session_id, owner, content_revision, status, current_stage,
                  status_json, stage_progress_json, meta_json, boot_id, updated_at
           FROM sessions WHERE session_id = %s""",
        (session_id,),
    )
    session = cur.fetchone()
    if session is None:
        raise CommitParentMissing(session_id)
    cur.execute(
        """SELECT stage, payload_json, version FROM session_stage_artifacts
           WHERE session_id = %s AND version > 0 ORDER BY stage""",
        (session_id,),
    )
    checkpoint_rows = list(cur.fetchall())
    cur.execute(
        """SELECT stage, working_json, preview_json, execution_json, execution_id,
                  base_version, updated_at
           FROM session_stage_working WHERE session_id = %s ORDER BY stage""",
        (session_id,),
    )
    working = {
        str(row["stage"]): {
            "working": row["working_json"],
            "preview": row["preview_json"],
            "execution": row["execution_json"],
            "execution_id": row["execution_id"],
            "base_version": row["base_version"],
            "updated_at": row["updated_at"],
        }
        for row in cur.fetchall()
    }
    return {
        "session_id": session_id,
        "owner": session.get("owner"),
        "revision": int(session.get("content_revision") or 0),
        "status": session.get("status"),
        "current_stage": session.get("current_stage"),
        "status_json": session.get("status_json") or {},
        "stage_progress_json": session.get("stage_progress_json") or {},
        "meta_json": session.get("meta_json") or {},
        "boot_id": session.get("boot_id"),
        "updated_at": session.get("updated_at"),
        "checkpoints": {str(row["stage"]): row["payload_json"] for row in checkpoint_rows},
        "checkpoint_versions": {str(row["stage"]): int(row["version"]) for row in checkpoint_rows},
        "working": working,
    }


def _lock_session(cur: Any, session_id: str, owner: str) -> int:
    cur.execute(
        "SELECT owner, content_revision FROM sessions WHERE session_id = %s FOR UPDATE",
        (session_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise CommitParentMissing(session_id)
    if str(row["owner"]) != str(owner):
        raise CommitConflict(f"owner mismatch: row={row['owner']} commit={owner}")
    return int(row["content_revision"] or 0)


# ── 提交入口 ────────────────────────────────────────────────
def commit_session_change(
    *,
    session_id: str,
    owner: str,
    expected_revision: Optional[int],
    stage_changes: Iterable[Dict[str, Any]] = (),
    files: Iterable[Dict[str, Any]] = (),
    session_patch: Optional[Dict[str, Any]] = None,
    retained_operation_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    """内容提交：强制 revision CAS，一个事务提交所有受影响阶段；返回完整 committed snapshot。"""
    if not session_id or not owner:
        raise ValueError("session_id/owner required")
    if expected_revision is None or isinstance(expected_revision, bool):
        raise CommitValidationError("content commit requires expected_revision (CAS)")
    patch = dict(session_patch or {})
    changes = list(stage_changes)
    file_rows = list(files)
    retained = list(dict.fromkeys(str(op) for op in retained_operation_ids))
    _validate_changes(changes)

    with transaction() as conn:
        with conn.cursor() as cur:
            current = _lock_session(cur, session_id, owner)
            if int(expected_revision) != current:
                raise CommitConflict(f"revision conflict: expected={expected_revision} actual={current}")
            _validate_files_and_operations(
                cur, domain="sessions", resource_id=session_id, owner=owner, files=file_rows, retained=retained,
            )
            for change in changes:
                _apply_stage_change(cur, session_id, change)
            _insert_new_files(cur, file_rows)
            if retained:
                updated = ObjectOperationRepo().mark_retained(retained, cur=cur)
                if updated != len(retained):
                    raise CommitValidationError(
                        f"retained operations not all prepared: expected={len(retained)} updated={updated}"
                    )
            _apply_session_patch(cur, session_id, patch, 1)
            snapshot = _snapshot_conn(cur, session_id)

    snapshot["retained_operations"] = retained
    return snapshot


def commit_session_progress(
    *,
    session_id: str,
    owner: str,
    stage: str,
    expected_execution_id: str,
    session_patch: Dict[str, Any],
) -> Dict[str, Any]:
    """进度提交：不改内容、不递增 revision；仅当该阶段仍在运行该 execution 时生效。"""
    if not session_id or not owner or not stage or not expected_execution_id:
        raise CommitValidationError("progress commit requires session/owner/stage/expected_execution_id")
    patch = dict(session_patch or {})
    disallowed = set(patch) - _PROGRESS_PATCH_KEYS
    if disallowed:
        raise CommitValidationError(f"progress-only commit patch keys: {sorted(disallowed)}")
    with transaction() as conn:
        with conn.cursor() as cur:
            revision = _lock_session(cur, session_id, owner)
            row = _read_working_row(cur, session_id, stage)
            execution = row.get("execution_json") or {}
            if str(row.get("execution_id") or "") != expected_execution_id or execution.get("status") != "running":
                raise CommitStale(
                    f"progress for {stage}/{expected_execution_id} ignored: "
                    f"current={row.get('execution_id')} status={execution.get('status')}"
                )
            _apply_session_patch(cur, session_id, patch, 0)
            cur.execute(
                """SELECT content_revision, status, current_stage, status_json,
                          stage_progress_json, boot_id, updated_at
                   FROM sessions WHERE session_id = %s""",
                (session_id,),
            )
            fields = dict(cur.fetchone())
    fields["revision"] = int(fields.pop("content_revision") or revision)
    fields["status_json"] = fields.get("status_json") or {}
    fields["stage_progress_json"] = fields.get("stage_progress_json") or {}
    return fields


def load_committed(session_id: str) -> Dict[str, Any]:
    """读取完整 committed snapshot（与 commit 返回值同契约）；单一 REPEATABLE READ 快照。"""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = _snapshot_conn(cur, session_id)
    snapshot["retained_operations"] = []
    return snapshot


def load_working(session_id: str, stage: Optional[str] = None) -> Dict[str, Any]:
    """读取工作层（不参与 list_checkpoints；恢复时不把它当成功快照）。"""
    with transaction() as conn:
        with conn.cursor() as cur:
            if stage is None:
                cur.execute(
                    "SELECT * FROM session_stage_working WHERE session_id = %s ORDER BY stage",
                    (session_id,),
                )
                return {str(row["stage"]): row for row in cur.fetchall()}
            cur.execute(
                "SELECT * FROM session_stage_working WHERE session_id = %s AND stage = %s",
                (session_id, stage),
            )
            return cur.fetchone() or {}


def session_revision(session_id: str) -> Optional[int]:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content_revision FROM sessions WHERE session_id = %s", (session_id,))
            row = cur.fetchone()
    return None if row is None else int(row["content_revision"] or 0)


# ── 会话生命周期（创建/列表/恢复/删除）───────────────────────────────────────────
def create_session_row(
    *,
    session_id: str,
    owner: str,
    current_stage: str,
    status_json: Dict[str, Any],
    meta_json: Dict[str, Any],
    status: str = "idle",
) -> bool:
    """只插入；已存在返回 False（调用方走 CAS 提交更新，不覆盖 owner）。"""
    if not session_id or not owner:
        raise ValueError("session_id/owner required")
    ts = now_ms()
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sessions
                     (session_id, owner, status, current_stage, status_json, stage_progress_json,
                      meta_json, boot_id, created_at, updated_at, content_revision)
                   VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, NULL, %s, %s, 0)
                   ON CONFLICT (session_id) DO NOTHING""",
                (session_id, owner, status, current_stage, jsonb(status_json), jsonb(meta_json), ts, ts),
            )
            return cur.rowcount > 0


def list_session_summaries(owner: str) -> List[Dict[str, Any]]:
    """列表读模型：sessions 行 + 剧本有效标题（工作稿优先于正式快照）。"""
    if not owner:
        raise ValueError("owner is required")
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.session_id, s.owner, s.status_json, s.current_stage, s.meta_json,
                          s.stage_progress_json, s.updated_at, s.content_revision,
                          COALESCE(w.working_json->>'title', a.payload_json->>'title') AS title
                   FROM sessions s
                   LEFT JOIN session_stage_artifacts a
                     ON a.session_id = s.session_id AND a.stage = 'script_generation' AND a.version > 0
                   LEFT JOIN session_stage_working w
                     ON w.session_id = s.session_id AND w.stage = 'script_generation'
                   WHERE s.owner = %s
                   ORDER BY s.updated_at DESC""",
                (owner,),
            )
            return list(cur.fetchall())


def sessions_with_running() -> List[str]:
    """启动恢复用：状态图或执行层仍为 running 的会话。"""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT session_id FROM sessions
                   WHERE EXISTS (SELECT 1 FROM jsonb_each_text(COALESCE(status_json, '{}'::jsonb)) s
                                 WHERE s.value = 'running')
                   UNION
                   SELECT session_id FROM session_stage_working
                   WHERE execution_json->>'status' = 'running'"""
            )
            return sorted(str(row["session_id"]) for row in cur.fetchall())


def session_ids() -> Set[str]:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT session_id FROM sessions")
            return {str(row["session_id"]) for row in cur.fetchall()}


def delete_session_cascade(
    session_id: str,
    owner: str,
    before_delete: Optional[Callable[[Any], Any]] = None,
) -> bool:
    """同一事务：锁父行 → 登记清理定位（before_delete(cur)）→ 删父行并级联。"""
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT owner FROM sessions WHERE session_id = %s FOR UPDATE", (session_id,))
            row = cur.fetchone()
            if row is None:
                return False
            if str(row["owner"]) != owner:
                raise CommitConflict(f"owner mismatch: row={row['owner']} delete={owner}")
            if before_delete is not None:
                before_delete(cur)
            cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
            return cur.rowcount > 0


__all__ = [
    "CommitConflict",
    "CommitParentMissing",
    "CommitStale",
    "CommitValidationError",
    "OperationOwnershipError",
    "commit_session_change",
    "commit_session_progress",
    "create_session_row",
    "delete_session_cascade",
    "finish_execution",
    "list_session_summaries",
    "load_committed",
    "load_working",
    "merge_execution",
    "rotate_execution",
    "session_ids",
    "session_revision",
    "sessions_with_running",
    "start_execution",
]
