"""Video dispatch queue repository: atomic batch enqueue and capacity checks.

Scheduling decisions, HTTP calls and probe logic do not live here. This module
only owns the PostgreSQL ledger so callers can rely on one short transaction.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from db.database import transaction
from db.repositories._common import jsonb, now_ms

# 单进程调度器仍可能有多线程/多请求同时入队；用事务级 advisory lock 把
# “查容量 + 查幂等键 + 插入”收成串行临界区。DB 唯一约束仍是最终防线。
_ENQUEUE_LOCK_KEY = 0x56494450  # "VIDP"
DEFAULT_MAX_QUEUED_JOBS = 1000
DEFAULT_QUEUE_TIMEOUT_SECONDS = 3600
DISPATCH_MODES = ("auto", "manual")
TERMINAL_JOB_STATES = ("succeeded", "failed", "cancelled")
ACTIVE_ATTEMPT_STATES = ("prepared", "submitting", "submitted", "unknown", "cancelled")


class QuarantineReleaseConflict(Exception):
    """隔离槽释放前置条件不满足（版本/远端未确认）。"""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class DispatchQueueError(Exception):
    """入队相关的可预期业务错误。"""


class DispatchConflict(DispatchQueueError):
    """同 owner/request_key/item_id 的输入指纹不一致。"""


class QueueFull(DispatchQueueError):
    """排队容量不足，整批未受理。"""


class SlotBusy(DispatchQueueError):
    """目标物理槽已被其他 attempt 占用。"""


class ReservationError(DispatchQueueError):
    """作业当前不允许预留（非 queued、已取消或已过期）。"""


@dataclass(frozen=True)
class EnqueueResult:
    jobs: List[Dict[str, Any]]
    created: int

    @property
    def idempotent(self) -> bool:
        return self.created == 0


class VideoDispatchRepo:
    def enqueue_batch(
        self,
        *,
        owner: str,
        batch_id: str,
        jobs: Iterable[Dict[str, Any]],
        queue_timeout_seconds: int = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        max_queued_jobs: int = DEFAULT_MAX_QUEUED_JOBS,
    ) -> EnqueueResult:
        """原子受理整批；同键同输入返回原记录，同键不同输入抛 DispatchConflict。"""
        owner = str(owner or "").strip()
        batch_id = str(batch_id or "").strip()
        if not owner or not batch_id:
            raise ValueError("owner/batch_id 不能为空")
        if queue_timeout_seconds <= 0 or max_queued_jobs <= 0:
            raise ValueError("queue_timeout_seconds/max_queued_jobs 必须为正整数")

        prepared = [_prepare_job(owner, batch_id, job) for job in jobs]
        if not prepared:
            raise ValueError("jobs 不能为空")
        keys = [(item["request_key"], item["item_id"]) for item in prepared]
        if len(set(keys)) != len(keys):
            raise ValueError("同一批次内 request_key/item_id 不能重复")

        accepted_at = now_ms()
        deadline = accepted_at + int(queue_timeout_seconds) * 1000

        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ENQUEUE_LOCK_KEY,))
                existing_by_key = self._load_existing(cur, owner, prepared)
                to_insert: List[Dict[str, Any]] = []
                for item in prepared:
                    key = (item["request_key"], item["item_id"])
                    existing = existing_by_key.get(key)
                    if existing is not None:
                        if _fingerprint_row(existing) != _fingerprint_item(item):
                            raise DispatchConflict(
                                "同一请求键提交了不同输入，拒绝复用既有作业"
                            )
                        continue
                    to_insert.append(item)

                if to_insert:
                    cur.execute(
                        "SELECT COUNT(*) AS count FROM video_dispatch_jobs WHERE state = 'queued'"
                    )
                    queued = int(cur.fetchone()["count"])
                    if queued + len(to_insert) > int(max_queued_jobs):
                        raise QueueFull("视频生成等待队列已满，整批未受理")

                for item in to_insert:
                    cur.execute(
                        """
                        INSERT INTO video_dispatch_jobs
                          (job_id, owner, batch_id, source_kind, source_id, execution_id,
                           item_id, request_key, dispatch_mode, profile_id, profile_revision,
                           snapshot_json, state, accepted_at, queue_deadline_at)
                        VALUES
                          (%(job_id)s, %(owner)s, %(batch_id)s, %(source_kind)s, %(source_id)s,
                           %(execution_id)s, %(item_id)s, %(request_key)s, %(dispatch_mode)s,
                           %(profile_id)s, %(profile_revision)s, %(snapshot_json)s, 'queued',
                           %(accepted_at)s, %(queue_deadline_at)s)
                        ON CONFLICT (owner, request_key, item_id) DO NOTHING
                        """,
                        {
                            **item,
                            "snapshot_json": jsonb(item["snapshot_json"]),
                            "accepted_at": accepted_at,
                            "queue_deadline_at": deadline,
                        },
                    )

                # 幂等重试也要返回原记录；这里按 owner + request_key 读回再按
                # (request_key, item_id) 过滤，避免把同批其他请求混入。
                all_request_keys = sorted({item["request_key"] for item in prepared})
                cur.execute(
                    "SELECT * FROM video_dispatch_jobs "
                    "WHERE owner = %s AND request_key = ANY(%s) "
                    "ORDER BY accepted_at, job_id",
                    (owner, all_request_keys),
                )
                key_set = set(keys)
                by_key = {
                    (item["request_key"], item["item_id"]): item for item in prepared
                }
                rows = [
                    row
                    for row in cur.fetchall()
                    if (str(row["request_key"]), str(row["item_id"])) in key_set
                ]
                if len(rows) != len(prepared):
                    raise DispatchQueueError("作业受理后无法完整读回")
                for row in rows:
                    key = (str(row["request_key"]), str(row["item_id"]))
                    if _fingerprint_row(row) != _fingerprint_item(by_key[key]):
                        raise DispatchConflict("同一请求键提交了不同输入，拒绝复用既有作业")
                return EnqueueResult(jobs=rows, created=len(to_insert))

    def reserve(
        self,
        *,
        job_id: str,
        resource_key: str,
        instance_id: str,
        token: Optional[str] = None,
        boot_id: Optional[str] = None,
        config_revision: Optional[str] = None,
        endpoint_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """短事务内锁定作业与槽位，写入 attempt 并返回带 token 的占位信息。"""
        job_id = str(job_id or "").strip()
        resource_key = str(resource_key or "").strip()
        instance_id = str(instance_id or "").strip()
        if not job_id or not resource_key or not instance_id:
            raise ValueError("job_id/resource_key/instance_id 不能为空")
        token = str(token or uuid.uuid4().hex)
        now = now_ms()

        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM video_dispatch_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                )
                job = cur.fetchone()
                if job is None:
                    raise ReservationError("作业不存在")
                if job["state"] != "queued":
                    raise ReservationError(f"作业状态 {job['state']} 不允许预留")
                if job["cancel_requested"]:
                    raise ReservationError("作业已取消")
                if int(job["queue_deadline_at"]) < now:
                    raise ReservationError("作业已超过等待期限")

                cur.execute(
                    "INSERT INTO video_resource_slots(resource_key) VALUES (%s) "
                    "ON CONFLICT (resource_key) DO NOTHING",
                    (resource_key,),
                )
                cur.execute(
                    "SELECT * FROM video_resource_slots WHERE resource_key = %s FOR UPDATE",
                    (resource_key,),
                )
                slot = cur.fetchone()
                if slot is None:
                    raise ReservationError("无法锁定资源槽")
                if slot.get("active_attempt_id") or slot.get("quarantine_reason"):
                    raise SlotBusy(f"资源槽 {resource_key} 已被占用或隔离")

                cur.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no "
                    "FROM video_dispatch_attempts WHERE job_id = %s",
                    (job_id,),
                )
                attempt_no = int(cur.fetchone()["next_no"])
                attempt_id = uuid.uuid4().hex
                cur.execute(
                    """
                    INSERT INTO video_dispatch_attempts
                      (attempt_id, job_id, attempt_no, instance_id, resource_key,
                       config_revision, submission_state, endpoint_snapshot, started_at)
                    VALUES (%s, %s, %s, %s, %s, %s, 'prepared', %s, %s)
                    RETURNING *
                    """,
                    (
                        attempt_id,
                        job_id,
                        attempt_no,
                        instance_id,
                        resource_key,
                        config_revision,
                        jsonb(endpoint_snapshot or {}),
                        now,
                    ),
                )
                attempt = cur.fetchone()
                cur.execute(
                    "UPDATE video_dispatch_jobs SET state = 'reserved' WHERE job_id = %s",
                    (job_id,),
                )
                cur.execute(
                    """
                    UPDATE video_resource_slots
                       SET active_attempt_id = %s, token = %s, boot_id = %s,
                           heartbeat_at = %s, quarantine_reason = NULL
                     WHERE resource_key = %s
                    """,
                    (attempt_id, token, boot_id, now, resource_key),
                )
                return {**attempt, "token": token}

    def heartbeat(self, *, resource_key: str, attempt_id: str, token: str) -> bool:
        """只有当前 attempt+token 能续租；旧回调不得延长后来者的租约。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE video_resource_slots SET heartbeat_at = %s
                     WHERE resource_key = %s AND active_attempt_id = %s AND token = %s
                    """,
                    (now_ms(), resource_key, attempt_id, token),
                )
                return cur.rowcount > 0

    def release(self, *, resource_key: str, attempt_id: str, token: str) -> bool:
        """条件释放槽位；旧 token 或已被后来者替换时返回 false。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE video_resource_slots
                       SET active_attempt_id = NULL, token = NULL, boot_id = NULL,
                           heartbeat_at = NULL
                     WHERE resource_key = %s AND active_attempt_id = %s AND token = %s
                    """,
                    (resource_key, attempt_id, token),
                )
                return cur.rowcount > 0

    def save_qualification(
        self,
        *,
        instance_id: str,
        resource_key: str,
        evidence: Dict[str, Any],
    ) -> Dict[str, Any]:
        """资格证据写入槽元数据；不改变 active_attempt_id/占用。"""
        instance_id = str(instance_id or "").strip()
        resource_key = str(resource_key or "").strip()
        if not instance_id or not resource_key or not isinstance(evidence, dict):
            raise ValueError("instance_id/resource_key/evidence 不能为空")
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO video_resource_slots(resource_key) VALUES (%s) "
                    "ON CONFLICT (resource_key) DO NOTHING",
                    (resource_key,),
                )
                cur.execute(
                    "SELECT metadata_json FROM video_resource_slots "
                    "WHERE resource_key = %s FOR UPDATE",
                    (resource_key,),
                )
                row = cur.fetchone()
                metadata = dict(row.get("metadata_json") or {}) if row else {}
                qualifications = dict(metadata.get("qualifications") or {})
                qualifications[instance_id] = evidence
                metadata["qualifications"] = qualifications
                cur.execute(
                    "UPDATE video_resource_slots SET metadata_json = %s "
                    "WHERE resource_key = %s",
                    (jsonb(metadata), resource_key),
                )
                return evidence

    def get_qualification(
        self,
        *,
        instance_id: str,
        resource_key: str,
    ) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT metadata_json FROM video_resource_slots WHERE resource_key = %s",
                    (resource_key,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                qualifications = (row.get("metadata_json") or {}).get("qualifications") or {}
                value = qualifications.get(instance_id)
                return value if isinstance(value, dict) else None

    def mark_attempt_unknown(self, *, attempt_id: str, reason: str, error: Any = None) -> None:
        """未知提交/取消未确认：保留槽位，只把 attempt 标为 unknown。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE video_dispatch_attempts "
                    "SET submission_state = 'unknown', error_json = %s "
                    "WHERE attempt_id = %s",
                    (jsonb({"reason": reason, "detail": error}), attempt_id),
                )

    def mark_attempt_terminal(self, *, attempt_id: str, reason: str = "", error: Any = None) -> None:
        """已知终态也只改账本，不在这里释放槽位。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE video_dispatch_attempts "
                    "SET submission_state = 'terminal', finished_at = %s, error_json = %s "
                    "WHERE attempt_id = %s",
                    (now_ms(), jsonb({"reason": reason, "detail": error}), attempt_id),
                )

    def mark_attempt_cancelled(
        self, *, attempt_id: str, reason: str = "", confirmed_at: Optional[int] = None
    ) -> None:
        """取消已被服务端确认：保留 slot 直到核对证明计算结束，但记录 cancelled 证据。"""
        payload = {"reason": reason, "confirmed_at": int(confirmed_at) if confirmed_at is not None else now_ms()}
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE video_dispatch_attempts "
                    "SET submission_state = 'cancelled', error_json = %s "
                    "WHERE attempt_id = %s",
                    (jsonb(payload), attempt_id),
                )

    def recover_startup(self, *, boot_id: str) -> int:
        """启动恢复：旧 boot 仍占用的槽转隔离，不自动释放、不自动重发。"""
        stamp = now_ms()
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT resource_key, active_attempt_id, boot_id FROM video_resource_slots "
                    "WHERE active_attempt_id IS NOT NULL AND quarantine_reason IS NULL "
                    "AND (boot_id IS NULL OR boot_id <> %s) FOR UPDATE",
                    (boot_id,),
                )
                rows = list(cur.fetchall())
                for row in rows:
                    cur.execute(
                        "UPDATE video_resource_slots "
                        "SET quarantine_reason = 'boot_changed', "
                        "metadata_json = metadata_json || %s::jsonb "
                        "WHERE resource_key = %s",
                        (
                            jsonb({"recovered_at": stamp, "previous_boot_id": row.get("boot_id")}),
                            row["resource_key"],
                        ),
                    )
                    cur.execute(
                        "UPDATE video_dispatch_attempts SET submission_state = 'unknown' "
                        "WHERE attempt_id = %s",
                        (row["active_attempt_id"],),
                    )
                return len(rows)

    def get_attempt(self, attempt_id: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM video_dispatch_attempts WHERE attempt_id = %s",
                    (attempt_id,),
                )
                return cur.fetchone()

    def count_inflight(self) -> int:
        """当前占用的物理槽数；unknown/取消排空只要仍 active 就计入。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS count FROM video_resource_slots "
                    "WHERE active_attempt_id IS NOT NULL"
                )
                return int(cur.fetchone()["count"])

    def active_instance_ids(self) -> set[str]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT a.instance_id FROM video_dispatch_attempts a "
                    "JOIN video_resource_slots s ON s.active_attempt_id = a.attempt_id"
                )
                return {str(row["instance_id"]) for row in cur.fetchall()}

    def list_quarantined(self) -> List[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.resource_key, s.quarantine_reason, s.active_attempt_id,
                           a.job_id, a.instance_id, a.submission_state, a.remote_job_id,
                           a.started_at, a.error_json AS attempt_error_json,
                           j.owner, j.profile_id, j.state AS job_state, j.error_json
                      FROM video_resource_slots s
                      LEFT JOIN video_dispatch_attempts a
                        ON a.attempt_id = s.active_attempt_id
                      LEFT JOIN video_dispatch_jobs j ON j.job_id = a.job_id
                     WHERE s.quarantine_reason IS NOT NULL
                     ORDER BY s.resource_key
                    """
                )
                return list(cur.fetchall())

    def release_quarantine(
        self,
        *,
        resource_key: str,
        released_by: str,
        reason: str = "",
        expected_attempt_id: Optional[str] = None,
        expected_remote_job_id: Optional[str] = None,
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """管理员核对后释放隔离槽；remote 未确认时必须显式 force。"""
        resource_key = str(resource_key or "").strip()
        if not resource_key:
            return None
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT resource_key, active_attempt_id, quarantine_reason "
                    "FROM video_resource_slots WHERE resource_key = %s FOR UPDATE",
                    (resource_key,),
                )
                slot = cur.fetchone()
                if slot is None or not slot.get("quarantine_reason"):
                    return None
                attempt_id = slot.get("active_attempt_id")
                job_id = None
                remote_job_id = None
                submission_state = None
                if attempt_id:
                    cur.execute(
                        "SELECT job_id, remote_job_id, submission_state "
                        "FROM video_dispatch_attempts WHERE attempt_id = %s",
                        (attempt_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        job_id = str(row["job_id"])
                        remote_job_id = str(row.get("remote_job_id") or "") or None
                        submission_state = str(row.get("submission_state") or "")
                if attempt_id and not expected_attempt_id:
                    raise QuarantineReleaseConflict(
                        "expected_attempt_required",
                        "诊断后槽位仍绑定 attempt，必须回传 expected_attempt_id",
                    )
                if expected_attempt_id and str(expected_attempt_id) != str(attempt_id or ""):
                    raise QuarantineReleaseConflict(
                        "attempt_mismatch", "隔离槽已被新的 attempt 替换，请刷新后重试"
                    )
                if remote_job_id and expected_remote_job_id is None:
                    raise QuarantineReleaseConflict(
                        "expected_remote_required",
                        "诊断中存在远端 job id，必须回传 expected_remote_job_id",
                    )
                if expected_remote_job_id is not None and str(expected_remote_job_id) != str(remote_job_id or ""):
                    raise QuarantineReleaseConflict(
                        "remote_job_mismatch", "远端 job id 与诊断快照不一致，请刷新后重试"
                    )
                if (
                    remote_job_id
                    and not force
                    and submission_state not in ("terminal", "cancelled")
                ):
                    raise QuarantineReleaseConflict(
                        "remote_unconfirmed",
                        "远端任务仍可能存在，必须确认远端终态或显式 force 释放",
                    )
                # 所有成功释放路径都在同一事务内先收口 attempt/job，再清 slot。
                if attempt_id:
                    cur.execute(
                        "UPDATE video_dispatch_attempts "
                        "SET submission_state = 'terminal', finished_at = %s, "
                        "error_json = %s WHERE attempt_id = %s",
                        (
                            now_ms(),
                            jsonb({
                                "reason": "admin_released",
                                "released_by": str(released_by),
                                "detail": str(reason or "")[:300],
                                "remote_job_id": remote_job_id,
                                "forced": bool(force),
                            }),
                            attempt_id,
                        ),
                    )
                if job_id:
                    # failed 可补审计；succeeded/cancelled 业务终态不被覆盖。
                    cur.execute(
                        "UPDATE video_dispatch_jobs SET state = 'failed', "
                        "error_json = %s "
                        "WHERE job_id = %s AND state NOT IN "
                        "('succeeded', 'cancelled')",
                        (
                            jsonb({
                                "error_code": "admin_released",
                                "reason": str(reason or "管理员释放隔离槽")[:300],
                            }),
                            job_id,
                        ),
                    )
                cur.execute(
                    "UPDATE video_resource_slots SET active_attempt_id = NULL, "
                    "token = NULL, boot_id = NULL, heartbeat_at = NULL, "
                    "quarantine_reason = NULL WHERE resource_key = %s",
                    (resource_key,),
                )
                return {
                    "resource_key": resource_key,
                    "attempt_id": attempt_id,
                    "job_id": job_id,
                    "released": True,
                }

    def count_quarantined(self) -> int:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS count FROM video_resource_slots "
                    "WHERE quarantine_reason IS NOT NULL"
                )
                return int(cur.fetchone()["count"])

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM video_dispatch_jobs WHERE job_id = %s", (job_id,))
                return cur.fetchone()

    def list_queued(self, *, limit: int = 500) -> List[Dict[str, Any]]:
        """按受理顺序返回等待派发的作业；取消标记不在这里过滤，由服务收尾。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM video_dispatch_jobs WHERE state = 'queued' "
                    "ORDER BY accepted_at, job_id LIMIT %s",
                    (int(limit),),
                )
                return list(cur.fetchall())

    DISPATCH_STOPPED_KEY = "video_dispatch_stopped"
    DRAINING_PROFILE_PREFIX = "video_draining_profile:"
    DRAINING_INSTANCE_PREFIX = "video_draining_instance:"

    def mark_dispatch_stopped(self, stopped: bool) -> None:
        if stopped:
            self.set_system_meta(self.DISPATCH_STOPPED_KEY, "1")
        else:
            self._delete_meta_key(self.DISPATCH_STOPPED_KEY)

    def is_dispatch_stopped(self) -> bool:
        return self.get_system_meta(self.DISPATCH_STOPPED_KEY) is not None

    def mark_profile_draining(self, profile_id: str, draining: bool) -> None:
        key = f"{self.DRAINING_PROFILE_PREFIX}{profile_id}"
        if draining:
            self.set_system_meta(key, "1")
        else:
            self._delete_meta_key(key)

    def list_draining_profiles(self) -> List[str]:
        return self._list_meta_suffixes(self.DRAINING_PROFILE_PREFIX)

    def mark_instance_draining(self, instance_id: str, draining: bool) -> None:
        key = f"{self.DRAINING_INSTANCE_PREFIX}{instance_id}"
        if draining:
            self.set_system_meta(key, "1")
        else:
            self._delete_meta_key(key)

    def list_draining_instances(self) -> List[str]:
        return self._list_meta_suffixes(self.DRAINING_INSTANCE_PREFIX)

    def _list_meta_suffixes(self, prefix: str) -> List[str]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key FROM schema_meta WHERE key LIKE %s ORDER BY key",
                    (prefix + "%",),
                )
                return [str(row["key"])[len(prefix):] for row in cur.fetchall()]

    SOURCE_DELETED_PREFIX = "video_deleted_source:"
    SOURCE_DELETING_PREFIX = "video_deleting_source:"

    def _delete_meta_key(self, key: str) -> None:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM schema_meta WHERE key = %s", (key,))

    def mark_source_deleting(self, source_kind: str, source_id: str) -> None:
        key = f"{self.SOURCE_DELETING_PREFIX}{source_kind}:{source_id}"
        self.set_system_meta(key, "1")

    def clear_source_deleting(self, source_kind: str, source_id: str) -> None:
        key = f"{self.SOURCE_DELETING_PREFIX}{source_kind}:{source_id}"
        self._delete_meta_key(key)

    def is_source_deleting(self, source_kind: str, source_id: str) -> bool:
        key = f"{self.SOURCE_DELETING_PREFIX}{source_kind}:{source_id}"
        return self.get_system_meta(key) is not None

    def list_source_deleting(self) -> List[tuple[str, str]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key FROM schema_meta WHERE key LIKE %s ORDER BY key",
                    (self.SOURCE_DELETING_PREFIX + "%",),
                )
                result = []
                for row in cur.fetchall():
                    token = str(row["key"])[len(self.SOURCE_DELETING_PREFIX):]
                    if ":" in token:
                        kind, sid = token.split(":", 1)
                        result.append((kind, sid))
                return result

    def mark_source_deleted(self, source_kind: str, source_id: str) -> None:
        # 每个来源一个 schema_meta 键：set_system_meta 是原子 upsert，
        # 不再对单个 JSON 列表做读-改-写，避免并发删除互相覆盖。
        key = f"{self.SOURCE_DELETED_PREFIX}{source_kind}:{source_id}"
        self.set_system_meta(key, "1")
        self.clear_source_deleting(source_kind, source_id)

    def is_source_deleted(self, source_kind: str, source_id: str) -> bool:
        key = f"{self.SOURCE_DELETED_PREFIX}{source_kind}:{source_id}"
        return self.get_system_meta(key) is not None

    def list_jobs_by_source(self, source_kind: str, source_id: str) -> List[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM video_dispatch_jobs "
                    "WHERE source_kind = %s AND source_id = %s "
                    "AND state NOT IN ('succeeded', 'failed', 'cancelled') "
                    "ORDER BY accepted_at, job_id",
                    (str(source_kind), str(source_id)),
                )
                return list(cur.fetchall())

    def list_jobs_by_states(self, states: Iterable[str]) -> List[Dict[str, Any]]:
        values = [str(state) for state in states]
        if not values:
            return []
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM video_dispatch_jobs WHERE state = ANY(%s) "
                    "ORDER BY accepted_at, job_id",
                    (values,),
                )
                return list(cur.fetchall())

    def list_active_attempts(self) -> List[Dict[str, Any]]:
        """当前仍占物理槽的尝试（含 unknown/取消排空）及其作业字段。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT a.*, s.token AS slot_token, s.heartbeat_at, s.quarantine_reason,
                           j.owner, j.batch_id, j.execution_id, j.dispatch_mode, j.profile_id,
                           j.source_kind, j.source_id,
                           j.snapshot_json, j.state AS job_state, j.cancel_requested
                      FROM video_dispatch_attempts a
                      JOIN video_resource_slots s ON s.active_attempt_id = a.attempt_id
                      JOIN video_dispatch_jobs j ON j.job_id = a.job_id
                     ORDER BY a.started_at NULLS FIRST, a.attempt_id
                    """
                )
                return list(cur.fetchall())

    def unresolved_attempts(self) -> List[Dict[str, Any]]:
        """提交状态未落定但仍占槽的尝试；启动恢复只核对、不重发。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT a.*, s.token AS slot_token, s.heartbeat_at, s.quarantine_reason,
                           j.owner, j.batch_id, j.execution_id, j.dispatch_mode, j.profile_id,
                           j.snapshot_json, j.state AS job_state
                      FROM video_dispatch_attempts a
                      JOIN video_resource_slots s ON s.active_attempt_id = a.attempt_id
                      JOIN video_dispatch_jobs j ON j.job_id = a.job_id
                     WHERE a.submission_state IN ('prepared', 'submitting', 'unknown')
                     ORDER BY a.started_at NULLS FIRST, a.attempt_id
                    """
                )
                return list(cur.fetchall())

    def batch_inflight_counts(self) -> Dict[str, int]:
        """按批次统计在途尝试；用于批次并发，不能固定入队时的空闲数。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT j.batch_id, COUNT(*) AS count
                      FROM video_resource_slots s
                      JOIN video_dispatch_attempts a ON s.active_attempt_id = a.attempt_id
                      JOIN video_dispatch_jobs j ON j.job_id = a.job_id
                     GROUP BY j.batch_id
                    """
                )
                return {str(row["batch_id"]): int(row["count"]) for row in cur.fetchall()}

    def execution_inflight_counts(self) -> Dict[str, int]:
        """按执行统计在途尝试；容量快照必须与 per_execution_limits 同键。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT j.execution_id, COUNT(*) AS count
                      FROM video_resource_slots s
                      JOIN video_dispatch_attempts a ON s.active_attempt_id = a.attempt_id
                      JOIN video_dispatch_jobs j ON j.job_id = a.job_id
                     GROUP BY j.execution_id
                    """
                )
                return {
                    str(row["execution_id"]): int(row["count"])
                    for row in cur.fetchall()
                }

    def active_resource_keys(self) -> set[str]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT resource_key FROM video_resource_slots "
                    "WHERE active_attempt_id IS NOT NULL"
                )
                return {str(row["resource_key"]) for row in cur.fetchall()}

    def quarantined_resource_keys(self) -> set[str]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT resource_key FROM video_resource_slots "
                    "WHERE quarantine_reason IS NOT NULL"
                )
                return {str(row["resource_key"]) for row in cur.fetchall()}

    def last_dispatch_times(self) -> Dict[str, int]:
        """同等级资源轮转依据：最近一次尝试的开始时间（毫秒）。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT resource_key, MAX(started_at) AS last_at "
                    "FROM video_dispatch_attempts GROUP BY resource_key"
                )
                return {
                    str(row["resource_key"]): int(row["last_at"] or 0)
                    for row in cur.fetchall()
                }

    def qualifications(self) -> Dict[str, Dict[str, Any]]:
        """汇总槽元数据里的资格证据：instance_id -> evidence。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT metadata_json FROM video_resource_slots")
                result: Dict[str, Dict[str, Any]] = {}
                for row in cur.fetchall():
                    qualifications = (row.get("metadata_json") or {}).get("qualifications") or {}
                    for instance_id, evidence in qualifications.items():
                        if isinstance(evidence, dict):
                            result[str(instance_id)] = evidence
                return result

    def latest_attempt(self, job_id: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM video_dispatch_attempts WHERE job_id = %s "
                    "ORDER BY attempt_no DESC LIMIT 1",
                    (job_id,),
                )
                return cur.fetchone()

    def slot_for_attempt(self, attempt_id: str) -> Optional[Dict[str, Any]]:
        """当前绑定该 attempt 的槽位（含 token）；旧 attempt 不返回。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM video_resource_slots WHERE active_attempt_id = %s",
                    (attempt_id,),
                )
                return cur.fetchone()

    def record_submitting(self, attempt_id: str) -> bool:
        """提交前落状态：崩溃后只能按 unknown 核对，不能重发。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE video_dispatch_attempts SET submission_state = 'submitting' "
                    "WHERE attempt_id = %s AND submission_state = 'prepared'",
                    (attempt_id,),
                )
                return cur.rowcount > 0

    def begin_submission(self, *, job_id: str, attempt_id: str) -> bool:
        """原子取得发送权：作业未取消、仍可发送且 attempt 仍为 prepared。

        并发取消先落库时这里必须返回 False，调用方不得再发 POST；已取得发送权
        的尝试则视为在途，取消只能等待核对。
        """
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT j.state AS job_state, j.cancel_requested, "
                    "       a.submission_state "
                    "  FROM video_dispatch_jobs j "
                    "  JOIN video_dispatch_attempts a ON a.job_id = j.job_id "
                    " WHERE j.job_id = %s AND a.attempt_id = %s "
                    "   FOR UPDATE OF j, a",
                    (job_id, attempt_id),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                if row["cancel_requested"]:
                    return False
                if row["job_state"] not in ("queued", "reserved"):
                    return False
                if row["submission_state"] != "prepared":
                    return False
                cur.execute(
                    "UPDATE video_dispatch_jobs SET state = 'submitting' WHERE job_id = %s",
                    (job_id,),
                )
                cur.execute(
                    "UPDATE video_dispatch_attempts SET submission_state = 'submitting' "
                    "WHERE attempt_id = %s",
                    (attempt_id,),
                )
                return True

    def cancel_prepared_attempt(self, *, attempt_id: str, reason: str = "") -> bool:
        """原子撤销尚未取得发送权的 attempt：锁 attempt 后复核状态并释放槽。

        与 begin_submission 通过 video_dispatch_attempts 行锁互斥；只有仍为
        prepared 时才释放槽并标 terminal。已提交/unknown 不在这里处理。
        """
        with transaction() as conn:
            with conn.cursor() as cur:
                # 与 release_quarantine 保持 slot -> attempt 的锁顺序，避免与管理
                # 员释放隔离槽并发时形成环。
                cur.execute(
                    "SELECT resource_key, token FROM video_resource_slots "
                    "WHERE active_attempt_id = %s FOR UPDATE",
                    (attempt_id,),
                )
                slot = cur.fetchone()
                cur.execute(
                    "SELECT submission_state FROM video_dispatch_attempts "
                    "WHERE attempt_id = %s FOR UPDATE",
                    (attempt_id,),
                )
                attempt = cur.fetchone()
                if attempt is None:
                    return False
                if str(attempt.get("submission_state") or "") != "prepared":
                    return False
                if slot is not None:
                    cur.execute(
                        "UPDATE video_resource_slots "
                        "SET active_attempt_id = NULL, token = NULL, boot_id = NULL, "
                        "    heartbeat_at = NULL "
                        "WHERE resource_key = %s AND active_attempt_id = %s",
                        (slot["resource_key"], attempt_id),
                    )
                cur.execute(
                    "UPDATE video_dispatch_attempts "
                    "SET submission_state = 'terminal', finished_at = %s, error_json = %s "
                    "WHERE attempt_id = %s AND submission_state = 'prepared'",
                    (
                        now_ms(),
                        jsonb({"reason": reason}),
                        attempt_id,
                    ),
                )
                return True

    def record_submitted(self, *, attempt_id: str, remote_job_id: str) -> None:
        """拿到远端 ID 后立即落库；后续查询/下载只认这条记录。"""
        if not remote_job_id:
            raise ValueError("remote_job_id 不能为空")
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE video_dispatch_attempts "
                    "SET submission_state = 'submitted', remote_job_id = %s "
                    "WHERE attempt_id = %s",
                    (str(remote_job_id), attempt_id),
                )

    def set_job_state(
        self,
        *,
        job_id: str,
        state: str,
        error: Any = None,
        result: Any = None,
        expected_states: Optional[Iterable[str]] = None,
    ) -> bool:
        """条件更新作业状态；终态不静默回退，调用方须显式给 expected_states。"""
        sql = "UPDATE video_dispatch_jobs SET state = %s"
        params: List[Any] = [state]
        if error is not None:
            sql += ", error_json = %s"
            params.append(jsonb(error))
        if result is not None:
            sql += ", result_json = %s"
            params.append(jsonb(result))
        sql += " WHERE job_id = %s"
        params.append(job_id)
        if expected_states is not None:
            expected = list(expected_states)
            if not expected:
                return False
            sql += " AND state = ANY(%s)"
            params.append(expected)
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount > 0

    def fail_queued_jobs(
        self,
        *,
        job_ids: Iterable[str],
        error_code: str,
        reason: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        """只结束仍未派发的作业；已预留/在途的不在此处收尾。"""
        ids = [str(item) for item in job_ids]
        if not ids:
            return 0
        error_json = dict(payload) if payload is not None else {
            "error_code": error_code,
            "reason": reason,
        }
        error_json.setdefault("error_code", error_code)
        error_json.setdefault("reason", reason)
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE video_dispatch_jobs SET state = 'failed', error_json = %s "
                    "WHERE job_id = ANY(%s) AND state = 'queued'",
                    (jsonb(error_json), ids),
                )
                return cur.rowcount

    def mark_job_cancel_requested(self, job_id: str) -> bool:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE video_dispatch_jobs SET cancel_requested = TRUE "
                    "WHERE job_id = %s AND state NOT IN ('succeeded', 'failed', 'cancelled')",
                    (job_id,),
                )
                return cur.rowcount > 0

    def quarantine_slot(
        self,
        *,
        resource_key: str,
        reason: str,
        detail: Optional[Dict[str, Any]] = None,
        only_with_attempt: bool = True,
    ) -> bool:
        """隔离不改 active_attempt_id；只阻止后续派发，等待人工核对。"""
        sql = (
            "UPDATE video_resource_slots "
            "SET quarantine_reason = %s, metadata_json = metadata_json || %s::jsonb "
            "WHERE resource_key = %s"
        )
        params: List[Any] = [reason, jsonb(detail or {}), resource_key]
        if only_with_attempt:
            sql += " AND active_attempt_id IS NOT NULL"
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount > 0

    def stale_active_attempts(self, *, older_than_ms: int, now: Optional[int] = None) -> List[Dict[str, Any]]:
        """心跳失联只触发核对：返回超时仍占槽的尝试，由服务隔离。"""
        moment = now_ms() if now is None else int(now)
        cutoff = moment - int(older_than_ms)
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT a.*, s.token AS slot_token, s.heartbeat_at, s.quarantine_reason,
                           j.owner, j.batch_id, j.execution_id, j.dispatch_mode, j.profile_id,
                           j.snapshot_json, j.state AS job_state
                      FROM video_dispatch_attempts a
                      JOIN video_resource_slots s ON s.active_attempt_id = a.attempt_id
                      JOIN video_dispatch_jobs j ON j.job_id = a.job_id
                     WHERE (s.heartbeat_at IS NULL OR s.heartbeat_at < %s)
                       AND s.quarantine_reason IS NULL
                     ORDER BY s.heartbeat_at NULLS FIRST
                    """,
                    (cutoff,),
                )
                return list(cur.fetchall())

    def count_queued(self) -> int:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS count FROM video_dispatch_jobs WHERE state = 'queued'")
                return int(cur.fetchone()["count"])

    def get_system_meta(self, key: str) -> Optional[str]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM schema_meta WHERE key = %s", (str(key),))
                row = cur.fetchone()
                return str(row["value"]) if row else None

    def set_system_meta(self, key: str, value: str) -> None:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schema_meta(key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (str(key), str(value)),
                )

    def _load_existing(
        self,
        cur: Any,
        owner: str,
        prepared: List[Dict[str, Any]],
    ) -> Dict[tuple[str, str], Dict[str, Any]]:
        keys = [(item["request_key"], item["item_id"]) for item in prepared]
        if not keys:
            return {}
        request_keys = sorted({key[0] for key in keys})
        cur.execute(
            "SELECT * FROM video_dispatch_jobs "
            "WHERE owner = %s AND request_key = ANY(%s)",
            (owner, request_keys),
        )
        rows = list(cur.fetchall())
        return {
            (str(row["request_key"]), str(row["item_id"])): row
            for row in rows
            if (str(row["request_key"]), str(row["item_id"])) in keys
        }


def _prepare_job(owner: str, batch_id: str, job: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(job, dict):
        raise ValueError("job 必须是映射")
    item_id = str(job.get("item_id") or "").strip()
    request_key = str(job.get("request_key") or "").strip()
    source_kind = str(job.get("source_kind") or "").strip()
    if not item_id or not request_key or not source_kind:
        raise ValueError("job 缺少 item_id/request_key/source_kind")
    dispatch_mode = str(job.get("dispatch_mode") or "auto").strip().lower()
    if dispatch_mode not in DISPATCH_MODES:
        raise ValueError("dispatch_mode 必须是 auto/manual")
    snapshot = job.get("snapshot_json")
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot_json 必须是映射")
    profile_revision = job.get("profile_revision")
    if profile_revision is not None:
        if isinstance(profile_revision, bool):
            raise ValueError("profile_revision 必须是整数")
        try:
            profile_revision = int(profile_revision)
        except (TypeError, ValueError) as exc:
            raise ValueError("profile_revision 必须是整数") from exc
    return {
        "job_id": str(job.get("job_id") or uuid.uuid4().hex),
        "owner": owner,
        "batch_id": batch_id,
        "source_kind": source_kind,
        "source_id": None if job.get("source_id") is None else str(job["source_id"]),
        "execution_id": None if job.get("execution_id") is None else str(job["execution_id"]),
        "item_id": item_id,
        "request_key": request_key,
        "dispatch_mode": dispatch_mode,
        "profile_id": None if job.get("profile_id") is None else str(job["profile_id"]),
        "profile_revision": profile_revision,
        "snapshot_json": snapshot,
    }


def _fingerprint_item(item: Dict[str, Any]) -> str:
    return _fingerprint(
        {
            "source_kind": item["source_kind"],
            "source_id": item["source_id"],
            "execution_id": item["execution_id"],
            "item_id": item["item_id"],
            "dispatch_mode": item["dispatch_mode"],
            "profile_id": item["profile_id"],
            "profile_revision": item["profile_revision"],
            "snapshot_json": item["snapshot_json"],
        }
    )


def _fingerprint_row(row: Dict[str, Any]) -> str:
    return _fingerprint(
        {
            "source_kind": row.get("source_kind"),
            "source_id": row.get("source_id"),
            "execution_id": row.get("execution_id"),
            "item_id": row.get("item_id"),
            "dispatch_mode": row.get("dispatch_mode"),
            "profile_id": row.get("profile_id"),
            "profile_revision": row.get("profile_revision"),
            "snapshot_json": row.get("snapshot_json") or {},
        }
    )


def _fingerprint(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "ACTIVE_ATTEMPT_STATES",
    "DEFAULT_MAX_QUEUED_JOBS",
    "DEFAULT_QUEUE_TIMEOUT_SECONDS",
    "DispatchConflict",
    "DispatchQueueError",
    "EnqueueResult",
    "QueueFull",
    "ReservationError",
    "SlotBusy",
    "TERMINAL_JOB_STATES",
    "VideoDispatchRepo",
]
