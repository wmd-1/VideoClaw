"""ProbeService：真实 HTTP 解析、singleflight、周期/退避与占位拒绝。"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import httpx

from core.video_dispatch_service import SharedObservationLedger
from core.video_instance_probe import ProbeObservation
from core.video_probe_service import ProbeService


class _Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _instance(
    *,
    instance_id: str = "i1",
    kind: str = "vllm_omni",
    jobs_url: str = "http://video-01.internal:8000/v1/videos",
    metrics_url: str = "",
    labels: dict | None = None,
):
    return SimpleNamespace(
        instance_id=instance_id,
        enabled=True,
        probe={
            "kind": kind,
            "jobs_url": jobs_url,
            "metrics_url": metrics_url,
            "labels": labels or {},
        },
    )


def _service(handler, *, clock: _Clock | None = None) -> tuple[ProbeService, _Clock]:
    moment = clock or _Clock()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        ProbeService(client=client, clock=moment, wall_clock=moment),
        moment,
    )


def test_vllm_pagination_busy_on_later_page():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if "after" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "c1", "status": "completed"}],
                    "has_more": True,
                    "last_id": "c1",
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [{"id": "b1", "status": "in_progress"}],
                "has_more": False,
            },
        )

    service, _ = _service(handler)
    observation = service.probe(_instance())
    service.close()

    assert observation.state == "busy"
    assert len(calls) == 2
    assert calls[1]["after"] == "c1"


def test_sglang_metrics_busy_html_404_and_missing_are_not_idle():
    metrics = (
        'sglang:diffusion_num_running_reqs{role="decode",replica="0"} 1\n'
        'sglang:diffusion_num_queue_reqs{role="decode",replica="0"} 0\n'
    )

    def bushy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=metrics)

    service, _ = _service(bushy)
    instance = _instance(
        kind="sglang_diffusion",
        metrics_url="http://video-01.internal:30010/metrics",
        labels={"role": "decode", "replica": "0"},
    )
    assert service.probe(instance).state == "busy"
    service.close()

    for response in (
        httpx.Response(200, text="<html>not metrics</html>"),
        httpx.Response(404, text="missing"),
        httpx.Response(200, text=""),
    ):
        service, _ = _service(lambda request, resp=response: resp)
        observation = service.probe(instance)
        service.close()
        assert observation.state in ("unknown", "unreachable")
        assert observation.state != "idle"


def test_singleflight_only_one_http_call():
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        started.set()
        assert release.wait(2)
        return httpx.Response(200, json={"data": [], "has_more": False})

    service, _ = _service(handler)
    instance = _instance()
    results: dict = {}

    def worker() -> None:
        results["first"] = service.probe(instance)

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(2)
    second = service.probe(instance)
    release.set()
    thread.join()
    service.close()

    assert second is None
    assert len(calls) == 1


def test_waiting_cadence_and_idle_cadence_are_separate():
    calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1.0)
        return httpx.Response(200, json={"data": [], "has_more": False})

    service, clock = _service(handler)
    instance = _instance()
    service.probe(instance, waiting=True)
    clock.value += 4.9
    assert len(calls) == 1
    clock.value += 0.1
    service.probe(instance, waiting=True)
    assert len(calls) == 2
    service.close()

    calls.clear()
    service, clock = _service(handler)
    service.probe(instance, waiting=False)
    clock.value += 29.9
    assert len(calls) == 1
    clock.value += 0.1
    service.probe(instance, waiting=False)
    assert len(calls) == 2
    service.close()


def test_on_demand_is_limited_to_five_seconds():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"data": [], "has_more": False})

    service, clock = _service(handler)
    instance = _instance()
    service.probe(instance, waiting=False)
    clock.value += 3
    service.probe(instance, on_demand=True)
    assert len(calls) == 1
    clock.value += 2
    service.probe(instance, on_demand=True)
    assert len(calls) == 2
    service.close()


def test_failure_backoff_15_30_60():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, text="boom")

    service, clock = _service(handler)
    instance = _instance()
    service.probe(instance, waiting=True)  # unknown, next allowed +15
    clock.value += 14.9
    service.probe(instance, waiting=True)
    assert len(calls) == 1
    clock.value += 0.1
    service.probe(instance, waiting=True)  # +30
    assert len(calls) == 2
    clock.value += 29.9
    service.probe(instance, waiting=True)
    assert len(calls) == 2
    clock.value += 0.1
    service.probe(instance, waiting=True)  # +60
    assert len(calls) == 3
    service.close()


def test_placeholder_address_never_sends_http():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise AssertionError("占位地址不得访问")

    service, _ = _service(handler)
    instance = _instance(jobs_url="http://video.example/v1/videos")
    observation = service.probe(instance)
    service.close()

    assert observation.state == "unknown"
    assert calls == []


def test_cached_observation_cannot_forge_second_idle():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "has_more": False})

    service, clock = _service(handler, clock=_Clock(1000.0))
    instance = _instance()
    first = service.observe_shared(instance)
    clock.value += 1
    second = service.observe_shared(instance)
    service.close()

    assert first is not None and second is not None
    assert first.observed_at == second.observed_at
    ledger = SharedObservationLedger()
    ledger.add("i1", first)
    ledger.add("i1", second)
    assert len(ledger.recent("i1", now=clock.value)) == 1
    assert ledger.two_idle("i1", now=clock.value) is False

def test_tick_probes_pool_members_concurrently():
    import time

    service = ProbeService()
    instances = [_instance(instance_id=f"i{index}") for index in range(4)]

    def slow_probe(instance, *, waiting=False, on_demand=False):
        time.sleep(0.15)
        return ProbeObservation("idle", "now", "complete")

    service.probe = slow_probe
    try:
        started = time.monotonic()
        results = service.tick(instances)
        elapsed = time.monotonic() - started
    finally:
        service.close()
    assert set(results) == {"i0", "i1", "i2", "i3"}
    assert elapsed < 0.5
