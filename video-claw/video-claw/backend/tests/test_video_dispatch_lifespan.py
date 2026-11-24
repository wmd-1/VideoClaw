"""4.11：应用 lifespan 启动/停止受管调度运行时。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.app import app
from core.video_dispatch_runtime import get_dispatch_runtime
from db.repositories.video_dispatch import VideoDispatchRepo


def test_lifespan_starts_and_stops_dispatch_runtime():
    for _ in range(2):
        with TestClient(app) as client:
            runtime = get_dispatch_runtime()
            assert runtime is not None
            assert runtime._thread is not None and runtime._thread.is_alive()
            from api.routers import video_resources

            assert video_resources._qualification_test_runner is not None
            assert client.get("/api/health").status_code == 200
        assert get_dispatch_runtime() is None
        assert runtime._thread is None

def test_lifespan_recovers_queued_jobs_before_start():
    repo = VideoDispatchRepo()
    batch = "startup-recovery"
    repo.enqueue_batch(
        owner="u1",
        batch_id=batch,
        jobs=[
            {
                "request_key": batch,
                "item_id": "seg-1",
                "source_kind": "workflow",
                "execution_id": "exec-startup",
                "dispatch_mode": "auto",
                "profile_id": "p1",
                "profile_revision": 1,
                "snapshot_json": {
                    "members": ["i1"],
                    "request": {"prompt": "p"},
                },
            }
        ],
        queue_timeout_seconds=3600,
        max_queued_jobs=10,
    )
    job_id = repo.list_queued()[0]["job_id"]

    with TestClient(app):
        job = repo.get_job(job_id)
        assert job is not None
        assert job["state"] == "failed"
        assert (job.get("error_json") or {}).get("error_code") == "interrupted"
