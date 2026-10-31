"""Core-side preparation of inference media references (design D14/D16/D17).

Core owns ownership checks, materialization, presign and legacy HTTP staging;
`models/*` only consumes the prepared `MediaReference`.

Two promises must actually be implementable:
- MinIO-only, multipart/VLM adapter → download to a local working copy (`need_local`);
- legacy/local file consumed by an HTTP-only adapter → copy into a managed staging
  object of the *receiving* session/task/record execution and hand out its presign
  (never a bare `/code` gateway URL).

Staging lifecycle (no artifact_files row, never retained):
1. register `prepared` with not-before = now + presign TTL + margin (committed before upload);
2. upload under an object pin, verify size;
3. every presign first extends not-before; if the row was already claimed, no URL is issued;
4. after the request reached a terminal state and retries are over, `release_staging(..., remote_done=True)`
   hands it to the cleaner immediately; otherwise it is removed after the last signature expires.
Callers keep `CLAIMS.pinned([staged.identity])` around the remote request so the cleaner
defers even past not-before.
"""
from __future__ import annotations

import mimetypes
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from config import settings
from core.media_refs import (
    PUBLIC_PREFIX,
    MediaNotFound,
    _check_public_owner,
    ensure_local,
    is_remote,
    to_local,
    to_public,
)
from core.resource_claims import CLAIMS, TargetBeingDeleted, object_identity
from db.repositories.operations import STAGING_REASON, ObjectOperationRepo
from models.media_types import HTTP_SCHEMES, MediaReference, scheme_of
from storage import StorageError, get_storage

S3_PRESIGN_MAX_TTL = 7 * 24 * 3600
STAGING_STAGE = "staging"
STAGING_MARGIN_SECONDS = 300
_STAGING_DOMAINS = ("sessions", "tasks", "sandbox")


class PresignTTLTooLong(ValueError):
    """预签名 TTL 超出 S3 上限；必须显式失败而不是静默截短。"""


class StagingUnavailable(RuntimeError):
    """legacy 文件无法转为受管暂存（无 MinIO / 文件缺失 / 上传核验失败）。"""


@dataclass(frozen=True)
class StagedObject:
    operation_id: str
    bucket: str
    object_key: str

    @property
    def identity(self) -> str:
        return object_identity(self.bucket, self.object_key)


def _generation_timeout() -> int:
    """真实配置：VC_TIMEOUT_IMAGE / VC_TIMEOUT_VIDEO（默认 10800 秒）。"""
    timeout = 0
    for attr in ("TIMEOUT_VIDEO", "TIMEOUT_IMAGE"):
        try:
            timeout = max(timeout, int(getattr(settings, attr, 0) or 0))
        except (TypeError, ValueError):
            continue
    return timeout


def presign_ttl() -> int:
    """max(storage.presign_ttl, 模型总超时 + 300)，超 S3 上限显式失败。"""
    base = int(getattr(settings, "STORAGE_PRESIGN_TTL", 3600) or 3600)
    timeout = _generation_timeout()
    ttl = max(base, timeout + 300) if timeout else base
    if ttl > S3_PRESIGN_MAX_TTL:
        raise PresignTTLTooLong(
            f"presign ttl {ttl}s exceeds S3 limit {S3_PRESIGN_MAX_TTL}s; 请调小模型超时或 presign_ttl"
        )
    return ttl


def _now_ms() -> int:
    return int(time.time() * 1000)


def _user_owner_key(user: Any) -> str:
    from core.media_refs import _user_owner_key as owner_key_of

    return owner_key_of(user)


def _user_login(user: Any) -> str:
    from core.media_refs import _user_login as login_of

    return login_of(user)


def _trusted_temp_local(public: str, user: Any) -> str:
    """`users/<owner_key>/...` 上传件：校验归属与目录包含性，不直接拼路径。"""
    owner_key = _user_owner_key(user)
    parts = public.split("/")
    if len(parts) < 3 or parts[0] != "users" or parts[1] != owner_key:
        raise MediaNotFound(public)
    if any(part in ("", ".", "..") for part in parts):
        raise MediaNotFound(public)
    root = os.path.realpath(os.path.join(settings.TEMP_DIR, "users", owner_key))
    local = os.path.realpath(os.path.join(settings.TEMP_DIR, public))
    if not (local == root or local.startswith(root + os.sep)) or not os.path.isfile(local):
        raise MediaNotFound(public)
    return local


def _local_if_present(public: str, row: dict) -> str:
    candidate = str(row.get("local_path") or "")
    if candidate and os.path.isfile(candidate):
        return candidate
    try:
        legacy = to_local(public)
    except Exception:
        return ""
    return legacy if os.path.isfile(legacy) else ""


def build_media_reference(value: Any, user: Any, *, need_local: bool = False) -> MediaReference:
    """canonical 值 → 供适配器消费的 MediaReference（归属校验在此完成）。

    受管对象的预签名只在适配器实际取候选时生成（每次重试取新签名），不在准备阶段预签。
    """
    raw = str(value or "").strip()
    if not raw:
        return MediaReference()
    if is_remote(raw):
        return MediaReference(public_path=raw, remote_uri=raw)
    normalized = raw.replace("\\", "/")
    if normalized.startswith("users/"):
        return MediaReference(public_path=normalized, local_path=_trusted_temp_local(normalized, user))
    public = to_public(normalized)
    if not public.startswith(PUBLIC_PREFIX):
        raise MediaNotFound(raw)
    row = _check_public_owner(public, user)
    local = _local_if_present(public, row)
    if str(row.get("storage_kind") or "local") == "local":
        return MediaReference(public_path=public, local_path=local)
    object_key = str(row.get("object_key") or "")
    bucket = str(row.get("bucket") or settings.STORAGE_BUCKET)
    if need_local and not local:
        # MinIO-only + multipart：按需下载到工作副本（trusted 由 core 保证）
        local = ensure_local(public, user)
    ttl = presign_ttl()

    def factory() -> List[str]:
        if not object_key:
            return []
        try:
            return [get_storage().presign(object_key, ttl=ttl, bucket=bucket)]
        except StorageError:
            return []

    return MediaReference(public_path=public, local_path=local, presign_factory=factory)


def _segment(value: str, name: str) -> str:
    text = str(value or "")
    if not text or text in (".", "..") or any(ch in text for ch in ("/", "\\", "\x00")):
        raise StagingUnavailable(f"invalid staging {name}: {value!r}")
    return text


def stage_for_http(
    value: Any,
    user: Any,
    *,
    domain: str,
    resource_id: str,
    execution_id: str,
    boot_id: str = "",
) -> Tuple[MediaReference, Optional[StagedObject]]:
    """legacy/local 文件 → 接收资源名下的受管暂存对象 + 按需预签名（不新建 artifact_files 行）。

    远程或已是受管对象时原样返回且不暂存（第二个返回值为 None）。
    """
    ref = build_media_reference(value, user)
    if ref.remote_uri or ref.presign_factory is not None:
        return ref, None
    if not ref.local_path:
        raise StagingUnavailable(f"legacy reference has no readable local file: {value}")
    if domain not in _STAGING_DOMAINS:
        raise StagingUnavailable(f"unsupported staging domain: {domain}")
    owner_key = _user_owner_key(user)
    ext = os.path.splitext(ref.local_path)[1].lower()
    file_id = uuid.uuid4().hex
    key = (
        f"{owner_key}/{domain}/{_segment(resource_id, 'resource_id')}/{STAGING_STAGE}/"
        f"{_segment(execution_id, 'execution_id')}/{file_id}{ext}"
    )
    bucket = str(settings.STORAGE_BUCKET)
    ttl = presign_ttl()
    lease_ms = (ttl + STAGING_MARGIN_SECONDS) * 1000
    repo = ObjectOperationRepo()
    operation_id = repo.register_prepared(
        owner=_user_login(user),
        target_kind="minio",
        bucket=bucket,
        object_key=key,
        domain=domain,
        resource_id=resource_id,
        stage=STAGING_STAGE,
        execution_id=execution_id,
        boot_id=boot_id,
        file_id=file_id,
        public_path="",
        local_paths=[],  # 源文件属于 legacy 行，绝不随暂存清理
        reason=STAGING_REASON,
        not_before=_now_ms() + lease_ms,
    )
    staged = StagedObject(operation_id, bucket, key)
    media_type = mimetypes.guess_type(ref.local_path)[0] or "application/octet-stream"
    try:
        with CLAIMS.pinned([staged.identity]):
            if (repo.get(operation_id) or {}).get("state") != "prepared":
                raise StagingUnavailable("staging target was claimed before upload")
            size = os.path.getsize(ref.local_path)
            get_storage().upload(ref.local_path, key, media_type, bucket=bucket)
            info = get_storage().stat(key, bucket=bucket)
            if info is None or int(info.get("size") or -1) != size:
                raise StagingUnavailable(f"staging upload unverified: {bucket}/{key}")
    except Exception as exc:
        release_staging(staged, remote_done=True)  # 交清理器删除可能已部分写入的对象
        if isinstance(exc, StagingUnavailable):
            raise
        if isinstance(exc, TargetBeingDeleted):
            raise StagingUnavailable(f"staging target is being deleted: {bucket}/{key}") from exc
        raise StagingUnavailable(f"staging upload failed: {exc}") from exc

    def factory() -> List[str]:
        # 先持久延长 not-before 再签发；行已被认领删除则不再给出 URL
        if not repo.extend_not_before(operation_id, _now_ms() + lease_ms):
            return []
        try:
            return [get_storage().presign(key, ttl=ttl, bucket=bucket)]
        except StorageError:
            return []

    staged_ref = MediaReference(
        public_path=str(ref.public_path),
        local_path=ref.local_path,
        presign_factory=factory,
    )
    return staged_ref, staged


def release_staging(staged: StagedObject, *, remote_done: bool) -> bool:
    """推理请求确认终态且重试结束：remote_done=True 立即交清理器；否则保留到最后签名过期。"""
    if not remote_done:
        return False
    return ObjectOperationRepo().release_staging(staged.operation_id)


def reference_kind(ref: MediaReference) -> str:
    """诊断用：remote / managed_http / local_only / empty。"""
    if ref.remote_uri:
        return "remote"
    if any(scheme_of(uri) in HTTP_SCHEMES for uri in ref.candidates()):
        return "managed_http"
    if ref.local_path:
        return "local_only"
    return "empty"


def needs_staging(value: Any, user: Any) -> bool:
    """该引用是否必须走受管暂存（legacy 本地文件且需要 HTTP）。"""
    try:
        ref = build_media_reference(value, user)
    except MediaNotFound:
        return False
    return bool(ref.local_path) and not ref.remote_uri and ref.presign_factory is None


__all__ = [
    "PresignTTLTooLong",
    "StagedObject",
    "StagingUnavailable",
    "build_media_reference",
    "needs_staging",
    "presign_ttl",
    "reference_kind",
    "release_staging",
    "stage_for_http",
]
