from types import SimpleNamespace

from core.video_dispatch_service import RuntimeInstance
from core.video_dispatch_transport import build_custom_video_transport
from core.video_job_lifecycle import SUBMIT_ACCEPTED, SUBMIT_UNKNOWN


class FakeClient:
    def __init__(self, *, fail_submit=None, status="completed"):
        self.fail_submit = fail_submit
        self.status = status
        self.payload = None
        self.download_args = None
        self.cancelled = None

    def submit_job(self, **kwargs):
        if self.fail_submit is not None:
            raise self.fail_submit
        self.payload = kwargs
        return {"job_id": "remote-1", "status": "queued"}

    def _extract_job_id(self, payload):
        return payload["job_id"]

    def get_job(self, remote_id):
        return {"job_id": remote_id, "status": self.status}

    def download_job(self, remote_id, save_path, url):
        self.download_args = (remote_id, save_path, url)
        with open(save_path, "wb") as handle:
            handle.write(b"video")

    def cancel_job(self, remote_id):
        self.cancelled = remote_id

    def close(self):
        self.closed = True


def _instance():
    return RuntimeInstance(
        instance_id="i1",
        model_id="vllm-model",
        resource_key="gpu-1",
        access_mode="exclusive",
        enabled=True,
        driver="vllm-omni",
    )


def _job(tmp_path):
    return {
        "job_id": "job-1",
        "snapshot_json": {
            "request": {
                "prompt": "p",
                "save_path": str(tmp_path / "out.mp4"),
                "image_path": "/tmp/in.png",
                "duration": 5,
                "video_ratio": "16:9",
            }
        },
    }


def _grant():
    return SimpleNamespace(job_id="job-1")


def test_submit_injects_attempt_job_and_managed_meta(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "models.config_model.resolve_model_entry",
        lambda model: ("custom", {"provider": "p", "model": "H3"}),
    )
    captured = {}

    def factory(meta):
        captured["meta"] = meta
        return FakeClient()

    transport = build_custom_video_transport(_instance(), _job(tmp_path), client_factory=factory)
    outcome = transport.submit(job=_job(tmp_path), instance=_instance(), grant=_grant())
    assert outcome.status == SUBMIT_ACCEPTED
    assert outcome.remote_job_id == "remote-1"
    assert captured["meta"]["dispatch_job_id"] == "job-1"
    assert captured["meta"]["managed"] is True


def test_unknown_submit_returns_unknown_without_second_post(monkeypatch, tmp_path):
    from models.custom_video import VideoSubmissionUnknown

    monkeypatch.setattr(
        "models.config_model.resolve_model_entry",
        lambda model: ("custom", {"provider": "p", "model": "H3"}),
    )
    posts = []

    class Client(FakeClient):
        def submit_job(self, **kwargs):
            posts.append(1)
            raise VideoSubmissionUnknown("timeout")

    transport = build_custom_video_transport(
        _instance(), _job(tmp_path), client_factory=lambda meta: Client()
    )
    outcome = transport.submit(job=_job(tmp_path), instance=_instance(), grant=_grant())
    assert outcome.status == SUBMIT_UNKNOWN
    assert len(posts) == 1


def test_poll_download_and_cancel(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "models.config_model.resolve_model_entry",
        lambda model: ("custom", {"provider": "p", "model": "H3"}),
    )
    client = FakeClient()
    transport = build_custom_video_transport(
        _instance(), _job(tmp_path), client_factory=lambda meta: client
    )
    attempt = {"remote_job_id": "remote-1", "remote_url": "http://v"}
    assert transport.poll(attempt=attempt, instance=_instance(), job=_job(tmp_path)).state == "success"
    result = transport.download(attempt=attempt, instance=_instance(), job=_job(tmp_path))
    assert result["save_path"] == str(tmp_path / "out.mp4")
    assert open(result["save_path"], "rb").read() == b"video"
    assert transport.cancel(attempt=attempt, instance=_instance(), job=_job(tmp_path)) is True
    assert client.cancelled == "remote-1"

def test_transport_uses_real_custom_video_client(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setattr(
        "models.config_model.resolve_model_entry",
        lambda model: (
            "custom",
            {
                "id": model,
                "provider": "p",
                "protocol": "vllm-omni",
                "model": "H3",
                "base_url": "http://video.internal",
            },
        ),
    )
    image = tmp_path / "in.png"
    image.write_bytes(b"png")
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(200, json={"id": "remote-real", "status": "queued"})

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), base_url=kwargs["base_url"])

    monkeypatch.setattr("models.custom_video.make_http_client", factory)
    job = _job(tmp_path)
    job["snapshot_json"]["request"]["image_path"] = str(image)
    from core.video_generation_service import default_grant_service

    transport = build_custom_video_transport(_instance(), job)
    grant = default_grant_service().issue(
        job_id="job-1",
        attempt_id="attempt-1",
        instance_id="i1",
        resource_key="gpu-1",
    )
    outcome = transport.submit(job=job, instance=_instance(), grant=grant)
    assert outcome.status == SUBMIT_ACCEPTED
    assert outcome.remote_job_id == "remote-real"
    assert len(posts) == 1
    assert posts[0].url.path.endswith("/videos")

def test_cancel_accepted_but_not_drained_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "models.config_model.resolve_model_entry",
        lambda model: ("custom", {"provider": "p", "model": "H3"}),
    )
    client = FakeClient(status="running")
    transport = build_custom_video_transport(
        _instance(), _job(tmp_path), client_factory=lambda meta: client
    )
    attempt = {"remote_job_id": "remote-1"}
    assert transport.cancel(attempt=attempt, instance=_instance(), job=_job(tmp_path)) is False


def test_real_client_download_without_remote_url(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setattr(
        "models.config_model.resolve_model_entry",
        lambda model: (
            "custom",
            {
                "id": model,
                "provider": "p",
                "protocol": "vllm-omni",
                "model": "H3",
                "base_url": "http://video.internal",
            },
        ),
    )
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, content=b"downloaded")

    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), base_url=kwargs["base_url"])

    monkeypatch.setattr("models.custom_video.make_http_client", factory)
    job = _job(tmp_path)
    job["snapshot_json"]["request"]["save_path"] = str(tmp_path / "out.mp4")
    transport = build_custom_video_transport(_instance(), job)
    result = transport.download(
        attempt={"remote_job_id": "remote-1", "remote_url": ""},
        instance=_instance(),
        job=job,
    )
    assert result["save_path"] == str(tmp_path / "out.mp4")
    assert open(result["save_path"], "rb").read() == b"downloaded"
    assert seen == ["/videos/remote-1/content"]
