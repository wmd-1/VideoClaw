"""4.11：单 worker 前提与空闲 loop 不 tick。"""
from __future__ import annotations

import time

import pytest

from api.app import assert_single_process_premise
from core.video_dispatch_runtime import DispatchRuntime


def test_single_worker_guard_rejects_multi_worker_env(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError):
        assert_single_process_premise()


def test_single_worker_guard_rejects_workers_arg(monkeypatch):
    monkeypatch.setattr("sys.argv", ["uvicorn", "--workers", "2"])
    with pytest.raises(RuntimeError):
        assert_single_process_premise()


def test_idle_runtime_does_not_tick_when_no_pending_work():
    calls = []

    class Service:
        def has_pending_work(self):
            return False

        def dispatch_once(self, _factory):
            calls.append("dispatch")

        def reconcile_once(self, _factory):
            calls.append("reconcile")

    runtime = DispatchRuntime(Service(), lambda *_: None, interval=0.01)
    runtime.start()
    try:
        time.sleep(0.06)
    finally:
        runtime.stop()
    assert calls == []


def test_enqueue_waker_wakes_idle_runtime():
    state = {"pending": False, "dispatched": 0}
    wake = {"fn": None}

    class Service:
        def set_waker(self, fn):
            wake["fn"] = fn

        def has_pending_work(self):
            return state["pending"]

        def dispatch_once(self, _factory):
            state["dispatched"] += 1
            state["pending"] = False

        def reconcile_once(self, _factory):
            pass

    runtime = DispatchRuntime(Service(), lambda *_: None, interval=0.2)
    runtime.start()
    try:
        state["pending"] = True
        wake["fn"]()
        time.sleep(0.05)
        assert state["dispatched"] >= 1
    finally:
        runtime.stop()
