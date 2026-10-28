"""Owner-checked media streaming.

Browser media is served by the backend, not by an anonymous StaticFiles mount.
Identity is the explicit `?user_login=` query (accepted bypass); there is no
cookie/302 flow. This endpoint is intentionally not protected by the API header
middleware because browsers cannot attach custom headers to <img>/<video>.
"""
from __future__ import annotations

import mimetypes
import os
from typing import Iterator, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse

from config import settings
from core.identity import safe_login
from core.media_refs import PUBLIC_PREFIX, to_local, to_public
from core.ownership import is_owner_login
from core.space_service import saved_media_row
from db.repositories import FileRepo
from storage import ObjectMissing, StorageError, get_storage

router = APIRouter(tags=["Media"])

_CHUNK = 256 * 1024


def _parse_range(raw: str, size: int) -> Optional[tuple[int, int]]:
    value = raw.strip()
    if not value.startswith("bytes=") or "," in value:
        return None
    spec = value[6:].strip()
    if "-" not in spec:
        return None
    start_raw, end_raw = spec.split("-", 1)
    if not start_raw and not end_raw:
        return None
    if not start_raw:
        suffix = int(end_raw)
        if suffix <= 0:
            return None
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _iter_file(handle, start: int, length: int) -> Iterator[bytes]:
    try:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        try:
            handle.close()
        except Exception:
            pass


def _iter_storage(handle, length: int) -> Iterator[bytes]:
    try:
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        try:
            handle.close()
            handle.release_conn()
        except Exception:
            pass


@router.api_route("/code/result/{path:path}", methods=["GET", "HEAD"])
async def get_result_media(
    path: str,
    request: Request,
    user_login: str = "",
    download: int = 0,
):
    try:
        login = safe_login(user_login)
    except HTTPException:
        raise HTTPException(401, "缺少或非法的用户身份")

    public = to_public(PUBLIC_PREFIX + path)
    if not public.startswith(PUBLIC_PREFIX):
        raise HTTPException(404, "Media not found")
    row = FileRepo().get_by_public_path(public)
    if not row or not is_owner_login(row.get("owner"), login):
        raise HTTPException(404, "Media not found")
    if row.get("missing_at"):
        raise HTTPException(404, "Media not found")

    storage_kind = str(row.get("storage_kind") or "")
    object_key = str(row.get("object_key") or "")
    local_path = str(row.get("local_path") or "")
    if storage_kind == "local":
        local = local_path or to_local(public)
        real = os.path.realpath(local)
        result_root = os.path.realpath(settings.RESULT_DIR)
        if real != result_root and not real.startswith(result_root + os.sep):
            raise HTTPException(404, "Media not found")
        try:
            size = os.path.getsize(real)
        except OSError:
            FileRepo().mark_missing(str(row["file_id"]), True)
            raise HTTPException(404, "Media not found")
        FileRepo().mark_missing(str(row["file_id"]), False)
    else:
        try:
            stat = await run_in_threadpool(get_storage().stat, object_key)
        except StorageError as exc:
            raise HTTPException(404, "Media not found") from exc
        if not stat:
            FileRepo().mark_missing(str(row["file_id"]), True)
            raise HTTPException(404, "Media not found")
        size = int(stat.get("size") or 0)

    range_header = request.headers.get("range", "")
    start, end = 0, max(0, size - 1)
    status = 200
    if range_header:
        try:
            parsed = _parse_range(range_header, size)
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{size}",
                    "Accept-Ranges": "bytes",
                },
            )
        start, end = parsed
        status = 206

    length = max(0, end - start + 1) if size else 0
    media_type = str(row.get("media_type") or "") or mimetypes.guess_type(path)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": media_type,
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if download:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(os.path.basename(path))}"
    else:
        headers["Content-Disposition"] = "inline"

    if request.method == "HEAD":
        return Response(status_code=status, headers=headers)

    if storage_kind == "local":
        try:
            handle = open(real, "rb")
        except OSError as exc:
            FileRepo().mark_missing(str(row["file_id"]), True)
            raise HTTPException(404, "Media not found") from exc
        return StreamingResponse(
            _iter_file(handle, start, length),
            status_code=status,
            headers=headers,
            media_type=media_type,
        )

    try:
        handle = await run_in_threadpool(
            lambda: get_storage().open_stream(object_key, offset=start, length=length)
        )
    except ObjectMissing as exc:
        FileRepo().mark_missing(str(row["file_id"]), True)
        raise HTTPException(404, "Media not found") from exc
    except StorageError as exc:
        raise HTTPException(404, "Media not found") from exc
    return StreamingResponse(
        _iter_storage(handle, length),
        status_code=status,
        headers=headers,
        media_type=media_type,
    )


@router.api_route("/api/space/saved/{saved_id}/media", methods=["GET", "HEAD"])
async def get_saved_media(
    saved_id: str,
    request: Request,
    user_login: str = "",
    download: int = 0,
):
    """Saved-media channel: query identity only, never the API header/cookie."""
    try:
        login = safe_login(user_login)
    except HTTPException:
        raise HTTPException(401, "缺少或非法的用户身份")
    row = saved_media_row(saved_id)
    if not row or not is_owner_login(row.get("owner"), login):
        raise HTTPException(404, "Media not found")
    bucket = str(row.get("bucket") or "")
    object_key = str(row.get("object_key") or "")
    if not bucket or not object_key:
        raise HTTPException(404, "Media not found")
    try:
        stat = await run_in_threadpool(get_storage().stat, object_key, bucket=bucket)
    except StorageError as exc:
        raise HTTPException(404, "Media not found") from exc
    if not stat:
        raise HTTPException(404, "Media not found")
    size = int(stat.get("size") or 0)
    range_header = request.headers.get("range", "")
    start, end = 0, max(0, size - 1)
    status = 200
    if range_header:
        try:
            parsed = _parse_range(range_header, size)
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
        start, end = parsed
        status = 206
    length = max(0, end - start + 1) if size else 0
    media_type = str(row.get("media_type") or "") or mimetypes.guess_type(object_key)[0] or "video/mp4"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": media_type,
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": (
            f"attachment; filename*=UTF-8''{quote(os.path.basename(object_key))}"
            if download
            else "inline"
        ),
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers)
    try:
        handle = await run_in_threadpool(
            lambda: get_storage().open_stream(object_key, offset=start, length=length, bucket=bucket)
        )
    except ObjectMissing as exc:
        raise HTTPException(404, "Media not found") from exc
    except StorageError as exc:
        raise HTTPException(404, "Media not found") from exc
    return StreamingResponse(_iter_storage(handle, length), status_code=status, headers=headers, media_type=media_type)
