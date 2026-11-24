"""7.x F8-A/B/C/E：8 用户同池/四池/零就绪/公平去重的真实调度服务测试（Fake transport）。"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List

from core.video_dispatch_service import (
    DispatchService,
    PollOutcome,
    SubmitOutcome,
)
from core.video_pool_config import compute_revision
from db.repositories.video_dispatch import VideoDispatchRepo


def _instance(instance_id: str, resource_key: str) -> Dict[str, Any]:
    return {
        "instance_id": instance_id,
        "model_id": f"model-{instance_id}",
        "resource_key": resource_key,
        "access_mode": "exclusive",
        "enabled": True,
        "driver": "vllm_omni",
        "probe": {"kind": "vllm_omni", "jobs_url": "", "metrics_url": "", "labels": {}},
    }


def _profile(profile_id: str, members: List[str]) -> Dict[str, Any]:
    return {
        "id": profile_id,
        "name": profile_id,
        "revision": 1,
        "instances": members,
        "enabled": True,
        "max_concurrency_per_execution": 2,
        "contracts": {
            "first_frame": {
                "task": "fl2va",
                "resolutions": ["720P"],
                "short_edge": 720,
                "ratios": ["16:9"],
                "duration": {"min": 4, "max": 15},
                "fps": [24],
            }
        },
    }


def _data(instances, profiles, max_running=8) -> Dict[str, Any]:
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


def _qualify(repo: VideoDispatchRepo, data: Dict[str, Any]) -> None:
    from core.video_qualification import build_qualification_evidence

    for row in data["video_instances"]:
        repo.save_qualification(
            instance_id=row["instance_id"],
            resource_key=row["resource_key"],
            evidence=build_qualification_evidence(
                instance_id=row["instance_id"],
                resource_key=row["resource_key"],
                config_revision=compute_revision(data),
                boot_id="boot-f8",
                checks={"probe_ready": True},
                checked_by="admin",
                confirmed=True,
                checked_at=time.time(),
            ).to_dict(),
        )


def _service(data: Dict[str, Any]) -> DispatchService:
    repo = VideoDispatchRepo()
    return DispatchService(
        pool_loader=lambda: (data, compute_revision(data)),
        repo=repo,
        boot_id="boot-f8",
        blocked_models_provider=lambda _data: set(),
        sleep=lambda _seconds: None,
    )


class _Remote:
    def __init__(self) -> None:
        self.submits: List[str] = []
        self.lock = threading.Lock()

    def factory(self, instance, job):
        return _Transport(self, instance, job)


class _Transport:
    def __init__(self, remote: _Remote, instance, job) -> None:
        self.remote = remote
        self.instance = instance
        self.job = job

    def submit(self, *, job, instance, grant):
        with self.remote.lock:
            self.remote.submits.append(str(job["job_id"]))
            return SubmitOutcome("accepted", remote_job_id=f"r-{len(self.remote.submits)}")

    def poll(self, *, attempt, instance, job):
        return PollOutcome("pending")

    def download(self, *, attempt, instance, job):
        return {"save_path": f"/tmp/{attempt['remote_job_id']}.mp4"}

    def cancel(self, *, attempt, instance, job):
        return True

    def close(self):
        return None


def _enqueue(service: DispatchService, owner: str, count: int = 1, profile_id: str = "p1", execution_id: str = "") -> None:
    batch_id = f"b-{owner}-{uuid.uuid4().hex[:6]}"
    jobs = [
        {
            "request_key": batch_id,
            "item_id": f"i-{index}",
            "source_kind": "workflow",
            "source_id": f"s-{owner}",
            "execution_id": execution_id or batch_id,
            "dispatch_mode": "auto",
            "profile_id": profile_id,
            "profile_revision": 1,
            "snapshot_json": {
                "mode": "first_frame",
                "members": [m for p in service.runtime().profiles.values() if p.profile_id == profile_id for m in p.members],
                "enable_concurrency": True,
                "max_concurrency_per_execution": 2,
                "request": {"prompt": "x"},
            },
        }
        for index in range(count)
    ]
    service.enqueue_batch(
        owner=owner, batch_id=batch_id, jobs=jobs,
        queue_timeout_seconds=3600, max_queued_jobs=1000,
    )


def test_f8a_eight_owners_two_slots_wait_fairly():
    data = _data([_instance("i1", "gpu-1"), _instance("i2", "gpu-2")], [_profile("p1", ["i1", "i2"])])
    service = _service(data)
    _qualify(service._repo, data)
    for index in range(8):
        _enqueue(service, f"u{index}", 2)
    remote = _Remote()
    report = service.dispatch_once(remote.factory)
    assert report.posts == 2
    assert len(remote.submits) == 2
    assert service._repo.count_queued() == 14
    active = service._repo.list_active_attempts()
    assert len({row["resource_key"] for row in active}) == 2


def test_f8b_four_pools_use_eight_slots_and_one_owner_result_each():
    instances = []
    profiles = []
    for pool in range(4):
        ids = [f"i{pool}a", f"i{pool}b"]
        instances.extend([_instance(ids[0], f"gpu-{ids[0]}"), _instance(ids[1], f"gpu-{ids[1]}")])
        profiles.append(_profile(f"p{pool}", ids))
    data = _data(instances, profiles)
    service = _service(data)
    _qualify(service._repo, data)
    for index in range(8):
        _enqueue(service, f"u{index}", 1, profile_id=f"p{index % 4}")
    remote = _Remote()
    report = service.dispatch_once(remote.factory)
    assert report.posts == 8
    assert len(remote.submits) == 8
    assert service._repo.count_queued() == 0


def test_f8c_zero_ready_posts_nothing_and_reports_waiting():
    data = _data([_instance("i1", "gpu-1")], [_profile("p1", ["i1"])])
    service = _service(data)
    # 不写资格，模拟零就绪
    for index in range(8):
        _enqueue(service, f"u{index}", 1)
    remote = _Remote()
    report = service.dispatch_once(remote.factory)
    assert report.posts == 0
    assert service._repo.count_queued() == 8
    assert service._repo.active_resource_keys() == set()
    assert all(service.job_phase(row["job_id"]) == "waiting_pool_unavailable" for row in service._repo.list_queued())


def test_f8e_first_round_rotates_owners_without_duplicate_grants():
    data = _data(
        [_instance("i1", "gpu-1"), _instance("i2", "gpu-2"), _instance("i3", "gpu-3"), _instance("i4", "gpu-4")],
        [_profile("p1", ["i1", "i2", "i3", "i4"])],
    )
    service = _service(data)
    _qualify(service._repo, data)
    for index in range(8):
        _enqueue(service, f"u{index}", 1)
    remote = _Remote()
    service.dispatch_once(remote.factory)
    owners = [service._repo.get_job(job_id)["owner"] for job_id in remote.submits]
    assert len(owners) == 4
    assert len(set(owners)) == 4


def test_f8d_drain_fails_unstarted_jobs_without_posts():
    data = _data([_instance("i1", "gpu-1")], [_profile("p1", ["i1"])])
    service = _service(data)
    _qualify(service._repo, data)
    for index in range(8):
        _enqueue(service, f"u{index}", 1)
    result = service.drain_profile("p1")
    assert result["stopped_queued"] == 8
    remote = _Remote()
    report = service.dispatch_once(remote.factory)
    assert report.posts == 0
    assert service._repo.count_queued() == 0
    assert service._repo.active_resource_keys() == set()
