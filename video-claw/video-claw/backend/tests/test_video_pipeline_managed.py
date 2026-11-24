"""4.8：Pipeline 受管视频必须带可信上下文并走 bridge。"""
from __future__ import annotations

import pytest

from core.observability import call_context
from core.video_managed_bridge import ManagedBridgeError
from pipelines import api_media


def test_pipeline_managed_video_requires_trusted_context(monkeypatch, tmp_path):
    monkeypatch.setattr(
        api_media, "resolve_managed_instance", lambda model: {"instance_id": "i1"}
    )
    monkeypatch.setattr(
        api_media, "parse_api_workflow", lambda model, kind: ("p", model)
    )
    monkeypatch.setattr(api_media, "normalize_video_duration", lambda p, m, d: d)
    with pytest.raises(ManagedBridgeError) as exc:
        api_media.generate_video_api(
            prompt="p",
            model="vllm-model",
            output_path=str(tmp_path / "out.mp4"),
            image_path=None,
        )
    assert exc.value.error_code == "trusted_context_required"


def test_pipeline_managed_video_uses_bridge(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        api_media, "resolve_managed_instance", lambda model: {"instance_id": "i1"}
    )
    monkeypatch.setattr(
        api_media, "parse_api_workflow", lambda model, kind: ("p", model)
    )
    monkeypatch.setattr(api_media, "normalize_video_duration", lambda p, m, d: d)
    monkeypatch.setattr(
        api_media,
        "managed_video_sync",
        lambda **kwargs: captured.update(kwargs) or {"save_path": kwargs["save_path"]},
    )
    with call_context(owner="alice", execution_id="task-1"):
        output = api_media.generate_video_api(
            prompt="p",
            model="vllm-model",
            output_path=str(tmp_path / "out.mp4"),
            image_path="/tmp/in.png",
        )
    assert output.endswith("out.mp4")
    assert captured["owner"] == "alice"
    assert captured["execution_id"] == "task-1"
    assert captured["mode"] == "first_frame"

def test_pipeline_rejects_last_without_first(monkeypatch, tmp_path):
    monkeypatch.setattr(api_media, "resolve_managed_instance", lambda model: {"instance_id": "i1"})
    monkeypatch.setattr(api_media, "parse_api_workflow", lambda model, kind: ("p", model))
    monkeypatch.setattr(api_media, "normalize_video_duration", lambda p, m, d: d)
    monkeypatch.setattr(api_media, "managed_video_sync", lambda **kwargs: {})
    with call_context(owner="alice", execution_id="task-1"):
        with pytest.raises(ManagedBridgeError) as exc:
            api_media.generate_video_api(
                prompt="p",
                model="vllm-model",
                output_path=str(tmp_path / "out.mp4"),
                last_image_path="/tmp/last.png",
            )
    assert exc.value.error_code == "invalid_input"


def test_pipeline_start_end_mode_passes_both_frames(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(api_media, "resolve_managed_instance", lambda model: {"instance_id": "i1"})
    monkeypatch.setattr(api_media, "parse_api_workflow", lambda model, kind: ("p", model))
    monkeypatch.setattr(api_media, "normalize_video_duration", lambda p, m, d: d)
    monkeypatch.setattr(
        api_media, "managed_video_sync",
        lambda **kwargs: captured.update(kwargs) or {"save_path": kwargs["save_path"]},
    )
    with call_context(owner="alice", execution_id="task-1"):
        api_media.generate_video_api(
            prompt="p",
            model="vllm-model",
            output_path=str(tmp_path / "out.mp4"),
            image_path="/tmp/first.png",
            last_image_path="/tmp/last.png",
        )
    assert captured["mode"] == "start_end_frame"
    assert captured["last_image_path"] == "/tmp/last.png"
