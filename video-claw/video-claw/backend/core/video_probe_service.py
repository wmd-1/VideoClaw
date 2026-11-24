"""视频实例探针服务：有界 HTTP 观测 + singleflight/限频/退避。

本模块是 ``ProbeScheduler`` 的生产调用方：真实调用 vLLM-Omni 的作业分页与
SGLang diffusion metrics，把结果转成 ``ObservationSample`` 供调度器分档准入。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from core.video_instance_admission import ObservationSample
from core.video_instance_probe import (
    PROBE_BUDGET_SECONDS,
    ProbeObservation,
    ProbeScheduler,
    observe_sglang_metrics,
    scan_vllm_jobs,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


def _reserved_host(url: str) -> bool:
    try:
        host = urlsplit(str(url or "")).hostname
    except ValueError:
        return False
    if not host:
        return False
    normalized = host.lower().rstrip(".")
    return normalized == "example" or normalized.endswith(".example")


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


class ProbeService:
    """一个进程一个实例；同实例 HTTP 观测 singleflight，失败按 ProbeScheduler 退避。"""

    def __init__(
        self,
        *,
        client: Optional[httpx.Client] = None,
        scheduler: Optional[ProbeScheduler] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        budget_seconds: float = PROBE_BUDGET_SECONDS,
    ) -> None:
        self._client = client or httpx.Client(timeout=budget_seconds)
        self._owns_client = client is None
        self._scheduler = scheduler or ProbeScheduler(budget_seconds=budget_seconds)
        self._clock = clock
        self._wall = wall_clock
        self._lock = threading.Lock()
        self._last: Dict[str, ProbeObservation] = {}
        self._last_epoch: Dict[str, float] = {}
        self._last_state: Dict[str, str] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ── 对外观测接口 ──

    def probe(
        self,
        instance: Any,
        *,
        on_demand: bool = False,
        waiting: bool = False,
    ) -> Optional[ProbeObservation]:
        instance_id = str(getattr(instance, "instance_id", "") or "")
        if not instance_id:
            return None
        now = self._clock()
        with self._lock:
            ticket = self._scheduler.try_begin(
                instance_id, now=now, waiting=waiting, on_demand=on_demand
            )
            cached = self._last.get(instance_id)
        if ticket is None:
            return cached
        observation = self._run(instance, ticket.deadline)
        with self._lock:
            self._scheduler.finish(
                ticket,
                success=observation.state not in ("unknown", "unreachable"),
                now=self._clock(),
            )
            observed_at = self._wall()
            self._scheduler.record_observation(
                instance_id, observation.state, observed_at
            )
            self._last[instance_id] = observation
            self._last_epoch[instance_id] = observed_at
            self._last_state[instance_id] = observation.state
        return observation

    def last_observation(self, instance_id: str) -> Optional[tuple[ProbeObservation, float]]:
        with self._lock:
            observation = self._last.get(str(instance_id))
            epoch = self._last_epoch.get(str(instance_id))
        if observation is None or epoch is None:
            return None
        return observation, epoch

    def observe_shared(self, instance: Any) -> Optional[ObservationSample]:
        observation = self.probe(instance, on_demand=True)
        if observation is None:
            return None
        instance_id = str(getattr(instance, "instance_id", "") or "")
        with self._lock:
            epoch = self._last_epoch.get(instance_id)
        if epoch is None:
            return None
        return ObservationSample(observation.state, epoch)

    def observe_exclusive(self, instance: Any) -> Optional[ObservationSample]:
        """独占只消费近期可靠 busy；unknown/stale 不作为派发闸门。"""
        instance_id = str(getattr(instance, "instance_id", "") or "")
        with self._lock:
            state = self._last_state.get(instance_id)
            epoch = self._last_epoch.get(instance_id)
        if state == "busy" and epoch is not None and self._wall() - epoch <= 15:
            return ObservationSample("busy", epoch)
        return None

    def tick(
        self,
        instances: Iterable[Any],
        *,
        waiting: bool = False,
        max_workers: int = 4,
    ) -> Dict[str, ProbeObservation]:
        """并发探测本轮实例，避免单池多成员把派发 loop 串行拖慢。"""
        targets = [item for item in instances if getattr(item, "enabled", True)]
        results: Dict[str, ProbeObservation] = {}
        if not targets:
            return results
        workers = max(1, min(int(max_workers), len(targets)))

        def observe(instance: Any) -> tuple[str, Any]:
            return (
                str(getattr(instance, "instance_id", "")),
                self.probe(instance, waiting=waiting),
            )

        if workers == 1:
            for instance in targets:
                instance_id, observation = observe(instance)
                if observation is not None:
                    results[instance_id] = observation
            return results
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(observe, instance) for instance in targets]
            for future in as_completed(futures):
                instance_id, observation = future.result()
                if observation is not None:
                    results[instance_id] = observation
        return results

    # ── 实际 HTTP 观测 ──

    def _run(self, instance: Any, deadline: float) -> ProbeObservation:
        probe = getattr(instance, "probe", None) or {}
        if not isinstance(probe, Mapping):
            probe = {}
        kind = str(probe.get("kind") or "")
        if kind == "vllm_omni":
            return self._probe_vllm(probe, deadline)
        if kind == "sglang_diffusion":
            return self._probe_sglang(probe, deadline)
        if kind == "custom":
            return ProbeObservation(
                "unknown", utc_now_iso(), "none", {"reason": "custom_probe_not_declared"}
            )
        return ProbeObservation(
            "unknown", utc_now_iso(), "none", {"reason": "unknown_probe_kind"}
        )

    def _remaining(self, deadline: float) -> float:
        return max(0.1, float(deadline) - self._clock())

    def _probe_vllm(
        self, probe: Mapping[str, Any], deadline: float
    ) -> ProbeObservation:
        jobs_url = str(probe.get("jobs_url") or "")
        if not jobs_url or _reserved_host(jobs_url):
            return ProbeObservation(
                "unknown", utc_now_iso(), "none", {"reason": "placeholder_or_missing_url"}
            )

        def fetch_page(after: Optional[str]):
            params: Dict[str, Any] = {"limit": 100}
            if after:
                params["after"] = after
            try:
                response = self._client.get(
                    jobs_url, params=params, timeout=self._remaining(deadline)
                )
            except Exception as exc:  # noqa: BLE001 - 探针失败必须转为 unknown
                return 0, {"error": type(exc).__name__}
            return response.status_code, _json_or_none(response)

        return scan_vllm_jobs(fetch_page)

    def _probe_sglang(
        self, probe: Mapping[str, Any], deadline: float
    ) -> ProbeObservation:
        metrics_url = str(probe.get("metrics_url") or "")
        if not metrics_url or _reserved_host(metrics_url):
            return ProbeObservation(
                "unknown", utc_now_iso(), "none", {"reason": "placeholder_or_missing_url"}
            )
        labels = probe.get("labels") if isinstance(probe.get("labels"), Mapping) else {}
        role = str(labels.get("role") or "")
        replica = str(labels.get("replica") or "")
        try:
            response = self._client.get(
                metrics_url, timeout=self._remaining(deadline)
            )
        except Exception as exc:  # noqa: BLE001 - 网络失败不是 idle
            return ProbeObservation(
                "unreachable",
                utc_now_iso(),
                "none",
                {"reason": type(exc).__name__},
            )
        return observe_sglang_metrics(
            response.status_code,
            response.text,
            role=role,
            replica=replica,
        )


__all__ = ["ProbeService", "_reserved_host"]
