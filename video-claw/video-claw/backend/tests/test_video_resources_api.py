"""资源池查询/保存 API、管理员资格/async 影响与 owner 作业可见性。"""
from __future__ import annotations

import copy
import json
import time
import uuid

from config import Config, settings
from core.identity import owner_key as make_owner_key
from core.video_async_impact import ASYNC_CONFIRM_KEY
from core.video_pool_config import load_pool_config, write_pool_config
from core.video_qualification import build_qualification_evidence
from db import database
from db.repositories.video_dispatch import VideoDispatchRepo


def _pool():
    contract = {
        "task": "fl2va",
        "resolutions": ["768P"],
        "short_edge": 768,
        "ratios": ["16:9"],
        "duration": {"min": 4, "max": 15},
        "fps": [24],
    }
    return {
        "video_dispatch": {
            "default_mode": "auto",
            "default_profiles": {},
            "queue_timeout_seconds": 3600,
            "max_queued_jobs": 1000,
            "max_running_jobs": 8,
        },
        "video_profiles": [
            {"id": "pool", "instances": ["sglang-01"], "contracts": {"first_frame": contract}}
        ],
        "video_instances": [
            {
                "instance_id": "sglang-01",
                "model_id": "pool-model",
                "resource_key": "gpu-01",
                "access_mode": "exclusive",
                "enabled": True,
                "driver": "sglang_async",
                "probe": {
                    "kind": "sglang_diffusion",
                    "jobs_url": "http://video-01.internal:30010/v1/videos",
                    "metrics_url": "http://video-01.internal:30010/metrics",
                },
            }
        ],
    }


def _install(monkeypatch, tmp_path):
    path = tmp_path / "video_profiles.yaml"
    write_pool_config(path, _pool())
    monkeypatch.setattr(settings, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(Config, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(settings, "VIDEO_DISPATCH_ENABLED", True)
    monkeypatch.setattr(Config, "VIDEO_DISPATCH_ENABLED", True)
    monkeypatch.setattr(
        Config,
        "CONFIG",
        {
            **Config.CONFIG,
            "api_providers": {"p": {"protocol": "sglang", "base_url": "http://video-01.internal"}},
            "custom_models": [{"id": "pool-model", "provider": "p", "types": ["video"]}],
        },
    )
    return path


def test_profiles_and_summary_are_read_only(client, monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    headers = {"X-User-Login-Name": "owner"}
    response = client.get("/api/video/profiles", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["dispatch_enabled"] is True
    assert payload["profiles"][0]["state"] == "not_ready"
    assert payload["profiles"][0]["missing"]

    summary = client.get("/api/video/resources/summary", headers=headers)
    assert summary.status_code == 200
    assert summary.json()["profiles"]["not_ready"] == 1


def test_only_admin_can_save_and_revision_is_required(client, monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    values = _pool()
    revision_response = client.get(
        "/api/video/profiles", headers={"X-User-Login-Name": "owner"}
    )
    revision = revision_response.json()["config_revision"]

    denied = client.put(
        "/api/video/profiles",
        json={"values": values, "expected_revision": revision},
        headers={"X-User-Login-Name": "owner"},
    )
    assert denied.status_code == 403

    no_revision = client.put(
        "/api/video/profiles",
        json={"values": values},
        headers={"X-User-Login-Name": "admin"},
    )
    assert no_revision.status_code == 400

    updated = copy.deepcopy(values)
    updated["video_dispatch"]["queue_timeout_seconds"] = 7200
    saved = client.put(
        "/api/video/profiles",
        json={"values": updated, "expected_revision": revision},
        headers={"X-User-Login-Name": "admin"},
    )
    assert saved.status_code == 200
    assert saved.json()["saved"] is True

    stale = client.put(
        "/api/video/profiles",
        json={"values": values, "expected_revision": revision},
        headers={"X-User-Login-Name": "admin"},
    )
    assert stale.status_code == 409


def _vllm_pool():
    data = _pool()
    data["video_instances"][0].update(
        {
            "model_id": "vllm-model",
            "probe": {
                "kind": "vllm_omni",
                "jobs_url": "http://video-01.internal:8000/v1/videos",
                "metrics_url": "",
                "labels": {},
            },
        }
    )
    return data


def _install_vllm(monkeypatch, tmp_path):
    path = tmp_path / "video_profiles.yaml"
    write_pool_config(path, _vllm_pool())
    monkeypatch.setattr(settings, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(Config, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(Config, "VIDEO_DISPATCH_ENABLED", True)
    monkeypatch.setattr(
        Config,
        "CONFIG",
        {
            **Config.CONFIG,
            "api_providers": {
                "p": {"protocol": "vllm-omni", "base_url": "http://video-01.internal"}
            },
            "custom_models": [{"id": "vllm-model", "provider": "p", "types": ["video"]}],
        },
    )
    return path


def _save_evidence(path, instance_id: str = "sglang-01", resource_key: str = "gpu-01"):
    snapshot = load_pool_config(path)
    evidence = build_qualification_evidence(
        instance_id=instance_id,
        resource_key=resource_key,
        config_revision=snapshot.revision,
        boot_id=str(Config.BOOT_ID),
        checked_by="admin",
        checks={"config": True},
        confirmed=True,
        checked_at=time.time(),
    )
    VideoDispatchRepo().save_qualification(
        instance_id=instance_id,
        resource_key=resource_key,
        evidence=evidence.to_dict(),
    )
    return evidence


def _clear_resource_state():
    with database.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM video_resource_slots")
                cur.execute("DELETE FROM schema_meta WHERE key = %s", (ASYNC_CONFIRM_KEY,))


def test_async_impact_confirm_gates_pool_readiness(client, monkeypatch, tmp_path):
    path = _install_vllm(monkeypatch, tmp_path)
    _save_evidence(path)
    try:
        owner = {"X-User-Login-Name": "owner"}
        admin = {"X-User-Login-Name": "admin"}
        assert client.get("/api/video/models/async-impact", headers=owner).status_code == 403

        impact = client.get("/api/video/models/async-impact", headers=admin)
        assert impact.status_code == 200
        items = impact.json()["items"]
        assert [item["model_id"] for item in items] == ["vllm-model"]
        assert items[0]["confirmed"] is False

        before = client.get("/api/video/profiles", headers=owner).json()
        assert before["profiles"][0]["state"] == "not_ready"

        confirmed = client.post(
            "/api/video/models/async-impact/confirm",
            json={"model_ids": ["vllm-model"]},
            headers=admin,
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["items"][0]["confirmed"] is True

        after = client.get("/api/video/profiles", headers=owner).json()
        assert after["profiles"][0]["state"] == "ready"
    finally:
        _clear_resource_state()


def test_qualify_rejects_unconfirmed_async_model(client, monkeypatch, tmp_path):
    path = _install_vllm(monkeypatch, tmp_path)
    admin = {"X-User-Login-Name": "admin"}
    try:
        pending = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"config": True}, "confirmed": True},
            headers=admin,
        )
        assert pending.status_code == 400
        assert pending.json()["detail"]["error_code"] == "async_model_unconfirmed"

        client.post(
            "/api/video/models/async-impact/confirm",
            json={"model_ids": ["vllm-model"]},
            headers=admin,
        )
        ok = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"config": True}, "confirmed": True},
            headers=admin,
        )
        assert ok.status_code == 200, ok.text
    finally:
        _clear_resource_state()


def test_qualify_route_permissions_placeholder_and_generate_test(
    client, monkeypatch, tmp_path
):
    from api.routers.video_resources import configure_qualification_test_runner

    path = _install(monkeypatch, tmp_path)
    owner = {"X-User-Login-Name": "owner"}
    admin = {"X-User-Login-Name": "admin"}
    try:
        assert client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"config": True}, "confirmed": True},
            headers=owner,
        ).status_code == 403
        invalid = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"anything": True}, "confirmed": True},
            headers=admin,
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["error_code"] == "invalid_check"

        placeholder = _pool()
        placeholder["video_instances"][0].update(
            {
                "enabled": False,
                "resource_key": "example-gpu-01",
                "probe": {
                    "kind": "vllm_omni",
                    "jobs_url": "http://video.example/v1/videos",
                    "metrics_url": "",
                },
            }
        )
        write_pool_config(path, placeholder)
        called: list[int] = []
        configure_qualification_test_runner(
            lambda instance, snapshot: called.append(1)
            or {"generate_test": True, "completed": True}
        )
        blocked = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"config": True}, "confirmed": True, "generate_test": True},
            headers=admin,
        )
        assert blocked.status_code == 400
        assert blocked.json()["detail"]["error_code"] == "placeholder_instance"
        assert called == []

        write_pool_config(path, _pool())
        configure_qualification_test_runner(None)
        self_reported = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"generate_test": True}, "confirmed": True},
            headers=admin,
        )
        assert self_reported.status_code == 400
        assert self_reported.json()["detail"]["error_code"] == "invalid_check"

        configure_qualification_test_runner(
            lambda instance, snapshot: {"generate_submitted": True}
        )
        incomplete = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"config": True}, "confirmed": True, "generate_test": True},
            headers=admin,
        )
        assert incomplete.status_code == 409
        assert incomplete.json()["detail"]["error_code"] == "generate_test_incomplete"

        configure_qualification_test_runner(
            lambda instance, snapshot: {
                "generate_test": True,
                "completed": True,
                "pending": "1",
            }
        )
        pending = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"config": True}, "confirmed": True, "generate_test": True},
            headers=admin,
        )
        assert pending.status_code == 409
        assert pending.json()["detail"]["error_code"] == "generate_test_incomplete"

        configure_qualification_test_runner(
            lambda instance, snapshot: {"generate_test": True, "completed": False}
        )
        unfinished = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"config": True}, "confirmed": True, "generate_test": True},
            headers=admin,
        )
        assert unfinished.status_code == 409
        assert unfinished.json()["detail"]["error_code"] == "generate_test_incomplete"

        configure_qualification_test_runner(None)
        no_runner = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"config": True}, "confirmed": True, "generate_test": True},
            headers=admin,
        )
        assert no_runner.status_code == 400
        assert no_runner.json()["detail"]["error_code"] == "generate_test_not_available"

        ok = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"config": True}, "confirmed": True},
            headers=admin,
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["evidence"]["checks"] == {"config": True}

        called2: list[int] = []
        configure_qualification_test_runner(
            lambda instance, snapshot: called2.append(1)
            or {"generate_test": True, "completed": True}
        )
        tested = client.post(
            "/api/video/instances/sglang-01/qualify",
            json={"checks": {"config": True}, "confirmed": True, "generate_test": True},
            headers=admin,
        )
        assert tested.status_code == 200, tested.text
        assert called2 == [1]
        assert tested.json()["evidence"]["checks"]["generate_test"] is True

        snapshot = load_pool_config(path)
        instance = next(
            row
            for row in snapshot.data["video_instances"]
            if row["instance_id"] == "sglang-01"
        )
        assert instance["enabled"] is True  # 资格检查不自动启用/停用
    finally:
        configure_qualification_test_runner(None)
        _clear_resource_state()


def test_job_visibility_is_owner_or_admin(client):
    login = f"api-{uuid.uuid4().hex[:8]}"
    owner = make_owner_key(login)
    repo = VideoDispatchRepo()
    job = repo.enqueue_batch(
        owner=owner,
        batch_id="batch",
        jobs=[{
            "request_key": "req",
            "item_id": "item",
            "source_kind": "workflow",
            "dispatch_mode": "auto",
            "snapshot_json": {"prompt": "secret"},
        }],
    ).jobs[0]
    try:
        own = client.get(
            f"/api/video/jobs/{job['job_id']}", headers={"X-User-Login-Name": login}
        )
        assert own.status_code == 200
        assert "prompt" not in own.json()
        assert own.json()["dispatch_phase"] in (
            "queued",
            "waiting_pool_unavailable",
            "unknown",
        )
        other = client.get(
            f"/api/video/jobs/{job['job_id']}", headers={"X-User-Login-Name": "other"}
        )
        assert other.status_code == 404
        admin = client.get(
            f"/api/video/jobs/{job['job_id']}", headers={"X-User-Login-Name": "admin"}
        )
        assert admin.status_code == 200
    finally:
        with database.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM video_resource_slots")
                    cur.execute("DELETE FROM video_dispatch_jobs WHERE owner = %s", (owner,))

def test_admin_can_release_quarantined_slot(client):
    repo = VideoDispatchRepo()
    repo.save_qualification(
        instance_id="i-quarantine",
        resource_key="gpu-quarantine",
        evidence={"checked_at": time.time()},
    )
    assert repo.quarantine_slot(
        resource_key="gpu-quarantine",
        reason="submission_unknown",
        only_with_attempt=False,
    )
    admin = {"X-User-Login-Name": "admin"}
    diagnostics = client.get("/api/video/resources/diagnostics", headers=admin)
    assert diagnostics.status_code == 200
    assert any(
        item["resource_key"] == "gpu-quarantine"
        for item in diagnostics.json()["quarantined"]
    )
    released = client.post(
        "/api/video/resources/quarantine/release",
        json={"resource_key": "gpu-quarantine", "reason": "手工核对完成"},
        headers=admin,
    )
    assert released.status_code == 200, released.text
    assert released.json()["released"] is True
    assert "gpu-quarantine" not in repo.quarantined_resource_keys()

def test_job_cancel_is_owner_scoped(client, monkeypatch):
    from types import SimpleNamespace

    repo = VideoDispatchRepo()
    owner = make_owner_key("alice")
    repo.enqueue_batch(
        owner=owner,
        batch_id="cancel-owner",
        jobs=[
            {
                "request_key": "cancel-owner",
                "item_id": "seg",
                "source_kind": "workflow",
                "execution_id": "exec-cancel",
                "dispatch_mode": "auto",
                "profile_id": "p1",
                "profile_revision": 1,
                "snapshot_json": {"members": ["i1"], "request": {"prompt": "p"}},
            }
        ],
        queue_timeout_seconds=60,
        max_queued_jobs=10,
    )
    job_id = repo.list_queued()[0]["job_id"]
    runtime = SimpleNamespace(
        service=SimpleNamespace(
            cancel_job=lambda job_id, transport_factory=None: {
                "state": "reconciling"
            }
        ),
        transport_factory=lambda instance, job: None,
    )
    monkeypatch.setattr(
        "core.video_dispatch_runtime.get_dispatch_runtime", lambda: runtime
    )
    assert client.post(
        f"/api/video/jobs/{job_id}/cancel", headers={"X-User-Login-Name": "bob"}
    ).status_code == 404
    response = client.post(
        f"/api/video/jobs/{job_id}/cancel", headers={"X-User-Login-Name": "alice"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "reconciling"

def test_stop_drain_resume_routes_use_runtime(client, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def stop_all():
        calls.append("stop")
        return {"stopped_queued": 2, "dispatch_enabled": False}

    def resume_all():
        calls.append("resume")
        return {"dispatch_enabled": True}

    def drain(profile_id):
        calls.append(("drain", profile_id))
        return {"profile_id": profile_id, "draining": True, "stopped_queued": 1}

    runtime = SimpleNamespace(
        service=SimpleNamespace(
            stop_all=stop_all,
            resume_all=resume_all,
            drain_profile=drain,
            undrain_profile=lambda profile_id: {"profile_id": profile_id, "draining": False},
        )
    )
    monkeypatch.setattr(
        "core.video_dispatch_runtime.get_dispatch_runtime", lambda: runtime
    )
    admin = {"X-User-Login-Name": "admin"}
    assert client.post("/api/video/dispatch/stop", headers=admin).json()["dispatch_enabled"] is False
    assert client.post("/api/video/dispatch/resume", headers=admin).json()["dispatch_enabled"] is True
    body = client.post("/api/video/profiles/p1/drain", headers=admin).json()
    assert body["draining"] is True
    assert client.post("/api/video/profiles/p1/undrain", headers=admin).json()["draining"] is False
    assert calls == ["stop", "resume", ("drain", "p1")]

def test_default_recommendation_and_dispatchable_state(client, monkeypatch, tmp_path):
    path = _install(monkeypatch, tmp_path)
    snapshot = load_pool_config(path)
    values = _pool()
    values["video_dispatch"]["default_profiles"] = {"first_frame": "pool"}
    write_pool_config(path, values, expected_revision=snapshot.revision)
    owner = {"X-User-Login-Name": "owner"}
    before = client.get("/api/video/profiles", headers=owner).json()
    assert before["has_default_recommendation"] is True
    assert before["recommendation_reason"] == "default_not_ready"
    assert before["profiles"][0]["dispatchable"] is False

    _save_evidence(path)
    after = client.get("/api/video/profiles", headers=owner).json()
    assert after["recommendation_reason"] == "none"
    assert after["profiles"][0]["state"] == "ready"
    assert after["profiles"][0]["dispatchable"] is True

def test_profiles_aggregate_real_probe_observation(client, monkeypatch, tmp_path):
    from types import SimpleNamespace

    path = _install(monkeypatch, tmp_path)
    _save_evidence(path)
    fake_service = SimpleNamespace(
        stopped=False,
        draining_profiles=set(),
        draining_instances=set(),
        probe_service=SimpleNamespace(
            last_observation=lambda instance_id: (
                SimpleNamespace(state="busy", observed_at="2026-01-01T00:00:00Z"),
                time.time(),
            )
        ),
    )
    monkeypatch.setattr(
        "core.video_dispatch_runtime.get_dispatch_runtime",
        lambda: SimpleNamespace(service=fake_service),
    )
    payload = client.get(
        "/api/video/profiles", headers={"X-User-Login-Name": "owner"}
    ).json()
    observation = payload["profiles"][0]["observation"]
    assert observation["state"] == "busy"
    assert observation["coverage"] == "complete"
    assert observation["stale"] is False
