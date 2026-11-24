"""5.4 真实诊断 runner：必须经统一调度入队，不直接调用 transport。"""
from __future__ import annotations

import os
from types import SimpleNamespace

from config import Config
from core import video_qualification_runner as runner_module


class _FakeService:
    def __init__(self, *, succeed: bool = True) -> None:
        self.job = None
        self.succeed = succeed
        self.cancelled = None

    def enqueue_batch(self, **kwargs):
        self.job = kwargs["jobs"][0]
        return SimpleNamespace(jobs=[{"job_id": self.job["request_key"]}])

    def get_job(self, job_id):
        assert self.job is not None
        if not self.succeed:
            return {"job_id": job_id, "state": "queued"}
        save_path = self.job["snapshot_json"]["request"]["save_path"]
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as handle:
            handle.write(b"video")
        return {"job_id": job_id, "state": "succeeded"}

    def cancel_job(self, job_id, **kwargs):
        self.cancelled = job_id
        return {"state": "cancelled"}


def test_qualification_runner_enqueues_diagnostic_job(monkeypatch, tmp_path):
    service = _FakeService()
    monkeypatch.setattr(
        runner_module,
        "get_dispatch_runtime",
        lambda: SimpleNamespace(service=service, transport_factory=None),
    )
    monkeypatch.setattr(
        runner_module,
        "resolve_model_entry",
        lambda model_id: ("custom", {"capabilities": {"tasks": ["fl2va"]}}),
    )
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path))
    snapshot = SimpleNamespace(
        data={
            "video_profiles": [
                {
                    "id": "p1",
                    "enabled": True,
                    "instances": ["i1"],
                    "revision": 7,
                    "contracts": {"first_frame": {}},
                }
            ]
        }
    )
    result = runner_module.build_qualification_test_runner()(
        {
            "instance_id": "i1",
            "model_id": "m1",
            "resource_key": "gpu-1",
            "access_mode": "exclusive",
            "enabled": True,
            "driver": "openai",
            "probe": {},
        },
        snapshot,
    )
    assert result["generate_test"] is True
    assert result["completed"] is True
    assert result["mode"] == "first_frame"
    assert service.job["profile_id"] == "p1"
    assert service.job["profile_revision"] == 7
    assert service.job["snapshot_json"]["diagnostic"] is True
    assert service.job["snapshot_json"]["members"] == ["i1"]


def test_qualification_runner_timeout_requests_cancel(monkeypatch, tmp_path):
    service = _FakeService(succeed=False)
    monkeypatch.setattr(
        runner_module,
        "get_dispatch_runtime",
        lambda: SimpleNamespace(service=service, transport_factory=None),
    )
    monkeypatch.setattr(
        runner_module,
        "resolve_model_entry",
        lambda model_id: ("custom", {"capabilities": {"tasks": ["fl2va"]}}),
    )
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path))
    snapshot = SimpleNamespace(
        data={
            "video_profiles": [
                {
                    "id": "p1",
                    "enabled": True,
                    "instances": ["i1"],
                    "revision": 1,
                    "contracts": {"first_frame": {}},
                }
            ]
        }
    )
    result = runner_module.build_qualification_test_runner(
        timeout_seconds=0.001, poll_interval=0.001
    )(
        {
            "instance_id": "i1",
            "model_id": "m1",
            "resource_key": "gpu-1",
            "access_mode": "exclusive",
            "enabled": True,
            "driver": "openai",
            "probe": {},
        },
        snapshot,
    )
    assert result["pending"] is True
    assert result["generate_test"] is False
    assert result["cancel_requested"] is True
    assert service.cancelled == result["job_id"]


def test_profile_selection_skips_incompatible_first_pool(monkeypatch, tmp_path):
    service = _FakeService()
    monkeypatch.setattr(
        runner_module,
        "get_dispatch_runtime",
        lambda: SimpleNamespace(service=service, transport_factory=None),
    )
    monkeypatch.setattr(
        runner_module,
        "resolve_model_entry",
        lambda model_id: ("custom", {"capabilities": {"tasks": ["fl2va"]}}),
    )
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path))
    snapshot = SimpleNamespace(
        data={
            "video_profiles": [
                {
                    "id": "wrong",
                    "enabled": True,
                    "instances": ["i1"],
                    "revision": 1,
                    "contracts": {"t2va": {}},
                },
                {
                    "id": "right",
                    "enabled": True,
                    "instances": ["i1"],
                    "revision": 2,
                    "contracts": {"first_frame": {}},
                },
            ]
        }
    )
    result = runner_module.build_qualification_test_runner()(
        {
            "instance_id": "i1",
            "model_id": "m1",
            "resource_key": "gpu-1",
            "access_mode": "exclusive",
            "enabled": True,
            "driver": "openai",
            "probe": {},
        },
        snapshot,
    )
    assert result["mode"] == "first_frame"
    assert service.job["profile_id"] == "right"


def test_mode_for_requires_pool_contract_not_only_model_capability():
    meta = {"capabilities": {"tasks": ["t2va", "fl2va"]}}
    assert runner_module._mode_for(meta, {"first_frame": {}}) == "first_frame"
    assert runner_module._mode_for({"capabilities": {"tasks": ["t2va"]}}, {"first_frame": {}}) is None
