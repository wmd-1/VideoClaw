"""Pipeline task / sandbox record / saved copy commit boundary (design D16/D17).

Same contracts as the session commits (`commits.py`):
- new file rows are insert-only MinIO rows that correspond 1:1 to this resource's
  prepared publish operations, flipped to ``retained`` in the same transaction;
- a terminal task accepts no further writes (late progress/artifact callbacks raise
  ``CommitStale``), a deleted parent is never re-created by an upsert;
- deletes lock the parent row, let the caller register every owned object/local file
  (``before_delete(cur)``) and then delete the parent with its cascades.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

from db.database import transaction
from db.repositories._common import jsonb, jsonb_list, now_ms
from db.repositories.commits import (
    CommitConflict,
    CommitParentMissing,
    CommitStale,
    CommitValidationError,
    _insert_new_files,
    _validate_files_and_operations,
)
from db.repositories.operations import ObjectOperationRepo

TASK_TERMINAL = ("completed", "failed", "interrupted")
TASK_ACTIVE = ("pending", "running")
_TASK_PATCH_COLUMNS = {
    "status", "progress", "message", "error", "started_at", "completed_at", "duration_seconds",
}


def _retain(cur: Any, retained: List[str]) -> None:
    if not retained:
        return
    updated = ObjectOperationRepo().mark_retained(retained, cur=cur)
    if updated != len(retained):
        raise CommitValidationError(
            f"retained operations not all prepared: expected={len(retained)} updated={updated}"
        )


def _artifact_key(item: Dict[str, Any]) -> tuple:
    return (item.get("kind"), item.get("name"), item.get("path"))


def merge_artifacts(existing: Iterable[Dict[str, Any]], incoming: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (kind, name, path) 去重保序；同一 path 不同 kind/name 仍是不同产物条目。"""
    merged: List[Dict[str, Any]] = []
    seen = set()
    for item in list(existing or []) + list(incoming or []):
        if not isinstance(item, dict):
            continue
        key = _artifact_key(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(item))
    return merged


# ── Pipeline tasks ─────────────────────────────────────────────
def _lock_task(cur: Any, task_id: str, owner: Optional[str] = None) -> Dict[str, Any]:
    cur.execute("SELECT * FROM pipeline_tasks WHERE task_id = %s FOR UPDATE", (task_id,))
    row = cur.fetchone()
    if row is None:
        raise CommitParentMissing(task_id)
    if owner is not None and str(row["owner"]) != str(owner):
        raise CommitConflict(f"owner mismatch: row={row['owner']} commit={owner}")
    return dict(row)


def create_task_row(
    *,
    task_id: str,
    owner: str,
    pipeline: str,
    input_json: Dict[str, Any],
    output_dir: str,
    boot_id: str,
    message: str = "Task created",
) -> Dict[str, Any]:
    if not task_id or not owner:
        raise ValueError("task_id/owner required")
    ts = now_ms()
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pipeline_tasks
                  (task_id, owner, pipeline, status, progress, message, input_json, output_json,
                   artifacts_json, error, created_at, updated_at, started_at, completed_at,
                   duration_seconds, output_dir, boot_id)
                VALUES (%s, %s, %s, 'pending', 0, %s, %s, %s, %s, NULL, %s, %s, NULL, NULL, NULL, %s, %s)
                ON CONFLICT (task_id) DO NOTHING
                RETURNING *
                """,
                (task_id, owner, pipeline, message, jsonb(input_json), jsonb({}), jsonb_list([]),
                 ts, ts, output_dir, boot_id),
            )
            row = cur.fetchone()
            if row is None:
                raise CommitConflict(f"task id already exists: {task_id}")
            return dict(row)


def update_task_row(task_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """非终态任务的进度/状态写入；终态后的迟到回调抛 CommitStale，父行不存在抛 CommitParentMissing。"""
    unknown = set(patch) - _TASK_PATCH_COLUMNS
    if unknown:
        raise CommitValidationError(f"unknown task patch keys: {sorted(unknown)}")
    if "status" in patch and patch["status"] not in TASK_ACTIVE:
        raise CommitValidationError("terminal status must go through finish_task_row")
    with transaction() as conn:
        with conn.cursor() as cur:
            row = _lock_task(cur, task_id)
            if row["status"] in TASK_TERMINAL:
                raise CommitStale(f"task {task_id} already {row['status']}")
            if not patch:
                return row
            columns = sorted(patch)
            assignments = ", ".join(f"{column} = %s" for column in columns)
            cur.execute(
                f"UPDATE pipeline_tasks SET {assignments}, updated_at = %s WHERE task_id = %s RETURNING *",
                [patch[column] for column in columns] + [now_ms(), task_id],
            )
            return dict(cur.fetchone())


def commit_task_artifact(
    *,
    task_id: str,
    owner: str,
    artifact: Dict[str, Any],
    files: Iterable[Dict[str, Any]] = (),
    retained: Iterable[str] = (),
) -> Dict[str, Any]:
    """中间产物：发布文件行 + 产物条目同事务；终态/已删除任务拒绝（不复活）。"""
    file_rows = list(files)
    ops = list(dict.fromkeys(str(op) for op in retained))
    with transaction() as conn:
        with conn.cursor() as cur:
            row = _lock_task(cur, task_id, owner)
            if row["status"] in TASK_TERMINAL:
                raise CommitStale(f"task {task_id} already {row['status']}")
            _validate_files_and_operations(
                cur, domain="tasks", resource_id=task_id, owner=owner, files=file_rows, retained=ops,
            )
            _insert_new_files(cur, file_rows)
            _retain(cur, ops)
            artifacts = merge_artifacts(row.get("artifacts_json") or [], [artifact])
            cur.execute(
                "UPDATE pipeline_tasks SET artifacts_json = %s, updated_at = %s WHERE task_id = %s RETURNING *",
                (jsonb_list(artifacts), now_ms(), task_id),
            )
            return dict(cur.fetchone())


def finish_task_row(
    *,
    task_id: str,
    owner: str,
    status: str,
    output: Optional[Dict[str, Any]] = None,
    artifacts: Iterable[Dict[str, Any]] = (),
    files: Iterable[Dict[str, Any]] = (),
    retained: Iterable[str] = (),
    error: Optional[str] = None,
    message: str = "",
    progress: Optional[int] = None,
    duration_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """终态提交：status/output/final 与本次文件同事务；已提交的中间产物保留。"""
    if status not in TASK_TERMINAL:
        raise CommitValidationError(f"invalid terminal status: {status}")
    file_rows = list(files)
    ops = list(dict.fromkeys(str(op) for op in retained))
    with transaction() as conn:
        with conn.cursor() as cur:
            row = _lock_task(cur, task_id, owner)
            if row["status"] in TASK_TERMINAL:
                raise CommitStale(f"task {task_id} already {row['status']}")
            _validate_files_and_operations(
                cur, domain="tasks", resource_id=task_id, owner=owner, files=file_rows, retained=ops,
            )
            _insert_new_files(cur, file_rows)
            _retain(cur, ops)
            merged = merge_artifacts(row.get("artifacts_json") or [], artifacts)
            ts = now_ms()
            cur.execute(
                """UPDATE pipeline_tasks
                   SET status = %s, progress = %s, message = %s, output_json = %s, artifacts_json = %s,
                       error = %s, completed_at = %s, duration_seconds = %s, updated_at = %s
                   WHERE task_id = %s RETURNING *""",
                (
                    status,
                    int(row.get("progress") or 0) if progress is None else int(progress),
                    message or row.get("message"),
                    jsonb(output if output is not None else (row.get("output_json") or {})),
                    jsonb_list(merged),
                    error,
                    ts,
                    duration_seconds,
                    ts,
                    task_id,
                ),
            )
            return dict(cur.fetchone())


def delete_task_cascade(
    task_id: str,
    owner: str,
    before_delete: Optional[Callable[[Any], Any]] = None,
) -> bool:
    """pending/running 拒绝（CommitConflict），runner 继续；终态任务登记清理后删父行并级联。"""
    with transaction() as conn:
        with conn.cursor() as cur:
            try:
                row = _lock_task(cur, task_id, owner)
            except CommitParentMissing:
                return False
            if row["status"] in TASK_ACTIVE:
                raise CommitConflict(f"task {task_id} is {row['status']}")
            if before_delete is not None:
                before_delete(cur)
            cur.execute("DELETE FROM pipeline_tasks WHERE task_id = %s", (task_id,))
            return cur.rowcount > 0


# ── Sandbox records ────────────────────────────────────────────
def commit_sandbox_record(
    *,
    record: Dict[str, Any],
    files: Iterable[Dict[str, Any]] = (),
    retained: Iterable[str] = (),
    paths: Iterable[str] = (),
) -> Dict[str, Any]:
    """成功记录：sandbox_records + artifact_files + sandbox_paths 同事务（纯文本结果 files 为空）。"""
    record_id = str(record.get("record_id") or "")
    owner = str(record.get("owner") or "")
    if not record_id or not owner:
        raise ValueError("record_id/owner required")
    file_rows = list(files)
    ops = list(dict.fromkeys(str(op) for op in retained))
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sandbox_records
                  (record_id, owner, tool, model, input_json, output_json, files_json, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (record_id) DO NOTHING
                RETURNING *
                """,
                (
                    record_id, owner, str(record.get("tool") or ""), str(record.get("model") or ""),
                    jsonb(record.get("input")), jsonb(record.get("output")),
                    jsonb_list(record.get("files")), int(record.get("created_at_ms") or now_ms()),
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise CommitConflict(f"sandbox record already exists: {record_id}")
            _validate_files_and_operations(
                cur, domain="sandbox", resource_id=record_id, owner=owner, files=file_rows, retained=ops,
            )
            _insert_new_files(cur, file_rows)
            _retain(cur, ops)
            for path in dict.fromkeys(str(p) for p in paths if p):
                cur.execute(
                    "INSERT INTO sandbox_paths(path, record_id) VALUES (%s, %s) ON CONFLICT (path) DO NOTHING",
                    (path, record_id),
                )
            return dict(row)


def delete_sandbox_cascade(
    record_id: str,
    owner: str,
    before_delete: Optional[Callable[[Any], Any]] = None,
) -> bool:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT owner FROM sandbox_records WHERE record_id = %s FOR UPDATE", (record_id,))
            row = cur.fetchone()
            if row is None or str(row["owner"]) != owner:
                return False
            if before_delete is not None:
                before_delete(cur)
            cur.execute("DELETE FROM sandbox_records WHERE record_id = %s", (record_id,))
            return cur.rowcount > 0


# ── Saved copies ───────────────────────────────────────────────
def commit_saved_item(*, item: Dict[str, Any], operation_id: str) -> Dict[str, Any]:
    """收藏副本：saved_items 行与副本 prepared→retained 同事务。"""
    owner = str(item.get("owner") or "")
    if not owner or not item.get("work_id") or not operation_id:
        raise ValueError("saved item owner/work_id/operation_id required")
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM object_operations WHERE operation_id = %s FOR UPDATE", (operation_id,))
            op = cur.fetchone()
            if (
                op is None
                or str(op["owner"]) != owner
                or (str(op.get("domain") or ""), str(op.get("resource_id") or "")) != ("saved", str(item["item_id"]))
                or (str(op["bucket"]), str(op["object_key"])) != (str(item["bucket"]), str(item["object_key"]))
                or str(op["state"]) != "prepared"
            ):
                raise CommitValidationError(f"saved copy operation does not match: {operation_id}")
            cur.execute(
                """
                INSERT INTO saved_items
                  (item_id, owner, work_id, domain, title, source_bucket,
                   source_key, bucket, object_key, size_bytes, saved_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    str(item["item_id"]), owner, str(item["work_id"]), str(item.get("domain") or ""),
                    item.get("title"), str(item.get("source_bucket") or ""), str(item.get("source_key") or ""),
                    str(item["bucket"]), str(item["object_key"]), item.get("size_bytes"),
                    int(item.get("saved_at") or now_ms()),
                ),
            )
            row = dict(cur.fetchone())
            _retain(cur, [operation_id])
            return row


def delete_saved_cascade(
    saved_id: str,
    owner: str,
    before_delete: Optional[Callable[[Any, Dict[str, Any]], Any]] = None,
) -> bool:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM saved_items WHERE item_id = %s FOR UPDATE", (saved_id,))
            row = cur.fetchone()
            if row is None or str(row["owner"]) != owner:
                return False
            if before_delete is not None:
                before_delete(cur, dict(row))
            cur.execute("DELETE FROM saved_items WHERE item_id = %s", (saved_id,))
            return cur.rowcount > 0


__all__ = [
    "TASK_ACTIVE",
    "TASK_TERMINAL",
    "commit_saved_item",
    "commit_sandbox_record",
    "commit_task_artifact",
    "create_task_row",
    "delete_saved_cascade",
    "delete_sandbox_cascade",
    "delete_task_cascade",
    "finish_task_row",
    "merge_artifacts",
    "update_task_row",
]
