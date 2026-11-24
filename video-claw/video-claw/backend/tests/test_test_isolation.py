"""1.6：fixture 收口后不留调度运行时、队列/槽或后台 loop。"""
from __future__ import annotations

import threading

from core.video_dispatch_runtime import get_dispatch_runtime
from db.repositories.video_dispatch import VideoDispatchRepo


def test_no_dispatch_runtime_or_queue_residue_after_fixture():
    assert get_dispatch_runtime() is None
    repo = VideoDispatchRepo()
    assert repo.count_queued() == 0
    assert not any(
        thread.name == "video-dispatch-runtime" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_fake_dispatch_runtime_is_independent(fake_dispatch_runtime):
    assert get_dispatch_runtime() is fake_dispatch_runtime
    assert fake_dispatch_runtime._thread is None


def test_fake_runtime_start_is_stopped_by_fixture(fake_dispatch_runtime):
    fake_dispatch_runtime.start()
    assert fake_dispatch_runtime._thread is not None
    assert fake_dispatch_runtime._thread.is_alive()
