"""Canonical media path handling.

Canonical values are what can be persisted (public_path / remote URL / temp relative);
materialized values are local files used by agents, ffmpeg and model clients.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

from config import settings
from db.repositories.files import FileRepo
from storage import ObjectMissing, StorageError, get_storage

REMOTE_PREFIXES = ("http://", "https://", "data:", "oss://")
PUBLIC_PREFIX = "/code/result/"


class MediaInputError(ValueError):
    """Invalid user-controlled media input."""


class MediaNotFound(MediaInputError):
    """Media is not owned/found; callers should return 404."""


class MediaMissingError(MediaNotFound):
    """A DB-owned object is missing from storage; callers may mark missing_at."""


class MediaUnavailable(MediaInputError):
    """Storage could not be reached/read; distinct from a confirmed NotFound."""


class ResolvedMedia(str):
    """A str-compatible canonical/materialized media value carrying trust metadata."""

    def __new__(cls, value: str, *, trusted: bool = True, public_path: str = "", local_path: str = ""):
        obj = super().__new__(cls, value)
        obj.trusted = trusted
        obj.public_path = public_path or (value if str(value).startswith(PUBLIC_PREFIX) else "")
        obj.local_path = local_path
        # core 为推理适配器准备的引用值对象（models.media_types.MediaReference），可为空
        obj.reference = None
        return obj


_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_REFS_ROOT = Path(settings.CODE_DIR) / "work" / "_refs"


def owner_key(login: str) -> str:
    return hashlib.sha256(str(login).strip().encode("utf-8")).hexdigest()[:16]


def _user_login(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("login") or "")
    return str(getattr(user, "login", "") or "")


def _user_owner_key(user: Any) -> str:
    if isinstance(user, dict) and user.get("owner_key"):
        return str(user["owner_key"])
    value = getattr(user, "owner_key", "") if not isinstance(user, dict) else ""
    return str(value or owner_key(_user_login(user)))


def is_remote(value: str) -> bool:
    return str(value).lower().startswith(REMOTE_PREFIXES)


def to_public(value: str) -> str:
    """Normalize historical local forms to /code/result/...; remote values are unchanged."""
    if not value:
        return ""
    raw = str(value)
    if is_remote(raw) or raw.startswith(("blob:", "file://")):
        return raw
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/app/code/result/"):
        return "/code/result/" + normalized[len("/app/code/result/"):]
    if normalized.startswith("code/result/"):
        return "/code/result/" + normalized[len("code/result/"):]
    if normalized.startswith("result/"):
        return "/code/result/" + normalized[len("result/"):]
    if normalized.startswith("/code/result/"):
        return posixpath.normpath(normalized)
    return normalized


def to_local(value: str) -> str:
    """Map a canonical public path to the legacy result directory (no download)."""
    public = to_public(value)
    if not public.startswith(PUBLIC_PREFIX):
        return str(value)
    rel = posixpath.normpath(public[len("/code"):]).lstrip("/")
    path = os.path.realpath(os.path.join(settings.CODE_DIR, rel))
    root = os.path.realpath(settings.RESULT_DIR)
    if path != root and not path.startswith(root + os.sep):
        raise MediaInputError("public path escaped result root")
    return path


def _check_public_owner(public: str, user: Any) -> Dict[str, Any]:
    row = FileRepo().get_by_public_path(public)
    if not row or row.get("owner") != _user_login(user):
        raise MediaNotFound(public)
    return row


def _canonical_temp(value: str, user: Any) -> str:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if len(parts) < 2 or parts[0] != "users" or parts[1] != _user_owner_key(user):
        raise MediaInputError("temp upload is not owned by current user")
    if any(part in ("", ".", "..") for part in parts):
        raise MediaInputError("invalid temp upload path")
    return normalized


def canonicalize(value: str, user: Any, purpose: str = "") -> ResolvedMedia:
    """Validate ownership and return a value safe to persist (never a _refs path)."""
    raw = str(value or "").strip()
    if not raw:
        raise MediaInputError("empty media path")
    if is_remote(raw):
        return ResolvedMedia(raw, public_path=raw)
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        raw = unquote(parsed.path)
    if raw.startswith(PUBLIC_PREFIX):
        public = to_public(raw)
        _check_public_owner(public, user)
        return ResolvedMedia(public, public_path=public)
    if raw.startswith("/"):
        real = os.path.realpath(raw)
        result_root = os.path.realpath(settings.RESULT_DIR)
        temp_root = os.path.realpath(settings.TEMP_DIR)
        if real == result_root or real.startswith(result_root + os.sep):
            rel = os.path.relpath(real, result_root).replace(os.sep, "/")
            public = to_public("result/" + rel)
            _check_public_owner(public, user)
            return ResolvedMedia(public, public_path=public)
        if real == temp_root or real.startswith(temp_root + os.sep):
            rel = os.path.relpath(real, temp_root).replace(os.sep, "/")
            return ResolvedMedia(_canonical_temp(rel, user))
        raise MediaNotFound(raw)
    if raw.startswith("users/"):
        return ResolvedMedia(_canonical_temp(raw, user))
    public = to_public(raw)
    if public.startswith(PUBLIC_PREFIX):
        _check_public_owner(public, user)
        return ResolvedMedia(public, public_path=public)
    raise MediaInputError("unsupported media path")


MEDIA_PAYLOAD_KEYS = {
    "selected",
    "selected_images",
    "selected_clips",
    "versions",
    "image",
    "images",
    "image_path",
    "image_paths",
    "image_asset",
    "video",
    "videos",
    "video_path",
    "video_paths",
    "video_asset",
    "character_image_path",
    "character_asset",
    "goods_image_path",
    "goods_asset",
    "reference_image",
    "reference_images",
    "reference_video",
    "reference_videos",
    "audio_url",
    "audio_reference_url",
    "first_clip_path",
    "reference_image_path",
    "output_path",
}


def canonicalize_media_value(value: str, user: Any, session_id: str = "") -> ResolvedMedia:
    """Canonicalize a media input, accepting legacy result paths only inside the owned session."""
    try:
        return canonicalize(value, user)
    except MediaNotFound:
        public = to_public(value)
        if session_id and public.startswith(PUBLIC_PREFIX) and f"/{session_id}/" in public:
            return ResolvedMedia(public, public_path=public)
        raise


def materialize_media_value(value: str, user: Any) -> str:
    """Return a local path for runtime consumption; remote URLs stay unchanged.

    The MinIO download path keeps its ``ResolvedMedia`` type so downstream
    ``guard_media_input``/``copy_input_file`` can accept the internal ``_refs``
    cache without widening the raw user-input allowlist.
    """
    raw = str(value or "").strip()
    if not raw or is_remote(raw):
        return raw
    if raw.startswith("users/"):
        return os.path.realpath(os.path.join(settings.TEMP_DIR, raw))
    public = to_public(raw)
    if public.startswith(PUBLIC_PREFIX):
        local = to_local(public)
        if os.path.isfile(local):
            return local
    return materialize(raw, user)


def materialize_media_payload(payload: Any, user: Any) -> Any:
    """Recursively materialize known media fields for agents/model clients."""
    if isinstance(payload, dict):
        result: Dict[str, Any] = {}
        for key, value in payload.items():
            if key in MEDIA_PAYLOAD_KEYS:
                if isinstance(value, str) and value.strip():
                    result[key] = materialize_media_value(value, user)
                elif isinstance(value, list):
                    result[key] = [
                        materialize_media_value(item, user)
                        if isinstance(item, str) and item.strip()
                        else item
                        for item in value
                    ]
                elif isinstance(value, dict):
                    result[key] = {
                        sub_key: materialize_media_value(sub_value, user)
                        if isinstance(sub_value, str) and sub_value.strip()
                        else materialize_media_payload(sub_value, user)
                        for sub_key, sub_value in value.items()
                    }
                else:
                    result[key] = materialize_media_payload(value, user)
            else:
                result[key] = materialize_media_payload(value, user)
        return result
    if isinstance(payload, list):
        return [materialize_media_payload(item, user) for item in payload]
    return payload


def canonicalize_media_payload(payload: Any, user: Any, session_id: str = "") -> Any:
    """Recursively canonicalize known media fields before persistence."""
    if isinstance(payload, dict):
        result: Dict[str, Any] = {}
        for key, value in payload.items():
            if key in MEDIA_PAYLOAD_KEYS:
                if isinstance(value, str) and value.strip():
                    result[key] = str(canonicalize_media_value(value, user, session_id))
                elif isinstance(value, list):
                    result[key] = [
                        str(canonicalize_media_value(item, user, session_id))
                        if isinstance(item, str) and item.strip()
                        else item
                        for item in value
                    ]
                elif isinstance(value, dict):
                    result[key] = {
                        sub_key: str(canonicalize_media_value(sub_value, user, session_id))
                        if isinstance(sub_value, str) and sub_value.strip()
                        else canonicalize_media_payload(sub_value, user, session_id)
                        for sub_key, sub_value in value.items()
                    }
                else:
                    result[key] = canonicalize_media_payload(value, user, session_id)
            else:
                result[key] = canonicalize_media_payload(value, user, session_id)
        return result
    if isinstance(payload, list):
        return [canonicalize_media_payload(item, user, session_id) for item in payload]
    return payload


def _refs_dir(owner: str) -> Path:
    return _REFS_ROOT / owner


def _lock_for(public: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(public, threading.Lock())


def _ref_target(public: str, owner_key_value: str) -> Path:
    return _refs_dir(owner_key_value) / (hashlib.sha256(public.encode()).hexdigest()[:16] + os.path.splitext(public)[1])


def _touch(path: Path) -> None:
    try:
        path.touch(exist_ok=True)
    except OSError:
        pass


def ensure_local(public_path: str, user: Any) -> str:
    """Download a public object to _refs with a per-key lock and atomic replace.

    The download reads the row's own bucket and holds an in-process pin on the cache
    file, so the cleaner cannot remove the exact cache entry mid-download.
    """
    from core.resource_claims import CLAIMS, TargetBeingDeleted, local_identity

    public = to_public(public_path)
    row = _check_public_owner(public, user)
    if row.get("storage_kind") == "local":
        local = row.get("local_path") or to_local(public)
        if not os.path.isfile(local):
            FileRepo().mark_missing(str(row["file_id"]), True)
            raise MediaMissingError(public)
        FileRepo().mark_missing(str(row["file_id"]), False)
        return local
    target = _ref_target(public, _user_owner_key(user))
    bucket = str(row.get("bucket") or "")
    try:
        with CLAIMS.pinned([local_identity(str(target))]), _lock_for(public):
            if target.is_file():
                _touch(target)
                return str(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                if bucket:
                    get_storage().download(str(row.get("object_key") or ""), str(tmp), bucket=bucket)
                else:
                    get_storage().download(str(row.get("object_key") or ""), str(tmp))
                os.replace(tmp, target)
            except ObjectMissing as exc:
                FileRepo().mark_missing(str(row["file_id"]), True)
                raise MediaMissingError(public) from exc
            except StorageError as exc:
                raise MediaUnavailable(f"media download failed: {exc}") from exc
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
    except TargetBeingDeleted as exc:
        raise MediaNotFound(public) from exc
    _touch(target)
    return str(target)


def cleanup_session_refs(session_id: str) -> int:
    """Delete cached refs mapped to a session via artifact_files.session_id."""
    if not str(getattr(settings, "DATABASE_DSN", "") or "").strip():
        return 0
    rows = FileRepo().list_by_session(session_id)
    removed = 0
    for row in rows:
        public = to_public(str(row.get("public_path") or ""))
        if not public.startswith(PUBLIC_PREFIX):
            continue
        owner = str(row.get("owner") or "")
        target = _ref_target(public, owner_key(owner))
        try:
            if target.is_file():
                target.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def cleanup_session_objects(session_id: str) -> int:
    """Delete stored objects owned by a session; local files are unlinked."""
    if not str(getattr(settings, "DATABASE_DSN", "") or "").strip():
        return 0
    rows = FileRepo().list_by_session(session_id)
    removed = 0
    for row in rows:
        try:
            if row.get("storage_kind") == "local":
                local = row.get("local_path") or to_local(str(row.get("public_path") or ""))
                if os.path.isfile(local):
                    os.remove(local)
                    removed += 1
            elif row.get("object_key"):
                get_storage().delete(str(row["object_key"]))
                removed += 1
        except (OSError, StorageError):
            continue
    return removed


REFS_RECENT_GUARD_SECONDS = 600


def prune_refs(
    max_age_seconds: int = 7 * 24 * 3600,
    max_bytes: int = 2 * 1024**3,
    recent_guard_seconds: int = REFS_RECENT_GUARD_SECONDS,
) -> int:
    """Bound _refs by age and total bytes (oldest mtime evicted first).

    只支持单实例启动期调用：跨进程的在用缓存无法感知。最近 ``recent_guard_seconds`` 内访问过
    （ensure_local 命中时会 touch）的条目与本进程持有 pin 的条目一律跳过。
    """
    from core.resource_claims import CLAIMS, local_identity

    if not _REFS_ROOT.exists():
        return 0

    def unlink(path: Path) -> bool:
        identity = local_identity(str(path))
        if not CLAIMS.claim(identity):
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False
        finally:
            CLAIMS.release(identity)

    now = time.time()
    files: List[Path] = []
    total = 0
    removed = 0
    for path in _REFS_ROOT.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        age = now - stat.st_mtime
        if age < recent_guard_seconds:
            total += stat.st_size  # 计入容量但不作为淘汰候选
            continue
        if age > max_age_seconds:
            if unlink(path):
                removed += 1
                continue
        files.append(path)
        total += stat.st_size
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        if total <= max_bytes:
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if unlink(path):
            total -= size
            removed += 1
    return removed


def materialize(value: str, user: Any) -> ResolvedMedia:
    """Return a trusted local path for a canonical value; raw strings are re-validated."""
    if isinstance(value, ResolvedMedia) and value.trusted:
        canonical = value
    else:
        canonical = canonicalize(str(value), user)
    if is_remote(str(canonical)):
        return ResolvedMedia(str(canonical), public_path=str(canonical))
    if str(canonical).startswith("users/"):
        local = os.path.realpath(os.path.join(settings.TEMP_DIR, str(canonical)))
        return ResolvedMedia(local, local_path=local)
    local = ensure_local(str(canonical), user)
    return ResolvedMedia(local, public_path=str(canonical), local_path=local)


def rehydrate(payload: Any, user: Any) -> Any:
    """Rebuild trusted metadata from a DB payload without trusting raw strings."""
    if isinstance(payload, str):
        value = to_public(payload)
        if value.startswith(PUBLIC_PREFIX):
            _check_public_owner(value, user)
            return ResolvedMedia(value, public_path=value)
        return ResolvedMedia(payload, trusted=is_remote(payload))
    if isinstance(payload, list):
        return [rehydrate(item, user) for item in payload]
    if isinstance(payload, dict):
        return {key: rehydrate(value, user) for key, value in payload.items()}
    return payload


def reference_urls(public_path: str, user: Any) -> List[str]:
    """Inference-facing URLs: presigned object + file:// fallback; never browser URLs."""
    public = to_public(public_path)
    row = _check_public_owner(public, user)
    local = row.get("local_path") or to_local(public)
    urls: List[str] = []
    if row.get("storage_kind") != "local" and row.get("object_key"):
        try:
            urls.append(get_storage().presign(str(row["object_key"])))
        except StorageError:
            pass
    urls.append(Path(local).as_uri())
    return urls
