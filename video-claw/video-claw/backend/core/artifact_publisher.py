"""Publication manifest and object publishing (design D11/D13/D14/D17).

Trust boundary:
- new outputs come only from this execution's work root
  (`CODE/work/executions/<owner_key>/<domain>/<resource_id>/<execution_id>/`);
- uploaded inputs must pass the owner guard (`temp/users/<owner_key>/...`, no `..`);
- materialized consumption values (`_refs` cache, legacy result files, temp copies)
  are mapped back to their canonical value through the run's `runtime_map` or the
  `ResolvedMedia.public_path` they carry;
- anything else under RESULT_DIR is only reusable when already indexed for the same
  owner (legacy local rows included), never uploaded as a fresh artifact.

Responsibilities: classify references, build immutable object keys/public paths,
register prepared operations (committed) before upload, freeze the source, upload
under a pin, verify (existence & size), and compensate **only** objects this batch
successfully claims for deletion.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from config import settings
from core.media_refs import PUBLIC_PREFIX, ResolvedMedia, is_remote, to_public
from core.resource_claims import CLAIMS, TargetBeingDeleted, local_identity, object_identity
from db.repositories.operations import ObjectOperationRepo
from storage import StorageError, get_storage

logger = logging.getLogger(__name__)

MEDIA_ROOT = "/code/result/_media/"
WORK_ROOT_SEGMENTS = ("work", "executions")
RESOURCE_KINDS = ("sessions", "tasks", "sandbox")


class PublicationError(ValueError):
    """Untrusted output path or invalid publication input."""


def new_execution_id() -> str:
    return uuid.uuid4().hex


def new_file_id() -> str:
    return uuid.uuid4().hex


def _segment(value: str, name: str) -> str:
    text = str(value or "")
    if not text or text in (".", "..") or any(ch in text for ch in ("/", "\\", "\x00")):
        raise PublicationError(f"invalid {name}: {value!r}")
    return text


def work_roots_base() -> str:
    return os.path.realpath(os.path.join(settings.CODE_DIR, *WORK_ROOT_SEGMENTS))


def execution_work_root(owner_key: str, domain: str, resource_id: str, execution_id: str) -> str:
    """D11 本地生成根（不解析 symlink 的规范路径）；路径段逐一校验，避免携带分隔符逃逸。

    使用方在信任该目录前必须调用 `assert_plain_work_root`：命名空间内任何一级是 symlink 都拒绝。
    """
    if domain not in RESOURCE_KINDS:
        raise PublicationError(f"unsupported domain: {domain}")
    return os.path.join(
        work_roots_base(),
        _segment(owner_key, "owner_key"),
        domain,
        _segment(resource_id, "resource_id"),
        _segment(execution_id, "execution_id"),
    )


def assert_plain_work_root(root: str) -> None:
    """执行根必须位于 executions 基目录下且各级都不是 symlink（解析后必须仍是它自己）。"""
    base = work_roots_base()
    if not _inside(root, base) or root == base:
        raise PublicationError(f"work root outside executions base: {root}")
    current = base
    for part in os.path.relpath(root, base).split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise PublicationError(f"symlink in execution namespace: {current}")
    if os.path.lexists(root) and os.path.realpath(root) != root:
        raise PublicationError(f"work root does not resolve to itself: {root}")


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _trusted_temp(public: str, owner_key: str) -> str:
    parts = public.split("/")
    if len(parts) < 3 or parts[0] != "users" or parts[1] != owner_key:
        raise PublicationError(f"temp upload not owned by current user: {public}")
    if any(part in ("", ".", "..") for part in parts):
        raise PublicationError(f"invalid temp upload path: {public}")
    root = os.path.realpath(os.path.join(settings.TEMP_DIR, "users", owner_key))
    local = os.path.realpath(os.path.join(settings.TEMP_DIR, public))
    if not _inside(local, root) or local == root:
        raise PublicationError(f"temp upload escapes owner root: {public}")
    if not os.path.isfile(local):
        raise PublicationError(f"temp upload missing: {public}")
    return local


def _public_of_absolute(path: str) -> str:
    real = os.path.realpath(path)
    result_root = os.path.realpath(settings.RESULT_DIR)
    if real.startswith(result_root + os.sep):
        return "/code/result/" + os.path.relpath(real, result_root).replace(os.sep, "/")
    return ""


class PublicationManifest:
    """Collect references for one execution; unknown paths are refused, not guessed."""

    def __init__(
        self,
        *,
        owner: str,
        owner_key: str,
        resource_kind: str,
        resource_id: str,
        stage: str,
        execution_id: str,
        work_root: Optional[str] = None,
        runtime_map: Optional[Dict[str, str]] = None,
    ) -> None:
        if resource_kind not in RESOURCE_KINDS:
            raise PublicationError(f"unsupported resource_kind: {resource_kind}")
        if not owner or not owner_key or not resource_id or not stage or not execution_id:
            raise ValueError("owner/owner_key/resource_id/stage/execution_id required")
        self.owner = owner
        self.owner_key = owner_key
        self.resource_kind = resource_kind
        self.resource_id = resource_id
        self.stage = _segment(stage, "stage")
        self.execution_id = execution_id
        self.work_root = os.path.abspath(
            work_root or execution_work_root(owner_key, resource_kind, resource_id, execution_id)
        )
        assert_plain_work_root(self.work_root)
        # 运行副本里的物化值 → 原 canonical；只用于反向映射，不扩大信任面
        self.runtime_map: Dict[str, str] = dict(runtime_map or {})
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []

    # ── 收集 ────────────────────────────────────────────────
    def _canonical_input(self, ref: Any) -> str:
        if isinstance(ref, ResolvedMedia) and str(getattr(ref, "public_path", "") or "").startswith(PUBLIC_PREFIX):
            return str(ref.public_path)
        raw = str(ref or "").strip()
        mapped = self.runtime_map.get(raw)
        return str(mapped) if mapped else raw

    def add(self, ref: Any, *, kind: str = "artifact", media_type: Optional[str] = None,
            locator: str = "") -> str:
        raw = self._canonical_input(ref)
        if not raw:
            return ""
        if is_remote(raw):
            self._remember(raw, {"remote": True, "kind": kind}, locator)
            return raw

        normalized = raw.replace("\\", "/")
        if normalized.startswith("users/"):
            local = _trusted_temp(normalized, self.owner_key)
            self._remember(normalized, {"kind": kind, "local": local, "media_type": media_type}, locator)
            return normalized

        # canonical/legacy 字符串（含 result/、code/result/、/app/code/result/ 形态）先归一识别
        public_form = to_public(normalized)
        if public_form.startswith(MEDIA_ROOT):
            self._remember(public_form, {"kind": kind, "reuse": True}, locator)
            return public_form
        if public_form.startswith(PUBLIC_PREFIX):
            # legacy 产物：只能复用已索引行，绝不重新上传任意 RESULT 文件
            self._remember(public_form, {"kind": kind, "reuse": True, "legacy": True}, locator)
            return public_form

        if not os.path.isabs(normalized):
            raise PublicationError(f"relative path is not a publication input: {raw}")
        real = os.path.realpath(normalized)
        if _inside(real, self.work_root) and real != self.work_root:
            if not os.path.isfile(real):
                raise PublicationError(f"work output missing: {raw}")
            # 新输出以本地工作路径作为源标识；发布后由 canonical_map 换成 public
            self._remember(real, {"kind": kind, "local": real, "media_type": media_type}, locator)
            return real

        public = _public_of_absolute(normalized)
        if public.startswith(MEDIA_ROOT):
            self._remember(public, {"kind": kind, "reuse": True}, locator)
            return public
        if public.startswith(PUBLIC_PREFIX):
            self._remember(public, {"kind": kind, "reuse": True, "legacy": True}, locator)
            return public
        raise PublicationError(f"unknown local path (refused): {raw}")

    def _remember(self, key: str, entry: Dict[str, Any], locator: str) -> None:
        entry.setdefault("canonical", key)
        if key not in self._entries:
            entry["locators"] = [locator] if locator else []
            self._order.append(key)
            self._entries[key] = entry
            return
        current = self._entries[key]
        if locator and locator not in current["locators"]:
            current["locators"].append(locator)

    @property
    def entries(self) -> List[Dict[str, Any]]:
        return [self._entries[key] for key in self._order]


# ── 复用 ────────────────────────────────────────────────────
def _parent_owner(row: Dict[str, Any]) -> str:
    from db.repositories import SandboxRepo, SessionRepo, TaskRepo

    if row.get("session_id"):
        return str(SessionRepo().owner_of(str(row["session_id"])) or "")
    if row.get("task_id"):
        return str(TaskRepo().owner_of(str(row["task_id"])) or "")
    if row.get("record_id"):
        return str(SandboxRepo().owner_of(str(row["record_id"])) or "")
    return ""


def reuse_existing(public_path: str, owner: str) -> Optional[Dict[str, Any]]:
    """复用已索引对象：文件行与父资源都必须属于调用方；不改绑、不重传。

    `_media/` 虚拟路径只能是 MinIO 行；legacy `/code/result/...` 行可以是 local 或已转换的 MinIO。
    缺失标记不影响复用（历史版本缺失不阻断），必需消费由 consumption 处理。
    """
    from db.repositories import FileRepo

    row = FileRepo().get_by_public_path(public_path)
    if not row or str(row.get("owner") or "") != owner:
        return None
    storage_kind = str(row.get("storage_kind") or "")
    if public_path.startswith(MEDIA_ROOT):
        if storage_kind != "minio":
            return None
    elif storage_kind not in ("minio", "local"):
        return None
    parent_owner = _parent_owner(row)
    if parent_owner != owner:
        return None
    return row


# ── 发布 ────────────────────────────────────────────────────
def _freeze(local: str) -> Tuple[int, int]:
    info = os.stat(local)
    return int(info.st_size), int(info.st_mtime_ns)


def _verified(key: str, size: int, bucket: str) -> bool:
    try:
        info = get_storage().stat(key, bucket=bucket)
    except StorageError:
        return False
    if info is None:
        return False
    actual = info.get("size")
    return actual is not None and int(actual) == int(size)


def _object_key(manifest: PublicationManifest, canonical: str, local: str, file_id: str) -> str:
    ext = os.path.splitext(local)[1].lower() or os.path.splitext(canonical)[1].lower()
    if not ext.replace(".", "").isalnum() or len(ext) > 10:
        ext = ""
    return (
        f"{manifest.owner_key}/{manifest.resource_kind}/{manifest.resource_id}/"
        f"{manifest.stage}/{manifest.execution_id}/{file_id}{ext}"
    )


def publish(
    manifest: PublicationManifest,
    *,
    boot_id: str = "",
    bucket: Optional[str] = None,
) -> Dict[str, Any]:
    """prepared 登记（已提交）→ pin → 状态复核 → upload → 冻结/大小核验；失败只清本批 claim 到的对象。"""
    target_bucket = str(bucket or settings.STORAGE_BUCKET)
    repo = ObjectOperationRepo()
    published: List[Dict[str, Any]] = []
    objects: List[Dict[str, str]] = []

    try:
        assert_plain_work_root(manifest.work_root)  # 收集后目录可能被替换：发布前再复核
        for entry in manifest.entries:
            canonical = str(entry.get("canonical") or "")
            if entry.get("remote"):
                continue
            if entry.get("reuse"):
                existing = reuse_existing(canonical, manifest.owner)
                if existing is None:
                    raise PublicationError(f"reuse target not indexed for this owner: {canonical}")
                published.append({
                    "canonical": canonical,
                    "public_path": str(existing["public_path"]),
                    "storage_kind": str(existing.get("storage_kind") or ""),
                    "object_key": str(existing.get("object_key") or ""),
                    "bucket": str(existing.get("bucket") or ""),
                    "local_path": str(existing.get("local_path") or ""),
                    "media_type": str(existing.get("media_type") or "application/octet-stream"),
                    "size_bytes": int(existing.get("size_bytes") or 0),
                    "file_id": str(existing["file_id"]),
                    "kind": str(existing.get("kind") or entry.get("kind") or "artifact"),
                    "operation_id": "",
                    "reused": True,
                    "locators": list(entry.get("locators") or []),
                })
                continue

            local = str(entry.get("local") or "")
            if canonical.startswith("users/"):
                local = _trusted_temp(canonical, manifest.owner_key)  # 发布时再次核验归属
            elif not (_inside(os.path.realpath(local), manifest.work_root) and os.path.isfile(local)):
                raise PublicationError(f"output outside execution work root: {canonical}")
            frozen = _freeze(local)
            media_type = str(
                entry.get("media_type") or mimetypes.guess_type(local)[0] or "application/octet-stream"
            )
            file_id = new_file_id()
            key = _object_key(manifest, canonical, local, file_id)
            public = MEDIA_ROOT + key
            operation_id = repo.register_prepared(
                owner=manifest.owner,
                target_kind="minio",
                bucket=target_bucket,
                object_key=key,
                domain=manifest.resource_kind,
                resource_id=manifest.resource_id,
                stage=manifest.stage,
                execution_id=manifest.execution_id,
                boot_id=boot_id,
                file_id=file_id,
                public_path=public,
                local_paths=[local] if not canonical.startswith("users/") else [],
                reason="publish",
            )
            objects.append({"operation_id": operation_id, "bucket": target_bucket, "object_key": key})
            try:
                with CLAIMS.pinned([object_identity(target_bucket, key), local_identity(local)]):
                    state = (repo.get(operation_id) or {}).get("state")
                    if state != "prepared":
                        # 父资源已在删除：不再上传，避免删除后落入孤儿
                        raise PublicationError(f"target no longer prepared (state={state}): {canonical}")
                    try:
                        get_storage().upload(local, key, media_type, bucket=target_bucket)
                    except StorageError as exc:
                        raise PublicationError(f"upload failed for {canonical}: {exc}") from exc
                    if _freeze(local) != frozen:
                        raise PublicationError(f"source changed during upload: {canonical}")
                    if not _verified(key, frozen[0], target_bucket):
                        raise PublicationError(f"upload unverified for {canonical}")
            except TargetBeingDeleted as exc:
                raise PublicationError(f"target is being deleted: {canonical}") from exc
            published.append({
                "canonical": canonical,
                "public_path": public,
                "storage_kind": "minio",
                "object_key": key,
                "bucket": target_bucket,
                "local_path": local if not canonical.startswith("users/") else "",
                "media_type": media_type,
                "size_bytes": frozen[0],
                "file_id": file_id,
                "kind": str(entry.get("kind") or "artifact"),
                "operation_id": operation_id,
                "reused": False,
                "locators": list(entry.get("locators") or []),
            })
    except Exception as exc:
        try:
            issues = abort_publication({"objects": list(objects)})
        except Exception as comp_exc:  # 补偿自身失败不得掩盖原因；残留仍在操作日志中
            logger.exception("publication compensation raised")
            issues = [{"bucket": "", "object_key": "", "error": f"compensation raised: {comp_exc}"}]
        message = str(exc)
        if issues:
            raise PublicationError(f"{message}; compensation issues: {issues}") from exc
        if isinstance(exc, PublicationError):
            raise
        raise PublicationError(message) from exc

    return {
        "owner": manifest.owner,
        "resource_kind": manifest.resource_kind,
        "resource_id": manifest.resource_id,
        "stage": manifest.stage,
        "execution_id": manifest.execution_id,
        "published": published,
        "operation_ids": [item["operation_id"] for item in objects],
        "objects": list(objects),
        "bucket": target_bucket,
        "canonical_map": {item["canonical"]: item["public_path"] for item in published},
    }


def _next_retry(attempts: int) -> int:
    delay = min(60 * (2 ** min(int(attempts), 6)), 3600)
    return int(time.time() * 1000) + delay * 1000


def abort_publication(result: Dict[str, Any]) -> List[Dict[str, str]]:
    """确定失败后的补偿：只删 claim 成功的 prepared；retained/已结束/他人租约一律跳过。

    删除未确认（删除异常、stat 不可判定或仍存在）时保留 delete_pending 交清理器退避重试。
    """
    repo = ObjectOperationRepo()
    failures: List[Dict[str, str]] = []
    for item in result.get("objects") or []:
        operation_id = str(item["operation_id"])
        bucket = str(item.get("bucket") or result.get("bucket") or settings.STORAGE_BUCKET)
        key = str(item["object_key"])
        identity = object_identity(bucket, key)
        if not CLAIMS.claim(identity):
            # 进程内仍有人持有（理论上只有发布者自身），交清理器稍后处理
            if repo.claim_for_delete(operation_id):
                repo.mark_failure(operation_id, "compensation deferred: target in use", _next_retry(0))
                failures.append({"bucket": bucket, "object_key": key, "error": "in use"})
            continue
        try:
            if not repo.claim_for_delete(operation_id):
                continue  # 未取得删除资格（已 retained/已结束/他人租约）→ 不删
            try:
                get_storage().delete(key, bucket=bucket)
                still_there = get_storage().stat(key, bucket=bucket) is not None
            except Exception as exc:
                repo.mark_failure(operation_id, f"compensation delete failed: {exc}", _next_retry(0))
                failures.append({"bucket": bucket, "object_key": key, "error": str(exc)})
                continue
            if still_there:
                repo.mark_failure(operation_id, "object still present after delete", _next_retry(0))
                failures.append({"bucket": bucket, "object_key": key, "error": "still present"})
                continue
            repo.mark_done(operation_id)
        finally:
            CLAIMS.release(identity)
    return failures


def publication_outcome(result: Dict[str, Any]) -> str:
    """提交结果未知时按操作状态判定：committed / rolled_back / unknown（不盲目补偿）。"""
    ids = [str(op) for op in result.get("operation_ids") or [] if op]
    if not ids:
        return "unknown"
    try:
        states = ObjectOperationRepo().get_states(ids)
    except Exception as exc:  # DB 仍不可达：保持 prepared，交下一进程清理
        logger.warning("publication outcome lookup failed: %s", exc)
        return "unknown"
    values = {states.get(op, "") for op in ids}
    if values == {"retained"}:
        return "committed"
    if values == {"prepared"}:
        return "rolled_back"
    # delete_pending/done 也可能发生在成功提交之后（父删除/清理），不能据此认定回滚
    return "unknown"


def build_file_rows(
    result: Dict[str, Any],
    *,
    owner: str,
    resource_kind: str,
    resource_id: str,
    stage: str,
) -> List[Dict[str, Any]]:
    """把发布结果转成 artifact_files 行（供同一提交事务写入）。复用对象不写行。"""
    if (result.get("owner"), result.get("resource_kind"), result.get("resource_id")) != (
        owner, resource_kind, resource_id,
    ):
        raise PublicationError("publication result belongs to another owner/resource")
    parent = {
        "session_id": resource_id if resource_kind == "sessions" else None,
        "task_id": resource_id if resource_kind == "tasks" else None,
        "record_id": resource_id if resource_kind == "sandbox" else None,
    }
    rows = []
    for item in result["published"]:
        if item.get("reused"):
            continue
        rows.append({
            "file_id": str(item["file_id"]),
            "owner": owner,
            "stage": stage,
            "kind": str(item["kind"]),
            "storage_kind": "minio",
            "bucket": str(item["bucket"]),
            "object_key": str(item["object_key"]),
            "public_path": str(item["public_path"]),
            "local_path": str(item["local_path"]) or None,
            "media_type": str(item["media_type"]),
            "size_bytes": int(item["size_bytes"]),
            **parent,
        })
    return rows
