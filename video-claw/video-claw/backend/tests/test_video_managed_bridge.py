"""4.8：受管同步桥通过 DispatchService 入队，不直接调用模型客户端。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.video_dispatch_runtime import set_dispatch_runtime
from core.video_managed_bridge import ManagedBridgeError, managed_video_sync


class FakeGeneration:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.submitted = None

    def submit(self, **kwargs):
        self.submitted = kwargs
        item = kwargs["items"][0]
        return SimpleNamespace(job_ids={item.item_id: "job-bridge"})

    def wait(self, handle, timeout=None):
        item_id = next(iter(handle.job_ids))
        return {
            item_id: {
                "state": "succeeded",
                "save_path": str(self.tmp_path / "out.mp4"),
            }
        }


def test_managed_bridge_enqueues_and_returns_output(tmp_path, monkeypatch):
    fake_generation = FakeGeneration(tmp_path)
    runtime = SimpleNamespace(
        generation=fake_generation,
        service=SimpleNamespace(
            runtime=lambda: SimpleNamespace(
                dispatch_enabled=True,
                profiles={
                    "p1": SimpleNamespace(
                        enabled=True, members=("i1",), contracts={"first_frame": {}}
                    )
                },
                queue_timeout_seconds=60,
                max_queued_jobs=10,
            ),
            resolve_execution_pool=lambda **kwargs: SimpleNamespace(
                error="",
                contract=SimpleNamespace(profile_id="p1"),
                members=("i1",),
            ),
        ),
    )
    monkeypatch.setattr(
        "core.video_managed_bridge.resolve_managed_instance",
        lambda model: {"instance_id": "i1", "model_id": model},
    )
    set_dispatch_runtime(runtime)
    try:
        result = managed_video_sync(
            model="vllm-model",
            prompt="p",
            save_path=str(tmp_path / "out.mp4"),
            owner="alice",
            session_id="sandbox-1",
            execution_id="exec-1",
            mode="first_frame",
            image_path="/tmp/in.png",
            duration=5,
        )
    finally:
        set_dispatch_runtime(None)
    assert result["save_path"].endswith("out.mp4")
    assert result["job_id"] == "job-bridge"
    submitted = fake_generation.submitted
    assert submitted["members"] == ["i1"]
    assert submitted["items"][0].image_path == "/tmp/in.png"
    assert submitted["queue_timeout_seconds"] == 60


def test_managed_bridge_requires_runtime(tmp_path):
    set_dispatch_runtime(None)
    with pytest.raises(ManagedBridgeError) as exc:
        managed_video_sync(
            model="vllm-model",
            prompt="p",
            save_path=str(tmp_path / "out.mp4"),
            owner="alice",
            session_id="s",
            execution_id="e",
            mode="first_frame",
        )
    assert exc.value.error_code == "dispatch_not_started"

def test_explicit_profile_must_contain_instance(tmp_path, monkeypatch):
    runtime = SimpleNamespace(
        generation=FakeGeneration(tmp_path),
        service=SimpleNamespace(
            runtime=lambda: SimpleNamespace(
                dispatch_enabled=True,
                profiles={
                    "p2": SimpleNamespace(
                        enabled=True, members=("other",), contracts={"first_frame": {}}
                    )
                },
                queue_timeout_seconds=60,
                max_queued_jobs=10,
            ),
            resolve_execution_pool=lambda **kwargs: SimpleNamespace(
                error="", contract=SimpleNamespace(profile_id="p2"), members=("other",)
            ),
        ),
    )
    monkeypatch.setattr(
        "core.video_managed_bridge.resolve_managed_instance",
        lambda model: {"instance_id": "i1", "model_id": model},
    )
    set_dispatch_runtime(runtime)
    try:
        with pytest.raises(ManagedBridgeError) as exc:
            managed_video_sync(
                model="vllm-model",
                prompt="p",
                save_path=str(tmp_path / "out.mp4"),
                owner="alice",
                session_id="s",
                execution_id="e",
                mode="first_frame",
                profile_id="p2",
            )
    finally:
        set_dispatch_runtime(None)
    assert exc.value.error_code == "pool_incompatible"
