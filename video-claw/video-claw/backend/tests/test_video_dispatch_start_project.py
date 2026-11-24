"""项目创建：auto 不落具体 video_model，manual 仍按设置链解析。"""
from __future__ import annotations

from api.dependencies import workflow_engine
from config import settings


def _headers() -> dict:
    return {"X-User-Login-Name": "local"}


def _body(mode: str) -> dict:
    return {
        "idea": "资源池默认 auto",
        "llm_model": "llm",
        "vlm_model": "vlm",
        "image_t2i_model": "t2i",
        "image_it2i_model": "i2i",
        "video_dispatch_mode": mode,
    }


def test_auto_start_does_not_persist_settings_video_model(client, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_MODEL", "sentinel-video")
    monkeypatch.setattr(settings, "VIDEO_FIRST_FRAME_MODEL", "sentinel-first")
    monkeypatch.setattr(settings, "VIDEO_START_END_MODEL", "sentinel-end")
    monkeypatch.setattr(settings, "VIDEO_REFERENCE_MODEL", "sentinel-ref")

    response = client.post("/api/project/start", headers=_headers(), json=_body("auto"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert not body["params"]["video_model"]
    state = workflow_engine.get_state(body["session_id"])
    assert state is not None
    assert not state.meta.get("video_model")
    assert not state.meta.get("video_first_frame_model")
    assert not state.meta.get("video_start_end_model")
    assert not state.meta.get("video_reference_model")
    assert state.meta["video_dispatch_mode"] == "auto"


def test_auto_start_keeps_explicit_user_model_choice(client, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_FIRST_FRAME_MODEL", "sentinel-first")
    body = {**_body("auto"), "video_reference_model": "user-ref"}

    response = client.post("/api/project/start", headers=_headers(), json=body)

    assert response.status_code == 200, response.text
    state = workflow_engine.get_state(response.json()["session_id"])
    assert state.meta["video_reference_model"] == "user-ref"
    assert not state.meta.get("video_first_frame_model")


def test_invalid_dispatch_mode_is_rejected(client):
    response = client.post(
        "/api/project/start",
        headers=_headers(),
        json={**_body("auto"), "video_dispatch_mode": "x"},
    )
    assert response.status_code == 422


def test_session_update_rejects_invalid_dispatch_mode(client):
    response = client.post("/api/project/start", headers=_headers(), json=_body("auto"))
    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]

    patched = client.patch(
        f"/api/project/{session_id}/models",
        headers=_headers(),
        json={"video_dispatch_mode": "x"},
    )
    assert patched.status_code == 400


def test_manual_start_still_persists_resolved_video_model(client, monkeypatch):
    monkeypatch.setattr(settings, "VIDEO_MODEL", "sentinel-video")
    monkeypatch.setattr(settings, "VIDEO_FIRST_FRAME_MODEL", "sentinel-first")
    monkeypatch.setattr(settings, "VIDEO_START_END_MODEL", "sentinel-end")
    monkeypatch.setattr(settings, "VIDEO_REFERENCE_MODEL", "sentinel-ref")

    response = client.post("/api/project/start", headers=_headers(), json=_body("manual"))

    assert response.status_code == 200, response.text
    state = workflow_engine.get_state(response.json()["session_id"])
    assert state.meta["video_model"] == "sentinel-first"
    assert state.meta["video_first_frame_model"] == "sentinel-first"
    assert state.meta["video_start_end_model"] == "sentinel-end"
    assert state.meta["video_reference_model"] == "sentinel-ref"
