"""资格证据：修订/boot 失效、禁用实例显式检查与占位闸门。"""
from __future__ import annotations

import uuid

import pytest

from core.video_qualification import (
    build_qualification_evidence,
    qualification_blockers,
    qualification_is_valid,
)
from db import database
from db.repositories.video_dispatch import VideoDispatchRepo

NOW = 1_000.0


def _evidence(**overrides):
    payload = {
        "instance_id": "sglang-01",
        "resource_key": "gpu-01",
        "config_revision": "rev-1",
        "boot_id": "boot-1",
        "checked_by": "admin",
        "checks": {"jobs": True, "metrics": True},
        "confirmed": True,
        "checked_at": NOW - 10,
    }
    payload.update(overrides)
    return build_qualification_evidence(**payload)


def test_evidence_valid_only_for_same_revision_boot_and_unexpired():
    evidence = _evidence()
    assert qualification_is_valid(
        evidence, config_revision="rev-1", boot_id="boot-1", now=NOW
    ) is True
    assert qualification_is_valid(
        evidence, config_revision="rev-2", boot_id="boot-1", now=NOW
    ) is False
    assert qualification_is_valid(
        evidence, config_revision="rev-1", boot_id="boot-2", now=NOW
    ) is False
    assert qualification_is_valid(
        evidence, config_revision="rev-1", boot_id="boot-1", now=NOW + 90_000
    ) is False
    assert qualification_is_valid(
        None, config_revision="rev-1", boot_id="boot-1", now=NOW
    ) is False


def test_evidence_requires_confirmation_and_all_checks():
    with pytest.raises(ValueError, match="显式确认"):
        _evidence(confirmed=False)
    with pytest.raises(ValueError, match="全部通过"):
        _evidence(checks={"jobs": False})
    with pytest.raises(ValueError, match="全部通过"):
        _evidence(checks={})


def test_placeholder_instance_is_blocked_before_any_probe():
    blockers = qualification_blockers(
        instance_id="sglang-01",
        resource_key="example-gpu-01",
        placeholder_issues=[
            {"field": "video_instances.sglang-01.probe.jobs_url", "code": "example_host", "reason": "保留域名"}
        ],
    )
    codes = {item["code"] for item in blockers}
    assert "example_host" in codes
    assert qualification_blockers(instance_id="sglang-01", resource_key="gpu-01") == []


def test_disabled_instance_evidence_can_be_saved_and_read_back():
    resource_key = f"gpu-{uuid.uuid4().hex[:8]}"
    repo = VideoDispatchRepo()
    evidence = _evidence(resource_key=resource_key)
    try:
        repo.save_qualification(
            instance_id=evidence.instance_id,
            resource_key=resource_key,
            evidence=evidence.to_dict(),
        )
        stored = repo.get_qualification(
            instance_id=evidence.instance_id,
            resource_key=resource_key,
        )
        assert stored is not None
        assert qualification_is_valid(
            stored, config_revision="rev-1", boot_id="boot-1", now=NOW
        ) is True
        assert qualification_is_valid(
            stored, config_revision="rev-x", boot_id="boot-1", now=NOW
        ) is False
    finally:
        with database.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM video_resource_slots WHERE resource_key = %s",
                        (resource_key,),
                    )
