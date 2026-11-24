"""有界的视频实例忙闲解析器（不直接发 HTTP）。

- vLLM-Omni：解析单作业状态、列表分页与扫描完整性；
- SGLang diffusion：解析 Prometheus 文本中的 running/queue 指标；
- 观测只保留状态、数量、时间和覆盖范围，不保留外部 prompt。

HTTP、重试、singleflight 由上层 probe 服务负责；本模块保持纯函数，便于虚拟时钟
和异常响应单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.video_instance_admission import ObservationSample

VLLM_TERMINAL = {"completed", "failed"}
VLLM_BUSY = {"queued", "in_progress"}
VLLM_KNOWN = VLLM_TERMINAL | VLLM_BUSY

SGLANG_RUNNING_METRIC = "sglang:diffusion_num_running_reqs"
SGLANG_QUEUE_METRIC = "sglang:diffusion_num_queue_reqs"


@dataclass(frozen=True)
class JobState:
    job_id: Optional[str]
    state: str  # queued/in_progress/completed/failed/unknown
    terminal: bool
    reason: str = ""


@dataclass(frozen=True)
class ProbeObservation:
    state: str  # idle/busy/unknown/unreachable
    observed_at: Optional[str]
    coverage: str  # complete/partial/none
    detail: Dict[str, Any] = field(default_factory=dict)


PROBE_BUDGET_SECONDS = 3.0
PROBE_WAITING_INTERVAL = 5.0
PROBE_IDLE_INTERVAL = 30.0
PROBE_BACKOFF_SECONDS = (15.0, 30.0, 60.0)


@dataclass(frozen=True)
class ProbeTicket:
    instance_id: str
    deadline: float
    on_demand: bool = False


@dataclass
class _ProbeState:
    in_flight: bool = False
    last_attempt_at: Optional[float] = None
    last_on_demand_at: Optional[float] = None
    failures: int = 0
    next_allowed_at: float = 0.0
    override_used: bool = False
    observations: List[ObservationSample] = field(default_factory=list)


class ProbeScheduler:
    """单实例 singleflight、周期/按需限频与失败退避（虚拟时钟可测）。"""

    def __init__(self, *, budget_seconds: float = PROBE_BUDGET_SECONDS) -> None:
        self.budget_seconds = budget_seconds
        self._states: Dict[str, _ProbeState] = {}

    def try_begin(
        self,
        instance_id: str,
        *,
        now: float,
        waiting: bool,
        on_demand: bool = False,
    ) -> Optional[ProbeTicket]:
        state = self._states.setdefault(instance_id, _ProbeState())
        if state.in_flight:
            return None
        interval = PROBE_WAITING_INTERVAL if waiting else PROBE_IDLE_INTERVAL
        last = state.last_attempt_at
        if on_demand:
            if last is not None and now - last < PROBE_WAITING_INTERVAL:
                return None
            if state.last_on_demand_at is not None and now - state.last_on_demand_at < PROBE_WAITING_INTERVAL:
                return None
            if now < state.next_allowed_at:
                if state.override_used:
                    return None
                state.override_used = True
            state.last_on_demand_at = now
        else:
            if last is not None and now - last < interval:
                return None
            if now < state.next_allowed_at:
                return None
            state.override_used = False
        state.in_flight = True
        return ProbeTicket(instance_id, now + self.budget_seconds, on_demand)

    def finish(self, ticket: ProbeTicket, *, success: bool, now: float) -> None:
        state = self._states.setdefault(ticket.instance_id, _ProbeState())
        state.in_flight = False
        state.last_attempt_at = now
        if success:
            state.failures = 0
            state.next_allowed_at = now
            state.override_used = False
            return
        state.failures += 1
        backoff = PROBE_BACKOFF_SECONDS[min(state.failures - 1, len(PROBE_BACKOFF_SECONDS) - 1)]
        state.next_allowed_at = now + backoff
        # 按需提前检查失败后，这个退避窗口不再接受第二次“点击绕过”；
        # 周期检查失败则保留一次有限优先的按需提前检查机会。
        state.override_used = bool(ticket.on_demand)

    def record_observation(self, instance_id: str, state: str, observed_at: float) -> bool:
        """同一时间戳的观测只记录一次，避免同一次缓存被当成两次独立 idle。"""
        item = self._states.setdefault(instance_id, _ProbeState())
        if any(abs(sample.observed_at - observed_at) < 1e-9 for sample in item.observations):
            return False
        item.observations.append(ObservationSample(state, observed_at))
        item.observations.sort(key=lambda sample: sample.observed_at)
        if len(item.observations) > 20:
            item.observations = item.observations[-20:]
        return True

    def recent_observations(
        self,
        instance_id: str,
        *,
        now: float,
        max_age: float = 15.0,
    ) -> List[ObservationSample]:
        item = self._states.get(instance_id)
        if item is None:
            return []
        return [
            sample
            for sample in item.observations
            if 0 <= now - sample.observed_at <= max_age
        ]

    def in_flight(self, instance_id: str) -> bool:
        item = self._states.get(instance_id)
        return bool(item and item.in_flight)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_vllm_job_response(status_code: int, payload: Any) -> JobState:
    """解析单作业响应；非 2xx 但带结构化 failed 状态时仍按作业失败解释。"""
    if not isinstance(payload, dict):
        return JobState(None, "unknown", False, f"http_{status_code}_invalid_body")
    job_id = str(payload.get("id") or "") or None
    raw_status = str(payload.get("status") or "").strip().lower()
    if status_code >= 400:
        if raw_status in VLLM_TERMINAL:
            return JobState(job_id, raw_status, True, "structured_failed")
        return JobState(job_id, "unknown", False, f"http_{status_code}")
    if raw_status not in VLLM_KNOWN:
        return JobState(job_id, "unknown", False, "unknown_status")
    return JobState(job_id, raw_status, raw_status in VLLM_TERMINAL)


def scan_vllm_jobs(
    fetch_page: Callable[[Optional[str]], Tuple[int, Any]],
    *,
    page_limit: int = 100,
    max_pages: int = 50,
    observed_at: Optional[str] = None,
) -> ProbeObservation:
    """完整扫描异步列表；分页异常、超预算或缺游标统一返回 unknown。

    ``fetch_page(after)`` 返回 ``(status_code, payload)``。只读取 id/status/分页字段，
    不保存 prompt、媒体或错误正文。
    """
    now = observed_at or utc_now_iso()
    after: Optional[str] = None
    pages = 0
    seen_ids: set[str] = set()
    terminal = 0
    busy = 0
    unknown = 0
    malformed = False

    while pages < max_pages:
        pages += 1
        try:
            status_code, payload = fetch_page(after)
        except Exception:  # noqa: BLE001 - 探针失败必须转为 unknown，不能打断调度
            return ProbeObservation(
                "unknown", now, "partial" if pages > 1 else "none",
                {"pages": pages, "reason": "fetch_failed"},
            )
        if status_code != 200 or not isinstance(payload, dict):
            return ProbeObservation(
                "unknown", now, "partial" if pages > 1 else "none",
                {"pages": pages, "reason": f"http_{status_code}"},
            )
        data = payload.get("data")
        has_more = payload.get("has_more")
        last_id = payload.get("last_id")
        if not isinstance(data, list) or not isinstance(has_more, bool):
            return ProbeObservation(
                "unknown", now, "partial" if pages > 1 else "none",
                {"pages": pages, "reason": "malformed_page"},
            )

        for item in data:
            job = parse_vllm_job_response(200, item)
            if job.job_id is None:
                malformed = True
                continue
            if job.job_id in seen_ids:
                malformed = True
                continue
            seen_ids.add(job.job_id)
            if job.state == "completed":
                terminal += 1
            elif job.state == "failed":
                terminal += 1
            elif job.state in VLLM_BUSY:
                busy += 1
            else:
                unknown += 1

        if not has_more:
            break
        if not last_id:
            return ProbeObservation(
                "unknown", now, "partial",
                {"pages": pages, "reason": "missing_cursor", "jobs": len(seen_ids)},
            )
        if last_id in seen_ids and last_id == after:
            return ProbeObservation(
                "unknown", now, "partial",
                {"pages": pages, "reason": "repeated_cursor", "jobs": len(seen_ids)},
            )
        after = str(last_id)
    else:
        return ProbeObservation(
            "unknown", now, "partial",
            {"pages": pages, "reason": "page_budget", "jobs": len(seen_ids)},
        )

    if malformed or unknown:
        state = "unknown"
        reason = "malformed_or_unknown_job" if malformed else "unknown_job_status"
    elif busy:
        state = "busy"
        reason = ""
    else:
        state = "idle"
        reason = ""
    detail: Dict[str, Any] = {
        "jobs": len(seen_ids),
        "terminal": terminal,
        "busy": busy,
        "unknown": unknown,
        "pages": pages,
    }
    if reason:
        detail["reason"] = reason
    return ProbeObservation(state, now, "complete", detail)


def parse_sglang_diffusion_metrics(
    text: str,
    *,
    role: str,
    replica: str,
) -> Optional[Dict[str, float]]:
    """解析 diffusion running/queue；缺序列、重复、NaN、负值或标签不符返回 None。"""
    if not isinstance(text, str) or not text.strip() or text.lstrip().startswith("<"):
        return None
    wanted = {SGLANG_RUNNING_METRIC, SGLANG_QUEUE_METRIC}
    found: Dict[str, float] = {}
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _parse_metric_line(line)
        if parsed is None:
            continue
        name, labels, value = parsed
        if name not in wanted:
            continue
        if labels.get("role") != role or labels.get("replica") != replica:
            continue
        if name in seen:
            return None
        seen.add(name)
        found[name] = value

    if set(found) != wanted:
        return None
    if any(value < 0 for value in found.values()):
        return None
    return {
        "running": found[SGLANG_RUNNING_METRIC],
        "queued": found[SGLANG_QUEUE_METRIC],
    }


def observe_sglang_metrics(
    status_code: int,
    text: Any,
    *,
    role: str,
    replica: str,
    observed_at: Optional[str] = None,
) -> ProbeObservation:
    """把 metrics 响应转换为观测；404/HTML/缺指标不视为空闲。"""
    now = observed_at or utc_now_iso()
    if status_code != 200:
        return ProbeObservation("unreachable", now, "none", {"reason": f"http_{status_code}"})
    metrics = parse_sglang_diffusion_metrics(str(text or ""), role=role, replica=replica)
    if metrics is None:
        return ProbeObservation("unknown", now, "none", {"reason": "metrics_unavailable"})
    running = int(metrics["running"])
    queued = int(metrics["queued"])
    state = "busy" if running > 0 or queued > 0 else "idle"
    return ProbeObservation(
        state,
        now,
        "complete",
        {"running": running, "queued": queued},
    )


def _parse_metric_line(line: str) -> Optional[Tuple[str, Dict[str, str], float]]:
    """解析 ``name{labels} value [timestamp]``；非法/NaN/Inf 返回 None。"""
    brace = line.find("{")
    if brace >= 0:
        close = _find_label_end(line, brace + 1)
        if close is None:
            return None
        name = line[:brace].strip()
        label_text = line[brace + 1 : close]
        rest = line[close + 1 :].strip()
        labels = _parse_labels(label_text)
        if labels is None:
            return None
    else:
        parts = line.split()
        if len(parts) < 2:
            return None
        name = parts[0]
        labels = {}
        rest = " ".join(parts[1:])
    if not name:
        return None
    value_token = rest.split()[0] if rest else ""
    try:
        value = float(value_token)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return name, labels, value


def _find_label_end(line: str, start: int) -> Optional[int]:
    escaped = False
    in_quote = False
    for index in range(start, len(line)):
        char = line[index]
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_quote:
            escaped = True
            continue
        if char == '"':
            in_quote = not in_quote
            continue
        if char == "}" and not in_quote:
            return index
    return None


def _parse_labels(text: str) -> Optional[Dict[str, str]]:
    labels: Dict[str, str] = {}
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t,":
            index += 1
        if index >= length:
            break
        eq = text.find("=", index)
        if eq < 0:
            return None
        key = text[index:eq].strip()
        if not key:
            return None
        cursor = eq + 1
        while cursor < length and text[cursor] in " \t":
            cursor += 1
        if cursor >= length or text[cursor] != '"':
            return None
        cursor += 1
        value_chars: List[str] = []
        escaped = False
        while cursor < length:
            char = text[cursor]
            if escaped:
                value_chars.append({"n": "\n", "\\": "\\", '"': '"'}.get(char, char))
                escaped = False
                cursor += 1
                continue
            if char == "\\":
                escaped = True
                cursor += 1
                continue
            if char == '"':
                break
            value_chars.append(char)
            cursor += 1
        else:
            return None
        if cursor >= length or text[cursor] != '"':
            return None
        labels[key] = "".join(value_chars)
        index = cursor + 1
        while index < length and text[index] in " \t":
            index += 1
        if index < length and text[index] == ",":
            index += 1
            continue
        if index < length:
            return None
    return labels


__all__ = [
    "JobState",
    "PROBE_BACKOFF_SECONDS",
    "PROBE_BUDGET_SECONDS",
    "PROBE_IDLE_INTERVAL",
    "PROBE_WAITING_INTERVAL",
    "ProbeObservation",
    "ProbeScheduler",
    "ProbeTicket",
    "SGLANG_QUEUE_METRIC",
    "SGLANG_RUNNING_METRIC",
    "VLLM_BUSY",
    "VLLM_TERMINAL",
    "observe_sglang_metrics",
    "parse_sglang_diffusion_metrics",
    "parse_vllm_job_response",
    "scan_vllm_jobs",
    "utc_now_iso",
]
