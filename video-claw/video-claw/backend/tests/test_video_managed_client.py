"""VideoClient 受管模型同层授权：缺少调度上下文时在 HTTP 前拒绝。"""
from __future__ import annotations

import pytest
import yaml

from config import Config
from core.video_generation_service import GrantError, default_grant_service
from models.custom_video import CustomVideoClient
from models.video_client import VideoClient


def _pool(instances: list[dict]) -> dict:
    return {
        "video_dispatch": {
            "default_mode": "auto",
            "default_profiles": {},
            "queue_timeout_seconds": 3600,
            "max_queued_jobs": 1000,
            "max_running_jobs": 8,
        },
        "video_profiles": [
            {
                "id": "p1",
                "name": "p1",
                "revision": 1,
                "instances": [row["instance_id"] for row in instances],
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
        "video_instances": instances,
    }


def _instance(instance_id: str = "i1", model_id: str = "m1") -> dict:
    return {
        "instance_id": instance_id,
        "model_id": model_id,
        "resource_key": f"gpu-{instance_id}",
        "access_mode": "exclusive",
        "enabled": True,
        "driver": "vllm_omni",
        "probe": {"kind": "vllm_omni", "jobs_url": "http://127.0.0.1/jobs", "metrics_url": "", "labels": {}},
    }


@pytest.fixture
def managed_pool(tmp_path, monkeypatch):
    path = tmp_path / "video_profiles.yaml"
    path.write_text(yaml.safe_dump(_pool([_instance()]), allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(Config, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(
        Config,
        "CONFIG",
        {
            "api_providers": {
                "prov": {
                    "protocol": "vllm-omni",
                    "base_url": "http://127.0.0.1:1/v1",
                    "api_key": "test-key",
                }
            },
            "custom_models": [
                {
                    "id": "m1",
                    "provider": "prov",
                    "model": "MiniMaxAI/H3",
                    "types": ["video"],
                    "concurrency": 1,
                },
                {
                    "id": "m1-alias",
                    "provider": "prov",
                    "model": "MiniMaxAI/H3",
                    "types": ["video"],
                    "concurrency": 1,
                },
                {
                    "id": "m-draft",
                    "provider": "prov",
                    "model": "MiniMaxAI/H3-draft",
                    "types": ["video"],
                    "concurrency": 1,
                },
            ],
        },
    )
    return path


def _grant(instance_id: str = "i1", resource_key: str = "gpu-i1"):
    return default_grant_service().issue(
        job_id="job-1",
        attempt_id="attempt-1",
        instance_id=instance_id,
        resource_key=resource_key,
        ttl_seconds=60,
    )


def test_managed_model_without_grant_is_rejected_before_http(monkeypatch, managed_pool):
    def unexpected(self, **kwargs):  # pragma: no cover - 只用于证明未被调用
        raise AssertionError("受管模型不得在无授权时进入客户端发送")

    monkeypatch.setattr(CustomVideoClient, "generate_video", unexpected)
    with pytest.raises(GrantError):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/never.mp4",
            model="m1",
        )


def test_managed_model_with_valid_grant_passes_managed_context(monkeypatch, managed_pool):
    captured: dict = {}

    def fake_generate(self, **kwargs):
        captured["meta"] = dict(self._meta)
        captured["grant"] = kwargs.get("dispatch_grant")
        return "ok"

    monkeypatch.setattr(CustomVideoClient, "generate_video", fake_generate)
    grant = _grant()
    result = VideoClient().generate_video(
        prompt="demo",
        image_path=None,
        save_path="/tmp/out.mp4",
        model="m1",
        dispatch_grant=grant,
        dispatch_instance={"instance_id": "i1"},
        dispatch_job_id="job-1",
    )

    assert result == "ok"
    assert captured["meta"]["managed"] is True
    assert captured["meta"]["dispatch_job_id"] == "job-1"
    assert captured["meta"]["managed_instance_id"] == "i1"
    assert captured["meta"]["managed_resource_key"] == "gpu-i1"
    assert captured["grant"] is grant


def test_same_endpoint_alias_cannot_bypass_grant(monkeypatch, managed_pool):
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: pytest.fail("同一远端端点的别名不得绕锁"),
    )
    with pytest.raises(GrantError):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m1-alias",
        )


def test_draft_instance_model_is_rejected(monkeypatch, managed_pool, tmp_path):
    path = tmp_path / "draft_profiles.yaml"
    draft = _instance("i-draft", "m-draft")
    draft["enabled"] = False
    path.write_text(yaml.safe_dump(_pool([draft]), allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(Config, "VIDEO_PROFILES_FILE", str(path))
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: pytest.fail("未启用草稿不得直接调用"),
    )
    with pytest.raises(GrantError):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m-draft",
        )


def test_same_endpoint_alias_with_valid_grant_passes(monkeypatch, managed_pool):
    captured: dict = {}

    def fake_generate(self, **kwargs):
        captured["meta"] = dict(self._meta)
        return "ok"

    monkeypatch.setattr(CustomVideoClient, "generate_video", fake_generate)
    grant = _grant()
    result = VideoClient().generate_video(
        prompt="demo",
        image_path=None,
        save_path="/tmp/out.mp4",
        model="m1-alias",
        dispatch_grant=grant,
        dispatch_instance={"instance_id": "i1"},
        dispatch_job_id="job-1",
    )

    assert result == "ok"
    assert captured["meta"]["managed"] is True
    assert captured["meta"]["managed_instance_id"] == "i1"


def test_broken_pool_file_allows_non_managed_after_valid_snapshot(
    monkeypatch, managed_pool
):
    from core.video_generation_service import managed_instances_for_model

    # 先加载一次有效快照，形成“最后有效配置”缓存。
    assert managed_instances_for_model("m1")
    managed_pool.write_text("video_dispatch: [oops\n", encoding="utf-8")
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: "ok",
    )

    # 非受管模型：坏文件不能阻断既有池外调用。
    assert VideoClient().generate_video(
        prompt="demo",
        image_path=None,
        save_path="/tmp/out.mp4",
        model="m-draft",
    ) == "ok"

    # 已知受管模型：继续 fail-closed。
    with pytest.raises(GrantError, match="受管"):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m1",
        )


def test_pool_load_warms_guard_cache_without_direct_call(monkeypatch, managed_pool):
    from core.video_pool_config import load_pool_config

    # 启动/预检正常加载一次池文件，不经过受管 guard。
    load_pool_config(managed_pool)
    managed_pool.write_text("video_dispatch: [oops\n", encoding="utf-8")
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: "ok",
    )

    assert VideoClient().generate_video(
        prompt="demo",
        image_path=None,
        save_path="/tmp/out.mp4",
        model="m-draft",
    ) == "ok"
    with pytest.raises(GrantError, match="受管"):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m1",
        )


def test_truncated_empty_file_does_not_clear_managed_cache(monkeypatch, managed_pool):
    from core.video_pool_config import load_pool_config

    load_pool_config(managed_pool)  # 有效缓存
    managed_pool.write_text("", encoding="utf-8")
    load_pool_config(managed_pool)  # 瞬时截断产生的空池不得覆盖缓存
    managed_pool.write_text("video_dispatch: [oops\n", encoding="utf-8")
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: "ok",
    )

    assert VideoClient().generate_video(
        prompt="demo",
        image_path=None,
        save_path="/tmp/out.mp4",
        model="m-draft",
    ) == "ok"
    # 受管集合仍来自截断前的有效快照。
    with pytest.raises(GrantError, match="受管"):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m1",
        )


def test_explicit_save_empty_pool_clears_managed_cache(monkeypatch, managed_pool):
    from core.video_pool_config import default_pool_config, load_pool_config, write_pool_config

    load_pool_config(managed_pool)
    write_pool_config(managed_pool, default_pool_config())  # 管理员显式清空
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: "ok",
    )

    assert VideoClient().generate_video(
        prompt="demo",
        image_path=None,
        save_path="/tmp/out.mp4",
        model="m1",
    ) == "ok"


def test_partial_valid_file_replaces_managed_cache(monkeypatch, managed_pool):
    import yaml

    from core.video_pool_config import load_pool_config

    load_pool_config(managed_pool)  # 缓存中的 m1
    managed_pool.write_text(
        yaml.safe_dump(_pool([_instance("i2", "m-draft")]), allow_unicode=True),
        encoding="utf-8",
    )
    load_pool_config(managed_pool)  # 语法合法的部分文件被视为新有效快照
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: "ok",
    )

    # 已知限制：被半成品文件移除的 m1 不再属于受管集合，按非受管放行。
    assert VideoClient().generate_video(
        prompt="demo",
        image_path=None,
        save_path="/tmp/out.mp4",
        model="m1",
    ) == "ok"
    # 新文件中的 m-draft 是受管模型，仍需授权。
    with pytest.raises(GrantError):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m-draft",
        )


def test_broken_pool_file_fails_closed(monkeypatch, managed_pool, tmp_path):
    broken = tmp_path / "broken_profiles.yaml"
    broken.write_text("video_dispatch: [oops\n", encoding="utf-8")
    monkeypatch.setattr(Config, "VIDEO_PROFILES_FILE", str(broken))
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: pytest.fail("配置损坏时不得进入发送"),
    )

    with pytest.raises(GrantError, match="配置"):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m1",
        )


def test_managed_call_requires_dispatch_job_id(monkeypatch, managed_pool):
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: pytest.fail("缺少 dispatch_job_id 时不得发送"),
    )
    with pytest.raises(GrantError, match="dispatch_job_id"):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m1",
            dispatch_grant=_grant(),
            dispatch_instance={"instance_id": "i1"},
        )


def test_grant_must_bind_to_the_calling_job(monkeypatch, managed_pool):
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: pytest.fail("作业不匹配时不得发送"),
    )
    with pytest.raises(GrantError, match="作业"):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m1",
            dispatch_grant=_grant(),
            dispatch_instance={"instance_id": "i1"},
            dispatch_job_id="other-job",
        )


def test_managed_meta_requires_attempt_job(monkeypatch, managed_pool):
    from core.video_generation_service import managed_meta_for_grant

    grant = _grant()
    instance = {"instance_id": "i1", "resource_key": "gpu-i1", "model_id": "m1"}
    with pytest.raises(GrantError, match="job"):
        managed_meta_for_grant({}, grant, instance, job_id="")
    with pytest.raises(GrantError, match="不一致"):
        managed_meta_for_grant({}, grant, instance, job_id="other-job")
    meta = managed_meta_for_grant({}, grant, instance, job_id="job-1")
    assert meta["dispatch_job_id"] == "job-1"


def test_unknown_instance_or_mismatched_grant_is_rejected(monkeypatch, managed_pool):
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, **kwargs: pytest.fail("不得进入客户端发送"),
    )
    with pytest.raises(GrantError):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m1",
            dispatch_grant=_grant(),
            dispatch_instance={"instance_id": "i2"},
            dispatch_job_id="job-1",
        )
    with pytest.raises(GrantError):
        VideoClient().generate_video(
            prompt="demo",
            image_path=None,
            save_path="/tmp/out.mp4",
            model="m1",
            dispatch_grant=_grant(resource_key="gpu-other"),
            dispatch_instance={"instance_id": "i1"},
            dispatch_job_id="job-1",
        )
