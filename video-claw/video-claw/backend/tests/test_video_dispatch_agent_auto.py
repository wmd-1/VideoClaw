"""4.6/4.7：auto 批量入队、固定池快照与真实调度服务联调（Fake transport）。"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from typing import Any, Dict, List

import pytest

from core.agents.video_agent import VideoDirectorAgent
from core.video_dispatch_runtime import DispatchRuntime, set_dispatch_runtime
from core.video_managed_bridge import ManagedBridgeError, managed_video_sync
from core.video_dispatch_service import (
    DispatchService,
    PollOutcome,
    SubmitOutcome,
)
from core.video_pool_config import compute_revision
from core.video_qualification import build_qualification_evidence
from db import database
from db.repositories.video_dispatch import VideoDispatchRepo


@pytest.fixture(autouse=True)
def clean_dispatch_tables():
    _truncate()
    yield
    set_dispatch_runtime(None)
    _truncate()


def _truncate() -> None:
    with database.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM video_resource_slots")
                cur.execute("DELETE FROM video_dispatch_jobs")


def _instance(instance_id: str, resource_key: str) -> dict:
    return {
        "instance_id": instance_id,
        "model_id": f"model-{instance_id}",
        "resource_key": resource_key,
        "access_mode": "exclusive",
        "enabled": True,
        "driver": "vllm_omni",
        "probe": {
            "kind": "vllm_omni",
            "jobs_url": "http://127.0.0.1/jobs",
            "metrics_url": "",
            "labels": {},
        },
    }


def _profile(members: List[str]) -> dict:
    return {
        "id": "p1",
        "name": "p1",
        "revision": 1,
        "instances": members,
        "enabled": True,
        "max_concurrency_per_execution": 3,
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


def _pool_data() -> dict:
    return {
        "video_dispatch": {
            "default_mode": "auto",
            "default_profiles": {},
            "queue_timeout_seconds": 3600,
            "max_queued_jobs": 1000,
            "max_running_jobs": 8,
        },
        "video_profiles": [_profile(["i1", "i2", "i3"])],
        "video_instances": [
            _instance("i1", "gpu-1"),
            _instance("i2", "gpu-2"),
            _instance("i3", "gpu-3"),
        ],
    }


def _qualify(repo: VideoDispatchRepo, data: dict) -> None:
    revision = compute_revision(data)
    for row in data["video_instances"]:
        evidence = build_qualification_evidence(
            instance_id=row["instance_id"],
            resource_key=row["resource_key"],
            config_revision=revision,
            boot_id="boot-auto",
            checked_by="admin",
            checks={"probe_ready": True},
            confirmed=True,
            checked_at=time.time(),
        )
        repo.save_qualification(
            instance_id=row["instance_id"],
            resource_key=row["resource_key"],
            evidence=evidence.to_dict(),
        )


class FakeTransport:
    def __init__(self, release: threading.Event, submits: List[str]):
        self.release = release
        self.submits = submits

    def submit(self, *, job, instance, grant) -> SubmitOutcome:
        self.submits.append(str(job["job_id"]))
        return SubmitOutcome("accepted", remote_job_id=f"remote-{job['job_id']}")

    def poll(self, *, attempt, instance, job) -> PollOutcome:
        return PollOutcome("success" if self.release.is_set() else "pending")

    def download(self, *, attempt, instance, job) -> Any:
        save_path = job["snapshot_json"]["request"]["save_path"]
        with open(save_path, "wb") as handle:
            handle.write(b"video")
        return {"save_path": save_path}

    def cancel(self, *, attempt, instance, job) -> bool:
        return True


def _input(tmp_path, count: int, session_id: str) -> Dict[str, Any]:
    image = tmp_path / "input.png"
    image.write_bytes(b"png")
    segments = []
    scenes = []
    for index in range(count):
        segment_id = f"seg-{index}"
        segments.append(
            {
                "segment_id": segment_id,
                "episode_number": 1,
                "segment_number": index + 1,
                "total_duration": 5,
                "characters": [],
                "location": "",
                "description": f"segment {index}",
                "shots": [{"shot_number": 1, "description": f"shot {index}"}],
            }
        )
        scenes.append({"id": segment_id, "selected": str(image), "versions": [str(image)]})
    artifacts = {
        "storyboard": {"episodes": [{"episode_number": 1, "segments": segments}]},
        "reference_generation": {"scenes": scenes},
        "character_design": {"characters": [], "settings": []},
        "video_generation": {"clips": []},
    }
    return {
        "session_id": session_id,
        "_session_owner": "owner-auto",
        "_session_meta": {"video_dispatch_mode": "auto"},
        "_session_artifacts": artifacts,
        "video_dispatch_mode": "auto",
        "video_generation_mode": "first_frame",
        "video_ratio": "16:9",
        "video_resolution": "720P",
        "enable_concurrency": True,
    }


def test_auto_batch_holds_slots_and_fixed_snapshot(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("auto 不应调用 _select_video_model")

    monkeypatch.setattr(
        VideoDirectorAgent, "_select_video_model", staticmethod(forbidden)
    )
    data = _pool_data()
    revision = compute_revision(data)
    repo = VideoDispatchRepo()
    _qualify(repo, data)
    service = DispatchService(
        pool_loader=lambda: (data, revision),
        repo=repo,
        boot_id="boot-auto",
        clock=time.time,
        sleep=lambda _seconds: None,
        download_attempts=2,
        max_safe_retries=1,
        blocked_models_provider=lambda data: set(),
    )
    release = threading.Event()
    submits: List[str] = []
    runtime = DispatchRuntime(
        service,
        lambda instance, job: FakeTransport(release, submits),
        interval=0.05,
    )
    runtime.start()
    set_dispatch_runtime(runtime)
    agent = VideoDirectorAgent()
    agent.execution_root = str(tmp_path / "exec")
    sid = f"s-{uuid.uuid4().hex[:8]}"

    result_holder: Dict[str, Any] = {}

    def run() -> None:
        result_holder["result"] = asyncio.run(
            agent.process(_input(tmp_path, 6, sid))
        )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while len(submits) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(submits) == 3, submits
    assert repo.count_inflight() == 3
    assert repo.count_queued() == 3
    first_job = repo.get_job(submits[0])
    snapshot = first_job["snapshot_json"]
    assert snapshot["members"] == ["i1", "i2", "i3"]
    assert snapshot["selection_source"]
    assert snapshot["profile_revision"] == 1
    assert snapshot["request"]["prompt"]
    # 4.9：job 输出独立落在本次执行根，不复用输入或旧版本路径。
    assert snapshot["request"]["save_path"].startswith(str(tmp_path / "exec"))
    assert snapshot["request"]["save_path"] != snapshot["request"].get("image_path")
    assert snapshot["owner_login"] == "owner-auto"
    assert "canonical_media" in snapshot
    assert snapshot["canonical_media"]["image_path"]

    release.set()
    worker.join(15)
    assert not worker.is_alive()
    payload = result_holder["result"]["payload"]
    assert len(payload["clips"]) == 6
    assert all(item["status"] == "done" for item in payload["clips"])
    assert len(submits) == 6

    # 4.7：片段重生成同样走批量入队；3/2/1 首轮分别派发 3/2/1 个片段。
    for expected, ids in (
        (3, ["seg-0", "seg-1", "seg-2"]),
        (2, ["seg-3", "seg-4"]),
        (1, ["seg-5"]),
    ):
        baseline = len(submits)
        release.clear()
        regen_input = _input(tmp_path, 6, sid)
        regen_holder: Dict[str, Any] = {}

        def regen() -> None:
            regen_holder["result"] = asyncio.run(
                agent.process(regen_input, {"regenerate_clips": ids})
            )

        regen_worker = threading.Thread(target=regen, daemon=True)
        regen_worker.start()
        deadline = time.monotonic() + 10
        while len(submits) < baseline + expected and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(submits) == baseline + expected
        release.set()
        regen_worker.join(15)
        assert not regen_worker.is_alive()
        regen_clips = {
            item["id"]: item for item in regen_holder["result"]["payload"]["clips"]
        }
        for seg_id in ids:
            assert regen_clips[seg_id]["status"] == "done"
            assert len(regen_clips[seg_id]["versions"]) >= 2

    runtime.stop()

def test_auto_wait_keeps_event_loop_responsive(tmp_path):
    data = _pool_data()
    revision = compute_revision(data)
    repo = VideoDispatchRepo()
    _qualify(repo, data)
    service = DispatchService(
        pool_loader=lambda: (data, revision),
        repo=repo,
        boot_id="boot-auto",
        clock=time.time,
        sleep=lambda _seconds: None,
        download_attempts=2,
        max_safe_retries=1,
        blocked_models_provider=lambda data: set(),
    )
    release = threading.Event()
    submits: List[str] = []
    runtime = DispatchRuntime(
        service, lambda instance, job: FakeTransport(release, submits), interval=0.05
    )
    runtime.start()
    set_dispatch_runtime(runtime)
    agent = VideoDirectorAgent()
    agent.execution_root = str(tmp_path / "exec")
    input_data = _input(tmp_path, 1, f"s-{uuid.uuid4().hex[:8]}")
    watchdog = threading.Timer(2.0, release.set)

    async def scenario():
        task = asyncio.create_task(agent.process(input_data))
        ticks = 0
        while not task.done() and ticks < 300:
            await asyncio.sleep(0.01)
            ticks += 1
            if submits and ticks >= 10:
                release.set()
        return ticks, await task

    watchdog.start()
    try:
        ticks, result = asyncio.run(scenario())
        assert ticks >= 10
        assert result["payload"]["clips"][0]["status"] == "done"
    finally:
        watchdog.cancel()
        release.set()
        runtime.stop()

def test_auto_unknown_submit_returns_failure_and_keeps_slot(tmp_path):
    data = _pool_data()
    revision = compute_revision(data)
    repo = VideoDispatchRepo()
    _qualify(repo, data)
    service = DispatchService(
        pool_loader=lambda: (data, revision),
        repo=repo,
        boot_id="boot-auto",
        clock=time.time,
        sleep=lambda _seconds: None,
        download_attempts=2,
        max_safe_retries=1,
        blocked_models_provider=lambda data: set(),
    )
    submits: List[str] = []

    class UnknownTransport(FakeTransport):
        def submit(self, *, job, instance, grant) -> SubmitOutcome:
            self.submits.append(str(job["job_id"]))
            return SubmitOutcome("unknown", detail={"reason": "read timeout"})

    runtime = DispatchRuntime(
        service, lambda instance, job: UnknownTransport(threading.Event(), submits), interval=0.05
    )
    runtime.start()
    set_dispatch_runtime(runtime)
    agent = VideoDirectorAgent()
    agent.execution_root = str(tmp_path / "exec")
    try:
        result = asyncio.run(
            asyncio.wait_for(
                agent.process(_input(tmp_path, 1, f"s-{uuid.uuid4().hex[:8]}")),
                timeout=5,
            )
        )
    finally:
        runtime.stop()
    clip = result["payload"]["clips"][0]
    assert clip["status"] == "failed"
    assert clip["error_type"] == "dispatch_unknown"
    assert len(submits) == 1
    assert "gpu-1" in repo.quarantined_resource_keys()
    assert repo.count_queued() == 0

def test_managed_bridge_shares_slot_with_workflow(tmp_path, monkeypatch):
    data = _pool_data()
    data["video_profiles"] = [_profile(["i1"])]
    data["video_instances"] = [_instance("i1", "gpu-1")]
    revision = compute_revision(data)
    repo = VideoDispatchRepo()
    _qualify(repo, data)
    service = DispatchService(
        pool_loader=lambda: (data, revision),
        repo=repo,
        boot_id="boot-auto",
        clock=time.time,
        sleep=lambda _seconds: None,
        download_attempts=1,
        max_safe_retries=1,
        blocked_models_provider=lambda data: set(),
    )
    release = threading.Event()
    submits: List[str] = []
    runtime = DispatchRuntime(
        service, lambda instance, job: FakeTransport(release, submits), interval=0.05
    )
    runtime.start()
    set_dispatch_runtime(runtime)
    monkeypatch.setattr(
        "core.video_managed_bridge.resolve_managed_instance",
        lambda model: {"instance_id": "i1", "model_id": model},
    )
    results: Dict[str, Any] = {}

    def call(name: str) -> None:
        results[name] = managed_video_sync(
            model="model-i1",
            prompt=name,
            save_path=str(tmp_path / f"{name}.mp4"),
            owner="alice",
            session_id=name,
            execution_id=name,
            mode="first_frame",
            image_path=str(tmp_path / "input.png"),
        )

    first = threading.Thread(target=call, args=("first",), daemon=True)
    second = threading.Thread(target=call, args=("second",), daemon=True)
    try:
        first.start()
        second.start()
        deadline = time.monotonic() + 20
        while len(submits) < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        assert len(submits) == 1
        assert repo.count_queued() == 1
        release.set()
        first.join(10)
        second.join(10)
    finally:
        release.set()
        runtime.stop()
    assert results["first"]["save_path"].endswith("first.mp4")
    assert results["second"]["save_path"].endswith("second.mp4")
    assert len(submits) == 2


def test_managed_bridge_rejects_disabled_dispatch(tmp_path, monkeypatch):
    data = _pool_data()
    data["video_profiles"] = [_profile(["i1"])]
    data["video_instances"] = [_instance("i1", "gpu-1")]
    service = DispatchService(
        pool_loader=lambda: (data, compute_revision(data)),
        repo=VideoDispatchRepo(),
        boot_id="boot-auto",
        dispatch_enabled=lambda: False,
        blocked_models_provider=lambda data: set(),
    )
    runtime = DispatchRuntime(service, lambda instance, job: FakeTransport(threading.Event(), []))
    runtime.start()
    set_dispatch_runtime(runtime)
    monkeypatch.setattr(
        "core.video_managed_bridge.resolve_managed_instance",
        lambda model: {"instance_id": "i1", "model_id": model},
    )
    try:
        with pytest.raises(ManagedBridgeError) as exc:
            managed_video_sync(
                model="model-i1",
                prompt="p",
                save_path=str(tmp_path / "out.mp4"),
                owner="alice",
                session_id="s",
                execution_id="e",
                mode="first_frame",
            )
    finally:
        runtime.stop()
    assert exc.value.error_code == "dispatch_disabled"

def test_auto_stop_returns_immediately_and_keeps_inflight_slot(tmp_path):
    data = _pool_data()
    data["video_profiles"] = [_profile(["i1"])]
    data["video_instances"] = [_instance("i1", "gpu-1")]
    revision = compute_revision(data)
    repo = VideoDispatchRepo()
    _qualify(repo, data)
    service = DispatchService(
        pool_loader=lambda: (data, revision),
        repo=repo,
        boot_id="boot-auto",
        clock=time.time,
        sleep=lambda _seconds: None,
        download_attempts=1,
        max_safe_retries=1,
        blocked_models_provider=lambda data: set(),
    )
    release = threading.Event()
    submits: List[str] = []
    runtime = DispatchRuntime(
        service, lambda instance, job: FakeTransport(release, submits), interval=0.05
    )
    runtime.start()
    set_dispatch_runtime(runtime)
    agent = VideoDirectorAgent()
    agent.execution_root = str(tmp_path / "exec")
    stopped = threading.Event()
    agent.set_cancellation_check(lambda: stopped.is_set())
    result_holder: Dict[str, Any] = {}

    def run() -> None:
        result_holder["result"] = asyncio.run(
            agent.process(_input(tmp_path, 1, f"s-{uuid.uuid4().hex[:8]}"))
        )

    worker = threading.Thread(target=run, daemon=True)
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while len(submits) < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        stopped.set()
        worker.join(5)
        inflight_at_stop = repo.count_inflight()
    finally:
        release.set()
        runtime.stop()
    assert not worker.is_alive()
    assert result_holder["result"]["payload"]["clips"][0]["status"] == "failed"
    assert len(submits) == 1
    assert inflight_at_stop == 1  # 停止不释放仍可能计算的槽

def test_auto_queue_full_race_returns_structured_clip_failure(tmp_path, monkeypatch):
    from core.video_dispatch_service import DispatchUnavailable

    data = _pool_data()
    data["video_profiles"] = [_profile(["i1"])]
    data["video_instances"] = [_instance("i1", "gpu-1")]
    revision = compute_revision(data)
    repo = VideoDispatchRepo()
    _qualify(repo, data)
    service = DispatchService(
        pool_loader=lambda: (data, revision),
        repo=repo,
        boot_id="boot-auto",
        clock=time.time,
        sleep=lambda _seconds: None,
        blocked_models_provider=lambda data: set(),
    )
    runtime = DispatchRuntime(
        service, lambda instance, job: FakeTransport(threading.Event(), []), interval=0.05
    )
    set_dispatch_runtime(runtime)

    def full(**kwargs):
        raise DispatchUnavailable("队列已满", error_code="queue_full")

    monkeypatch.setattr(runtime.generation, "submit", full)
    agent = VideoDirectorAgent()
    agent.execution_root = str(tmp_path / "exec")
    try:
        result = asyncio.run(
            agent.process(_input(tmp_path, 1, f"s-{uuid.uuid4().hex[:8]}"))
        )
    finally:
        runtime.stop()
    clip = result["payload"]["clips"][0]
    assert clip["status"] == "failed"
    assert clip["error_type"] == "queue_full"
    assert repo.count_queued() == 0

def test_next_clip_version_does_not_overwrite_previous_output(tmp_path):
    agent = VideoDirectorAgent()
    agent.execution_root = str(tmp_path / "exec")
    sid = f"s-{uuid.uuid4().hex[:8]}"
    first = agent._next_version_path(sid, "seg-1")
    os.makedirs(os.path.dirname(first), exist_ok=True)
    with open(first, "wb") as handle:
        handle.write(b"first")
    second = agent._next_version_path(sid, "seg-1")
    assert first != second
    assert not os.path.exists(second)
    with open(second, "wb") as handle:
        handle.write(b"second")
    assert open(first, "rb").read() == b"first"
    assert open(second, "rb").read() == b"second"
