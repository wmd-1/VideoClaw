"""调度队列表：原子入队、幂等、冲突与容量。"""
from __future__ import annotations

import threading
import uuid

import pytest
from psycopg import errors

from db import database
from db.repositories.video_dispatch import (
    DispatchConflict,
    QueueFull,
    SlotBusy,
    VideoDispatchRepo,
)


@pytest.fixture(autouse=True)
def clean_dispatch_jobs():
    with database.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM video_resource_slots")
                cur.execute("DELETE FROM video_dispatch_jobs")
    yield
    with database.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM video_resource_slots")
                cur.execute("DELETE FROM video_dispatch_jobs")


@pytest.fixture
def owner():
    return f"repo-{uuid.uuid4().hex[:8]}"


def _job(request_key: str, item_id: str, **snapshot):
    return {
        "request_key": request_key,
        "item_id": item_id,
        "source_kind": "workflow",
        "execution_id": f"exec-{request_key}",
        "dispatch_mode": "auto",
        "profile_id": "h3-fl2va",
        "profile_revision": 1,
        "snapshot_json": {"prompt": "demo", **snapshot},
    }


def test_batch_enqueue_and_same_input_is_idempotent(owner: str):
    repo = VideoDispatchRepo()
    jobs = [_job("req-1", "item-1"), _job("req-1", "item-2")]

    first = repo.enqueue_batch(owner=owner, batch_id="batch-1", jobs=jobs)
    assert first.created == 2
    assert len(first.jobs) == 2
    assert {row["state"] for row in first.jobs} == {"queued"}
    first_ids = sorted(row["job_id"] for row in first.jobs)

    again = repo.enqueue_batch(owner=owner, batch_id="batch-1", jobs=jobs)
    assert again.created == 0
    assert again.idempotent is True
    assert sorted(row["job_id"] for row in again.jobs) == first_ids
    assert repo.count_queued() >= 2


def test_same_key_different_input_conflicts(owner: str):
    repo = VideoDispatchRepo()
    repo.enqueue_batch(owner=owner, batch_id="batch-1", jobs=[_job("req-1", "item-1")])

    with pytest.raises(DispatchConflict):
        repo.enqueue_batch(
            owner=owner,
            batch_id="batch-1",
            jobs=[_job("req-1", "item-1", prompt="changed")],
        )

    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM video_dispatch_jobs WHERE owner = %s", (owner,))
            assert cur.fetchone()["count"] == 1


def test_capacity_rejects_whole_batch(owner: str):
    repo = VideoDispatchRepo()
    with pytest.raises(QueueFull):
        repo.enqueue_batch(
            owner=owner,
            batch_id="batch-1",
            jobs=[_job("req-1", "item-1"), _job("req-1", "item-2")],
            max_queued_jobs=1,
        )
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM video_dispatch_jobs WHERE owner = %s", (owner,))
            assert cur.fetchone()["count"] == 0


def test_database_error_rolls_back_whole_batch(owner: str):
    repo = VideoDispatchRepo()
    first = repo.enqueue_batch(owner=owner, batch_id="batch-1", jobs=[_job("req-1", "item-1")])
    existing_job_id = first.jobs[0]["job_id"]

    new_job = _job("req-2", "item-2")
    new_job["job_id"] = "new-job-id"
    conflicting = _job("req-2", "item-3")
    conflicting["job_id"] = existing_job_id
    with pytest.raises(errors.UniqueViolation):
        repo.enqueue_batch(owner=owner, batch_id="batch-2", jobs=[new_job, conflicting])

    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM video_dispatch_jobs WHERE owner = %s", (owner,))
            assert cur.fetchone()["count"] == 1


def test_duplicate_item_key_rolls_back_before_insert(owner: str):
    repo = VideoDispatchRepo()
    duplicate = [_job("req-1", "item-1"), _job("req-1", "item-1")]
    with pytest.raises(ValueError):
        repo.enqueue_batch(owner=owner, batch_id="batch-1", jobs=duplicate)
    assert repo.count_queued() == 0


def test_reserve_is_exclusive_and_old_token_cannot_release(owner: str):
    repo = VideoDispatchRepo()
    jobs = repo.enqueue_batch(
        owner=owner,
        batch_id="batch-1",
        jobs=[_job("req-1", "item-1"), _job("req-1", "item-2")],
    ).jobs
    first, second = jobs[0], jobs[1]

    attempt = repo.reserve(
        job_id=first["job_id"],
        resource_key="gpu-1",
        instance_id="sglang-01",
        boot_id="boot-1",
    )
    assert attempt["token"]
    assert repo.count_inflight() == 1

    with database.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE video_dispatch_attempts SET submission_state = 'unknown' "
                    "WHERE attempt_id = %s",
                    (attempt["attempt_id"],),
                )
    assert repo.count_inflight() == 1

    with pytest.raises(SlotBusy):
        repo.reserve(
            job_id=second["job_id"],
            resource_key="gpu-1",
            instance_id="sglang-02",
        )

    assert repo.heartbeat(
        resource_key="gpu-1",
        attempt_id=attempt["attempt_id"],
        token="wrong-token",
    ) is False
    assert repo.heartbeat(
        resource_key="gpu-1",
        attempt_id=attempt["attempt_id"],
        token=attempt["token"],
    ) is True

    assert repo.release(
        resource_key="gpu-1",
        attempt_id=attempt["attempt_id"],
        token="wrong-token",
    ) is False
    assert repo.release(
        resource_key="gpu-1",
        attempt_id=attempt["attempt_id"],
        token=attempt["token"],
    ) is True
    assert repo.count_inflight() == 0

    second_attempt = repo.reserve(
        job_id=second["job_id"],
        resource_key="gpu-1",
        instance_id="sglang-02",
    )
    assert second_attempt["attempt_id"] != attempt["attempt_id"]
    assert repo.release(
        resource_key="gpu-1",
        attempt_id=attempt["attempt_id"],
        token=attempt["token"],
    ) is False
    assert repo.release(
        resource_key="gpu-1",
        attempt_id=second_attempt["attempt_id"],
        token=second_attempt["token"],
    ) is True


def test_concurrent_reserve_only_one_wins(owner: str):
    repo = VideoDispatchRepo()
    jobs = repo.enqueue_batch(
        owner=owner,
        batch_id="batch-1",
        jobs=[_job("req-1", "item-1"), _job("req-1", "item-2")],
    ).jobs
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            repo.reserve(
                job_id=jobs[index]["job_id"],
                resource_key="gpu-1",
                instance_id=f"sglang-0{index + 1}",
            )
            result = "reserved"
        except SlotBusy:
            result = "busy"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["busy", "reserved"]
    assert repo.count_inflight() == 1


def test_concurrent_enqueue_respects_capacity(owner: str):
    repo = VideoDispatchRepo()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            repo.enqueue_batch(
                owner=owner,
                batch_id=f"batch-{index}",
                jobs=[_job(f"req-{index}", "item-1")],
                max_queued_jobs=1,
            )
            result = "ok"
        except QueueFull:
            result = "full"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["full", "ok"]
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM video_dispatch_jobs WHERE owner = %s", (owner,))
            assert cur.fetchone()["count"] == 1


def test_startup_recovery_quarantines_without_releasing(owner: str):
    repo = VideoDispatchRepo()
    job = repo.enqueue_batch(
        owner=owner,
        batch_id="batch-1",
        jobs=[_job("req-1", "item-1")],
    ).jobs[0]
    attempt = repo.reserve(
        job_id=job["job_id"],
        resource_key="gpu-1",
        instance_id="sglang-01",
        boot_id="boot-old",
    )

    assert repo.recover_startup(boot_id="boot-new") == 1
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT active_attempt_id, quarantine_reason FROM video_resource_slots "
                "WHERE resource_key = 'gpu-1'"
            )
            slot = cur.fetchone()
    assert slot["active_attempt_id"] == attempt["attempt_id"]
    assert slot["quarantine_reason"] == "boot_changed"
    assert repo.count_inflight() == 1
    assert repo.get_attempt(attempt["attempt_id"])["submission_state"] == "unknown"
    assert repo.recover_startup(boot_id="boot-new") == 0
