"""运行时视频池配置的纯配置测试（不访问数据库/模型服务）。"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from core.video_pool_config import (
    PoolConfigConflict,
    VideoPoolConfigError,
    VideoPoolConfigStore,
    collect_placeholder_issues,
    compute_revision,
    default_pool_config,
    effective_instance_view,
    effective_pool_view,
    is_placeholder_text,
    load_pool_config,
    normalize_pool_config,
    resolve_dispatch_enabled,
    resolve_profiles_path,
    validate_pool_config,
    write_pool_config,
)


def _raw_config(**overrides):
    raw = {
        "video_dispatch": {
            "default_mode": "auto",
            "default_profiles": {"first_frame": "h3-fl2va"},
            "queue_timeout_seconds": 3600,
            "max_queued_jobs": 1000,
            "max_running_jobs": 8,
        },
        "video_profiles": [
            {
                "id": "h3-fl2va",
                "name": "H3 首尾帧池",
                "revision": 1,
                "model_family": "MiniMax-H3",
                "artifact_revision": "art-1",
                "prompt_contract_revision": "prompt-1",
                "sampling_contract_revision": "sample-1",
                "instances": ["sglang-01"],
                "contracts": {
                    "first_frame": {
                        "task": "fl2va",
                        "resolutions": ["768P"],
                        "short_edge": 768,
                        "ratios": ["16:9"],
                        "duration": {"min": 4, "max": 15},
                        "fps": [24],
                    }
                },
            }
        ],
        "video_instances": [
            {
                "instance_id": "sglang-01",
                "model_id": "pool-h3-fl2va",
                "resource_key": "gpu-01",
                "access_mode": "exclusive",
                "enabled": False,
                "driver": "sglang_async",
                "probe": {
                    "kind": "sglang_diffusion",
                    "jobs_url": "http://video-01.example:30010/v1/videos",
                    "metrics_url": "http://video-01.example:30010/metrics",
                    "labels": {"role": "monolithic", "replica": "0"},
                },
            }
        ],
    }
    for key, value in overrides.items():
        if key in raw and isinstance(raw[key], dict) and isinstance(value, dict):
            raw[key].update(value)
        else:
            raw[key] = value
    return raw


def test_missing_file_uses_empty_defaults(tmp_path: Path):
    snapshot = load_pool_config(tmp_path / "missing.yaml")
    assert snapshot.data == default_pool_config()
    assert snapshot.data["video_dispatch"]["default_profiles"] == {}
    assert "enabled" not in snapshot.data["video_dispatch"]
    assert snapshot.revision == compute_revision(snapshot.data)


def test_empty_file_uses_defaults(tmp_path: Path):
    path = tmp_path / "video_profiles.yaml"
    path.write_text("# 空文件\n", encoding="utf-8")
    snapshot = load_pool_config(path)
    assert snapshot.data == default_pool_config()


def test_env_switch_and_path(tmp_path: Path):
    assert resolve_dispatch_enabled({}) is True
    assert resolve_dispatch_enabled({"VC_VIDEO_DISPATCH__ENABLED": ""}) is True
    assert resolve_dispatch_enabled({"VC_VIDEO_DISPATCH__ENABLED": "false"}) is False
    assert resolve_dispatch_enabled({"VC_VIDEO_DISPATCH__ENABLED": "0"}) is False
    with pytest.raises(VideoPoolConfigError):
        resolve_dispatch_enabled({"VC_VIDEO_DISPATCH__ENABLED": "maybe"})

    explicit = tmp_path / "pool.yaml"
    assert resolve_profiles_path({"VC_VIDEO_PROFILES_FILE": str(explicit)}) == explicit
    assert resolve_profiles_path({}, config_path=tmp_path / "config.yaml") == (
        tmp_path / "data" / "video_profiles.yaml"
    )


def test_explicit_null_and_false_are_not_truthy_defaults():
    raw = _raw_config()
    raw["video_dispatch"] = None
    raw["video_profiles"] = None
    raw["video_instances"] = None
    data = normalize_pool_config(raw)
    assert data == default_pool_config()

    raw = _raw_config()
    raw["video_instances"][0]["enabled"] = False
    raw["video_profiles"][0]["enabled"] = False
    data = normalize_pool_config(raw)
    assert data["video_instances"][0]["enabled"] is False
    assert data["video_profiles"][0]["enabled"] is False


def test_yaml_enabled_is_rejected():
    raw = _raw_config()
    raw["video_dispatch"]["enabled"] = True
    with pytest.raises(VideoPoolConfigError, match="VC_VIDEO_DISPATCH__ENABLED"):
        normalize_pool_config(raw)


def test_unknown_and_type_errors_are_rejected():
    raw = _raw_config()
    raw["unknown_section"] = {}
    with pytest.raises(VideoPoolConfigError, match="未知字段"):
        normalize_pool_config(raw)

    raw = _raw_config()
    raw["video_dispatch"]["max_running_jobs"] = True
    with pytest.raises(VideoPoolConfigError, match="整数"):
        normalize_pool_config(raw)

    raw = _raw_config()
    raw["video_dispatch"]["max_running_jobs"] = 9
    with pytest.raises(VideoPoolConfigError, match="不能大于"):
        normalize_pool_config(raw)

    raw = _raw_config()
    raw["video_dispatch"]["default_mode"] = "manual"
    with pytest.raises(VideoPoolConfigError, match="只支持 auto"):
        normalize_pool_config(raw)


def test_default_profile_must_exist_and_declare_mode():
    raw = _raw_config()
    raw["video_dispatch"]["default_profiles"] = {"reference": "h3-fl2va"}
    data = normalize_pool_config(raw)
    with pytest.raises(VideoPoolConfigError, match="未声明该生成方式契约"):
        validate_pool_config(data)

    raw["video_dispatch"]["default_profiles"] = {"first_frame": "missing-pool"}
    data = normalize_pool_config(raw)
    with pytest.raises(VideoPoolConfigError, match="不存在的池"):
        validate_pool_config(data)


def test_reference_validation_with_registry():
    data = normalize_pool_config(_raw_config())
    models = {"pool-h3-fl2va": {"provider": "pool_sglang_01"}}
    providers = {"pool_sglang_01": {"protocol": "sglang"}}
    validate_pool_config(data, providers=providers, models=models)

    with pytest.raises(VideoPoolConfigError, match="未注册模型"):
        validate_pool_config(data, providers=providers, models={})

    with pytest.raises(VideoPoolConfigError, match="未注册供应商"):
        validate_pool_config(
            data,
            providers={},
            models={"pool-h3-fl2va": {"provider": "pool_sglang_01"}},
        )


def test_duplicate_ids_and_dangling_members_are_rejected():
    raw = _raw_config()
    raw["video_profiles"].append(copy.deepcopy(raw["video_profiles"][0]))
    data = normalize_pool_config(raw)
    with pytest.raises(VideoPoolConfigError, match="id 重复"):
        validate_pool_config(data)

    raw = _raw_config()
    raw["video_profiles"][0]["instances"] = ["missing-instance"]
    data = normalize_pool_config(raw)
    with pytest.raises(VideoPoolConfigError, match="不存在的实例"):
        validate_pool_config(data)


def test_atomic_write_reload_and_conflict(tmp_path: Path):
    path = tmp_path / "video_profiles.yaml"
    raw = _raw_config()
    snapshot = write_pool_config(path, raw)
    assert path.exists()
    assert load_pool_config(path).revision == snapshot.revision

    with pytest.raises(PoolConfigConflict):
        write_pool_config(
            path,
            {**_raw_config(), "video_dispatch": {**_raw_config()["video_dispatch"], "queue_timeout_seconds": 7200}},
            expected_revision="stale-revision",
        )
    assert load_pool_config(path).data["video_dispatch"]["queue_timeout_seconds"] == 3600

    updated = _raw_config()
    updated["video_dispatch"]["queue_timeout_seconds"] = 7200
    snapshot2 = write_pool_config(path, updated, expected_revision=snapshot.revision)
    assert snapshot2.data["video_dispatch"]["queue_timeout_seconds"] == 7200
    assert snapshot2.revision != snapshot.revision
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


def test_store_reloads_external_file_change(tmp_path: Path):
    path = tmp_path / "video_profiles.yaml"
    store = VideoPoolConfigStore(path)
    first = store.load()
    assert first.data["video_dispatch"]["queue_timeout_seconds"] == 3600

    raw = _raw_config()
    raw["video_dispatch"]["queue_timeout_seconds"] = 7200
    path.write_text(
        yaml.safe_dump(normalize_pool_config(raw), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    reloaded, changed = store.reload_if_changed()
    assert changed is True
    assert reloaded.data["video_dispatch"]["queue_timeout_seconds"] == 7200
    assert store.snapshot.revision == reloaded.revision


def test_placeholder_detection_is_field_level():
    raw = _raw_config()
    raw["video_instances"][0]["resource_key"] = "example-gpu-01"
    raw["video_instances"][0]["probe"]["labels"] = {"role": "monolithic", "replica": "CHANGE_ME"}
    raw["video_profiles"][0]["name"] = "CHANGE_ME example 仅显示名"
    raw["video_profiles"][0]["artifact_revision"] = "CHANGE_ME"
    raw["video_profiles"][0]["prompt_contract_revision"] = "CHANGE_ME"
    raw["video_profiles"][0]["sampling_contract_revision"] = "CHANGE_ME"
    data = normalize_pool_config(raw)
    providers = {"pool_sglang_01": {"base_url": "http://video-01.example:30010/v1", "api_key": ""}}
    models = {"pool-h3-fl2va": {"provider": "pool_sglang_01", "model": "CHANGE_ME"}}

    issues = collect_placeholder_issues(data, providers=providers, models=models)
    fields = {item["field"] for item in issues}
    assert "video_instances.sglang-01.probe.jobs_url" in fields
    assert "video_instances.sglang-01.probe.metrics_url" in fields
    assert "video_instances.sglang-01.resource_key" in fields
    assert "video_instances.sglang-01.probe.labels.replica" in fields
    assert "custom_models.pool-h3-fl2va.model" in fields
    assert "api_providers.pool_sglang_01.base_url" in fields
    assert any(item["field"].endswith(".artifact_revision") for item in issues)
    assert not any("显示名" in item["field"] for item in issues)

    assert is_placeholder_text("  change_me  ") is True
    assert is_placeholder_text("CHANGE_ME_TOO") is False


def test_example_host_case_and_root_dot_are_normalized():
    raw = _raw_config()
    raw["video_instances"][0]["probe"]["jobs_url"] = "http://VIDEO-01.EXAMPLE.:30010/v1/videos"
    raw["video_instances"][0]["probe"]["metrics_url"] = ""
    raw["video_instances"][0]["resource_key"] = "example-gpu-x"
    data = normalize_pool_config(raw)
    issues = collect_placeholder_issues(data)
    fields = {item["field"] for item in issues}
    assert "video_instances.sglang-01.probe.jobs_url" in fields
    assert "video_instances.sglang-01.resource_key" not in fields


def test_non_reserved_hosts_and_empty_api_key_are_allowed():
    raw = _raw_config()
    raw["video_instances"][0]["probe"]["jobs_url"] = "http://api.example.internal:30010/v1/videos"
    raw["video_instances"][0]["probe"]["metrics_url"] = "http://api.example.internal:30010/metrics"
    data = normalize_pool_config(raw)
    providers = {"pool_sglang_01": {"base_url": "http://api.example.internal:30010/v1", "api_key": ""}}
    models = {"pool-h3-fl2va": {"provider": "pool_sglang_01", "model": "MiniMaxAI/MiniMax-H3"}}

    issues = collect_placeholder_issues(data, providers=providers, models=models)
    assert not any(item["code"] == "example_host" for item in issues)
    assert not any("api_key" in item["field"] for item in issues)


def test_draft_can_save_but_enabled_placeholder_is_rejected(tmp_path: Path):
    path = tmp_path / "video_profiles.yaml"
    raw = _raw_config()
    raw["video_instances"][0]["enabled"] = False
    write_pool_config(path, raw)

    raw["video_instances"][0]["enabled"] = True
    with pytest.raises(VideoPoolConfigError, match="启用前仍有占位配置"):
        write_pool_config(path, raw)
    assert load_pool_config(path).data["video_instances"][0]["enabled"] is False

    raw["video_instances"][0]["probe"]["jobs_url"] = "http://video-01.internal:30010/v1/videos"
    raw["video_instances"][0]["probe"]["metrics_url"] = "http://video-01.internal:30010/metrics"
    write_pool_config(path, raw)
    assert load_pool_config(path).data["video_instances"][0]["enabled"] is True


def test_effective_views_follow_enabled_and_placeholders():
    raw = _raw_config()
    raw["video_instances"][0]["enabled"] = True
    data = normalize_pool_config(raw)
    instance = effective_instance_view(data)[0]
    assert instance["enabled"] is True
    assert instance["effective_enabled"] is False
    assert instance["issues"]

    pool = effective_pool_view(data)[0]
    assert pool["enabled"] is True
    assert pool["effective_enabled"] is False

    raw["video_instances"][0]["probe"]["jobs_url"] = "http://video-01.internal:30010/v1/videos"
    raw["video_instances"][0]["probe"]["metrics_url"] = "http://video-01.internal:30010/metrics"
    data = normalize_pool_config(raw)
    assert effective_instance_view(data)[0]["effective_enabled"] is True
    assert effective_pool_view(data)[0]["effective_enabled"] is True

    raw["video_profiles"][0]["enabled"] = False
    data = normalize_pool_config(raw)
    assert effective_pool_view(data)[0]["effective_enabled"] is False
