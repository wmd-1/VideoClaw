"""Process-local media stream counters.

Kept independent from the media router so `/api/health` can expose counters
without importing storage/database dependencies. These values are diagnostic
only; they are not a cross-process or cross-instance source of truth.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

_LOCK = threading.Lock()
_PROCESS_STARTED = time.monotonic()
_EMPTY = {
    "active": 0,
    "queued": 0,
    "completed": 0,
    "failed": 0,
    "disconnected": 0,
    "partial": 0,
    "timed_out": 0,
    "cancelled": 0,
    "rejected": 0,
    "bytes": 0,
    "elapsed_ms": 0,
    "wait_ms": 0,
    "max_wait_ms": 0,
}
_METRICS = dict(_EMPTY)


def reset() -> None:
    with _LOCK:
        _METRICS.clear()
        _METRICS.update(_EMPTY)


def begin_wait() -> None:
    with _LOCK:
        _METRICS["queued"] += 1


def finish_wait(wait_ms: int) -> None:
    with _LOCK:
        _METRICS["queued"] = max(0, _METRICS["queued"] - 1)
        _METRICS["active"] += 1
        _METRICS["wait_ms"] += max(0, wait_ms)
        _METRICS["max_wait_ms"] = max(_METRICS["max_wait_ms"], max(0, wait_ms))


def cancel_wait() -> None:
    with _LOCK:
        _METRICS["queued"] = max(0, _METRICS["queued"] - 1)
        _METRICS["cancelled"] += 1


def reject_wait(wait_ms: int) -> None:
    with _LOCK:
        _METRICS["queued"] = max(0, _METRICS["queued"] - 1)
        _METRICS["rejected"] += 1
        _METRICS["wait_ms"] += max(0, wait_ms)
        _METRICS["max_wait_ms"] = max(_METRICS["max_wait_ms"], max(0, wait_ms))


def release(*, bytes_sent: int, elapsed_ms: int, outcome: str) -> None:
    with _LOCK:
        _METRICS["active"] = max(0, _METRICS["active"] - 1)
        _METRICS["bytes"] += max(0, bytes_sent)
        _METRICS["elapsed_ms"] += max(0, elapsed_ms)
        key = outcome if outcome in (
            "completed", "failed", "disconnected", "partial", "timed_out",
        ) else "failed"
        _METRICS[key] += 1


def snapshot() -> Dict[str, Any]:
    with _LOCK:
        result = dict(_METRICS)
    # Cumulative average since process start: useful as a coarse diagnostic,
    # not as an instantaneous alerting rate. Per-stream elapsed time is summed
    # in `elapsed_ms` and has no wall-clock meaning under concurrency.
    uptime = max(0.001, time.monotonic() - _PROCESS_STARTED)
    result["bytes_per_second_avg"] = round(float(result.get("bytes") or 0) / uptime, 2)
    return result
