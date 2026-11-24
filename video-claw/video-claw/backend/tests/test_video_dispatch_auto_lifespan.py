"""4.11 + 4.6：应用 lifespan 创建的调度运行时直接驱动 video_agent auto 路径。"""
from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from api.app import app
from config import settings
from core.agents.video_agent import VideoDirectorAgent
from core.video_dispatch_runtime import get_dispatch_runtime
from core.video_dispatch_service import PollOutcome, SubmitOutcome
from core.video_pool_config import compute_revision
from core.video_qualification import build_qualification_evidence
from db.repositories.video_dispatch import VideoDispatchRepo


class FakeTransport:
    submits: List[str] = []

    def submit(self, *, job, instance, grant) -> SubmitOutcome:
        self.submits.append(str(job["job_id"]))
        return SubmitOutcome("accepted", remote_job_id=f"remote-{job['job_id']}")

    def poll(self, *, attempt, instance, job) -> PollOutcome:
        return PollOutcome("success")

    def download(self, *, attempt, instance, job) -> Any:
        save_path = job["snapshot_json"]["request"]["save_path"]
        with open(save_path, "wb") as handle:
            handle.write(b"video")
        return {"save_path": save_path}

    def cancel(self, *, attempt, instance, job) -> bool:
        return True


def test_auto_uses_lifespan_runtime(tmp_path, monkeypatch):
    image = tmp_path / "input.png"
    image.write_bytes(b"png")
    data = {
        "video_dispatch": {
            "default_mode": "auto",
            "default_profiles": {"first_frame": "p1"},
            "queue_timeout_seconds": 60,
            "max_queued_jobs": 10,
            "max_running_jobs": 2,
        },
        "video_profiles": [
            {
                "id": "p1",
                "name": "p1",
                "revision": 1,
                "instances": ["i1"],
                "enabled": True,
                "max_concurrency_per_execution": 1,
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
        ],
        "video_instances": [
            {
                "instance_id": "i1",
                "model_id": "model-i1",
                "resource_key": "gpu-1",
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
        ],
    }
    revision = compute_revision(data)
    monkeypatch.setattr(
        "core.video_dispatch_bootstrap.load_pool_config",
        lambda path, providers=None, models=None: SimpleNamespace(
            data=data, revision=revision
        ),
    )
    monkeypatch.setattr(
        "core.video_dispatch_bootstrap.build_custom_video_transport",
        lambda instance, job: FakeTransport(),
    )
    FakeTransport.submits = []

    segment_id = "seg-1"
    input_data: Dict[str, Any] = {
        "session_id": f"s-{uuid.uuid4().hex[:8]}",
        "_session_owner": "owner-auto",
        "_session_meta": {"video_dispatch_mode": "auto"},
        "_session_artifacts": {
            "storyboard": {
                "episodes": [
                    {
                        "episode_number": 1,
                        "segments": [
                            {
                                "segment_id": segment_id,
                                "episode_number": 1,
                                "segment_number": 1,
                                "total_duration": 5,
                                "characters": [],
                                "location": "",
                                "description": "segment",
                                "shots": [{"shot_number": 1, "description": "shot"}],
                            }
                        ],
                    }
                ]
            },
            "reference_generation": {
                "scenes": [
                    {
                        "id": segment_id,
                        "selected": str(image),
                        "versions": [str(image)],
                    }
                ]
            },
            "character_design": {"characters": [], "settings": []},
            "video_generation": {"clips": []},
        },
        "video_dispatch_mode": "auto",
        "video_generation_mode": "first_frame",
        "video_ratio": "16:9",
        "video_resolution": "720P",
        "enable_concurrency": True,
    }

    with TestClient(app):
        runtime = get_dispatch_runtime()
        assert runtime is not None
        repo = VideoDispatchRepo()
        evidence = build_qualification_evidence(
            instance_id="i1",
            resource_key="gpu-1",
            config_revision=revision,
            boot_id=str(settings.BOOT_ID),
            checked_by="admin",
            checks={"probe_ready": True},
            confirmed=True,
            checked_at=time.time(),
        )
        repo.save_qualification(
            instance_id="i1", resource_key="gpu-1", evidence=evidence.to_dict()
        )

        agent = VideoDirectorAgent()
        agent.execution_root = str(tmp_path / "exec")
        payload = asyncio.run(agent.process(input_data))

    assert payload["payload"]["clips"][0]["status"] == "done"
    assert len(FakeTransport.submits) == 1
    assert get_dispatch_runtime() is None
