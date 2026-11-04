"""D2：会话级 grounding 缓存键、容量、TTL、失败与单飞。"""
from __future__ import annotations

import threading
import time

from core.grounding_cache import GroundingCache


def test_same_key_single_flight_and_scope():
    cache = GroundingCache()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return "desc"

    assert cache.get_or_compute("s1", "hash", "vlm", "p1", compute) == "desc"
    assert cache.get_or_compute("s1", "hash", "vlm", "p1", compute) == "desc"
    assert calls["n"] == 1
    # 会话/模型/策略任一变化都重新计算
    assert cache.get_or_compute("s2", "hash", "vlm", "p1", compute) == "desc"
    assert cache.get_or_compute("s1", "hash", "vlm2", "p1", compute) == "desc"
    assert cache.get_or_compute("s1", "hash", "vlm", "p2", compute) == "desc"
    assert calls["n"] == 4


def test_per_session_global_and_byte_capacity():
    cache = GroundingCache(max_per_session=2, max_global=3, max_bytes=8, ttl_seconds=999)
    cache.get_or_compute("s1", "h1", "m", "p", lambda: "a" * 4)
    cache.get_or_compute("s1", "h2", "m", "p", lambda: "b" * 4)
    cache.get_or_compute("s1", "h3", "m", "p", lambda: "c" * 4)
    stats = cache.stats()
    assert stats["entries"] <= 2
    assert cache.get("s1", "h1", "m", "p") is None

    cache.get_or_compute("s2", "h4", "m", "p", lambda: "d" * 4)
    cache.get_or_compute("s3", "h5", "m", "p", lambda: "e" * 4)
    assert cache.stats()["entries"] <= 3


def test_ttl_expiry_and_clear_session():
    now = {"t": 100.0}
    cache = GroundingCache(ttl_seconds=10, clock=lambda: now["t"])
    cache.get_or_compute("s1", "h", "m", "p", lambda: "desc")
    assert cache.get("s1", "h", "m", "p") == "desc"
    now["t"] = 111.0
    assert cache.get("s1", "h", "m", "p") is None

    cache.get_or_compute("s2", "h", "m", "p", lambda: "desc2")
    cache.clear_session("s2")
    assert cache.get("s2", "h", "m", "p") is None


def test_empty_and_exception_are_not_cached():
    cache = GroundingCache()
    calls = {"n": 0}

    def empty():
        calls["n"] += 1
        return ""

    assert cache.get_or_compute("s1", "empty", "m", "p", empty) == ""
    assert cache.get_or_compute("s1", "empty", "m", "p", empty) == ""
    assert calls["n"] == 2

    def boom():
        raise RuntimeError("vlm down")

    try:
        cache.get_or_compute("s1", "boom", "m", "p", boom)
    except RuntimeError:
        pass
    # 失败后不留下 in-flight，后续可重试
    assert cache.get_or_compute("s1", "boom", "m", "p", lambda: "ok") == "ok"


def test_concurrent_single_flight():
    cache = GroundingCache()
    calls = {"n": 0}
    barrier = threading.Barrier(3)

    def compute():
        calls["n"] += 1
        time.sleep(0.1)
        return "desc"

    results = []

    def worker():
        barrier.wait()
        results.append(cache.get_or_compute("s", "h", "m", "p", compute))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == ["desc", "desc", "desc"]
    assert calls["n"] == 1


def test_clear_session_during_inflight_does_not_reinsert():
    cache = GroundingCache()
    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(2)
        return "late"

    result = []

    def worker():
        result.append(cache.get_or_compute("s", "h", "m", "p", slow))

    thread = threading.Thread(target=worker)
    thread.start()
    started.wait(2)
    cache.clear_session("s")
    release.set()
    thread.join()
    assert result == ["late"]
    assert cache.get("s", "h", "m", "p") is None


def test_new_process_cache_starts_empty():
    first = GroundingCache()
    first.get_or_compute("s", "h", "m", "p", lambda: "desc")
    second = GroundingCache()
    assert second.get("s", "h", "m", "p") is None
