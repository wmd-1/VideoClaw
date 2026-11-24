"""auto 预检不消费具体模型/settings 哨兵；manual 保持原预检。"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import HTTPException

from api.routers.workflow import (
    _preflight_stage_models,
    _require_model_fields,
)
from config import Config, settings
from core.video_async_impact import ASYNC_CONFIRM_KEY
from core.video_pool_config import load_pool_config, write_pool_config
from core.video_qualification import build_qualification_evidence
from db import database
from db.repositories.video_dispatch import VideoDispatchRepo


def _values(**overrides):
    values = {
        "video_dispatch_mode": "auto",
        "video_generation_mode": "first_frame",
        "llm_model": "llm",
        "vlm_model": "vlm",
        "image_t2i_model": "t2i",
        "image_it2i_model": "i2i",
    }
    values.update(overrides)
    return values


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


def _structured(exc: pytest.ExceptionInfo) -> dict:
    detail = exc.value.detail
    assert detail["code"] == "video_dispatch_error"
    for key in (
        "error_code",
        "stage",
        "mode",
        "field",
        "profile_id",
        "execution_id",
        "submission_id",
        "reason",
        "missing",
        "actions",
    ):
        assert key in detail, key
    assert detail["stage"] == "video_generation"
    assert detail["mode"] == "auto"
    assert detail["execution_id"] is None
    return detail


def test_missing_dispatch_mode_defaults_to_auto(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_MODEL", "")
    monkeypatch.setattr(settings, "VIDEO_FIRST_FRAME_MODEL", "")
    values = _values()
    values.pop("video_dispatch_mode")
    # 缺省 auto：不因具体视频模型为空而要求 manual 模型字段。
    _require_model_fields(values)


def test_auto_skips_video_model_requirement_but_manual_keeps_it(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_FIRST_FRAME_MODEL", "")
    monkeypatch.setattr(settings, "VIDEO_MODEL", "")
    _require_model_fields(_values())
    with pytest.raises(HTTPException) as exc:
        _require_model_fields(_values(video_dispatch_mode="manual"))
    assert exc.value.status_code == 400
    assert "video_model" in str(exc.value.detail)


def test_auto_preflight_never_uses_sentinel_model(monkeypatch, tmp_path):
    path = tmp_path / "video_profiles.yaml"
    write_pool_config(path, _pool())
    snapshot = load_pool_config(path)
    evidence = build_qualification_evidence(
        instance_id="sglang-01",
        resource_key="gpu-01",
        config_revision=snapshot.revision,
        boot_id=str(Config.BOOT_ID),
        checked_by="admin",
        checks={"jobs": True},
        confirmed=True,
        checked_at=time.time(),
    )
    repo = VideoDispatchRepo()
    repo.save_qualification(
        instance_id="sglang-01",
        resource_key="gpu-01",
        evidence=evidence.to_dict(),
    )
    monkeypatch.setattr(settings, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(Config, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(settings, "VIDEO_DISPATCH_ENABLED", True)
    monkeypatch.setattr(Config, "VIDEO_DISPATCH_ENABLED", True)
    try:
        # 即使会话/设置里塞了哨兵模型，auto 也只走池配置。
        _preflight_stage_models(
            "video_generation",
            _values(video_first_frame_model="sentinel-model", video_model="sentinel-model"),
        )
    finally:
        with database.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM video_resource_slots WHERE resource_key = 'gpu-01'")


def test_auto_empty_pool_returns_structured_pool_not_ready(monkeypatch, tmp_path):
    path = tmp_path / "video_profiles.yaml"
    write_pool_config(
        path,
        {
            "video_dispatch": {
                "default_mode": "auto",
                "default_profiles": {},
                "queue_timeout_seconds": 3600,
                "max_queued_jobs": 1000,
                "max_running_jobs": 8,
            },
            "video_profiles": [],
            "video_instances": [],
        },
    )
    monkeypatch.setattr(settings, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(settings, "VIDEO_DISPATCH_ENABLED", True)
    with pytest.raises(HTTPException) as exc:
        _preflight_stage_models("video_generation", _values())
    assert exc.value.status_code == 409
    detail = _structured(exc)
    assert detail["error_code"] == "pool_not_ready"
    assert detail["missing"][0]["field"] == "video_instances"
    assert "configure_pool" in detail["actions"]
    assert "use_manual_once" in detail["actions"]


def test_auto_dispatch_disabled_is_structured(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_DISPATCH_ENABLED", False)
    with pytest.raises(HTTPException) as exc:
        _preflight_stage_models("video_generation", _values())
    assert exc.value.status_code == 409
    detail = _structured(exc)
    assert detail["error_code"] == "dispatch_disabled"
    assert detail["field"] == "VC_VIDEO_DISPATCH__ENABLED"


def test_explicit_pool_without_qualified_member_is_selected_not_ready(monkeypatch, tmp_path):
    path = tmp_path / "video_profiles.yaml"
    write_pool_config(path, _pool())
    monkeypatch.setattr(settings, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(settings, "VIDEO_DISPATCH_ENABLED", True)
    with pytest.raises(HTTPException) as exc:
        _preflight_stage_models(
            "video_generation", _values(video_profile_id="pool")
        )
    assert exc.value.status_code == 409
    detail = _structured(exc)
    assert detail["error_code"] == "selected_pool_not_ready"
    assert detail["profile_id"] == "pool"
    assert detail["missing"][0]["code"] == "no_qualified_member"


def test_explicit_unknown_pool_is_unavailable(monkeypatch, tmp_path):
    path = tmp_path / "video_profiles.yaml"
    write_pool_config(path, _pool())
    monkeypatch.setattr(settings, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(settings, "VIDEO_DISPATCH_ENABLED", True)
    with pytest.raises(HTTPException) as exc:
        _preflight_stage_models(
            "video_generation", _values(video_profile_id="missing-pool")
        )
    detail = _structured(exc)
    assert detail["error_code"] == "pool_unavailable"
    assert "select_pool" in detail["actions"]


def test_ready_pool_without_mode_contract_is_incompatible(monkeypatch, tmp_path):
    data = _pool()
    contract = data["video_profiles"][0]["contracts"].pop("first_frame")
    data["video_profiles"][0]["contracts"]["reference"] = {
        **contract,
        "task": "ref2va",
    }
    path = tmp_path / "video_profiles.yaml"
    write_pool_config(path, data)
    snapshot = load_pool_config(path)
    evidence = build_qualification_evidence(
        instance_id="sglang-01",
        resource_key="gpu-01",
        config_revision=snapshot.revision,
        boot_id=str(Config.BOOT_ID),
        checked_by="admin",
        checks={"jobs": True},
        confirmed=True,
        checked_at=time.time(),
    )
    VideoDispatchRepo().save_qualification(
        instance_id="sglang-01",
        resource_key="gpu-01",
        evidence=evidence.to_dict(),
    )
    monkeypatch.setattr(settings, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(settings, "VIDEO_DISPATCH_ENABLED", True)
    try:
        with pytest.raises(HTTPException) as exc:
            _preflight_stage_models("video_generation", _values())
        detail = _structured(exc)
        assert detail["error_code"] == "pool_incompatible"
        assert "edit_parameters" in detail["actions"]
    finally:
        with database.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM video_resource_slots WHERE resource_key = 'gpu-01'"
                    )


def test_unconfirmed_async_model_blocks_pool_until_confirmed(monkeypatch, tmp_path):
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
    path = tmp_path / "video_profiles.yaml"
    write_pool_config(path, data)
    snapshot = load_pool_config(path)
    evidence = build_qualification_evidence(
        instance_id="sglang-01",
        resource_key="gpu-01",
        config_revision=snapshot.revision,
        boot_id=str(Config.BOOT_ID),
        checked_by="admin",
        checks={"config": True},
        confirmed=True,
        checked_at=time.time(),
    )
    repo = VideoDispatchRepo()
    repo.save_qualification(
        instance_id="sglang-01",
        resource_key="gpu-01",
        evidence=evidence.to_dict(),
    )
    monkeypatch.setattr(settings, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(settings, "VIDEO_DISPATCH_ENABLED", True)
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
    try:
        with pytest.raises(HTTPException) as exc:
            _preflight_stage_models("video_generation", _values())
        assert exc.value.status_code == 409
        assert exc.value.detail["error_code"] == "pool_not_ready"

        repo.set_system_meta(ASYNC_CONFIRM_KEY, json.dumps(["vllm-model"]))
        _preflight_stage_models("video_generation", _values())
    finally:
        with database.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM video_resource_slots")
                    cur.execute(
                        "DELETE FROM schema_meta WHERE key = %s", (ASYNC_CONFIRM_KEY,)
                    )


def test_manual_preflight_still_checks_concrete_model(monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_DISPATCH_ENABLED", True)
    with pytest.raises(HTTPException) as exc:
        _preflight_stage_models(
            "video_generation",
            _values(video_dispatch_mode="manual", video_first_frame_model="missing-model"),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "model_unavailable"

