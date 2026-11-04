"""D2/WF-12：会话级 grounding 缓存。

- 进程内、按 session 隔离；键含图片内容 sha256、VLM 模型、策略版本；
- 每会话 64 条、全局 256 条、全局 8MB、TTL 2 小时（可配置）；
- 成功描述才缓存；空/失败最多由调用方在当前执行内做负缓存；
- 同键并发 single-flight；不存 URL/签名，不改变媒体权限校验。
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from typing import Callable, Dict, Hashable, Optional, Tuple

MAX_PER_SESSION = 64
MAX_GLOBAL = 256
MAX_BYTES = 8 * 1024 * 1024
TTL_SECONDS = 2 * 60 * 60


def content_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _Entry:
    __slots__ = ("value", "size", "created_at")

    def __init__(self, value: str, created_at: float):
        self.value = value
        self.size = len(value.encode("utf-8"))
        self.created_at = created_at


class GroundingCache:
    def __init__(
        self,
        *,
        max_per_session: int = MAX_PER_SESSION,
        max_global: int = MAX_GLOBAL,
        max_bytes: int = MAX_BYTES,
        ttl_seconds: float = TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        waiter_timeout: float = 120.0,
    ):
        self.max_per_session = max_per_session
        self.max_global = max_global
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self.waiter_timeout = waiter_timeout
        self._lock = threading.RLock()
        self._entries: "OrderedDict[Hashable, _Entry]" = OrderedDict()
        self._sessions: Dict[str, "OrderedDict[Hashable, None]"] = {}
        self._inflight: Dict[Hashable, Future] = {}
        self._session_generation: Dict[str, int] = {}
        self._total_bytes = 0

    @staticmethod
    def make_key(session_id: str, image_hash: str, model: str, policy_version: str) -> Tuple:
        return (str(session_id), str(image_hash), str(model), str(policy_version))

    def _expire(self) -> None:
        cutoff = self._clock() - self.ttl_seconds
        expired = [
            key for key, entry in self._entries.items()
            if entry.created_at < cutoff
        ]
        for key in expired:
            self._drop_locked(key)

    def _drop_locked(self, key: Hashable) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        self._total_bytes -= entry.size
        session_id = key[0] if isinstance(key, tuple) and key else ""
        bucket = self._sessions.get(session_id)
        if bucket is not None:
            bucket.pop(key, None)
            if not bucket:
                self._sessions.pop(session_id, None)

    def _put_locked(self, key: Hashable, value: str) -> None:
        session_id = key[0] if isinstance(key, tuple) and key else ""
        self._drop_locked(key)
        entry = _Entry(value, self._clock())
        if entry.size > self.max_bytes:
            return
        self._entries[key] = entry
        self._sessions.setdefault(session_id, OrderedDict())[key] = None
        self._total_bytes += entry.size
        while len(self._sessions[session_id]) > self.max_per_session:
            oldest = next(iter(self._sessions[session_id]))
            self._drop_locked(oldest)
        while len(self._entries) > self.max_global:
            oldest = next(iter(self._entries))
            self._drop_locked(oldest)
        while self._total_bytes > self.max_bytes and self._entries:
            oldest = next(iter(self._entries))
            self._drop_locked(oldest)

    def get(self, session_id: str, image_hash: str, model: str, policy_version: str) -> Optional[str]:
        key = self.make_key(session_id, image_hash, model, policy_version)
        with self._lock:
            self._expire()
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            self._sessions[session_id].move_to_end(key)
            return entry.value

    def get_or_compute(
        self,
        session_id: str,
        image_hash: str,
        model: str,
        policy_version: str,
        factory: Callable[[], str],
    ) -> str:
        """同键 single-flight；空结果不进入跨执行缓存。"""
        if not session_id:
            return factory()
        key = self.make_key(session_id, image_hash, model, policy_version)
        with self._lock:
            generation = self._session_generation.get(session_id, 0)
            self._expire()
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                self._sessions[session_id].move_to_end(key)
                return entry.value
            waiter = self._inflight.get(key)
            if waiter is None:
                waiter = Future()
                self._inflight[key] = waiter
                owner = True
            else:
                owner = False

        if not owner:
            try:
                return waiter.result(timeout=self.waiter_timeout)
            except TimeoutError:
                # 等待者超时后自行计算/回退，避免线程被卡死的 owner 拖住
                return factory()

        try:
            value = factory()
        except Exception as exc:
            with self._lock:
                self._inflight.pop(key, None)
                waiter.set_exception(exc)
            raise

        with self._lock:
            self._inflight.pop(key, None)
            # clear_session 发生在计算期间时，不把结果重新写回已清理的会话
            if value and self._session_generation.get(session_id, 0) == generation:
                self._put_locked(key, value)
            waiter.set_result(value)
        return value

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            self._session_generation[session_id] = self._session_generation.get(session_id, 0) + 1
            bucket = self._sessions.get(session_id)
            if not bucket:
                return
            for key in list(bucket):
                self._drop_locked(key)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            self._expire()
            return {
                "entries": len(self._entries),
                "sessions": len(self._sessions),
                "bytes": self._total_bytes,
            }


grounding_cache = GroundingCache()
