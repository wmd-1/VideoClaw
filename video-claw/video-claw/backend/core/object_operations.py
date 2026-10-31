"""Object operation cleanup worker and deletion registration (design D17).

Deletion requires an atomic claim on both sides:
- DB side: `claim_for_delete` is a lease (prepared / expired delete_pending →
  delete_pending); retained rows can never be claimed by the worker or by
  failure compensation;
- in-process side: the exact object identity and every registered local path are
  claimed in `core.resource_claims.CLAIMS`, so an uploader/reader pin cannot slip in
  between the "not pinned" check and the delete.

References are checked exactly: a MinIO target is in use only while a file row or
a saved copy points at the same bucket/key; a legacy local target only while a
local file row points at the same path (or the same legacy public_path).

Local cleanup never recurses: legacy local targets delete the registered file
inside RESULT_DIR; auxiliary paths of MinIO targets are limited to files inside the
execution work roots or exact `_refs/<owner_key>/<file>` cache entries. A whole
execution root is removed only by `cleanup_execution_root` once nothing is in flight.

Rows that cannot be processed are rescheduled instead of blocking the queue head.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from typing import Any, Dict, List, Optional

from config import settings
from core import media_refs
from core.artifact_publisher import (
    PublicationError,
    assert_plain_work_root,
    execution_work_root,
    work_roots_base,
)
from core.resource_claims import CLAIMS, local_identity, object_identity
from db.repositories import FileRepo, SavedRepo
from db.repositories.operations import LOCAL_BUCKET, ObjectOperationRepo, OperationOwnershipError
from storage import StorageError, get_storage

logger = logging.getLogger(__name__)
DEFER_SECONDS = 60
ALERT_ATTEMPTS = 30
SCAN_INTERVAL_SECONDS = 60

_PARENT_COLUMNS = {"sessions": "session_id", "tasks": "task_id", "sandbox": "record_id"}
# 本进程启动标识：发布 prepared 登记必须携带它，清理器只把旧进程的 prepared 当孤儿
_PROCESS = {"boot_id": ""}


def current_boot_id() -> str:
    return _PROCESS["boot_id"]


def _retry_delay_seconds(attempts: int) -> int:
    return min(60 * (2 ** min(int(attempts or 0), 6)), 3600)


def _inside(path: str, root: str) -> bool:
    return path.startswith(root.rstrip(os.sep) + os.sep)


def _local_paths(op: Dict[str, Any]) -> List[str]:
    value = op.get("local_paths_json")
    return [str(item) for item in value] if isinstance(value, list) else []


def _refs_root() -> str:
    return os.path.realpath(str(media_refs._REFS_ROOT))


def is_aux_cleanup_path(path: str) -> bool:
    """MinIO 目标附带的本地文件是否可随对象清理：执行工作目录内文件或精确 `_refs` 缓存文件。

    不要求文件已存在（登记时缓存可能尚未下载），清理时会再次复核。
    """
    if not path or os.path.islink(path) or os.path.isdir(path):
        return False
    real = os.path.realpath(path)
    if _inside(real, work_roots_base()):
        return True
    refs = _refs_root()
    # 只允许 _refs/<owner_key>/<file> 这一层的精确文件，绝不触及共享根或用户目录
    return _inside(real, refs) and os.path.dirname(os.path.dirname(real)) == refs


def _delete_file(path: str) -> None:
    if os.path.lexists(path):
        os.remove(path)
    if os.path.lexists(path):
        raise StorageError(f"local file still present: {path}")


class ObjectCleanupWorker:
    def __init__(
        self,
        *,
        boot_id: str = "",
        repo: Optional[ObjectOperationRepo] = None,
        max_batch: int = 100,
    ) -> None:
        self.boot_id = boot_id
        self.repo = repo or ObjectOperationRepo()
        self.max_batch = max_batch

    # ── 引用判定（精确定位）────────────────────────────────────
    def _has_business_reference(self, op: Dict[str, Any]) -> bool:
        try:
            if str(op.get("target_kind")) == "minio":
                bucket = str(op.get("bucket") or "")
                key = str(op.get("object_key") or "")
                if FileRepo().find_by_object(bucket, key, default_bucket=settings.STORAGE_BUCKET):
                    return True
                return SavedRepo().find_by_object(bucket, key) is not None  # 收藏副本没有文件行
            return FileRepo().find_local_reference(
                [*_local_paths(op), str(op.get("object_key") or "")],
                str(op.get("public_path") or ""),
            ) is not None
        except Exception as exc:  # DB 不可用：保守不删
            logger.warning("cleanup: reference lookup failed for %s: %s", op.get("operation_id"), exc)
            return True

    @staticmethod
    def _identities(op: Dict[str, Any]) -> List[str]:
        identities = [local_identity(path) for path in _local_paths(op)]
        if str(op.get("target_kind")) == "minio":
            identities.append(object_identity(str(op.get("bucket") or ""), str(op.get("object_key") or "")))
        else:
            identities.append(str(op.get("object_key") or ""))
        return identities

    # ── 删除 ────────────────────────────────────────────────
    def _delete_minio(self, op: Dict[str, Any]) -> None:
        bucket = str(op.get("bucket") or "")
        key = str(op.get("object_key") or "")
        if not bucket or bucket == LOCAL_BUCKET or not key:
            raise StorageError(f"invalid object target: {bucket}/{key}")
        get_storage().delete(key, bucket=bucket)  # 适配器会吞删除异常，必须 stat 复核
        if get_storage().stat(key, bucket=bucket) is not None:
            raise StorageError(f"object still present after delete: {bucket}/{key}")
        for path in _local_paths(op):
            if not os.path.lexists(path):
                continue
            if not is_aux_cleanup_path(path):
                raise StorageError(f"refusing to delete non-execution/non-cache path: {path}")
            _delete_file(path)

    def _delete_local(self, op: Dict[str, Any]) -> None:
        paths = _local_paths(op)
        target = str(op.get("object_key") or "")
        if not paths:
            raise StorageError("local operation has no registered file path")
        root = os.path.realpath(settings.RESULT_DIR)
        for path in paths:
            if not os.path.lexists(path):
                continue  # 已不存在：删除幂等成功
            real = os.path.realpath(path)
            if real != target:
                raise StorageError(f"registered path no longer resolves to target: {path} -> {real}")
            if not _inside(real, root) or not _inside(os.path.abspath(path), root):
                raise StorageError(f"refusing to delete outside result root: {path}")
            if os.path.isdir(real):
                raise StorageError(f"refusing to delete directory: {path}")
            _delete_file(path)

    def _delete(self, op: Dict[str, Any]) -> None:
        if str(op.get("target_kind")) == "local":
            self._delete_local(op)
            return
        self._delete_minio(op)

    # ── 主循环 ──────────────────────────────────────────────
    def run_once(self, now: Optional[int] = None) -> Dict[str, int]:
        ts = int(now if now is not None else time.time() * 1000)
        counts = {"scanned": 0, "deleted": 0, "skipped": 0, "deferred": 0, "failed": 0}
        for op in self.repo.list_eligible(self.boot_id, ts, self.max_batch):
            counts["scanned"] += 1
            operation_id = str(op["operation_id"])
            if self._has_business_reference(op):
                self.repo.reschedule(operation_id, ts + DEFER_SECONDS * 1000)
                counts["deferred"] += 1
                continue
            identities = self._identities(op)
            if not CLAIMS.claim_all(identities):
                self.repo.reschedule(operation_id, ts + DEFER_SECONDS * 1000)
                counts["deferred"] += 1
                continue
            try:
                if not self.repo.claim_for_delete(operation_id, now=ts, scan_boot_id=self.boot_id):
                    counts["skipped"] += 1  # 资格已变（续签/retained/他人租约）：不删
                    continue
                try:
                    self._delete(op)
                except Exception as exc:
                    attempts = self.repo.mark_failure(
                        operation_id, str(exc),
                        ts + _retry_delay_seconds(int(op.get("attempts") or 0)) * 1000,
                    )
                    log = logger.error if attempts >= ALERT_ATTEMPTS else logger.warning
                    log(
                        "cleanup failed bucket=%s key=%s attempts=%s: %s",
                        op.get("bucket"), op.get("object_key"), attempts, exc,
                    )
                    counts["failed"] += 1
                else:
                    if not self.repo.mark_done(operation_id):
                        logger.warning("cleanup: mark_done refused for %s", operation_id)
                    counts["deleted"] += 1
            finally:
                CLAIMS.release_all(identities)
        return counts


def start_cleanup_loop(boot_id: str, *, interval: int = SCAN_INTERVAL_SECONDS) -> threading.Event:
    """启动后及每 interval 秒扫描一次；DB 不可用时本轮跳过（不做无索引猜测删除）。返回停止事件。"""
    _PROCESS["boot_id"] = str(boot_id or "")
    stop = threading.Event()
    worker = ObjectCleanupWorker(boot_id=boot_id)

    def loop() -> None:
        while not stop.is_set():
            try:
                worker.run_once()
            except Exception as exc:
                logger.warning("cleanup scan skipped: %s", exc)
            stop.wait(interval)

    threading.Thread(target=loop, name="object-cleanup", daemon=True).start()
    return stop


def cleanup_execution_root(
    owner_key: str,
    domain: str,
    resource_id: str,
    execution_id: str,
    *,
    repo: Optional[ObjectOperationRepo] = None,
) -> bool:
    """execution 终态后由调用方触发：无在途操作、无 pin 才整根删除，否则返回 False 稍后再试。

    命名空间任何一级是 symlink 都拒绝（不跟随链接递归删除别处），取得前缀 claim 后再复核一次。
    """
    root = execution_work_root(owner_key, domain, resource_id, execution_id)
    try:
        assert_plain_work_root(root)
    except PublicationError as exc:
        logger.warning("execution root cleanup refused: %s", exc)
        return False
    if not os.path.lexists(root):
        return True
    if not os.path.isdir(root):
        return False
    if (repo or ObjectOperationRepo()).count_in_flight(execution_id) > 0:
        return False
    if not CLAIMS.claim_prefix(root):
        return False
    try:
        assert_plain_work_root(root)
        shutil.rmtree(root)
    except PublicationError as exc:
        logger.warning("execution root cleanup refused after claim: %s", exc)
        return False
    finally:
        CLAIMS.release_prefix(root)
    return not os.path.lexists(root)


def prune_empty_execution_dirs() -> int:
    """启动期（单实例、尚无执行）自底向上移除 executions 下的空目录：只 rmdir，不删任何文件、不跟随链接。

    运行期不调用：与并发 makedirs 的资源/域目录会产生竞态。
    """
    base = work_roots_base()
    if not os.path.isdir(base) or os.path.islink(base):
        return 0
    removed = 0
    for current, _dirs, files in os.walk(base, topdown=False, followlinks=False):
        if current == base or files or os.path.islink(current):
            continue
        try:
            os.rmdir(current)  # 非空（含子链接/文件）时自然失败
            removed += 1
        except OSError:
            continue
    return removed


# ── 删除事务内的登记接口 ──────────────────────────────────────
def register_delete_snapshot(
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
    """删除事务内调用：把 retained/prepared/done 转 delete_pending（父删后凭据仍在）。"""
    return ObjectOperationRepo().register_delete(
        cur=cur,
        owner=owner,
        target_kind=target_kind,
        bucket=bucket,
        object_key=object_key,
        public_path=public_path,
        local_paths=local_paths or [],
        domain=domain,
        resource_id=resource_id,
        stage=stage,
        file_id=file_id,
        reason=reason,
    )


def register_resource_delete(
    cur: Any,
    *,
    owner: str,
    domain: str,
    resource_id: str,
    reason: str = "delete",
) -> List[Dict[str, Any]]:
    """父资源删除事务内（删除父行之前）登记它拥有的全部对象与精确本地文件。

    MinIO 行：bucket/key + 执行工作文件 + 该 public_path 的 `_refs` 缓存文件；
    legacy local 行：登记时 realpath 与行内精确 local_path；
    尚未建文件行的本资源发布 prepared：一并转 delete_pending，发布者上传前复核时会停止。
    推理暂存不在此转换（远端可能仍在取图），由 not-before 窗口清理。外部资源对这些对象的引用
    不改变归属；收藏副本不在此清单。
    """
    column = _PARENT_COLUMNS.get(domain)
    if column is None:
        raise ValueError(f"unsupported domain: {domain}")
    cur.execute(f"SELECT * FROM artifact_files WHERE {column} = %s ORDER BY file_id FOR UPDATE", (resource_id,))
    rows = list(cur.fetchall())
    owner_key = media_refs.owner_key(owner)
    registered: List[Dict[str, Any]] = []
    for row in rows:
        if str(row.get("owner") or "") != owner:
            raise OperationOwnershipError(f"file row owned by another user: {row.get('file_id')}")
        public = str(row.get("public_path") or "")
        common = {
            "cur": cur, "owner": owner, "public_path": public, "domain": domain,
            "resource_id": resource_id, "stage": str(row.get("stage") or ""),
            "file_id": str(row.get("file_id") or ""), "reason": reason,
        }
        if str(row.get("storage_kind") or "") == "local":
            path = str(row.get("local_path") or "") or media_refs.to_local(public)
            result = register_delete_snapshot(
                target_kind="local", bucket=LOCAL_BUCKET, object_key=os.path.realpath(path),
                local_paths=[path], **common,
            )
        else:
            aux = [str(row.get("local_path") or ""), str(media_refs._ref_target(public, owner_key))]
            result = register_delete_snapshot(
                target_kind="minio",
                bucket=str(row.get("bucket") or settings.STORAGE_BUCKET),
                object_key=str(row.get("object_key") or ""),
                local_paths=[path for path in aux if is_aux_cleanup_path(path)],
                **common,
            )
        registered.append(result)
    for op in ObjectOperationRepo().register_resource_prepared_delete(
        cur, owner=owner, domain=domain, resource_id=resource_id, reason=reason,
    ):
        registered.append({"operation_id": str(op["operation_id"]), "state": "delete_pending", "claimed": True})
    return registered


def register_saved_delete(cur: Any, *, owner: str, saved: Dict[str, Any], reason: str = "unsave") -> Dict[str, Any]:
    """取消收藏：删除 saved_items 前登记副本定位（domain=saved, resource_id=saved_id）。"""
    if str(saved.get("owner") or "") != owner:
        raise OperationOwnershipError(f"saved item owned by another user: {saved.get('item_id')}")
    return register_delete_snapshot(
        cur=cur,
        owner=owner,
        target_kind="minio",
        bucket=str(saved["bucket"]),
        object_key=str(saved["object_key"]),
        domain="saved",
        resource_id=str(saved["item_id"]),
        reason=reason,
    )


__all__ = [
    "CLAIMS",
    "ObjectCleanupWorker",
    "cleanup_execution_root",
    "current_boot_id",
    "is_aux_cleanup_path",
    "prune_empty_execution_dirs",
    "register_delete_snapshot",
    "register_resource_delete",
    "register_saved_delete",
    "start_cleanup_loop",
]
