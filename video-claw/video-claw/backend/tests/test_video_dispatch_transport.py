"""CustomVideoClient 异步默认与提交结果分类（MockTransport）。"""
from __future__ import annotations

import logging

import httpx
import pytest

from core.video_generation_service import GrantError, default_grant_service
from models.custom_video import CustomVideoClient, VideoSubmissionUnknown


def _meta(api_mode: str = "async"):
    return {
        "id": "vllm-test",
        "request_model": "MiniMaxAI/MiniMax-H3",
        "protocol": "vllm-omni",
        "base_url": "http://mock/v1",
        "video_api_mode": api_mode,
        "capabilities": {"tasks": ["t2va"], "duration": {"min": 4, "max": 15}},
    }


def _client(handler):
    client = CustomVideoClient(_meta(), poll_interval=0.001, poll_timeout=1, timeout=1)
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    return client


def test_vllm_default_async_submits_once_and_downloads(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"id": "job-1"})
        if request.method == "GET" and request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"video-bytes")
        if request.method == "GET" and "job-1" in request.url.path:
            return httpx.Response(
                200,
                json={"id": "job-1", "status": "completed", "url": "http://mock/content"},
            )
        return httpx.Response(404)

    client = _client(handler)
    target = tmp_path / "out.mp4"
    try:
        result = client.generate_video(
            prompt="demo",
            image_path=None,
            save_path=str(target),
            duration=4,
            resolution="768P",
        )
        assert target.read_bytes() == b"video-bytes"
        assert result.startswith("http")
        assert all("sync" not in path for _, path in calls)
        assert sum(1 for method, _ in calls if method == "POST") == 1
    finally:
        client._client.close()


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("late", request=request)),
        lambda request: httpx.Response(500, json={"detail": "ambiguous"}),
        lambda request: httpx.Response(200, json={"detail": "no id"}),
    ],
)
def test_unknown_submission_never_tries_another_create_variant(tmp_path, handler):
    calls = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return handler(request)

    client = _client(wrapped)
    try:
        with pytest.raises(VideoSubmissionUnknown):
            client.generate_video(
                prompt="demo",
                image_path=None,
                save_path=str(tmp_path / "out.mp4"),
                duration=4,
                resolution="768P",
            )
        assert calls.count("POST") == 1
    finally:
        client._client.close()


def test_explicit_sync_is_the_only_path_that_calls_sync_endpoint(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/videos/sync"):
            return httpx.Response(200, content=b"sync-video", headers={"content-type": "video/mp4"})
        return httpx.Response(404)

    client = CustomVideoClient(
        _meta(api_mode="sync"), poll_interval=0.001, poll_timeout=1, timeout=1
    )
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    target = tmp_path / "sync.mp4"
    try:
        result = client.generate_video(
            prompt="demo",
            image_path=None,
            save_path=str(target),
            duration=4,
            resolution="768P",
        )
        assert target.read_bytes() == b"sync-video"
        assert result.startswith("sync://")
        assert any(path.endswith("/videos/sync") for _, path in calls)
        assert all(not path.endswith("/videos") or method == "POST" for method, path in calls)
    finally:
        client._client.close()


def test_sync_read_timeout_never_creates_async_job(tmp_path):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/videos/sync"):
            raise httpx.ReadTimeout("ambiguous", request=request)
        return httpx.Response(200, json={"id": "async-job"})

    client = CustomVideoClient(
        _meta(api_mode="sync"), poll_interval=0.001, poll_timeout=1, timeout=1
    )
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(VideoSubmissionUnknown):
            client.generate_video(
                prompt="demo",
                image_path=None,
                save_path=str(tmp_path / "x.mp4"),
                duration=4,
                resolution="768P",
            )
        assert calls == [("POST", "/v1/videos/sync")]
    finally:
        client._client.close()


def test_sync_5xx_is_never_retried_as_async(tmp_path):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/videos/sync"):
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"id": "async-job"})

    client = CustomVideoClient(
        _meta(api_mode="sync"), poll_interval=0.001, poll_timeout=1, timeout=1
    )
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(VideoSubmissionUnknown):
            client.generate_video(
                prompt="demo",
                image_path=None,
                save_path=str(tmp_path / "x.mp4"),
                duration=4,
                resolution="768P",
            )
        assert calls == [("POST", "/v1/videos/sync")]
    finally:
        client._client.close()


def test_sync_endpoint_missing_may_safely_fall_back_to_async(tmp_path):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/videos/sync"):
            return httpx.Response(404, text="not found")
        if request.method == "POST" and request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"id": "job-1"})
        if request.method == "GET" and request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"video-bytes")
        if request.method == "GET" and "job-1" in request.url.path:
            return httpx.Response(
                200,
                json={"id": "job-1", "status": "completed", "url": "http://mock/content"},
            )
        return httpx.Response(404)

    client = CustomVideoClient(
        _meta(api_mode="sync"), poll_interval=0.001, poll_timeout=1, timeout=1
    )
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    target = tmp_path / "fallback.mp4"
    try:
        client.generate_video(
            prompt="demo",
            image_path=None,
            save_path=str(target),
            duration=4,
            resolution="768P",
        )
        assert target.read_bytes() == b"video-bytes"
        assert calls[:2] == [("POST", "/v1/videos/sync"), ("POST", "/v1/videos")]
    finally:
        client._client.close()


def test_managed_instance_rejects_sync_before_http(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=b"x")

    grant = default_grant_service().issue(
        job_id="job-1",
        attempt_id="attempt-1",
        instance_id="i1",
        resource_key="gpu-1",
        ttl_seconds=60,
    )
    meta = _managed_meta(video_api_mode="sync")
    client = CustomVideoClient(meta, poll_interval=0.001, poll_timeout=1, timeout=1)
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="sync"):
            client.generate_video(
                prompt="demo",
                image_path=None,
                save_path=str(tmp_path / "x.mp4"),
                duration=4,
                dispatch_grant=grant,
                dispatch_job_id="job-1",
            )
        assert calls == []
    finally:
        client._client.close()


def _managed_meta(**overrides) -> dict:
    meta = _meta()
    meta.update(
        {
            "managed": True,
            "dispatch_job_id": "job-1",
            "managed_instance_id": "i1",
            "managed_resource_key": "gpu-1",
        }
    )
    meta.update(overrides)
    return meta


def test_managed_client_requires_grant_before_any_http(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": "job-1"})

    client = CustomVideoClient(_managed_meta(), poll_interval=0.001, poll_timeout=1, timeout=1)
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GrantError):
            client.generate_video(
                prompt="demo",
                image_path=None,
                save_path=str(tmp_path / "x.mp4"),
                duration=4,
                dispatch_job_id="job-1",
            )
        assert calls == []
    finally:
        client._client.close()


def test_managed_async_submit_requires_grant(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.method == "POST" and request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"id": "job-1"})
        return httpx.Response(404)

    client = CustomVideoClient(_managed_meta(), poll_interval=0.001, poll_timeout=1, timeout=1)
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    kwargs = dict(
        prompt="demo",
        image_path=None,
        save_path=str(tmp_path / "x.mp4"),
        duration=4,
        resolution="768P",
    )
    try:
        with pytest.raises(GrantError):
            client.submit_job(dispatch_job_id="job-1", **kwargs)
        assert calls == []

        grant = default_grant_service().issue(
            job_id="job-1",
            attempt_id="attempt-1",
            instance_id="i1",
            resource_key="gpu-1",
            ttl_seconds=60,
        )
        payload = client.submit_job(dispatch_grant=grant, dispatch_job_id="job-1", **kwargs)
        assert payload["id"] == "job-1"
        assert calls == ["/v1/videos"]
    finally:
        client._client.close()


def test_async_submit_reuses_shared_parameter_mapping(tmp_path):
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if request.method == "POST" and request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"id": "job-1"})
        return httpx.Response(404)

    client = CustomVideoClient(_meta(), poll_interval=0.001, poll_timeout=1, timeout=1)
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    try:
        client.submit_job(
            prompt="demo",
            image_path=None,
            save_path=str(tmp_path / "x.mp4"),
            duration=1,
            video_ratio="9:16",
            resolution="768P",
        )
    finally:
        client._client.close()

    body = bodies[0].decode("utf-8", "ignore")
    assert 'name="seconds"' in body
    assert "\r\n\r\n4\r\n" in body  # 模型能力 4-15s：提交前已夹取
    assert 'name="size"' in body


def _form_fields(request: httpx.Request) -> dict[str, bytes]:
    """从 multipart 请求中提取标量字段，用于两条链路的字段级对照。"""
    content_type = request.headers.get("content-type", "")
    assert "boundary=" in content_type, content_type
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"').encode()
    fields: dict[str, bytes] = {}
    for part in request.content.split(b"--" + boundary):
        if b'name="' not in part or b"\r\n\r\n" not in part:
            continue
        name = part.split(b'name="', 1)[1].split(b'"', 1)[0].decode("utf-8")
        value = part.split(b"\r\n\r\n", 1)[1]
        if value.endswith(b"\r\n"):
            value = value[:-2]
        fields[name] = value
    return fields


def test_generate_and_submit_share_same_prepared_fields(tmp_path):
    bodies: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/videos"):
            bodies.append(request)
            return httpx.Response(200, json={"id": "job-1"})
        if request.method == "GET" and request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"video-bytes")
        if request.method == "GET" and "job-1" in request.url.path:
            return httpx.Response(
                200,
                json={"id": "job-1", "status": "completed", "url": "http://mock/content"},
            )
        return httpx.Response(404)

    client = CustomVideoClient(_meta(), poll_interval=0.001, poll_timeout=1, timeout=1)
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    inputs = dict(
        prompt="demo",
        image_path=None,
        duration=1,
        video_ratio="9:16",
        resolution="768P",
    )
    try:
        client.generate_video(save_path=str(tmp_path / "a.mp4"), **inputs)
        client.submit_job(save_path=str(tmp_path / "b.mp4"), **inputs)
    finally:
        client._client.close()

    assert len(bodies) == 2
    assert _form_fields(bodies[0]) == _form_fields(bodies[1])


def test_managed_client_rejects_missing_job_context(tmp_path):
    grant = default_grant_service().issue(
        job_id="job-1",
        attempt_id="attempt-1",
        instance_id="i1",
        resource_key="gpu-1",
        ttl_seconds=60,
    )
    meta = _managed_meta()
    meta.pop("dispatch_job_id")
    client = CustomVideoClient(meta, poll_interval=0.001, poll_timeout=1, timeout=1)
    try:
        with pytest.raises(GrantError, match="dispatch_job_id"):
            client.generate_video(
                prompt="demo",
                image_path=None,
                save_path=str(tmp_path / "x.mp4"),
                duration=4,
                dispatch_grant=grant,
                dispatch_job_id="job-1",
            )
    finally:
        client._client.close()


def test_managed_client_rejects_attempt_job_mismatch(tmp_path):
    grant = default_grant_service().issue(
        job_id="job-1",
        attempt_id="attempt-1",
        instance_id="i1",
        resource_key="gpu-1",
        ttl_seconds=60,
    )
    client = CustomVideoClient(_managed_meta(), poll_interval=0.001, poll_timeout=1, timeout=1)
    try:
        with pytest.raises(GrantError, match="不一致"):
            client.generate_video(
                prompt="demo",
                image_path=None,
                save_path=str(tmp_path / "x.mp4"),
                duration=4,
                dispatch_grant=grant,
                dispatch_job_id="job-2",
            )
    finally:
        client._client.close()


def test_generate_and_submit_share_reference_and_audio_fields(tmp_path):
    image = tmp_path / "ref.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    video = tmp_path / "ref.mp4"
    video.write_bytes(b"video-bytes")

    meta = _meta()
    meta["capabilities"] = {"tasks": ["ref2va"], "duration": {"min": 4, "max": 15}}
    bodies: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/videos"):
            bodies.append(request)
            return httpx.Response(200, json={"id": "job-1"})
        if request.method == "GET" and request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"video-bytes")
        if request.method == "GET" and "job-1" in request.url.path:
            return httpx.Response(
                200,
                json={"id": "job-1", "status": "completed", "url": "http://mock/content"},
            )
        return httpx.Response(404)

    client = CustomVideoClient(meta, poll_interval=0.001, poll_timeout=1, timeout=1)
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    inputs = dict(
        prompt="demo",
        image_path=None,
        last_image_path=None,
        reference_image_paths=[str(image)],
        reference_video_paths=[str(video)],
        audio_reference_url="https://example.com/ref.wav",
        duration=4,
        resolution="768P",
    )
    try:
        client.generate_video(save_path=str(tmp_path / "a.mp4"), **inputs)
        client.submit_job(save_path=str(tmp_path / "b.mp4"), **inputs)
    finally:
        client._client.close()

    assert len(bodies) == 2
    assert _form_fields(bodies[0]) == _form_fields(bodies[1])


def test_submit_job_warns_on_legacy_fields(tmp_path, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"id": "job-1"})
        return httpx.Response(404)

    client = CustomVideoClient(_meta(), poll_interval=0.001, poll_timeout=1, timeout=1)
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    try:
        with caplog.at_level(logging.WARNING):
            client.submit_job(
                prompt="demo",
                image_path=None,
                save_path=str(tmp_path / "x.mp4"),
                duration=4,
                reference_image_path="/tmp/legacy.png",
            )
    finally:
        client._client.close()
    assert "ignored legacy fields" in caplog.text
    assert "reference_image_path" in caplog.text


def test_async_submit_never_calls_sync(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"id": "job-1"})
        return httpx.Response(404)

    client = CustomVideoClient(
        _meta(api_mode="sync"), poll_interval=0.001, poll_timeout=1, timeout=1
    )
    monkeypatch.setattr(
        client,
        "_try_sync_generate",
        lambda **kwargs: pytest.fail("submit_job 不得反向触发 sync"),
    )
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    try:
        client.submit_job(
            prompt="demo",
            image_path=None,
            save_path=str(tmp_path / "x.mp4"),
            duration=4,
        )
    finally:
        client._client.close()
    assert calls == [("POST", "/v1/videos")]


def test_managed_client_accepts_dispatcher_grant(tmp_path):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"id": "job-1"})
        if request.method == "GET" and request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"video-bytes")
        if request.method == "GET" and "job-1" in request.url.path:
            return httpx.Response(
                200,
                json={"id": "job-1", "status": "completed", "url": "http://mock/content"},
            )
        return httpx.Response(404)

    grant = default_grant_service().issue(
        job_id="job-1",
        attempt_id="attempt-1",
        instance_id="i1",
        resource_key="gpu-1",
        ttl_seconds=60,
    )
    client = CustomVideoClient(_managed_meta(), poll_interval=0.001, poll_timeout=1, timeout=1)
    client._client = httpx.Client(base_url="http://mock/v1/", transport=httpx.MockTransport(handler))
    target = tmp_path / "managed.mp4"
    try:
        client.generate_video(
            prompt="demo",
            image_path=None,
            save_path=str(target),
            duration=4,
            dispatch_grant=grant,
            dispatch_job_id="job-1",
        )
        assert target.read_bytes() == b"video-bytes"
        assert calls.count(("POST", "/v1/videos")) == 1
    finally:
        client._client.close()


def test_managed_client_rejects_mismatched_grant(tmp_path):
    grant = default_grant_service().issue(
        job_id="job-1",
        attempt_id="attempt-1",
        instance_id="i1",
        resource_key="gpu-1",
        ttl_seconds=60,
    )
    client = CustomVideoClient(
        _managed_meta(managed_resource_key="gpu-2"),
        poll_interval=0.001,
        poll_timeout=1,
        timeout=1,
    )
    try:
        with pytest.raises(GrantError):
            client.generate_video(
                prompt="demo",
                image_path=None,
                save_path=str(tmp_path / "x.mp4"),
                duration=4,
                dispatch_grant=grant,
                dispatch_job_id="job-1",
            )
    finally:
        client._client.close()
