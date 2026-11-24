"""受管生成授权：伪造、过期、目标不匹配均拒绝。"""
from __future__ import annotations

import dataclasses

import pytest

from core.video_generation_service import (
    BatchHandle,
    DispatchGrant,
    GrantError,
    GrantService,
    VideoGenerationService,
)


def _grant(service: GrantService, now: float = 100.0):
    return service.issue(
        job_id="job-1",
        attempt_id="attempt-1",
        instance_id="sglang-01",
        resource_key="gpu-01",
        ttl_seconds=3,
        now=now,
    )


def test_valid_grant_is_accepted():
    service = GrantService(b"secret")
    grant = _grant(service)
    service.require(
        grant,
        job_id="job-1",
        instance_id="sglang-01",
        resource_key="gpu-01",
        now=101,
    )


def test_forged_tampered_or_expired_grant_is_rejected():
    service = GrantService(b"secret")
    grant = _grant(service)
    with pytest.raises(GrantError):
        service.require(None, job_id="job-1", instance_id="sglang-01", resource_key="gpu-01")
    with pytest.raises(GrantError):
        service.require(
            dataclasses.replace(grant, resource_key="gpu-02"),
            job_id="job-1",
            instance_id="sglang-01",
            resource_key="gpu-02",
            now=101,
        )
    with pytest.raises(GrantError):
        service.require(
            grant,
            job_id="job-2",
            instance_id="sglang-01",
            resource_key="gpu-01",
            now=101,
        )
    with pytest.raises(GrantError):
        service.require(
            grant,
            job_id="job-1",
            instance_id="sglang-01",
            resource_key="gpu-01",
            now=200,
        )


def test_user_constructed_dataclass_cannot_forge_signature():
    service = GrantService(b"secret")
    forged = DispatchGrant(
        job_id="job-1",
        attempt_id="attempt-1",
        instance_id="sglang-01",
        resource_key="gpu-01",
        expires_at=200,
        nonce="no",
        signature="0" * 64,
    )
    with pytest.raises(GrantError):
        service.require(
            forged,
            job_id="job-1",
            instance_id="sglang-01",
            resource_key="gpu-01",
            now=100,
        )

def test_wait_unknown_returns_failed_without_releasing_slot():
    class Dispatch:
        def get_job(self, job_id):
            return {
                "job_id": job_id,
                "state": "unknown",
                "error_json": {"error_code": "dispatch_unknown"},
            }

        def cancel_job(self, job_id):
            return {}

    handle = BatchHandle(batch_id="b", owner="u", job_ids={"seg": "job-1"})
    results = VideoGenerationService(Dispatch(), poll_interval=0.01).wait(
        handle, timeout=1
    )
    assert results["seg"]["state"] == "failed"
    assert results["seg"]["error"]["error_code"] == "dispatch_unknown"


def test_wait_timeout_reports_needs_reconciliation():
    class Dispatch:
        def get_job(self, job_id):
            return {"job_id": job_id, "state": "reconciling"}

        def cancel_job(self, job_id):
            return {}

    handle = BatchHandle(batch_id="b", owner="u", job_ids={"seg": "job-1"})
    results = VideoGenerationService(Dispatch(), poll_interval=0.01).wait(
        handle, timeout=0.05
    )
    assert results["seg"]["state"] == "failed"
    assert results["seg"]["error"]["error_code"] == "needs_reconciliation"


def test_wait_returns_published_canonical_from_job_result():
    class Dispatch:
        def get_job(self, job_id):
            return {
                "job_id": job_id,
                "state": "succeeded",
                "result_json": {
                    "download": {"save_path": "/work/out.mp4"},
                    "published": {"published": [{"public_path": "_media/out.mp4"}]},
                },
            }

        def cancel_job(self, job_id):
            return {}

    handle = BatchHandle(batch_id="b", owner="u", job_ids={"seg": "job-1"})
    events = []
    results = VideoGenerationService(Dispatch(), poll_interval=0.01).wait(
        handle, on_event=events.append, timeout=1
    )
    assert results["seg"]["save_path"] == "/work/out.mp4"
    assert results["seg"]["published"]["published"][0]["public_path"] == "_media/out.mp4"
    assert results["seg"]["published_paths"] == ["_media/out.mp4"]
    assert events[-1]["published"]["published"][0]["public_path"] == "_media/out.mp4"
    assert events[-1]["published_paths"] == ["_media/out.mp4"]
