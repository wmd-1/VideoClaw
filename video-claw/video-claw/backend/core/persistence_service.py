"""Prepare → commit → install-cache service (design D13).

I/O (upload, materialize) stays outside the state lock; the commit is a short DB
transaction with mandatory revision CAS. Cache install is monotonic and refuses
stale views; any failure evicts the entry so the next read goes to the DB.

The committed snapshot is complete (`load_committed` shape), never a partial diff.

Failure compensation for published objects:
- definite failure (conflict / stale / parent missing / validation: the transaction
  rolled back) → `abort_publication` deletes only objects it can claim;
- unknown outcome (connection lost around COMMIT) → the operation states decide:
  all retained = committed (reload and return), still prepared = rolled back
  (compensate), otherwise leave everything for the cleaner and raise
  `CommitOutcomeUnknown`. Retained objects are never deleted.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from core.artifact_publisher import abort_publication, publication_outcome
from db.repositories.commits import (
    CommitConflict,
    CommitParentMissing,
    CommitStale,
    CommitValidationError,
    commit_session_change,
    commit_session_progress,
    load_committed,
    session_revision,
)

logger = logging.getLogger(__name__)

_DEFINITE_FAILURES = (CommitConflict, CommitParentMissing, CommitValidationError)
_PROGRESS_FIELDS = ("status", "current_stage", "status_json", "stage_progress_json", "boot_id", "updated_at")


class CommitOutcomeUnknown(RuntimeError):
    """The DB commit may or may not have happened; published objects were left untouched."""

    def __init__(self, message: str, published: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.objects = list((published or {}).get("objects") or [])


class SnapshotCache:
    """按 session 保存已提交完整快照 + revision；只接受更新的安装。

    “更新”按 (revision, updated_at) 比较：同 revision 仅进度变化时 updated_at 前进。
    """

    def __init__(self) -> None:
        self._entries: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def install(self, session_id: str, revision: int, snapshot: Dict[str, Any]) -> bool:
        with self._lock:
            current = self._entries.get(session_id)
            if current is not None:
                incoming = (int(revision), int(snapshot.get("updated_at") or 0))
                existing = (int(current[0]), int(current[1].get("updated_at") or 0))
                if incoming <= existing:
                    return False
            self._entries[session_id] = (int(revision), snapshot)
            return True

    def apply_progress(self, session_id: str, fields: Dict[str, Any]) -> bool:
        """进度不递增 revision：仅当缓存仍是同一 revision 且更新时间不倒退时替换进度字段。"""
        with self._lock:
            current = self._entries.get(session_id)
            if current is None or int(current[0]) != int(fields.get("revision", -1)):
                return False
            if int(fields.get("updated_at") or 0) < int(current[1].get("updated_at") or 0):
                return False
            updated = dict(current[1])  # 不原地修改：读者可能持有旧快照引用
            updated.update({key: fields[key] for key in _PROGRESS_FIELDS if key in fields})
            self._entries[session_id] = (int(current[0]), updated)
            return True

    def get(self, session_id: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        with self._lock:
            return self._entries.get(session_id)

    def evict(self, session_id: str) -> None:
        with self._lock:
            self._entries.pop(session_id, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class PersistenceService:
    def __init__(
        self,
        cache: Optional[SnapshotCache] = None,
        loader: Callable[[str], Dict[str, Any]] = load_committed,
    ) -> None:
        self.cache = cache or SnapshotCache()
        self.loader = loader
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @staticmethod
    def _compensate(session_id: str, published: Dict[str, Any], exc: BaseException) -> None:
        """补偿只附加诊断，绝不替换原异常（调用方依赖其类型做合并/重试）。"""
        try:
            issues = abort_publication(published)
        except Exception as comp_exc:
            logger.exception("publication compensation raised for %s", session_id)
            setattr(exc, "compensation_error", comp_exc)
            return
        if issues:
            # 残留已是 delete_pending + 退避，由清理器重试
            logger.error("publication compensation incomplete for %s: %s", session_id, issues)
            setattr(exc, "compensation_issues", issues)

    def resource_lock(self, resource_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(resource_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[resource_id] = lock
            return lock

    def _install(self, session_id: str, snapshot: Dict[str, Any]) -> None:
        """单调安装：更旧 revision 被拒绝时保留现存更新视图；安装异常则驱逐。"""
        try:
            self.cache.install(session_id, int(snapshot["revision"]), snapshot)
        except Exception:  # 缓存异常不得影响已提交结果
            logger.exception("cache install failed for %s; evicted", session_id)
            self.cache.evict(session_id)

    def load(self, session_id: str) -> Dict[str, Any]:
        """命中需 revision 一致；安装用快照自身 revision（与内容同一事务读取），不用先读的计数。"""
        try:
            revision = session_revision(session_id)
        except Exception:
            self.cache.evict(session_id)
            raise
        if revision is None:
            self.cache.evict(session_id)
            raise CommitParentMissing(session_id)
        cached = self.cache.get(session_id)
        if cached is not None and int(cached[0]) == int(revision):
            return cached[1]
        snapshot = self.loader(session_id)
        self._install(session_id, snapshot)
        return snapshot

    def commit(self, published: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Any]:
        session_id = str(kwargs.get("session_id") or "")
        lock = self.resource_lock(session_id)
        try:
            with lock:
                result = commit_session_change(**kwargs)
        except _DEFINITE_FAILURES as exc:
            self.cache.evict(session_id)
            if published:
                self._compensate(session_id, published, exc)
            raise
        except Exception as exc:
            self.cache.evict(session_id)
            if not published or not published.get("operation_ids"):
                raise
            outcome = publication_outcome(published)
            if outcome == "committed":
                logger.warning("commit for %s reported %r but operations are retained; reloading", session_id, exc)
                snapshot = self.loader(session_id)
                self._install(session_id, snapshot)
                return snapshot
            if outcome == "rolled_back":
                self._compensate(session_id, published, exc)
                raise
            raise CommitOutcomeUnknown(
                f"commit outcome unknown for {session_id}; objects left for cleanup: {exc}", published,
            ) from exc
        self._install(session_id, result)
        return result

    def commit_progress(self, **kwargs: Any) -> Dict[str, Any]:
        session_id = str(kwargs.get("session_id") or "")
        try:
            with self.resource_lock(session_id):
                fields = commit_session_progress(**kwargs)
        except CommitStale:
            raise
        except Exception:
            self.cache.evict(session_id)
            raise
        if not self.cache.apply_progress(session_id, fields):
            self.cache.evict(session_id)
        return fields


__all__ = [
    "CommitConflict",
    "CommitOutcomeUnknown",
    "CommitParentMissing",
    "CommitStale",
    "CommitValidationError",
    "PersistenceService",
    "SnapshotCache",
]
