"""Owner-checked media streaming.

Browser media is served by the backend, not by an anonymous StaticFiles mount.
Identity is the explicit `?user_login=` query (accepted bypass); there is no
cookie/302 flow. This endpoint is intentionally not protected by the API header
middleware because browsers cannot attach custom headers to <img>/<video>.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import mimetypes
import os
import threading
import time
import weakref
from typing import Iterator, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from starlette.requests import ClientDisconnect

from config import settings
from core import media_metrics
from core.identity import safe_login
from core.media_refs import PUBLIC_PREFIX, to_local, to_public
from core.ownership import is_owner_login
from core.space_service import saved_media_row
from db.repositories import FileRepo
from storage import ObjectMissing, StorageError, get_storage

router = APIRouter(tags=["Media"])
logger = logging.getLogger(__name__)

_CHUNK = 256 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# R15: the single backend process must not forward an unbounded number of video
# streams. Waiting is bounded; saturation is surfaced as 503 + Retry-After instead
# of letting one client's seek/scrub starve every other media request.
_MAX_STREAMS = _env_int("VC_MEDIA_MAX_STREAMS", 8)
_STREAM_WAIT_SECONDS = _env_float("VC_MEDIA_STREAM_WAIT_SECONDS", 10.0)
_STREAM_MAX_SECONDS = _env_float("VC_MEDIA_STREAM_MAX_SECONDS", 300.0)
_STREAM_SEMAPHORES: "weakref.WeakKeyDictionary[object, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_SEMAPHORE_LOCK = threading.Lock()
_SLOT_CTX: contextvars.ContextVar[Optional["_StreamSlot"]] = contextvars.ContextVar(
    "media_stream_slot", default=None,
)


def stream_metrics() -> dict:
    """Small, payload-free counter snapshot for health/log monitoring."""
    return media_metrics.snapshot()


def _semaphore() -> asyncio.Semaphore:
    """Keep one limiter per event loop so TestClient/embedded loops cannot bind
    the production loop to a different loop when saturation is reached."""
    loop = asyncio.get_running_loop()
    with _SEMAPHORE_LOCK:
        semaphore = _STREAM_SEMAPHORES.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(_MAX_STREAMS)
            _STREAM_SEMAPHORES[loop] = semaphore
    return semaphore


class _StreamSlot:
    """Idempotent reservation, released by the response and the request guard."""

    def __init__(self, semaphore: asyncio.Semaphore, wait_ms: int):
        self._semaphore = semaphore
        self.wait_ms = wait_ms
        self._lock = threading.Lock()
        self._released = False
        self.context_token = None

    def release(self, *, bytes_sent: int = 0, elapsed_ms: int = 0, outcome: str = "failed") -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        try:
            self._semaphore.release()
        finally:
            media_metrics.release(
                bytes_sent=bytes_sent,
                elapsed_ms=elapsed_ms,
                outcome=outcome,
            )


async def _stream_slot_guard():
    """Release a reservation if middleware/request cancellation prevents the
    StreamingResponse from being called at all."""
    try:
        yield
    finally:
        slot = _SLOT_CTX.get()
        if slot is not None:
            slot.release(outcome="failed")
            token = getattr(slot, "context_token", None)
            if token is not None:
                try:
                    _SLOT_CTX.reset(token)
                except (RuntimeError, ValueError):
                    pass


async def _reserve_stream_slot() -> _StreamSlot:
    semaphore = _semaphore()
    media_metrics.begin_wait()
    started = time.monotonic()
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=_STREAM_WAIT_SECONDS)
    except asyncio.TimeoutError as exc:
        wait_ms = int((time.monotonic() - started) * 1000)
        media_metrics.reject_wait(wait_ms)
        raise HTTPException(
            status_code=503,
            detail="媒体流并发已满，请稍后重试",
            headers={"Retry-After": str(max(1, math.ceil(_STREAM_WAIT_SECONDS)))},
        ) from exc
    except BaseException:
        media_metrics.cancel_wait()
        raise
    wait_ms = int((time.monotonic() - started) * 1000)
    media_metrics.finish_wait(wait_ms)
    slot = _StreamSlot(semaphore, wait_ms)
    slot.context_token = _SLOT_CTX.set(slot)
    return slot


class _LimitedStreamingResponse(StreamingResponse):
    """Release the R15 slot even when the browser disconnects mid-stream."""

    def __init__(self, content, *, stream_counter: list[int], slot: _StreamSlot,
                 expected_bytes: int, cleanup=None, **kwargs):
        super().__init__(content, **kwargs)
        self._stream_counter = stream_counter
        self._slot = slot
        self._expected_bytes = max(0, expected_bytes)
        self._cleanup = cleanup

    async def __call__(self, scope, receive, send):
        started = time.monotonic()
        failed = False
        disconnected = False

        async def _watch_disconnect():
            nonlocal disconnected
            message = await receive()
            if message.get("type") == "http.disconnect":
                disconnected = True
            return message

        timed_out = False
        try:
            await asyncio.wait_for(
                super().__call__(scope, _watch_disconnect, send),
                timeout=_STREAM_MAX_SECONDS,
            )
        except ClientDisconnect:
            disconnected = True
            failed = True
            raise
        except asyncio.TimeoutError:
            timed_out = True
            failed = True
            raise
        except BaseException:
            failed = True
            raise
        finally:
            # The iterator may never start when the client disconnects before the
            # first chunk; close the underlying file/socket independently.
            if self._cleanup is not None:
                try:
                    self._cleanup()
                except Exception:
                    pass
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if timed_out:
                outcome = "timed_out"
            elif disconnected and self._stream_counter[0] < self._expected_bytes:
                outcome = "disconnected"
            elif failed:
                outcome = "failed"
            elif self._stream_counter[0] < self._expected_bytes:
                outcome = "partial"
            else:
                outcome = "completed"
            self._slot.release(
                bytes_sent=self._stream_counter[0],
                elapsed_ms=elapsed_ms,
                outcome=outcome,
            )
            snap = stream_metrics()
            logger.info(
                "media_stream status=%s outcome=%s bytes=%s elapsed_ms=%s active=%s "
                "queued=%s bps=%s disconnect_seen=%s",
                self.status_code,
                outcome,
                self._stream_counter[0],
                elapsed_ms,
                snap["active"],
                snap["queued"],
                snap["bytes_per_second_avg"],
                disconnected,
            )


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


def _iter_file(handle, start: int, length: int, counter: list[int]) -> Iterator[bytes]:
    try:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            counter[0] += len(chunk)
            yield chunk
    finally:
        try:
            handle.close()
        except Exception:
            pass


def _close_storage_handle(handle) -> None:
    try:
        handle.close()
    finally:
        try:
            handle.release_conn()
        except Exception:
            pass


def _iter_storage(handle, length: int, counter: list[int]) -> Iterator[bytes]:
    try:
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            counter[0] += len(chunk)
            yield chunk
    finally:
        _close_storage_handle(handle)


@router.api_route("/code/result/{path:path}", methods=["GET", "HEAD"])
async def get_result_media(
    path: str,
    request: Request,
    user_login: str = "",
    download: int = 0,
    _slot_guard: None = Depends(_stream_slot_guard),
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
        slot = await _reserve_stream_slot()
        try:
            handle = open(real, "rb")
        except OSError as exc:
            slot.release(outcome="failed")
            FileRepo().mark_missing(str(row["file_id"]), True)
            raise HTTPException(404, "Media not found") from exc
        counter = [0]
        return _LimitedStreamingResponse(
            _iter_file(handle, start, length, counter),
            stream_counter=counter,
            slot=slot,
            expected_bytes=length,
            cleanup=handle.close,
            status_code=status,
            headers=headers,
            media_type=media_type,
        )

    slot = await _reserve_stream_slot()
    try:
        handle = await run_in_threadpool(
            lambda: get_storage().open_stream(object_key, offset=start, length=length)
        )
    except ObjectMissing as exc:
        slot.release(outcome="failed")
        FileRepo().mark_missing(str(row["file_id"]), True)
        raise HTTPException(404, "Media not found") from exc
    except StorageError as exc:
        slot.release(outcome="failed")
        raise HTTPException(404, "Media not found") from exc
    except BaseException:
        slot.release(outcome="failed")
        raise
    counter = [0]
    return _LimitedStreamingResponse(
        _iter_storage(handle, length, counter),
        stream_counter=counter,
        slot=slot,
        expected_bytes=length,
        cleanup=lambda: _close_storage_handle(handle),
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
    _slot_guard: None = Depends(_stream_slot_guard),
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
    slot = await _reserve_stream_slot()
    try:
        handle = await run_in_threadpool(
            lambda: get_storage().open_stream(object_key, offset=start, length=length, bucket=bucket)
        )
    except ObjectMissing as exc:
        slot.release(outcome="failed")
        raise HTTPException(404, "Media not found") from exc
    except StorageError as exc:
        slot.release(outcome="failed")
        raise HTTPException(404, "Media not found") from exc
    except BaseException:
        slot.release(outcome="failed")
        raise
    counter = [0]
    return _LimitedStreamingResponse(
        _iter_storage(handle, length, counter),
        stream_counter=counter,
        slot=slot,
        expected_bytes=length,
        cleanup=lambda: _close_storage_handle(handle),
        status_code=status,
        headers=headers,
        media_type=media_type,
    )
