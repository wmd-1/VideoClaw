"""默认 async 影响清单：只列真实 vLLM 引用，占位实例排除。"""
from __future__ import annotations

from core.video_async_impact import collect_async_impact, unconfirmed_async_models


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
            {"id": "p1", "instances": ["real-a", "real-b"], "contracts": {"first_frame": contract}},
            {"id": "p2", "instances": ["placeholder"], "contracts": {"first_frame": contract}},
        ],
        "video_instances": [
            {
                "instance_id": "real-a",
                "model_id": "vllm-a",
                "resource_key": "gpu-a",
                "access_mode": "exclusive",
                "enabled": True,
                "driver": "vllm_omni_async",
                "probe": {"kind": "vllm_omni", "jobs_url": "http://video-a.internal:8000/v1/videos"},
            },
            {
                "instance_id": "real-b",
                "model_id": "vllm-b",
                "resource_key": "gpu-b",
                "access_mode": "shared",
                "enabled": False,
                "driver": "vllm_omni_async",
                "probe": {"kind": "vllm_omni", "jobs_url": "http://video-b.internal:8000/v1/videos"},
            },
            {
                "instance_id": "placeholder",
                "model_id": "vllm-c",
                "resource_key": "example-gpu-01",
                "access_mode": "exclusive",
                "enabled": False,
                "driver": "vllm_omni_async",
                "probe": {"kind": "vllm_omni", "jobs_url": "http://video.example/v1/videos"},
            },
        ],
    }


def test_collect_lists_only_unconfirmed_real_vllm_models():
    models = {
        "vllm-a": {"provider": "p-vllm", "video_api_mode": ""},
        "vllm-b": {"provider": "p-vllm", "video_api_mode": "sync"},
        "vllm-c": {"provider": "p-vllm"},
    }
    providers = {"p-vllm": {"protocol": "vllm-omni"}}
    impacts = collect_async_impact(_pool(), models=models, providers=providers)
    assert [item.model_id for item in impacts] == ["vllm-a"]
    assert impacts[0].instances == ("real-a",)
    assert impacts[0].profiles == ("p1",)
    assert all("placeholder" not in item.instances for item in impacts)


def test_sglang_and_explicit_async_are_not_listed():
    models = {
        "vllm-a": {"provider": "p-vllm", "video_api_mode": "async"},
        "vllm-b": {"provider": "p-vllm", "video_api_mode": "async"},
    }
    providers = {"p-vllm": {"protocol": "vllm-omni"}}
    assert collect_async_impact(_pool(), models=models, providers=providers) == []

    providers = {"p-vllm": {"protocol": "sglang"}}
    assert collect_async_impact(_pool(), models=models, providers=providers) == []


def test_confirmation_removes_model_from_unconfirmed_blockers():
    models = {"vllm-a": {"provider": "p-vllm"}, "vllm-b": {"provider": "p-vllm"}}
    providers = {"p-vllm": {"protocol": "vllm-omni"}}
    impacts = collect_async_impact(_pool(), models=models, providers=providers)
    assert unconfirmed_async_models(impacts) == ["vllm-a", "vllm-b"]

    impacts = collect_async_impact(
        _pool(), models=models, providers=providers, confirmed_models=["vllm-a"]
    )
    assert unconfirmed_async_models(impacts) == ["vllm-b"]
    assert next(item for item in impacts if item.model_id == "vllm-a").confirmed is True

def test_provider_or_model_placeholder_is_excluded():
    models = {
        "vllm-a": {"provider": "p-vllm", "model": "CHANGE_ME"},
        "vllm-b": {"provider": "p-vllm", "model": "H3"},
    }
    providers = {
        "p-vllm": {"protocol": "vllm-omni", "base_url": "http://api.example"}
    }
    assert collect_async_impact(_pool(), models=models, providers=providers) == []

    providers = {
        "p-vllm": {"protocol": "vllm-omni", "base_url": "http://video.internal"}
    }
    models = {
        "vllm-a": {"provider": "p-vllm", "model": "H3"},
        "vllm-b": {"provider": "p-vllm", "model": "H3"},
    }
    impacts = collect_async_impact(_pool(), models=models, providers=providers)
    assert [item.model_id for item in impacts] == ["vllm-a", "vllm-b"]
