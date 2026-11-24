"""调度服务接线：真实 repository + Fake transport，覆盖预留/准入/崩溃点/核对。"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx
import pytest
from psycopg import OperationalError

from core.video_dispatch_service import (
    DispatchCrashInjected,
    DispatchService,
    DispatchUnavailable,
    PollOutcome,
    SubmitOutcome,
)
from core.video_instance_admission import ObservationSample
from core.video_generation_service import BatchHandle, VideoGenerationService
from core.video_pool_config import compute_revision
from core.video_qualification import build_qualification_evidence
from db import database
from db.repositories.video_dispatch import QuarantineReleaseConflict, VideoDispatchRepo
from models.custom_video import CustomVideoClient

BOOT = "boot-test"
NOW = 1000.0


@pytest.fixture(autouse=True)
def clean_dispatch_tables():
    _truncate()
    yield
    _truncate()


def _truncate() -> None:
    with database.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM video_resource_slots")
                cur.execute("DELETE FROM video_dispatch_jobs")
                cur.execute(
                    "DELETE FROM schema_meta WHERE key = 'video_dispatch_stopped' "
                    "OR key LIKE 'video_draining_%' "
                    "OR key LIKE 'video_deleted_source:%' "
                    "OR key LIKE 'video_deleting_source:%'"
                )


def _instance(instance_id: str, resource_key: str, *, access_mode: str = "exclusive") -> dict:
    return {
        "instance_id": instance_id,
        "model_id": f"model-{instance_id}",
        "resource_key": resource_key,
        "access_mode": access_mode,
        "enabled": True,
        "driver": "vllm_omni" if access_mode == "exclusive" else "sglang_diffusion",
        "probe": {"kind": "vllm_omni", "jobs_url": "http://127.0.0.1/jobs", "metrics_url": "", "labels": {}},
    }


def _profile(profile_id: str, members: List[str], *, cap: int = 8, enabled: bool = True) -> dict:
    return {
        "id": profile_id,
        "name": profile_id,
        "revision": 1,
        "instances": members,
        "enabled": enabled,
        "max_concurrency_per_execution": cap,
        "contracts": {
            "first_frame": {
                "task": "fl2va",
                "resolutions": ["720P"],
                "short_edge": 720,
                "ratios": ["16:9"],
                "duration": {"min": 1, "max": 10},
                "fps": [24],
            }
        },
    }


def _pool_data(
    *,
    instances: List[dict],
    profiles: List[dict],
    max_running: int = 8,
) -> dict:
    return {
        "video_dispatch": {
            "default_mode": "auto",
            "default_profiles": {},
            "queue_timeout_seconds": 3600,
            "max_queued_jobs": 1000,
            "max_running_jobs": max_running,
        },
        "video_profiles": profiles,
        "video_instances": instances,
    }


class Clock:
    def __init__(self, value: float = NOW) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _service(
    data: Mapping[str, Any],
    *,
    boot_id: str = BOOT,
    clock: Optional[Clock] = None,
    shared_observer=None,
    exclusive_observer=None,
    crash_hook=None,
    heartbeats: Tuple[float, float] = (30.0, 90.0),
    cancel_grace: float = 300.0,
    probe_service=None,
    blocked_models_provider=None,
    job_input_preparer=None,
    result_publisher=None,
) -> DispatchService:
    revision = compute_revision(data)
    return DispatchService(
        pool_loader=lambda: (data, revision),
        repo=VideoDispatchRepo(),
        boot_id=boot_id,
        clock=clock or Clock(),
        shared_observer=shared_observer,
        exclusive_observer=exclusive_observer,
        heartbeat_interval_seconds=heartbeats[0],
        heartbeat_stale_seconds=heartbeats[1],
        cancel_grace_seconds=cancel_grace,
        probe_service=probe_service,
        blocked_models_provider=blocked_models_provider,
        job_input_preparer=job_input_preparer,
        result_publisher=result_publisher,
        sleep=lambda _seconds: None,
        crash_hook=crash_hook,
        download_attempts=3,
        max_safe_retries=1,
    )


def _qualify(
    repo: VideoDispatchRepo,
    data: Mapping[str, Any],
    *,
    boot_id: str = BOOT,
    checked_at: float = NOW,
) -> None:
    revision = compute_revision(data)
    for row in data["video_instances"]:
        evidence = build_qualification_evidence(
            instance_id=row["instance_id"],
            resource_key=row["resource_key"],
            config_revision=revision,
            boot_id=boot_id,
            checked_by="admin",
            checks={"probe_ready": True},
            confirmed=True,
            checked_at=checked_at,
        )
        repo.save_qualification(
            instance_id=row["instance_id"],
            resource_key=row["resource_key"],
            evidence=evidence.to_dict(),
        )


def _snapshot(
    members: List[str],
    *,
    mode: str = "first_frame",
    enable_concurrency: bool = True,
    cap: int = 8,
) -> dict:
    return {
        "mode": mode,
        "members": members,
        "enable_concurrency": enable_concurrency,
        "max_concurrency_per_execution": cap,
        "request": {"prompt": "demo"},
    }


def _enqueue(
    service: DispatchService,
    *,
    owner: str = "u1",
    batch_id: Optional[str] = None,
    items: int = 1,
    profile_id: str = "p1",
    snapshot: Optional[dict] = None,
    timeout_seconds: int = 3600,
):
    batch = batch_id or f"b-{uuid.uuid4().hex[:8]}"
    jobs = [
        {
            "request_key": batch,
            "item_id": f"item-{index}",
            "source_kind": "workflow",
            "source_id": batch,
            "execution_id": f"exec-{batch}",
            "dispatch_mode": "auto",
            "profile_id": profile_id,
            "profile_revision": 1,
            "snapshot_json": snapshot or _snapshot(["i1"]),
        }
        for index in range(items)
    ]
    return service.enqueue_batch(
        owner=owner,
        batch_id=batch,
        jobs=jobs,
        queue_timeout_seconds=timeout_seconds,
        max_queued_jobs=1000,
    )


class FakeRemote:
    """可阻塞/可注入错误的远端；submit/poll/download 调用都计数。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.submit_calls: List[dict] = []
        self.poll_calls: List[dict] = []
        self.download_calls: List[dict] = []
        self.cancel_calls: List[dict] = []
        self.submit_mode = "accepted"  # accepted / rejected / unknown
        self.poll_state = "pending"  # pending / success / failure / unknown
        self.drain_confirmed = False
        self.download_failures = 0
        self.cancel_confirmed = True
        self.block_submit: Optional[threading.Event] = None
        self._sequence = 0

    def factory(self, instance, job) -> "FakeTransport":
        return FakeTransport(self, instance, job)

    @property
    def submit_count(self) -> int:
        return len(self.submit_calls)


class FakeTransport:
    def __init__(self, remote: FakeRemote, instance, job) -> None:
        self.remote = remote
        self.instance = instance
        self.job = job

    def submit(self, *, job, instance, grant) -> SubmitOutcome:
        with self.remote.lock:
            self.remote.submit_calls.append(
                {"job_id": job["job_id"], "instance_id": instance.instance_id, "grant": grant}
            )
        if self.remote.block_submit is not None:
            self.remote.block_submit.wait(timeout=10)
        mode = self.remote.submit_mode
        if mode == "rejected":
            raise httpx.ConnectError("connection refused")
        if mode == "unknown":
            raise httpx.ReadTimeout("read timed out")
        with self.remote.lock:
            self.remote._sequence += 1
            remote_id = f"remote-{self.remote._sequence}"
        return SubmitOutcome("accepted", remote_job_id=remote_id)

    def poll(self, *, attempt, instance, job) -> PollOutcome:
        with self.remote.lock:
            self.remote.poll_calls.append(
                {"attempt_id": attempt["attempt_id"], "remote_job_id": attempt["remote_job_id"]}
            )
        return PollOutcome(
            self.remote.poll_state,
            detail={"state": self.remote.poll_state},
            drain_confirmed=self.remote.drain_confirmed,
        )

    def download(self, *, attempt, instance, job):
        with self.remote.lock:
            self.remote.download_calls.append(
                {"attempt_id": attempt["attempt_id"], "remote_job_id": attempt["remote_job_id"]}
            )
            failing = self.remote.download_failures > 0
            if failing:
                self.remote.download_failures -= 1
        if failing:
            raise RuntimeError("download interrupted")
        return {"save_path": f"/tmp/{attempt['remote_job_id']}.mp4"}

    def cancel(self, *, attempt, instance, job) -> bool:
        with self.remote.lock:
            self.remote.cancel_calls.append({"attempt_id": attempt["attempt_id"]})
        return self.remote.cancel_confirmed


def _two_slot_setup(**kwargs):
    data = _pool_data(
        instances=[
            _instance("i1", "gpu-1"),
            _instance("i2", "gpu-2"),
        ],
        profiles=[_profile("p1", ["i1", "i2"], **kwargs)],
    )
    service = _service(data)
    _qualify(service._repo, data)
    return data, service


def test_dispatch_uses_multiple_slots_and_respects_capacity():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1"), _instance("i2", "gpu-2"), _instance("i3", "gpu-3")],
        profiles=[_profile("p1", ["i1", "i2", "i3"], cap=3)],
    )
    service = _service(data)
    _qualify(service._repo, data)
    _enqueue(service, items=4, snapshot=_snapshot(["i1", "i2", "i3"], cap=3))

    remote = FakeRemote()
    report = service.dispatch_once(remote.factory)

    assert report.posts == 3
    assert report.submitted == 3
    rows = service._repo.list_active_attempts()
    assert {row["instance_id"] for row in rows} == {"i1", "i2", "i3"}
    assert service._repo.count_queued() == 1
    assert len({row["resource_key"] for row in rows}) == 3


def test_capacity_snapshot_is_the_same_source_as_actual_grants():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1"), _instance("i2", "gpu-2")],
        profiles=[_profile("p1", ["i1", "i2"], cap=2)],
    )
    service = _service(data)
    _qualify(service._repo, data)
    _enqueue(service, items=3, snapshot=_snapshot(["i1", "i2"], cap=2))
    runtime = service.runtime()
    capacity = service.capacity_snapshot(runtime, service._repo.list_queued())
    assert capacity.profile_slots["p1"] == 2
    assert capacity.free_for(service._capacity_job(service._repo.list_queued()[0])) == 2
    remote = FakeRemote()
    report = service.dispatch_once(remote.factory)
    assert report.submitted == 2
    assert service._repo.count_queued() == 1


def test_resolve_execution_pool_prefers_busy_default():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    data["video_dispatch"]["default_profiles"] = {"first_frame": "p1"}
    service = _service(data)
    _qualify(service._repo, data)
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    service.dispatch_once(FakeRemote().factory)  # 默认池进入 busy

    resolution = service.resolve_execution_pool(
        mode="first_frame", parameters={"duration": 8}
    )

    assert resolution.error == ""
    assert resolution.contract.profile_id == "p1"
    assert resolution.contract.selection_source == "admin_default"
    assert resolution.contract.recommendation_reason == "none"
    assert resolution.contract.revision == 1
    assert resolution.contract.parameters == {"duration": 8}


def test_resolve_execution_pool_fallback_is_not_sticky():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    data["video_dispatch"]["default_profiles"] = {"first_frame": "missing-pool"}
    service = _service(data)
    _qualify(service._repo, data)
    first = service.resolve_execution_pool(mode="first_frame")
    assert first.contract.profile_id == "p1"
    assert first.contract.selection_source == "first_ready"
    assert first.contract.recommendation_reason == "default_not_ready"

    data2 = _pool_data(
        instances=[_instance("i1", "gpu-1"), _instance("i2", "gpu-2")],
        profiles=[_profile("p1", ["i1"]), _profile("p2", ["i2"])],
    )
    data2["video_dispatch"]["default_profiles"] = {"first_frame": "p2"}
    service2 = _service(data2)
    _qualify(service2._repo, data2)
    second = service2.resolve_execution_pool(mode="first_frame")
    assert second.contract.profile_id == "p2"
    assert second.contract.selection_source == "admin_default"
    assert second.contract.recommendation_reason == "none"


def test_resolve_execution_pool_no_default_is_normal():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)

    resolution = service.resolve_execution_pool(mode="first_frame")

    assert resolution.error == ""
    assert resolution.contract.profile_id == "p1"
    assert resolution.contract.selection_source == "first_ready"
    assert resolution.contract.recommendation_reason == "no_default"


def test_resolve_execution_pool_explicit_preference_is_user_source():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)

    resolution = service.resolve_execution_pool(
        mode="first_frame", explicit_profile_id="p1"
    )

    assert resolution.error == ""
    assert resolution.contract.profile_id == "p1"
    assert resolution.contract.selection_source == "user"


def test_snapshot_never_expands_to_new_members():
    data = _pool_data(
        instances=[
            _instance("i1", "gpu-1"),
            _instance("i2", "gpu-2"),
            _instance("i3", "gpu-3"),
        ],
        profiles=[_profile("p1", ["i1", "i2"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    _enqueue(service, items=2, snapshot=_snapshot(["i1", "i2", "i3"]))

    remote = FakeRemote()
    service.dispatch_once(remote.factory)

    rows = service._repo.list_active_attempts()
    assert {row["instance_id"] for row in rows} == {"i1", "i2"}
    assert "i3" not in {row["instance_id"] for row in rows}


def test_removed_member_is_not_dispatched_and_waits():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1"), _instance("i2", "gpu-2")],
        profiles=[_profile("p1", ["i1", "i2"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, snapshot=_snapshot(["i2"]))
    job_id = result.jobs[0]["job_id"]

    # 受理后管理员从池中移除 i2：不能改派其他成员，也不能重置期限。
    data["video_profiles"][0]["instances"] = ["i1"]
    remote = FakeRemote()
    report = service.dispatch_once(remote.factory)

    assert report.posts == 0
    assert service._repo.count_inflight() == 0
    assert service._repo.get_job(job_id)["state"] == "queued"
    assert any(
        event["kind"] == "waiting_pool_unavailable" for event in report.events
    )


def test_instance_removed_quarantines_slot_without_poll():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    assert remote.submit_count == 1

    # 管理员把实例从 video_instances 整体删除（不仅从 profile 移除）。
    data["video_instances"] = []
    remote.poll_state = "success"
    report = service.reconcile_once(remote.factory)

    assert report.quarantined == 1
    assert remote.poll_calls == []  # 不查询已删除实例
    assert service._repo.count_inflight() == 1
    assert service._repo.get_job(job_id)["state"] == "reconciling"
    attempt = service._repo.latest_attempt(job_id)
    assert attempt["submission_state"] == "unknown"

    # 连续第二次 reconcile：已隔离项不重复计数，状态不变。
    second = service.reconcile_once(remote.factory)
    assert second.quarantined == 0
    assert service._repo.count_inflight() == 1
    assert service._repo.get_job(job_id)["state"] == "reconciling"
    assert service._repo.latest_attempt(job_id)["submission_state"] == "unknown"
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT quarantine_reason FROM video_resource_slots WHERE resource_key = %s",
                ("gpu-1",),
            )
            assert cur.fetchone()["quarantine_reason"] == "instance_removed"


def test_missing_snapshot_members_is_rejected_at_enqueue():
    data, service = _two_slot_setup()
    with pytest.raises(DispatchUnavailable) as exc:
        _enqueue(
            service,
            items=1,
            snapshot={"mode": "first_frame", "request": {"prompt": "demo"}},
        )
    assert exc.value.error_code == "pool_configuration_error"
    assert service._repo.count_queued() == 0
    assert service._repo.count_inflight() == 0


def test_inflight_member_continues_after_removal():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1"), _instance("i2", "gpu-2")],
        profiles=[_profile("p1", ["i1", "i2"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, snapshot=_snapshot(["i2"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    assert service._repo.latest_attempt(job_id)["instance_id"] == "i2"

    data["video_profiles"][0]["instances"] = ["i1"]
    remote.poll_state = "success"
    service.reconcile_once(remote.factory)

    assert remote.submit_count == 1
    assert service._repo.count_inflight() == 0
    assert service._repo.get_job(job_id)["state"] == "succeeded"


def test_global_limit_caps_across_owners():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1"), _instance("i2", "gpu-2")],
        profiles=[_profile("p1", ["i1", "i2"])],
        max_running=1,
    )
    service = _service(data)
    _qualify(service._repo, data)
    _enqueue(service, owner="u1", items=1, snapshot=_snapshot(["i1"]))
    _enqueue(service, owner="u2", items=1, snapshot=_snapshot(["i2"]))

    remote = FakeRemote()
    report = service.dispatch_once(remote.factory)

    assert report.posts == 1
    assert service._repo.count_inflight() == 1
    assert service._repo.count_queued() == 1


def test_probe_tick_only_for_pools_with_queued_jobs():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1"), _instance("i2", "gpu-2")],
        profiles=[_profile("p1", ["i1"]), _profile("p2", ["i2"])],
    )

    class FakeProbe:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def tick(self, instances, waiting=False):
            self.calls.append([item.instance_id for item in instances])

    probe = FakeProbe()
    service = _service(data, probe_service=probe)
    _qualify(service._repo, data)
    remote = FakeRemote()

    service.dispatch_once(remote.factory)
    assert probe.calls == []  # 无队列不探测

    _enqueue(service, items=1, profile_id="p1", snapshot=_snapshot(["i1"]))
    service.dispatch_once(remote.factory)
    assert probe.calls == [["i1"]]  # 只探测有排队作业的池


def test_default_async_gate_uses_config_and_schema_meta(monkeypatch):
    from config import Config
    from core.video_async_impact import ASYNC_CONFIRM_KEY

    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    data["video_instances"][0]["model_id"] = "vllm-gate-model"
    monkeypatch.setattr(
        Config,
        "CONFIG",
        {
            "api_providers": {
                "p-vllm": {"protocol": "vllm-omni", "base_url": "http://video.internal"}
            },
            "custom_models": [
                {"id": "vllm-gate-model", "provider": "p-vllm", "model": "H3"}
            ],
        },
    )
    service = _service(data)
    service._repo.set_system_meta(ASYNC_CONFIRM_KEY, "")
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()

    assert service.dispatch_once(remote.factory).posts == 0
    assert service._repo.get_job(job_id)["state"] == "queued"

    service._repo.set_system_meta(ASYNC_CONFIRM_KEY, '["vllm-gate-model"]')
    assert service.dispatch_once(remote.factory).posts == 1
    assert service._repo.get_job(job_id)["state"] != "queued"


def test_unconfirmed_async_model_is_not_eligible_in_service():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    data["video_instances"][0]["model_id"] = "vllm-model"
    blocked = {"vllm-model"}
    service = _service(data, blocked_models_provider=lambda data: blocked)
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()

    first = service.dispatch_once(remote.factory)
    assert first.posts == 0
    assert service._repo.get_job(job_id)["state"] == "queued"

    blocked.clear()
    second = service.dispatch_once(remote.factory)
    assert second.posts == 1
    assert service._repo.count_inflight() == 1


def test_dispatch_uses_probe_service_for_shared_admission():
    from core.video_probe_service import ProbeService

    data = _pool_data(
        instances=[_instance("i1", "gpu-1", access_mode="shared")],
        profiles=[_profile("p1", ["i1"])],
    )
    clock = Clock()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"data": [], "has_more": False})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    probe = ProbeService(client=client, clock=clock, wall_clock=clock)
    service = _service(data, clock=clock, probe_service=probe)
    try:
        _qualify(service._repo, data)
        _enqueue(service, items=1, snapshot=_snapshot(["i1"]))

        remote = FakeRemote()
        first = service.dispatch_once(remote.factory)
        assert first.posts == 0  # 只有一次 idle
        assert len(calls) == 1

        clock.value += 6
        second = service.dispatch_once(remote.factory)
        assert second.posts == 1  # 两次独立 idle + 预留后确认
        assert len(calls) == 2
    finally:
        probe.close()


def test_service_grant_is_accepted_by_real_custom_video_client(tmp_path):
    """服务签发与客户端验签必须使用同一个 GrantService 单例。"""
    now = time.time()
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data, clock=Clock(now))
    _qualify(service._repo, data, checked_at=now)
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    remote = FakeRemote()
    service.dispatch_once(remote.factory)

    grant = remote.submit_calls[0]["grant"]
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"id": "job-1"})
        if request.method == "GET" and request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"video-bytes")
        if request.method == "GET" and "job-1" in request.url.path:
            return httpx.Response(
                200,
                json={"id": "job-1", "status": "completed", "url": "http://mock/content"},
            )
        return httpx.Response(404)

    meta = {
        "id": "managed-model",
        "request_model": "MiniMaxAI/MiniMax-H3",
        "protocol": "vllm-omni",
        "base_url": "http://mock/v1",
        "capabilities": {"tasks": ["t2va"], "duration": {"min": 1, "max": 15}},
        "managed": True,
        "dispatch_job_id": grant.job_id,
        "managed_instance_id": "i1",
        "managed_resource_key": "gpu-1",
    }
    client = CustomVideoClient(meta, poll_interval=0.001, poll_timeout=1, timeout=1)
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    target = tmp_path / "grant.mp4"
    try:
        client.generate_video(
            prompt="demo",
            image_path=None,
            save_path=str(target),
            duration=4,
            dispatch_grant=grant,
            dispatch_job_id=grant.job_id,
        )
        assert target.read_bytes() == b"video-bytes"
        assert sum(1 for method, path in calls if method == "POST" and path == "/v1/videos") == 1
    finally:
        client._client.close()


def test_cancel_wins_before_send_produces_zero_post():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()

    class CancelFirstRepo(VideoDispatchRepo):
        def begin_submission(self, *, job_id: str, attempt_id: str) -> bool:
            service.cancel_job(job_id)
            return super().begin_submission(job_id=job_id, attempt_id=attempt_id)

    service._repo = CancelFirstRepo()
    report = service.dispatch_once(remote.factory)

    assert report.posts == 0
    assert remote.submit_count == 0
    assert service._repo.get_job(job_id)["state"] == "cancelled"
    assert service._repo.count_inflight() == 0


def test_concurrent_cancel_before_authorization_zero_post():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    authorized = threading.Event()
    cancelled = threading.Event()

    class RacingRepo(VideoDispatchRepo):
        def begin_submission(self, *, job_id: str, attempt_id: str) -> bool:
            authorized.set()
            assert cancelled.wait(5)
            return super().begin_submission(job_id=job_id, attempt_id=attempt_id)

    service._repo = RacingRepo()
    errors: List[BaseException] = []

    def send() -> None:
        try:
            service.dispatch_once(remote.factory)
        except BaseException as exc:  # noqa: BLE001 - 测试收集
            errors.append(exc)

    def cancel() -> None:
        try:
            assert authorized.wait(5)
            service.cancel_job(job_id)
        finally:
            cancelled.set()

    sender = threading.Thread(target=send)
    canceller = threading.Thread(target=cancel)
    sender.start()
    canceller.start()
    sender.join()
    canceller.join()

    assert not errors
    assert remote.submit_count == 0
    assert service._repo.get_job(job_id)["state"] == "cancelled"
    assert service._repo.count_inflight() == 0


def test_lost_send_right_returns_job_to_queue_without_post():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()

    class LostRightRepo(VideoDispatchRepo):
        def begin_submission(self, *, job_id: str, attempt_id: str) -> bool:
            return False

    service._repo = LostRightRepo()
    report = service.dispatch_once(remote.factory)

    assert report.posts == 0
    assert remote.submit_count == 0
    assert service._repo.get_job(job_id)["state"] == "queued"
    assert service._repo.count_inflight() == 0
    assert any(event["kind"] == "send_right_lost" for event in report.events)


def test_concurrency_disabled_grants_only_one_slot():
    data, service = _two_slot_setup()
    _enqueue(service, items=2, snapshot=_snapshot(["i1", "i2"], enable_concurrency=False))

    remote = FakeRemote()
    report = service.dispatch_once(remote.factory)

    assert report.posts == 1
    assert service._repo.count_inflight() == 1
    assert service._repo.count_queued() == 1


def test_shared_requires_two_independent_idle_and_final_confirmation():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1", access_mode="shared")],
        profiles=[_profile("p1", ["i1"])],
    )
    clock = Clock()
    service = _service(
        data,
        clock=clock,
        shared_observer=lambda _instance: ObservationSample("idle", clock.value),
    )
    _qualify(service._repo, data)
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    remote = FakeRemote()

    # 第一次 idle：shared 不得提交，也不得为未就绪的 shared 预留 attempt。
    assert service.dispatch_once(remote.factory).posts == 0
    assert service._repo.latest_attempt(_first_job_id(service)) is None

    # 第二次间隔 3 秒的 idle 仍不足 5 秒。
    clock.value = NOW + 3
    assert service.dispatch_once(remote.factory).posts == 0
    assert service._repo.latest_attempt(_first_job_id(service)) is None

    # 两次独立 idle（间隔 ≥ 5 秒）+ 预留后确认 idle。
    clock.value = NOW + 6
    report = service.dispatch_once(remote.factory)
    assert report.posts == 1
    assert report.submitted == 1


def test_shared_final_confirmation_failure_releases_without_post():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1", access_mode="shared")],
        profiles=[_profile("p1", ["i1"])],
    )
    clock = Clock()
    observations = [
        ObservationSample("idle", NOW - 12),
        ObservationSample("busy", NOW),
    ]

    def observer(_instance) -> ObservationSample:
        return observations.pop(0)

    service = _service(data, clock=clock, shared_observer=observer)
    _qualify(service._repo, data)
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    service.record_shared_observation("i1", ObservationSample("idle", NOW - 6))

    remote = FakeRemote()
    report = service.dispatch_once(remote.factory)

    assert report.posts == 0
    assert service._repo.count_inflight() == 0
    assert service._repo.count_queued() == 1


def test_unknown_submit_keeps_slot_and_never_resends():
    data, service = _two_slot_setup()
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    remote = FakeRemote()
    remote.submit_mode = "unknown"

    report = service.dispatch_once(remote.factory)
    assert report.posts == 1
    assert report.unknown == 1
    assert remote.submit_count == 1
    assert service._repo.count_inflight() == 1
    assert service._repo.count_quarantined() == 1

    attempt = service._repo.latest_attempt(_first_job_id(service))
    assert attempt["submission_state"] == "unknown"
    assert service.dispatch_once(remote.factory).posts == 0
    assert remote.submit_count == 1


def test_crash_after_remote_accept_is_quarantined_without_resend():
    data, service = _two_slot_setup()
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))

    def crash(stage: str, payload: Mapping[str, Any]) -> None:
        if stage == "after_submit":
            raise DispatchCrashInjected("simulated crash after POST")

    crashing = _service(data, crash_hook=crash)
    remote = FakeRemote()
    with pytest.raises(DispatchCrashInjected):
        crashing.dispatch_once(remote.factory)
    assert remote.submit_count == 1
    assert service._repo.count_inflight() == 1

    restarted = _service(data, boot_id="boot-new")
    summary = restarted.recover_startup(interrupt_queued=False)
    assert summary["quarantined_old_boot"] == 1
    attempt = restarted._repo.latest_attempt(_first_job_id(service))
    assert attempt["submission_state"] == "unknown"
    assert service._repo.count_inflight() == 1

    assert restarted.dispatch_once(remote.factory).posts == 0
    assert remote.submit_count == 1


def test_crash_before_submit_never_posts_and_restart_quarantines():
    data, service = _two_slot_setup()
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))

    def crash(stage: str, payload: Mapping[str, Any]) -> None:
        if stage == "after_submitting":
            raise DispatchCrashInjected("simulated crash before POST")

    crashing = _service(data, crash_hook=crash)
    remote = FakeRemote()
    with pytest.raises(DispatchCrashInjected):
        crashing.dispatch_once(remote.factory)
    assert remote.submit_count == 0
    assert service._repo.count_inflight() == 1

    restarted = _service(data, boot_id="boot-new")
    summary = restarted.recover_startup(interrupt_queued=False)
    assert summary["quarantined_old_boot"] == 1
    assert restarted.dispatch_once(remote.factory).posts == 0
    assert remote.submit_count == 0
    assert service._repo.count_inflight() == 1


def test_restart_queries_original_remote_job_and_never_resubmits():
    data, service = _two_slot_setup()
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    assert remote.submit_count == 1

    remote.poll_state = "success"
    restarted = _service(data, boot_id="boot-new")
    summary = restarted.recover_startup(interrupt_queued=False)
    assert summary["quarantined_old_boot"] == 1

    report = restarted.reconcile_once(remote.factory)
    assert remote.submit_count == 1
    assert len(remote.poll_calls) == 1
    assert report.downloaded == 1
    assert service._repo.count_inflight() == 0
    job = service._repo.get_job(_first_job_id(service))
    assert job["state"] == "succeeded"


def test_failed_remote_keeps_slot_until_drain_confirmed():
    data, service = _two_slot_setup()
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    remote = FakeRemote()
    service.dispatch_once(remote.factory)

    remote.poll_state = "failure"
    report = service.reconcile_once(remote.factory)
    assert report.quarantined == 1
    assert service._repo.count_inflight() == 1
    job = service._repo.get_job(_first_job_id(service))
    assert job["state"] == "reconciling"

    remote.drain_confirmed = True
    service.reconcile_once(remote.factory)
    assert service._repo.count_inflight() == 0
    job = service._repo.get_job(_first_job_id(service))
    assert job["state"] == "failed"


def test_download_failure_retries_download_only():
    data, service = _two_slot_setup()
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    remote = FakeRemote()
    service.dispatch_once(remote.factory)

    remote.poll_state = "success"
    remote.download_failures = 1
    report = service.reconcile_once(remote.factory)

    assert remote.submit_count == 1
    assert len(remote.download_calls) == 2
    assert report.downloaded == 1
    job = service._repo.get_job(_first_job_id(service))
    assert job["state"] == "succeeded"


def test_heartbeat_stale_quarantines_without_releasing():
    data, service = _two_slot_setup()
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    service.dispatch_once(FakeRemote().factory)

    with database.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE video_resource_slots SET heartbeat_at = 0 WHERE resource_key = %s",
                    ("gpu-1",),
                )

    assert service.detect_stale() == 1
    assert service.detect_stale() == 0  # 已隔离的槽不重复计数
    assert service._repo.count_inflight() == 1
    assert service._repo.count_quarantined() == 1
    assert service.heartbeat_once() == 0  # 隔离槽不再续租
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT heartbeat_at FROM video_resource_slots WHERE resource_key = %s",
                ("gpu-1",),
            )
            assert int(cur.fetchone()["heartbeat_at"]) == 0


def test_db_failure_stops_dispatch_without_transport(monkeypatch):
    data, service = _two_slot_setup()
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    remote = FakeRemote()

    def boom(**kwargs):
        raise OperationalError("database down")

    monkeypatch.setattr(service._repo, "list_queued", boom)
    with pytest.raises(DispatchUnavailable):
        service.dispatch_once(remote.factory)
    assert remote.submit_count == 0


def test_concurrent_dispatch_single_slot_only_one_post():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    first = _service(data)
    second = _service(data)
    _qualify(first._repo, data)
    _enqueue(first, owner="u1", items=1, snapshot=_snapshot(["i1"]))
    _enqueue(first, owner="u2", items=1, snapshot=_snapshot(["i1"]))

    remote = FakeRemote()
    barrier = threading.Barrier(2)
    errors: List[BaseException] = []

    def worker(service: DispatchService) -> None:
        try:
            barrier.wait()
            service.dispatch_once(remote.factory)
        except BaseException as exc:  # noqa: BLE001 - 测试收集
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(first,)),
        threading.Thread(target=worker, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert remote.submit_count == 1
    assert first._repo.count_inflight() == 1


def test_queue_timeout_and_pool_offline_fail_without_post():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    with database.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE video_dispatch_jobs SET queue_deadline_at = 1 WHERE job_id = %s",
                    (job_id,),
                )

    remote = FakeRemote()
    report = service.dispatch_once(remote.factory)
    assert report.posts == 0
    assert service._repo.get_job(job_id)["state"] == "failed"
    assert service._repo.get_job(job_id)["error_json"]["error_code"] == "queue_timeout"

    offline = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"], enabled=False)],
    )
    offline_service = _service(offline)
    _qualify(offline_service._repo, offline)
    _enqueue(offline_service, items=1, snapshot=_snapshot(["i1"]))
    report = offline_service.dispatch_once(remote.factory)
    assert report.posts == 0
    assert offline_service._repo.count_queued() == 0
    assert offline_service._repo.list_active_attempts() == []


def test_cancel_queued_job_never_posts():
    _data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]

    job = service.cancel_job(job_id)
    assert job["state"] == "cancelled"
    remote = FakeRemote()
    assert service.dispatch_once(remote.factory).posts == 0


def test_cancel_submitted_keeps_slot_until_remote_finishes():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    service.dispatch_once(remote.factory)

    job = service.cancel_job(job_id, transport_factory=remote.factory)
    assert job["cancel_requested"] is True
    assert len(remote.cancel_calls) == 1
    assert service._repo.count_inflight() == 1

    remote.poll_state = "success"
    service.reconcile_once(remote.factory)
    assert service._repo.count_inflight() == 0
    assert service._repo.get_job(job_id)["state"] == "cancelled"
    assert remote.download_calls == []


def test_cancel_unconfirmed_keeps_slot():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    service.dispatch_once(remote.factory)

    remote.cancel_confirmed = False
    job = service.cancel_job(job_id, transport_factory=remote.factory)
    assert len(remote.cancel_calls) == 1
    assert job["cancel_requested"] is True
    assert job["state"] == "reconciling"
    assert service._repo.count_inflight() == 1


def test_cancel_confirmed_keeps_slot_until_remote_terminal():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    service.dispatch_once(remote.factory)

    job = service.cancel_job(job_id, transport_factory=remote.factory)
    assert job["state"] == "reconciling"
    attempt = service._repo.latest_attempt(job_id)
    assert attempt["submission_state"] == "cancelled"
    assert service._repo.count_inflight() == 1

    remote.poll_state = "failure"
    service.reconcile_once(remote.factory)
    assert service._repo.count_inflight() == 0
    assert service._repo.get_job(job_id)["state"] == "cancelled"


def test_cancel_after_authorization_allows_one_post_then_reconciles():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()

    class CancelAfterAuthRepo(VideoDispatchRepo):
        def begin_submission(self, *, job_id: str, attempt_id: str) -> bool:
            ok = super().begin_submission(job_id=job_id, attempt_id=attempt_id)
            if ok:
                service.cancel_job(job_id)
            return ok

    service._repo = CancelAfterAuthRepo()
    report = service.dispatch_once(remote.factory)

    assert report.posts == 1
    assert remote.submit_count == 1
    assert service._repo.count_inflight() == 1
    assert service._repo.get_job(job_id)["state"] == "remote_queued"

    remote.poll_state = "success"
    service.reconcile_once(remote.factory)
    assert service._repo.count_inflight() == 0
    assert service._repo.get_job(job_id)["state"] == "cancelled"
    assert remote.download_calls == []


def test_transport_factory_failure_releases_and_requeues():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]

    def broken_factory(instance, job):
        raise RuntimeError("transport init failed")

    report = service.dispatch_once(broken_factory)

    assert report.posts == 0
    assert service._repo.get_job(job_id)["state"] == "queued"
    assert service._repo.count_inflight() == 0
    assert service._repo.latest_attempt(job_id)["submission_state"] == "terminal"


def test_poll_unknown_records_diagnostic_without_resubmit():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    service.dispatch_once(remote.factory)

    remote.poll_state = "unknown"
    service.reconcile_once(remote.factory)

    assert remote.submit_count == 1
    assert service._repo.count_inflight() == 1
    assert service._repo.get_job(job_id)["state"] == "unknown"
    attempt = service._repo.latest_attempt(job_id)
    assert attempt["submission_state"] == "unknown"
    assert attempt["error_json"]["reason"] == "poll_unknown"


def test_cancel_confirmed_pending_beyond_grace_quarantines():
    clock = Clock()
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data, clock=clock)
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    service.cancel_job(job_id, transport_factory=remote.factory)

    clock.value += 301
    remote.poll_state = "pending"
    report = service.reconcile_once(remote.factory)

    assert report.quarantined == 1
    assert service._repo.count_inflight() == 1
    assert service._repo.get_job(job_id)["state"] == "reconciling"
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT quarantine_reason FROM video_resource_slots WHERE resource_key = %s",
                ("gpu-1",),
            )
            assert cur.fetchone()["quarantine_reason"] == "cancel_confirmed_pending"


def test_cancel_confirmed_unknown_beyond_grace_quarantines():
    clock = Clock()
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data, clock=clock)
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    service.cancel_job(job_id, transport_factory=remote.factory)

    clock.value += 301
    remote.poll_state = "unknown"
    report = service.reconcile_once(remote.factory)

    assert report.quarantined == 1
    assert service._repo.count_inflight() == 1
    assert service._repo.get_job(job_id)["state"] == "reconciling"
    attempt = service._repo.latest_attempt(job_id)
    assert attempt["submission_state"] == "cancelled"
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT quarantine_reason FROM video_resource_slots WHERE resource_key = %s",
                ("gpu-1",),
            )
            assert cur.fetchone()["quarantine_reason"] == "cancel_confirmed_pending"


def test_download_factory_failure_retries_and_fails_download():
    data, service = _two_slot_setup()
    result = _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    service.dispatch_once(remote.factory)

    calls = {"count": 0}

    def factory(instance, job):
        calls["count"] += 1
        # 第一次给 poll 用；之后下载工厂一律失败。
        if calls["count"] > 1:
            raise RuntimeError("download factory failed")
        return remote.factory(instance, job)

    remote.poll_state = "success"
    report = service.reconcile_once(factory)

    assert remote.submit_count == 1
    assert remote.download_calls == []
    assert report.failed == 1
    assert service._repo.count_inflight() == 0  # 计算已结束，槽已释放
    assert service._repo.get_job(job_id)["state"] == "failed"
    assert service._repo.get_job(job_id)["error_json"]["error_code"] == "download_failed"


def test_missing_profile_is_configuration_error_not_offline():
    data, service = _two_slot_setup()
    result = _enqueue(
        service, items=1, profile_id="missing-pool", snapshot=_snapshot(["i1"])
    )
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()

    report = service.dispatch_once(remote.factory)

    assert report.posts == 0
    job = service._repo.get_job(job_id)
    assert job["state"] == "failed"
    assert job["error_json"]["error_code"] == "pool_configuration_error"


def test_stop_undispatched_only_fails_queued_jobs():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    _enqueue(service, batch_id="batch-a", items=1, snapshot=_snapshot(["i1"]))
    _enqueue(service, batch_id="batch-b", items=1, snapshot=_snapshot(["i1"]))
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    assert service._repo.count_inflight() == 1

    stopped = service.stop_undispatched()
    assert stopped == 1
    assert service._repo.count_inflight() == 1
    assert service._repo.count_queued() == 0


def _first_job_id(service: DispatchService) -> str:
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT job_id FROM video_dispatch_jobs ORDER BY accepted_at, job_id LIMIT 1")
            return str(cur.fetchone()["job_id"])

def test_runtime_exposes_queue_limits_from_yaml():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    data["video_dispatch"]["queue_timeout_seconds"] = 123
    data["video_dispatch"]["max_queued_jobs"] = 7
    runtime = _service(data).runtime()
    assert runtime.queue_timeout_seconds == 123
    assert runtime.max_queued_jobs == 7

def test_unknown_without_remote_id_fails_job_and_keeps_slot_quarantined():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(
        service, items=1, batch_id="batch-unknown", snapshot=_snapshot(["i1"])
    )
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    remote.submit_mode = "unknown"

    assert service.dispatch_once(remote.factory).posts == 1
    assert service._repo.get_job(job_id)["state"] == "failed"

    service.reconcile_once(remote.factory)
    job = service._repo.get_job(job_id)
    assert job["state"] == "failed"
    assert (job.get("error_json") or {}).get("error_code") == "dispatch_unknown"
    assert "gpu-1" in service._repo.quarantined_resource_keys()
    assert remote.poll_calls == []
    assert service._repo.count_queued() == 0
    assert service.has_pending_work() is False
    before = remote.submit_count
    assert service.dispatch_once(remote.factory).posts == 0
    assert remote.submit_count == before

    results = VideoGenerationService(service, poll_interval=0.01).wait(
        BatchHandle(batch_id="batch-unknown", owner="u1", job_ids={"item-0": job_id}),
        timeout=1,
    )
    assert results["item-0"]["state"] == "failed"
    assert results["item-0"]["error"]["error_code"] == "dispatch_unknown"

    attempt = service._repo.latest_attempt(job_id)
    assert attempt is not None
    released = service._repo.release_quarantine(
        resource_key="gpu-1",
        released_by="admin",
        reason="手工核对完成",
        expected_attempt_id=str(attempt["attempt_id"]),
    )
    assert released is not None
    assert "gpu-1" not in service._repo.quarantined_resource_keys()
    released_attempt = service._repo.latest_attempt(job_id)
    assert released_attempt["submission_state"] == "terminal"
    assert released_attempt["error_json"]["reason"] == "admin_released"
    assert released_attempt["error_json"]["released_by"] == "admin"
    job = service._repo.get_job(job_id)
    assert job["state"] == "failed"
    assert job["error_json"]["error_code"] == "admin_released"

def test_poll_unknown_with_remote_id_can_recover_to_success():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(
        service, items=1, batch_id="batch-recover", snapshot=_snapshot(["i1"])
    )
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    remote.poll_state = "unknown"
    assert service.dispatch_once(remote.factory).posts == 1
    service.reconcile_once(remote.factory)
    assert service._repo.get_job(job_id)["state"] == "unknown"

    observed: Dict[str, Any] = {}

    def wait_job() -> None:
        observed.update(
            VideoGenerationService(service, poll_interval=0.01).wait(
                BatchHandle(
                    batch_id="batch-recover",
                    owner="u1",
                    job_ids={"item-0": job_id},
                ),
                timeout=2,
            )
        )

    waiter = threading.Thread(target=wait_job, daemon=True)
    waiter.start()
    time.sleep(0.1)
    assert waiter.is_alive()  # poll unknown 不应提前判失败

    remote.poll_state = "success"
    service.reconcile_once(remote.factory)
    waiter.join(2)
    assert not waiter.is_alive()
    assert observed["item-0"]["state"] == "succeeded"

def test_quarantine_release_requires_force_for_remote_job():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, batch_id="batch-release", snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    assert service.dispatch_once(remote.factory).posts == 1
    assert service._repo.quarantine_slot(
        resource_key="gpu-1", reason="engine_drain_unconfirmed"
    )
    attempt = service._repo.latest_attempt(job_id)
    assert attempt and attempt["remote_job_id"]
    with pytest.raises(QuarantineReleaseConflict) as exc:
        service._repo.release_quarantine(
            resource_key="gpu-1",
            released_by="admin",
            expected_attempt_id=str(attempt["attempt_id"]),
            expected_remote_job_id=str(attempt["remote_job_id"]),
        )
    assert exc.value.code == "remote_unconfirmed"
    released = service._repo.release_quarantine(
        resource_key="gpu-1",
        released_by="admin",
        expected_attempt_id=str(attempt["attempt_id"]),
        expected_remote_job_id=str(attempt["remote_job_id"]),
        force=True,
    )
    assert released is not None
    assert "gpu-1" not in service._repo.quarantined_resource_keys()
    released_attempt = service._repo.latest_attempt(job_id)
    assert released_attempt["submission_state"] == "terminal"
    assert released_attempt["error_json"]["forced"] is True
    assert released_attempt["error_json"]["remote_job_id"] == attempt["remote_job_id"]
    assert released_attempt["error_json"]["released_by"] == "admin"
    assert service._repo.get_job(job_id)["state"] == "failed"

def test_quarantine_release_does_not_overwrite_succeeded_job():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, batch_id="batch-succeeded", snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    assert service.dispatch_once(remote.factory).posts == 1
    assert service._repo.quarantine_slot(
        resource_key="gpu-1", reason="engine_drain_unconfirmed"
    )
    assert service._repo.set_job_state(
        job_id=job_id, state="succeeded", expected_states=("remote_queued",)
    )
    attempt = service._repo.latest_attempt(job_id)
    released = service._repo.release_quarantine(
        resource_key="gpu-1",
        released_by="admin",
        expected_attempt_id=str(attempt["attempt_id"]),
        expected_remote_job_id=str(attempt["remote_job_id"]),
        force=True,
    )
    assert released is not None
    assert service._repo.get_job(job_id)["state"] == "succeeded"
    assert service._repo.latest_attempt(job_id)["submission_state"] == "terminal"

def test_fail_profile_queued_stops_only_that_pool():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1"), _instance("i2", "gpu-2")],
        profiles=[_profile("p1", ["i1"]), _profile("p2", ["i2"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    first = _enqueue(service, items=1, profile_id="p1", snapshot=_snapshot(["i1"]))
    second = _enqueue(service, items=1, profile_id="p2", snapshot=_snapshot(["i2"]))
    assert service.fail_profile_queued("p1") == 1
    assert service._repo.get_job(first.jobs[0]["job_id"])["state"] == "failed"
    assert service._repo.get_job(second.jobs[0]["job_id"])["state"] == "queued"

def test_stop_all_blocks_new_jobs_until_resume():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    stopped = service.stop_all()
    assert stopped["dispatch_enabled"] is False
    with pytest.raises(DispatchUnavailable):
        _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    assert service.dispatch_once(FakeRemote().factory).posts == 0
    assert service.resume_all()["dispatch_enabled"] is True
    _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    assert service.dispatch_once(FakeRemote().factory).posts == 1


def test_drain_profile_blocks_eligibility_and_new_jobs_until_undrain():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1"), _instance("i2", "gpu-2")],
        profiles=[_profile("p1", ["i1"]), _profile("p2", ["i2"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    drained = service.drain_profile("p1")
    assert drained["draining"] is True
    assert service.runtime().eligible_members("p1") == ()
    with pytest.raises(DispatchUnavailable):
        _enqueue(service, items=1, profile_id="p1", snapshot=_snapshot(["i1"]))
    _enqueue(service, items=1, profile_id="p2", snapshot=_snapshot(["i2"]))
    assert service.undrain_profile("p1")["draining"] is False
    assert len(service.runtime().eligible_members("p1")) == 1

def test_enqueue_and_stop_are_serialized(monkeypatch):
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    original = service._repo.enqueue_batch
    entered = threading.Event()
    release = threading.Event()

    def slow_enqueue(**kwargs):
        entered.set()
        release.wait(2)
        return original(**kwargs)

    monkeypatch.setattr(service._repo, "enqueue_batch", slow_enqueue)
    errors = []

    def enqueue():
        try:
            _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=enqueue, daemon=True)
    worker.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=service.stop_all, daemon=True)
    stopper.start()
    release.set()
    worker.join(3)
    stopper.join(3)
    assert not worker.is_alive() and not stopper.is_alive()
    assert service._repo.count_queued() == 0
    assert errors == []


def test_stop_all_covers_reserved_prepared_attempt():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, batch_id="reserved-stop", snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    attempt = service._repo.reserve(
        job_id=job_id, resource_key="gpu-1", instance_id="i1"
    )
    stopped = service.stop_all()
    assert stopped["stopped_queued"] == 1
    assert service._repo.get_job(job_id)["state"] == "failed"
    assert service._repo.latest_attempt(job_id)["submission_state"] == "terminal"
    assert attempt["attempt_id"] not in service._repo.active_resource_keys()

def test_drain_instance_blocks_profile_when_it_is_the_only_member():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    service.drain_instance("i1", ["p1"])
    with pytest.raises(DispatchUnavailable) as exc:
        _enqueue(service, items=1, profile_id="p1", snapshot=_snapshot(["i1"]))
    assert exc.value.error_code == "pool_draining"
    service.undrain_instance("i1")
    _enqueue(service, items=1, profile_id="p1", snapshot=_snapshot(["i1"]))

def test_cancel_source_cancels_queued_jobs_for_business_delete():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(
        service, items=1, batch_id="batch-source", snapshot=_snapshot(["i1"])
    )
    job_id = result.jobs[0]["job_id"]
    stats = service.cancel_source("workflow", "batch-source")
    assert stats["total"] == 1 and stats["cancelled"] == 1
    assert service._repo.get_job(job_id)["state"] == "cancelled"

def test_deleted_source_discards_late_remote_success():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(
        service, items=1, batch_id="batch-late", snapshot=_snapshot(["i1"])
    )
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    assert service.dispatch_once(remote.factory).posts == 1
    service.mark_source_deleted("workflow", "batch-late")
    remote.poll_state = "success"
    service.reconcile_once(remote.factory)
    job = service._repo.get_job(job_id)
    assert job["state"] == "cancelled"
    assert job["error_json"]["error_code"] == "source_deleted"
    assert remote.download_calls == []
    assert service._repo.count_inflight() == 0

def test_tombstone_queries_fail_closed():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(
        service, items=1, batch_id="batch-closed", snapshot=_snapshot(["i1"])
    )
    job_id = result.jobs[0]["job_id"]
    remote = FakeRemote()
    service.dispatch_once(remote.factory)

    def broken(*args, **kwargs):
        raise OperationalError("db down")

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(service._repo, "is_source_deleted", broken)
    try:
        remote.poll_state = "success"
        service.reconcile_once(remote.factory)
    finally:
        monkeypatch.undo()
    assert service._repo.get_job(job_id)["state"] == "reconciling"
    assert service._repo.count_inflight() == 1
    assert remote.download_calls == []


def test_tombstone_per_source_keys_are_independent():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    service.mark_source_deleted("workflow", "a")
    service.mark_source_deleted("workflow", "b")
    assert service._repo.is_source_deleted("workflow", "a") is True
    assert service._repo.is_source_deleted("workflow", "b") is True
    assert service._repo.is_source_deleted("workflow", "c") is False

def test_deleting_source_blocks_new_enqueue_until_rollback():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    service.begin_source_delete("workflow", "batch-window")
    try:
        with pytest.raises(DispatchUnavailable) as exc:
            _enqueue(
                service,
                items=1,
                batch_id="batch-window",
                snapshot=_snapshot(["i1"]),
            )
        assert exc.value.error_code == "source_deleting"
    finally:
        service.end_source_delete("workflow", "batch-window")
    _enqueue(service, items=1, batch_id="batch-window", snapshot=_snapshot(["i1"]))

def test_deleting_marker_persists_and_recovers_as_tombstone():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    service.begin_source_delete("workflow", "crash-session")
    assert service._repo.is_source_deleting("workflow", "crash-session")

    restarted = _service(data)
    result = restarted.reconcile_pending_deletions(lambda sid: False)
    assert result["tombstoned"] == 1
    assert restarted._repo.is_source_deleted("workflow", "crash-session")
    assert not restarted._repo.is_source_deleting("workflow", "crash-session")


def test_deleting_marker_rolls_back_if_session_still_exists():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    service.begin_source_delete("workflow", "alive-session")
    restarted = _service(data)
    result = restarted.reconcile_pending_deletions(lambda sid: sid == "alive-session")
    assert result["restored"] == 1
    assert not restarted._repo.is_source_deleting("workflow", "alive-session")
    assert not restarted._repo.is_source_deleted("workflow", "alive-session")

def test_begin_delete_db_and_memory_are_under_same_lock(monkeypatch):
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    entered = threading.Event()
    release = threading.Event()
    original = service._repo.mark_source_deleting

    def slow_mark(kind, sid):
        entered.set()
        release.wait(2)
        return original(kind, sid)

    monkeypatch.setattr(service._repo, "mark_source_deleting", slow_mark)
    errors = []

    def begin():
        service.begin_source_delete("workflow", "race-session")

    def enqueue():
        try:
            _enqueue(
                service,
                items=1,
                batch_id="race-session",
                snapshot=_snapshot(["i1"]),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    first = threading.Thread(target=begin, daemon=True)
    second = threading.Thread(target=enqueue, daemon=True)
    first.start()
    assert entered.wait(2)
    second.start()
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert errors and getattr(errors[0], "error_code", "") == "source_deleting"
    assert service._repo.count_queued() == 0

def test_queue_full_is_mapped_to_structured_error(monkeypatch):
    from db.repositories.video_dispatch import QueueFull

    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)

    def full(**kwargs):
        raise QueueFull("full")

    monkeypatch.setattr(service._repo, "enqueue_batch", full)
    with pytest.raises(DispatchUnavailable) as exc:
        _enqueue(service, items=1, snapshot=_snapshot(["i1"]))
    assert exc.value.error_code == "queue_full"

def test_stop_and_drain_survive_service_restart():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    service.stop_all()
    service.drain_profile("p1")

    restarted = _service(data)
    state = restarted.restore_runtime_state()
    assert state["dispatch_stopped"] is True
    assert state["draining_profiles"] == 1
    with pytest.raises(DispatchUnavailable) as exc:
        _enqueue(restarted, items=1, profile_id="p1", snapshot=_snapshot(["i1"]))
    assert exc.value.error_code == "dispatch_disabled"

    restarted.resume_all()
    restarted.undrain_profile("p1")
    again = _service(data)
    state = again.restore_runtime_state()
    assert state["dispatch_stopped"] is False
    assert state["draining_profiles"] == 0
    _enqueue(again, items=1, profile_id="p1", snapshot=_snapshot(["i1"]))

def test_restart_with_draining_profile_fails_queued_jobs():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(
        service, items=1, batch_id="batch-drain-crash", snapshot=_snapshot(["i1"])
    )
    job_id = result.jobs[0]["job_id"]
    service._repo.mark_profile_draining("p1", True)

    restarted = _service(data)
    state = restarted.restore_runtime_state()
    assert state["failed_queued"] == 1
    job = restarted._repo.get_job(job_id)
    assert job["state"] == "failed"
    assert job["error_json"]["error_code"] == "pool_unavailable"

def test_restart_draining_profile_releases_prepared_attempt():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(
        service, items=1, batch_id="prepared-drain", snapshot=_snapshot(["i1"])
    )
    job_id = result.jobs[0]["job_id"]
    attempt = service._repo.reserve(
        job_id=job_id, resource_key="gpu-1", instance_id="i1"
    )
    service._repo.mark_profile_draining("p1", True)

    restarted = _service(data)
    state = restarted.restore_runtime_state()
    assert state["failed_queued"] == 1
    assert restarted._repo.get_job(job_id)["state"] == "failed"
    assert restarted._repo.latest_attempt(job_id)["submission_state"] == "terminal"
    assert attempt["attempt_id"] not in restarted._repo.active_resource_keys()


def test_restart_draining_instance_fails_corrupt_snapshot():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(
        service, items=1, batch_id="corrupt-snapshot", snapshot=_snapshot(["i1"])
    )
    job_id = result.jobs[0]["job_id"]
    attempt = service._repo.reserve(
        job_id=job_id, resource_key="gpu-1", instance_id="i1"
    )
    with database.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE video_dispatch_jobs SET snapshot_json = %s::jsonb "
                "WHERE job_id = %s",
                ('"broken"', job_id),
            )
    service._repo.mark_instance_draining("i1", True)

    restarted = _service(data)
    state = restarted.restore_runtime_state()
    assert state["failed_queued"] == 1
    job = restarted._repo.get_job(job_id)
    assert job["state"] == "failed"
    assert job["error_json"]["error_code"] == "pool_configuration_error"

def test_cancel_prepared_attempt_and_begin_submission_are_mutually_exclusive():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)

    first = _enqueue(service, items=1, batch_id="begin-wins", snapshot=_snapshot(["i1"]))
    first_job = first.jobs[0]["job_id"]
    first_attempt = service._repo.reserve(
        job_id=first_job, resource_key="gpu-1", instance_id="i1"
    )
    assert service._repo.begin_submission(
        job_id=first_job, attempt_id=first_attempt["attempt_id"]
    )
    assert service._repo.cancel_prepared_attempt(
        attempt_id=first_attempt["attempt_id"], reason="drain"
    ) is False
    assert service._repo.latest_attempt(first_job)["submission_state"] == "submitting"
    assert "gpu-1" in service._repo.active_resource_keys()

    service._repo.release(
        resource_key="gpu-1",
        attempt_id=first_attempt["attempt_id"],
        token=first_attempt["token"],
    )
    service._repo.mark_attempt_terminal(
        attempt_id=first_attempt["attempt_id"], reason="test_cleanup"
    )

    second = _enqueue(service, items=1, batch_id="cancel-wins", snapshot=_snapshot(["i1"]))
    second_job = second.jobs[0]["job_id"]
    second_attempt = service._repo.reserve(
        job_id=second_job, resource_key="gpu-1", instance_id="i1"
    )
    assert service._repo.cancel_prepared_attempt(
        attempt_id=second_attempt["attempt_id"], reason="drain"
    ) is True
    assert service._repo.begin_submission(
        job_id=second_job, attempt_id=second_attempt["attempt_id"]
    ) is False
    assert service._repo.latest_attempt(second_job)["submission_state"] == "terminal"
    assert "gpu-1" not in service._repo.active_resource_keys()


def test_cancel_prepared_attempt_concurrent_with_begin_has_one_winner():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    result = _enqueue(service, items=1, batch_id="race", snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    attempt = service._repo.reserve(
        job_id=job_id, resource_key="gpu-1", instance_id="i1"
    )
    barrier = threading.Barrier(2)
    outcome = {}

    def begin():
        barrier.wait()
        outcome["begin"] = service._repo.begin_submission(
            job_id=job_id, attempt_id=attempt["attempt_id"]
        )

    def cancel():
        barrier.wait()
        outcome["cancel"] = service._repo.cancel_prepared_attempt(
            attempt_id=attempt["attempt_id"], reason="drain"
        )

    threads = [threading.Thread(target=begin), threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert not any(thread.is_alive() for thread in threads)
    assert outcome["begin"] is not outcome["cancel"]
    state = service._repo.latest_attempt(job_id)["submission_state"]
    if outcome["begin"]:
        assert state == "submitting"
        assert "gpu-1" in service._repo.active_resource_keys()
    else:
        assert state == "terminal"
        assert "gpu-1" not in service._repo.active_resource_keys()

def test_diagnostic_job_uses_unqualified_instance_but_still_reserves_slot():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    snapshot = _snapshot(["i1"], mode="first_frame", enable_concurrency=False)
    snapshot["diagnostic"] = True
    result = _enqueue(
        service,
        owner="system:qualification",
        batch_id="diagnostic-1",
        snapshot=snapshot,
    )
    remote = FakeRemote()
    report = service.dispatch_once(remote.factory)
    assert report.posts == 1
    assert remote.submit_count == 1
    assert service._repo.get_job(result.jobs[0]["job_id"])["state"] == "remote_queued"
    assert "gpu-1" in service._repo.active_resource_keys()
    remote.poll_state = "success"
    service.reconcile_once(remote.factory)
    assert service._repo.get_job(result.jobs[0]["job_id"])["state"] == "succeeded"
    assert service._repo.active_resource_keys() == set()


def test_non_diagnostic_job_still_requires_qualification():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    result = _enqueue(service, batch_id="normal-unqualified", snapshot=_snapshot(["i1"]))
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    assert remote.submit_count == 0
    assert service._repo.get_job(result.jobs[0]["job_id"])["state"] == "queued"

def test_diagnostic_job_does_not_bypass_unconfirmed_async_model():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(
        data, blocked_models_provider=lambda _data: {"model-i1"}
    )
    snapshot = _snapshot(["i1"], mode="first_frame")
    snapshot["diagnostic"] = True
    result = _enqueue(
        service, owner="system:qualification", batch_id="diagnostic-blocked", snapshot=snapshot
    )
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    assert remote.submit_count == 0
    assert service._repo.get_job(result.jobs[0]["job_id"])["state"] == "queued"

def test_input_materialization_failure_releases_slot_without_post():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )

    def broken(_job, _attempt):
        raise RuntimeError("canonical missing")

    service = _service(data, job_input_preparer=broken)
    _qualify(service._repo, data)
    result = _enqueue(service, batch_id="materialize-fail", snapshot=_snapshot(["i1"]))
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    assert remote.submit_count == 0
    job = service._repo.get_job(result.jobs[0]["job_id"])
    assert job["state"] == "failed"
    assert job["error_json"]["error_code"] == "input_materialization_failed"
    assert service._repo.active_resource_keys() == set()


def test_result_publisher_failure_is_not_marked_succeeded():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )

    def broken_publisher(_job, _attempt, _result):
        raise RuntimeError("publisher down")

    service = _service(data, result_publisher=broken_publisher)
    _qualify(service._repo, data)
    result = _enqueue(service, batch_id="publish-fail", snapshot=_snapshot(["i1"]))
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    attempt = service._repo.latest_attempt(result.jobs[0]["job_id"])
    downloaded = f"/tmp/{attempt['remote_job_id']}.mp4"
    with open(downloaded, "wb") as handle:
        handle.write(b"half-published")
    remote.poll_state = "success"
    service.reconcile_once(remote.factory)
    job = service._repo.get_job(result.jobs[0]["job_id"])
    assert job["state"] == "failed"
    assert job["error_json"]["error_code"] == "publish_failed"
    assert not os.path.exists(downloaded)

def test_dispatch_uses_materialized_job_copy_without_mutating_snapshot():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    seen = {}

    def prepare(job, _attempt):
        snapshot = dict(job["snapshot_json"])
        request = dict(snapshot["request"])
        request["image_path"] = "/run/inputs/job.png"
        snapshot["request"] = request
        return {**job, "snapshot_json": snapshot}

    service = _service(data, job_input_preparer=prepare)
    _qualify(service._repo, data)
    result = _enqueue(service, batch_id="materialized", snapshot=_snapshot(["i1"]))

    class CaptureTransport(FakeTransport):
        def __init__(self, instance, job):
            seen["image_path"] = job["snapshot_json"]["request"]["image_path"]
            super().__init__(FakeRemote(), instance, job)

    def factory(instance, job):
        return CaptureTransport(instance, job)

    service.dispatch_once(factory)
    assert seen["image_path"] == "/run/inputs/job.png"
    original = service._repo.get_job(result.jobs[0]["job_id"])["snapshot_json"]
    assert original["request"].get("image_path") != "/run/inputs/job.png"

def test_result_publisher_called_before_succeeded_state():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    published = []

    def publisher(job, attempt, result):
        published.append((job["job_id"], result["save_path"]))
        return {"published": [{"public_path": "_media/result.mp4"}]}

    service = _service(data, result_publisher=publisher)
    _qualify(service._repo, data)
    result = _enqueue(service, batch_id="publish-ok", snapshot=_snapshot(["i1"]))
    remote = FakeRemote()
    service.dispatch_once(remote.factory)
    remote.poll_state = "success"
    service.reconcile_once(remote.factory)
    job = service._repo.get_job(result.jobs[0]["job_id"])
    assert job["state"] == "succeeded"
    assert published and published[0][0] == job["job_id"]
    assert job["result_json"]["published"]["published"][0]["public_path"] == (
        "_media/result.mp4"
    )

def test_job_phase_reports_waiting_until_member_is_eligible():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1")],
        profiles=[_profile("p1", ["i1"])],
    )
    service = _service(data)
    result = _enqueue(service, batch_id="phase", snapshot=_snapshot(["i1"]))
    job_id = result.jobs[0]["job_id"]
    assert service.job_phase(job_id) == "waiting_pool_unavailable"
    _qualify(service._repo, data)
    assert service.job_phase(job_id) == "queued"

def test_capacity_snapshot_counts_active_by_execution_not_batch():
    data = _pool_data(
        instances=[_instance("i1", "gpu-1"), _instance("i2", "gpu-2"), _instance("i3", "gpu-3")],
        profiles=[_profile("p1", ["i1", "i2", "i3"], cap=2)],
    )
    service = _service(data)
    _qualify(service._repo, data)

    def enqueue(batch):
        service.enqueue_batch(
            owner="u1",
            batch_id=batch,
            jobs=[
                {
                    "request_key": batch,
                    "item_id": "item",
                    "source_kind": "workflow",
                    "execution_id": "exec-shared",
                    "dispatch_mode": "auto",
                    "profile_id": "p1",
                    "profile_revision": 1,
                    "snapshot_json": _snapshot(["i1", "i2", "i3"], cap=2),
                }
            ],
            queue_timeout_seconds=3600,
            max_queued_jobs=1000,
        )

    enqueue("batch-a")
    enqueue("batch-b")
    enqueue("batch-c")
    remote = FakeRemote()
    report = service.dispatch_once(remote.factory)
    assert report.submitted == 2
    assert service._repo.count_queued() == 1

    queued = service._repo.list_queued()
    capacity = service.capacity_snapshot(service.runtime(), queued)
    assert capacity.execution_active["exec-shared"] == 2
    assert capacity.per_execution_limits["exec-shared"] == 2
    assert capacity.free_for(service._capacity_job(queued[0])) == 0
