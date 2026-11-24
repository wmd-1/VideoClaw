"""4.9：派发时 canonical 物化、签名刷新和发布回调的链路测试。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import video_dispatch_media as media
from core.video_dispatch_media import prepare_dispatch_job, publish_dispatch_result


def test_prepare_dispatch_job_copies_canonical_inputs_to_job_dir(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    work_root = tmp_path / "work"
    monkeypatch.setattr(media, "execution_work_root", lambda *args: str(work_root))
    monkeypatch.setattr(
        media.media_refs,
        "materialize",
        lambda value, user: SimpleNamespace(local_path=str(source)),
    )
    job = {
        "job_id": "job-1",
        "source_kind": "workflow",
        "source_id": "sid-1",
        "execution_id": "ex-1",
        "snapshot_json": {
            "owner_login": "alice",
            "request": {"image_path": "/old/runtime.png", "save_path": str(work_root / "out.mp4")},
            "canonical_media": {
                "image_path": "users/abc/scene.png",
                "reference_video_paths": ["users/abc/ref.mp4"],
            },
        },
    }
    prepared = prepare_dispatch_job(job, {"attempt_id": "a1"})
    request = prepared["snapshot_json"]["request"]
    assert request["image_path"].startswith(str(work_root / "inputs" / "job-1"))
    assert request["image_path"] != "/old/runtime.png"
    assert open(request["image_path"], "rb").read() == b"image"
    assert request["reference_video_paths"][0].startswith(
        str(work_root / "inputs" / "job-1")
    )
    assert "canonical_media" in prepared["snapshot_json"]
    assert "users/abc" not in str(request)


def test_prepare_dispatch_job_refreshes_audio_candidates(tmp_path, monkeypatch):
    work_root = tmp_path / "work"
    monkeypatch.setattr(media, "execution_work_root", lambda *args: str(work_root))
    calls = []

    class FakeRef:
        def candidates(self):
            calls.append(1)
            return [f"https://inference/audio?sig={len(calls)}"]

    monkeypatch.setattr(media, "build_media_reference", lambda value, user: FakeRef())
    job = {
        "job_id": "job-2",
        "source_kind": "workflow",
        "source_id": "sid-1",
        "execution_id": "ex-1",
        "snapshot_json": {
            "owner_login": "alice",
            "request": {"audio_reference_url": "https://old?sig=0"},
            "canonical_media": {"audio_reference_url": "users/abc/a.mp3"},
        },
    }
    first = prepare_dispatch_job(job, {"attempt_id": "a1"})["snapshot_json"]["request"]
    second = prepare_dispatch_job(job, {"attempt_id": "a2"})["snapshot_json"]["request"]
    assert first["audio_reference_url"] != second["audio_reference_url"]
    assert "sig=1" in first["audio_reference_url"]
    assert "sig=2" in second["audio_reference_url"]


def test_prepare_dispatch_job_rejects_foreign_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "execution_work_root", lambda *args: str(tmp_path))
    monkeypatch.setattr(
        media.media_refs,
        "materialize",
        lambda value, user: (_ for _ in ()).throw(media.media_refs.MediaNotFound(value)),
    )
    job = {
        "job_id": "job-foreign",
        "source_kind": "workflow",
        "source_id": "sid-1",
        "execution_id": "ex-1",
        "snapshot_json": {
            "owner_login": "bob",
            "request": {},
            "canonical_media": {"image_path": "users/alice/secret.png"},
        },
    }
    with pytest.raises(media.media_refs.MediaNotFound):
        prepare_dispatch_job(job, {"attempt_id": "a1"})


def test_prepare_dispatch_job_rejects_missing_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "execution_work_root", lambda *args: str(tmp_path))
    job = {
        "job_id": "job-3",
        "source_kind": "workflow",
        "source_id": "sid-1",
        "execution_id": "ex-1",
        "snapshot_json": {
            "request": {},
            "canonical_media": {"image_path": "users/abc/a.png"},
        },
    }
    with pytest.raises(RuntimeError):
        prepare_dispatch_job(job, {"attempt_id": "a1"})


def test_publish_dispatch_result_uses_existing_publisher(monkeypatch, tmp_path):
    called = {}

    class FakePublisher:
        def __init__(self, **kwargs):
            called["init"] = kwargs

        def publish(self, payloads, carried):
            called["payloads"] = payloads
            return payloads, {"published": [{"public_path": "result/p.png"}]}

    monkeypatch.setattr(media, "ExecutionPublisher", FakePublisher)
    job = {
        "source_kind": "workflow",
        "source_id": "sid-1",
        "execution_id": "ex-1",
        "snapshot_json": {
            "owner_login": "alice",
            "publish_result": True,
        },
    }
    published = publish_dispatch_result(
        job, {"attempt_id": "a1"}, {"save_path": str(tmp_path / "out.mp4")}
    )
    assert published["published"][0]["public_path"] == "result/p.png"
    clip = called["payloads"]["video_generation"]["clips"][0]
    assert clip["selected"].endswith("out.mp4")
    assert called["init"]["session_id"] == "sid-1"
    assert called["init"]["execution_id"] == "ex-1"
