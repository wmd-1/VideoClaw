"""6.7：Pipeline auto/manual 参数与选池/实例模型解析。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipelines import api_media
from core.video_managed_bridge import ManagedBridgeError


def test_generate_video_api_auto_resolves_pool_and_instance_model(monkeypatch, tmp_path):
    seen = {}

    class Service:
        def resolve_execution_pool(self, **kwargs):
            return SimpleNamespace(
                error="",
                contract=SimpleNamespace(profile_id="p1"),
                members=("i0", "i1"),
            )

        def runtime(self):
            return SimpleNamespace(
                instances={
                    "i0": SimpleNamespace(instance_id="i0", enabled=False, model_id="disabled-model"),
                    "i1": SimpleNamespace(instance_id="i1", enabled=True, model_id="instance-model"),
                }
            )

    monkeypatch.setattr(api_media, "get_dispatch_runtime", lambda: SimpleNamespace(service=Service()))
    monkeypatch.setattr(api_media, "parse_api_workflow", lambda model, kind: ("custom", model))
    monkeypatch.setattr(api_media, "normalize_video_duration", lambda provider, model, duration: duration)
    monkeypatch.setattr(api_media, "resolve_managed_instance", lambda model, instance_id=None: {"instance_id": instance_id})
    recorded = {}
    def fake_managed(**kwargs):
        seen.update(kwargs)
        kwargs["on_job_created"]("job-1")
        return {"job_id": "job-1"}

    monkeypatch.setattr(api_media, "managed_video_sync", fake_managed)
    monkeypatch.setattr(api_media, "record_video_job", lambda task_id, job_id: recorded.update({"task_id": task_id, "job_id": job_id}))
    monkeypatch.setattr(
        api_media, "current_call_context", lambda: {"owner": "alice", "execution_id": "ex-1"}
    )

    result = api_media.generate_video_api(
        prompt="p",
        model="",
        output_path=str(tmp_path / "out.mp4"),
        image_path="/tmp/input.png",
        video_dispatch_mode="auto",
        video_profile_id="",
    )
    assert result == str(tmp_path / "out.mp4")
    assert seen["model"] == "instance-model"
    assert seen["profile_id"] == "p1"
    assert seen["instance_id"] == "i1"
    assert recorded == {"task_id": "ex-1", "job_id": "job-1"}


def test_generate_video_api_manual_requires_model():
    with pytest.raises(ManagedBridgeError) as exc:
        api_media.generate_video_api(
            prompt="p",
            model="",
            output_path="/tmp/out.mp4",
            video_dispatch_mode="manual",
        )
    assert exc.value.error_code == "model_required"


def test_task_view_exposes_structured_pipeline_error():
    from pipelines.storage import task_view

    row = {
        "task_id": "t1", "owner": "alice", "pipeline": "standard", "status": "failed",
        "error": "video_dispatch_error:pool_not_ready:no instance",
    }
    view = task_view(row)
    assert view["error_code"] == "pool_not_ready"


def test_task_persists_all_pipeline_video_jobs():
    from db.repositories.tasks import TaskRepo
    from pipelines.storage import task_view

    repo = TaskRepo()
    assert repo.insert_if_absent({
        "task_id": "task-multi-job",
        "owner": "alice",
        "pipeline": "standard",
        "status": "running",
        "input": {},
        "output": {},
    })
    repo.set_video_job("task-multi-job", "job-a")
    repo.set_video_job("task-multi-job", "job-b")
    row = repo.get("task-multi-job")
    assert row["video_job_ids"] == ["job-a", "job-b"]
    assert task_view(row)["video_job_ids"] == ["job-a", "job-b"]
