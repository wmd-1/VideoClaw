"""调度队列轮转与容量授予。"""
from __future__ import annotations

import threading
import time

from core.video_dispatch import (
    CapacitySnapshot,
    DispatchJob,
    VideoDispatchScheduler,
)


def _job(job_id: str, owner: str, session: str = "s1", **kwargs):
    return DispatchJob(
        job_id=job_id,
        owner=owner,
        session_id=session,
        execution_id=kwargs.get("execution_id", session),
        profile_id=kwargs.get("profile_id", "p1"),
        mode=kwargs.get("mode", "first_frame"),
        accepted_at=kwargs.get("accepted_at", 0),
        queue_deadline_at=kwargs.get("queue_deadline_at", 3600),
    )


def _capacity(**kwargs):
    payload = {
        "profile_slots": {"p1": 8},
        "profile_modes": {"p1": {"first_frame", "reference"}},
        "per_execution_limits": {},
        "global_limit": 8,
        "active_global": 0,
    }
    payload.update(kwargs)
    return CapacitySnapshot(**payload)


def test_enqueue_wakes_waiting_consumer():
    scheduler = VideoDispatchScheduler()
    scheduler.enqueue(_job("a", "A"))
    results: list[bool] = []

    def waiter() -> None:
        results.append(scheduler.wait_for_change(timeout=2))

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    scheduler.enqueue(_job("b", "A"))
    thread.join()

    assert results == [True]


def test_concurrent_enqueue_and_grant_is_serialized():
    scheduler = VideoDispatchScheduler()
    jobs = [_job(f"j-{index}", "A", execution_id=f"e-{index}") for index in range(40)]
    collected: list = []
    errors: list[BaseException] = []

    def producer() -> None:
        try:
            for job in jobs:
                scheduler.enqueue(job)
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001 - 测试收集
            errors.append(exc)

    def consumer() -> None:
        try:
            deadline = time.time() + 5
            while len(collected) < len(jobs) and time.time() < deadline:
                collected.extend(scheduler.grant(_capacity(profile_slots={"p1": 8})))
                if len(collected) < len(jobs):
                    scheduler.wait_for_change(
                        timeout=0.05, predicate=lambda: scheduler.pending() > 0
                    )
        except BaseException as exc:  # noqa: BLE001 - 测试收集
            errors.append(exc)

    threads = [threading.Thread(target=producer), threading.Thread(target=consumer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(collected) == len(jobs)
    assert len({grant.job_id for grant in collected}) == len(jobs)
    assert scheduler.pending() == 0


def test_multiple_waiters_are_all_woken():
    scheduler = VideoDispatchScheduler()
    results: list[bool] = []

    def waiter() -> None:
        results.append(
            scheduler.wait_for_change(
                timeout=2, predicate=lambda: scheduler.pending() > 0
            )
        )

    threads = [threading.Thread(target=waiter) for _ in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    scheduler.enqueue(_job("a", "A"))
    for thread in threads:
        thread.join()

    assert results == [True, True]


def test_predicate_timeout_returns_false():
    scheduler = VideoDispatchScheduler()
    assert scheduler.wait_for_change(
        timeout=0.05, predicate=lambda: scheduler.pending() > 0
    ) is False


def test_new_small_owner_is_not_starved_by_large_owner():
    scheduler = VideoDispatchScheduler()
    for index in range(6):
        scheduler.enqueue(_job(f"a-{index}", "A"))
    scheduler.enqueue(_job("b-0", "B"))

    grants = scheduler.grant(_capacity(profile_slots={"p1": 2}))
    assert [grant.job_id for grant in grants] == ["a-0", "b-0"]


def test_owner_session_rotation_and_multi_session_no_extra_weight():
    scheduler = VideoDispatchScheduler()
    scheduler.enqueue(_job("a-s1", "A", session="s1"))
    scheduler.enqueue(_job("a-s2", "A", session="s2"))
    scheduler.enqueue(_job("b-s1", "B", session="s9"))

    first = scheduler.grant(_capacity(profile_slots={"p1": 1}))
    second = scheduler.grant(_capacity(profile_slots={"p1": 1}))
    third = scheduler.grant(_capacity(profile_slots={"p1": 1}))
    assert [first[0].job_id, second[0].job_id, third[0].job_id] == [
        "a-s1",
        "b-s1",
        "a-s2",
    ]


def test_incompatible_head_is_skipped_without_blocking_queue():
    scheduler = VideoDispatchScheduler()
    scheduler.enqueue(_job("head", "A", profile_id="p1", mode="reference"))
    scheduler.enqueue(_job("tail", "A", profile_id="p2", mode="first_frame"))

    grants = scheduler.grant(
        _capacity(
            profile_slots={"p1": 0, "p2": 1},
            profile_modes={"p1": {"first_frame"}, "p2": {"first_frame"}},
        )
    )
    assert [grant.job_id for grant in grants] == ["tail"]


def test_capacity_grants_three_then_stops_at_slots():
    scheduler = VideoDispatchScheduler()
    for index in range(5):
        scheduler.enqueue(_job(f"job-{index}", "A"))
    capacity = _capacity(profile_slots={"p1": 3})
    grants = scheduler.grant(capacity)
    assert len(grants) == 3
    assert capacity.active_global == 3
    assert capacity.profile_slots["p1"] == 0
    assert scheduler.pending() == 2


def test_global_and_execution_limits_are_independent():
    scheduler = VideoDispatchScheduler()
    for index in range(3):
        scheduler.enqueue(_job(f"job-{index}", "A"))

    global_full = _capacity(profile_slots={"p1": 8}, global_limit=8, active_global=7)
    assert len(scheduler.grant(global_full)) == 1

    scheduler2 = VideoDispatchScheduler()
    for index in range(3):
        scheduler2.enqueue(_job(f"job-{index}", "A"))
    execution_limited = _capacity(
        profile_slots={"p1": 8},
        per_execution_limits={"s1": 2},
    )
    assert len(scheduler2.grant(execution_limited)) == 2


def test_two_executions_same_pool_have_independent_limits():
    scheduler = VideoDispatchScheduler()
    for index in range(3):
        scheduler.enqueue(_job(f"e1-{index}", "A", execution_id="e1"))
    for index in range(2):
        scheduler.enqueue(_job(f"e2-{index}", "B", execution_id="e2"))

    capacity = _capacity(
        profile_slots={"p1": 8},
        per_execution_limits={"e1": 2, "e2": 1},
    )
    grants = scheduler.grant(capacity)
    assert sorted(grant.execution_id for grant in grants) == ["e1", "e1", "e2"]
    assert capacity.execution_active == {"e1": 2, "e2": 1}


def test_unknown_active_counts_towards_global_limit_and_policy_reduction_cannot_cancel():
    scheduler = VideoDispatchScheduler()
    scheduler.enqueue(_job("job-0", "A"))
    assert scheduler.grant(_capacity(active_global=8)) == []
    assert scheduler.pending() == 1

    scheduler2 = VideoDispatchScheduler()
    for index in range(2):
        scheduler2.enqueue(_job(f"job-{index}", "A"))
    # 已存在的在途数高于新策略上限时，调度器只停止新增，不“取消”已有。
    capacity = _capacity(
        per_execution_limits={"s1": 1},
        execution_active={"s1": 3},
    )
    assert scheduler2.grant(capacity) == []
    assert capacity.execution_active["s1"] == 3
