"""Object lifecycle log / cleanup queue (design D17).

Rows survive parent deletion (no business FK): they are the only durable evidence
of which bucket/key still needs cleanup after `artifact_files` is cascaded away.

State machine (each transition is a single conditional UPDATE):

    prepared ──mark_retained (business commit, same tx)──▶ retained
    prepared ──claim_for_delete (compensation / cleaner)──▶ delete_pending (leased)
    prepared|retained|done ──register_delete (parent delete tx)──▶ delete_pending
    delete_pending ──claim_for_delete (lease expired)──▶ delete_pending (leased)
    delete_pending ──mark_done──▶ done
    delete_pending ──mark_failure──▶ delete_pending (backoff)

A target (bucket, object_key) is never re-assigned to a new upload: registration
succeeds only for an unknown target or an idempotent retry of the same prepared
identity. `register_delete` may re-open a `done` row of the same owner/resource
(deleting is idempotent) but never hands the target to another purpose.

`claim_for_delete` is a lease: while `next_retry_at` lies in the future no second
deleter can claim a delete_pending row, and a crashed deleter's claim expires.
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Iterable, List, Optional

from db.database import transaction
from db.repositories._common import jsonb_list, now_ms

TARGET_KINDS = ("minio", "local")
LOCAL_BUCKET = "_local"
STAGING_REASON = "staging"
CLAIM_LEASE_MS = 10 * 60 * 1000
DELETE_DOMAINS = ("sessions", "tasks", "sandbox", "saved")
_IDENTITY_FIELDS = ("owner", "target_kind", "domain", "resource_id", "stage", "execution_id", "file_id")


class ObjectTargetConflict(RuntimeError):
    """The target bucket/key is already registered for a different purpose or state."""


class OperationOwnershipError(RuntimeError):
    """Caller tried to operate on another owner's (or another resource's) operation."""


def _row_id() -> str:
    return f"op_{uuid.uuid4().hex}"


def _run(cur: Any, fn):
    if cur is not None:
        return fn(cur)
    with transaction() as conn:
        with conn.cursor() as cursor:
            return fn(cursor)


def _validate_target(target_kind: str, bucket: str, object_key: str, local_paths: List[str]) -> None:
    if target_kind not in TARGET_KINDS:
        raise ValueError(f"unsupported target_kind: {target_kind}")
    if not bucket or not object_key:
        raise ValueError("bucket/object_key must not be empty")
    if target_kind == "local":
        # 旧 local 产物：哨兵桶 + 登记时 realpath；只允许精确文件，不接受目录/通配
        if bucket != LOCAL_BUCKET:
            raise ValueError("local targets must use the _local sentinel bucket")
        if not os.path.isabs(object_key) or os.path.realpath(object_key) != object_key:
            raise ValueError(f"local target must be a resolved absolute path: {object_key}")
        if not local_paths:
            raise ValueError("local target requires its exact registered file path")
    elif bucket == LOCAL_BUCKET:
        raise ValueError("the _local sentinel bucket is reserved for local targets")


def _merge_paths(existing: Any, extra: Iterable[str]) -> List[str]:
    current = [str(item) for item in existing] if isinstance(existing, list) else []
    return list(dict.fromkeys([*current, *[str(item) for item in extra if item]]))


class ObjectOperationRepo:
    # ── 新对象：prepared 登记（独立事务，必须在上传前已提交）──────────
    def register_prepared(
        self,
        *,
        owner: str,
        target_kind: str,
        bucket: str,
        object_key: str,
        domain: str = "",
        resource_id: str = "",
        stage: str = "",
        execution_id: str = "",
        boot_id: str = "",
        file_id: str = "",
        public_path: str = "",
        local_paths: Optional[List[str]] = None,
        reason: str = "",
        not_before: Optional[int] = None,
    ) -> str:
        """Register intent before any upload/copy. Idempotent only for the same prepared identity.

        Deliberately has no ``cur`` parameter: the row must be committed before the
        caller starts writing the object, otherwise a crash leaves an untracked object.
        """
        if not owner:
            raise ValueError("owner must not be empty")
        paths = list(local_paths or [])
        _validate_target(target_kind, bucket, object_key, paths)
        operation_id = _row_id()
        ts = now_ms()
        identity = {
            "owner": owner, "target_kind": target_kind, "domain": domain,
            "resource_id": resource_id, "stage": stage, "execution_id": execution_id,
            "file_id": file_id,
        }

        def run(cursor: Any) -> str:
            cursor.execute(
                """
                INSERT INTO object_operations
                  (operation_id, owner, domain, resource_id, stage, execution_id, boot_id,
                   file_id, target_kind, bucket, object_key, public_path, local_paths_json,
                   state, reason, attempts, next_retry_at, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'prepared',%s,0,%s,%s,%s)
                ON CONFLICT (bucket, object_key) DO NOTHING
                RETURNING operation_id
                """,
                (
                    operation_id, owner, domain, resource_id, stage, execution_id, boot_id,
                    file_id, target_kind, bucket, object_key, public_path, jsonb_list(paths),
                    reason, int(not_before if not_before is not None else ts), ts, ts,
                ),
            )
            inserted = cursor.fetchone()
            if inserted:
                return str(inserted["operation_id"])
            cursor.execute(
                "SELECT * FROM object_operations WHERE bucket = %s AND object_key = %s FOR UPDATE",
                (bucket, object_key),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise ObjectTargetConflict(f"target vanished during register: {bucket}/{object_key}")
            same = all(str(existing.get(name) or "") == str(identity[name] or "") for name in _IDENTITY_FIELDS)
            if existing["state"] == "prepared" and same:
                return str(existing["operation_id"])
            raise ObjectTargetConflict(
                f"target already registered (state={existing['state']}): {bucket}/{object_key}"
            )

        return _run(None, run)

    # ── 业务删除：登记清理意图（必须在父删除事务内调用）────────────
    def register_delete(
        self,
        *,
        cur: Any,
        owner: str,
        target_kind: str,
        bucket: str,
        object_key: str,
        public_path: str = "",
        local_paths: Optional[List[str]] = None,
        domain: str = "",
        resource_id: str = "",
        stage: str = "",
        file_id: str = "",
        reason: str = "delete",
    ) -> Dict[str, Any]:
        """Turn the target into `delete_pending` (insert one if no log row exists).

        Returns ``{operation_id, state, claimed}``; ``claimed`` is False when the row
        was already delete_pending (another deletion registered it first).

        The deleting resource (domain/resource_id) is mandatory and must be the one the
        operation was recorded for; only rows without any recorded scope accept a scope.
        """
        if cur is None:
            raise ValueError("register_delete must run inside the parent-delete transaction")
        if not owner:
            raise ValueError("owner must not be empty")
        if domain not in DELETE_DOMAINS or not resource_id:
            raise ValueError("register_delete requires the deleting domain/resource_id")
        paths = list(local_paths or [])
        _validate_target(target_kind, bucket, object_key, paths)
        ts = now_ms()

        cur.execute(
            "SELECT * FROM object_operations WHERE bucket = %s AND object_key = %s FOR UPDATE",
            (bucket, object_key),
        )
        existing = cur.fetchone()
        if existing is None:
            operation_id = _row_id()
            cur.execute(
                """
                INSERT INTO object_operations
                  (operation_id, owner, domain, resource_id, stage, execution_id, boot_id,
                   file_id, target_kind, bucket, object_key, public_path, local_paths_json,
                   state, reason, attempts, next_retry_at, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,'','',%s,%s,%s,%s,%s,%s,'delete_pending',%s,0,%s,%s,%s)
                """,
                (
                    operation_id, owner, domain, resource_id, stage, file_id, target_kind,
                    bucket, object_key, public_path, jsonb_list(paths), reason, ts, ts, ts,
                ),
            )
            return {"operation_id": operation_id, "state": "delete_pending", "claimed": True}
        if str(existing["owner"]) != owner:
            raise OperationOwnershipError(f"object belongs to another owner: {bucket}/{object_key}")
        if str(existing["target_kind"]) != target_kind:
            raise ObjectTargetConflict(f"target kind mismatch: {bucket}/{object_key}")
        existing_scope = (str(existing.get("domain") or ""), str(existing.get("resource_id") or ""))
        if existing_scope != ("", "") and existing_scope != (domain, resource_id):
            # 跨资源同 owner 引用不改变对象归属：只能由拥有它的资源登记删除
            raise OperationOwnershipError(
                f"object belongs to {existing_scope[0]}/{existing_scope[1]}, not {domain}/{resource_id}"
            )
        merged = _merge_paths(existing.get("local_paths_json"), paths)
        state = str(existing["state"])
        if state == "delete_pending":
            cur.execute(
                "UPDATE object_operations SET local_paths_json = %s, updated_at = %s WHERE operation_id = %s",
                (jsonb_list(merged), ts, str(existing["operation_id"])),
            )
            return {"operation_id": str(existing["operation_id"]), "state": state, "claimed": False}
        cur.execute(
            """UPDATE object_operations
               SET state = 'delete_pending', reason = %s, next_retry_at = %s, updated_at = %s,
                   local_paths_json = %s
               WHERE operation_id = %s AND state IN ('prepared', 'retained', 'done')""",
            (reason, ts, ts, jsonb_list(merged), str(existing["operation_id"])),
        )
        return {"operation_id": str(existing["operation_id"]), "state": "delete_pending", "claimed": True}

    def register_resource_prepared_delete(
        self, cur: Any, *, owner: str, domain: str, resource_id: str, reason: str = "delete",
    ) -> List[Dict[str, Any]]:
        """父删除事务内：本资源尚未建文件行的发布 prepared 转 delete_pending（暂存除外）。"""
        if cur is None:
            raise ValueError("must run inside the parent-delete transaction")
        ts = now_ms()
        cur.execute(
            """SELECT * FROM object_operations
               WHERE domain = %s AND resource_id = %s AND state = 'prepared'
                 AND COALESCE(reason, '') <> %s
               ORDER BY operation_id FOR UPDATE""",
            (domain, resource_id, STAGING_REASON),
        )
        rows = list(cur.fetchall())
        for row in rows:
            if str(row["owner"]) != owner:
                raise OperationOwnershipError(f"operation owned by another user: {row['operation_id']}")
        if rows:
            cur.execute(
                """UPDATE object_operations
                   SET state = 'delete_pending', reason = %s, next_retry_at = %s, updated_at = %s
                   WHERE operation_id = ANY(%s) AND state = 'prepared'""",
                (reason, ts, ts, [str(row["operation_id"]) for row in rows]),
            )
        return rows

    # ── 删除资格（租约）──────────────────────────────────────
    def claim_for_delete(self, operation_id: str, *, now: Optional[int] = None,
                         lease_ms: int = CLAIM_LEASE_MS, scan_boot_id: Optional[str] = None) -> bool:
        """取得删除租约；retained/done 一律失败。

        - 补偿（scan_boot_id=None）：本批 prepared 无条件可取，或租约已到期的 delete_pending；
        - 清理扫描：在同一条件 UPDATE 内重新评估资格（最新 not-before、旧 boot 或暂存），
          扫描后被续签的暂存对象无法被认领。
        """
        ts = int(now if now is not None else now_ms())

        def run(cursor: Any) -> bool:
            if scan_boot_id is None:
                cursor.execute(
                    """UPDATE object_operations
                       SET state = 'delete_pending', next_retry_at = %s, updated_at = %s
                       WHERE operation_id = %s
                         AND (state = 'prepared'
                              OR (state = 'delete_pending' AND COALESCE(next_retry_at, 0) <= %s))
                       RETURNING operation_id""",
                    (ts + int(lease_ms), now_ms(), operation_id, ts),
                )
            else:
                cursor.execute(
                    """UPDATE object_operations
                       SET state = 'delete_pending', next_retry_at = %s, updated_at = %s
                       WHERE operation_id = %s
                         AND COALESCE(next_retry_at, 0) <= %s
                         AND (state = 'delete_pending'
                              OR (state = 'prepared'
                                  AND (boot_id IS DISTINCT FROM %s OR reason = %s)))
                       RETURNING operation_id""",
                    (ts + int(lease_ms), now_ms(), operation_id, ts, scan_boot_id, STAGING_REASON),
                )
            return cursor.fetchone() is not None

        return bool(_run(None, run))

    def extend_not_before(self, operation_id: str, until: int) -> bool:
        """暂存租约：仍为 prepared 才能延长；已被认领删除则返回 False（调用方不得再签发）。"""

        def run(cursor: Any) -> bool:
            cursor.execute(
                """UPDATE object_operations
                   SET next_retry_at = GREATEST(COALESCE(next_retry_at, 0), %s), updated_at = %s
                   WHERE operation_id = %s AND state = 'prepared'
                   RETURNING operation_id""",
                (int(until), now_ms(), operation_id),
            )
            return cursor.fetchone() is not None

        return bool(_run(None, run))

    def release_staging(self, operation_id: str) -> bool:
        """推理确认终态且重试结束：暂存 prepared 立即转 delete_pending 交清理器。"""
        ts = now_ms()

        def run(cursor: Any) -> bool:
            cursor.execute(
                """UPDATE object_operations
                   SET state = 'delete_pending', next_retry_at = %s, updated_at = %s
                   WHERE operation_id = %s AND state = 'prepared' AND reason = %s
                   RETURNING operation_id""",
                (ts, ts, operation_id, STAGING_REASON),
            )
            return cursor.fetchone() is not None

        return bool(_run(None, run))

    # ── 查询 ─────────────────────────────────────────────────
    def get(self, operation_id: str) -> Optional[Dict[str, Any]]:
        return _run(None, lambda c: (c.execute(
            "SELECT * FROM object_operations WHERE operation_id = %s", (operation_id,)
        ), c.fetchone())[1])

    def get_states(self, operation_ids: Iterable[str]) -> Dict[str, str]:
        ids = list(dict.fromkeys(operation_ids))
        if not ids:
            return {}
        rows = _run(None, lambda c: (c.execute(
            "SELECT operation_id, state FROM object_operations WHERE operation_id = ANY(%s)", (ids,)
        ), c.fetchall())[1])
        return {str(row["operation_id"]): str(row["state"]) for row in rows}

    def get_by_target(self, bucket: str, object_key: str) -> Optional[Dict[str, Any]]:
        return _run(None, lambda c: (c.execute(
            "SELECT * FROM object_operations WHERE bucket = %s AND object_key = %s",
            (bucket, object_key),
        ), c.fetchone())[1])

    def list_eligible(self, boot_id: str, now: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """到期且具备资格的行（在 LIMIT 之前过滤，避免永久跳过项占据队首）。

        - delete_pending：到期（含租约过期）即可处理；
        - prepared：上一进程遗留的发布，或任意进程的暂存对象（not-before 即租约）。
        当前进程在途的发布 prepared 不在此列。
        """
        ts = int(now if now is not None else now_ms())

        def run(cursor: Any) -> List[Dict[str, Any]]:
            cursor.execute(
                """SELECT * FROM object_operations
                   WHERE (state = 'delete_pending'
                          AND (next_retry_at IS NULL OR next_retry_at <= %s))
                      OR (state = 'prepared'
                          AND (next_retry_at IS NULL OR next_retry_at <= %s)
                          AND (boot_id IS DISTINCT FROM %s OR reason = %s))
                   ORDER BY COALESCE(next_retry_at, 0) ASC, updated_at ASC
                   LIMIT %s""",
                (ts, ts, boot_id, STAGING_REASON, int(limit)),
            )
            return list(cursor.fetchall())

        return _run(None, run)

    def count_in_flight(self, execution_id: str) -> int:
        """工作根清理用：该 execution 仍有 prepared（上传在途或待补偿）时不可清根。"""
        row = _run(None, lambda c: (c.execute(
            "SELECT COUNT(*) AS n FROM object_operations WHERE execution_id = %s AND state = 'prepared'",
            (execution_id,),
        ), c.fetchone())[1])
        return int((row or {}).get("n") or 0)

    def reschedule(self, operation_id: str, next_retry_at: int) -> None:
        _run(None, lambda c: c.execute(
            "UPDATE object_operations SET next_retry_at = GREATEST(COALESCE(next_retry_at, 0), %s), "
            "updated_at = %s WHERE operation_id = %s AND state IN ('prepared', 'delete_pending')",
            (int(next_retry_at), now_ms(), operation_id),
        ))

    def verify_owned_prepared(
        self,
        operation_ids: Iterable[str],
        *,
        owner: str,
        domain: str,
        resource_id: str,
        cur: Any,
    ) -> List[Dict[str, Any]]:
        """提交事务内锁定并核对：均为本 owner/本资源的 prepared 发布操作（非暂存）。"""
        ids = list(dict.fromkeys(operation_ids))
        if not ids:
            return []
        cur.execute(
            "SELECT * FROM object_operations WHERE operation_id = ANY(%s) ORDER BY operation_id FOR UPDATE",
            (ids,),
        )
        rows = list(cur.fetchall())
        if len(rows) != len(ids):
            raise OperationOwnershipError("operation not found for this commit")
        for row in rows:
            if str(row["owner"]) != owner:
                raise OperationOwnershipError(f"operation owned by another user: {row['operation_id']}")
            if (str(row.get("domain") or ""), str(row.get("resource_id") or "")) != (domain, resource_id):
                raise OperationOwnershipError(f"operation belongs to another resource: {row['operation_id']}")
            if str(row["target_kind"]) != "minio" or str(row.get("reason") or "") == STAGING_REASON:
                raise ObjectTargetConflict(f"operation is not a publication: {row['operation_id']}")
            if str(row["state"]) != "prepared":
                raise ObjectTargetConflict(f"operation not prepared (state={row['state']}): {row['operation_id']}")
        return rows

    # ── 状态流转 ─────────────────────────────────────────────
    def mark_retained(self, operation_ids: List[str], cur: Any) -> int:
        ids = list(dict.fromkeys(operation_ids))
        if not ids:
            return 0
        cur.execute(
            """UPDATE object_operations SET state = 'retained', updated_at = %s
               WHERE operation_id = ANY(%s) AND state = 'prepared'""",
            (now_ms(), ids),
        )
        return cur.rowcount

    def mark_done(self, operation_id: str) -> bool:
        """只能结束 delete_pending；不能用 done 覆盖 retained/prepared。"""
        return bool(_run(None, lambda c: (c.execute(
            "UPDATE object_operations SET state = 'done', last_error = NULL, updated_at = %s "
            "WHERE operation_id = %s AND state = 'delete_pending' RETURNING operation_id",
            (now_ms(), operation_id),
        ), c.fetchone())[1]))

    def mark_failure(self, operation_id: str, error: str, next_retry_at: int) -> int:
        row = _run(None, lambda c: (c.execute(
            """UPDATE object_operations
               SET attempts = attempts + 1, last_error = %s, next_retry_at = %s, updated_at = %s
               WHERE operation_id = %s AND state = 'delete_pending'
               RETURNING attempts""",
            (str(error)[:2000], int(next_retry_at), now_ms(), operation_id),
        ), c.fetchone())[1])
        return int((row or {}).get("attempts") or 0)


__all__ = [
    "CLAIM_LEASE_MS",
    "LOCAL_BUCKET",
    "STAGING_REASON",
    "ObjectOperationRepo",
    "ObjectTargetConflict",
    "OperationOwnershipError",
]
