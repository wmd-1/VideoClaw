"""模型连通测试按池契约选择最小可用模式，而不是固定 t2va。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.routers.models import _test_video


def _runtime(*contracts):
    profiles = {
        f"p-{index}": SimpleNamespace(
            enabled=True, members=("i1",), contracts={mode: {} for mode in modes}
        )
        for index, modes in enumerate(contracts)
    }
    def eligible(profile_id):
        return [SimpleNamespace(instance_id="i1")]

    return SimpleNamespace(
        service=SimpleNamespace(
            runtime=lambda: SimpleNamespace(
                profiles=profiles, eligible_members=eligible
            )
        )
    )


def _patch(monkeypatch, runtime, captured):
    monkeypatch.setattr(
        "core.video_generation_service.resolve_managed_instance",
        lambda model: {"instance_id": "i1", "model_id": model},
    )
    monkeypatch.setattr(
        "core.video_dispatch_runtime.get_dispatch_runtime", lambda: runtime
    )

    def fake_sync(**kwargs):
        captured.update(kwargs)
        return {"save_path": kwargs["save_path"]}

    monkeypatch.setattr("core.video_managed_bridge.managed_video_sync", fake_sync)


def test_model_test_uses_first_frame_when_no_t2va(monkeypatch):
    captured = {}
    _patch(monkeypatch, _runtime(("first_frame",)), captured)
    detail = _test_video({"id": "vllm-model"}, owner="alice")
    assert "mode=first_frame" in detail
    assert captured["mode"] == "first_frame"
    assert captured["image_path"] and captured["image_path"].endswith("test_input.png")
    assert captured["reference_image_paths"] is None


def test_model_test_prefers_t2va(monkeypatch):
    captured = {}
    _patch(monkeypatch, _runtime(("first_frame",), ("t2va",)), captured)
    detail = _test_video({"id": "vllm-model"}, owner="alice")
    assert "mode=t2va" in detail
    assert captured["mode"] == "t2va"
    assert captured["image_path"] is None


def test_model_test_reports_no_compatible_contract(monkeypatch):
    captured = {}
    _patch(monkeypatch, _runtime(("start_end_frame",)), captured)
    with pytest.raises(Exception, match="已资格就绪"):
        _test_video({"id": "vllm-model"}, owner="alice")
