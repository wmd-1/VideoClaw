"""exclusive/shared 实例的纯准入策略。

不发起探测、不读写数据库；输入是已核对的资格/启动证据与近期的观测样本，
输出是可直接用于调度决策的 allowed/action/reason。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

ACCESS_MODES = ("exclusive", "shared")
OBSERVED_STATES = ("idle", "busy", "unknown", "unreachable", "mixed")


@dataclass(frozen=True)
class ObservationSample:
    state: str
    observed_at: float


@dataclass(frozen=True)
class AdmissionInput:
    access_mode: str
    enabled: bool
    qualified: bool
    boot_verified: bool
    local_slot_free: bool
    now: float
    pending_attempt: bool = False
    quarantined: bool = False
    observations: Tuple[ObservationSample, ...] = field(default_factory=tuple)
    final_confirmation: Optional[ObservationSample] = None
    shared_min_gap: float = 5.0
    shared_max_age: float = 15.0


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    action: str  # dispatch/wait/quarantine/deny
    reason: str


def evaluate_admission(payload: AdmissionInput) -> AdmissionDecision:
    """按 access_mode 分档判定；unknown 对 exclusive 仅展示，对 shared 阻止提交。"""
    mode = str(payload.access_mode or "").strip().lower()
    if mode not in ACCESS_MODES:
        return AdmissionDecision(False, "deny", "invalid_access_mode")
    if not payload.enabled:
        return AdmissionDecision(False, "deny", "instance_disabled")
    if payload.quarantined:
        return AdmissionDecision(False, "deny", "quarantined")
    if not payload.qualified:
        return AdmissionDecision(False, "deny", "not_qualified")
    if not payload.boot_verified:
        return AdmissionDecision(False, "deny", "boot_unverified")
    if payload.pending_attempt:
        return AdmissionDecision(False, "deny", "pending_attempt")
    if not payload.local_slot_free:
        return AdmissionDecision(False, "wait", "slot_busy")

    if mode == "exclusive":
        return _evaluate_exclusive(payload)
    return _evaluate_shared(payload)


def _evaluate_exclusive(payload: AdmissionInput) -> AdmissionDecision:
    """独占正常态不等待普通探针；近期可靠 busy 与空账本矛盾时隔离核对。"""
    fresh_busy = [
        item
        for item in payload.observations
        if item.state == "busy" and _age(item, payload.now) <= payload.shared_max_age
    ]
    if fresh_busy:
        return AdmissionDecision(False, "quarantine", "exclusive_ledger_conflict")
    # unknown/unreachable/stale 只作为健康展示，不作为派发闸门。
    return AdmissionDecision(True, "dispatch", "exclusive_ok")


def _evaluate_shared(payload: AdmissionInput) -> AdmissionDecision:
    fresh = [
        item
        for item in payload.observations
        if _age(item, payload.now) <= payload.shared_max_age and item.state != "mixed"
    ]
    if any(item.state in {"busy", "unreachable"} for item in fresh):
        return AdmissionDecision(False, "wait", "observation_busy_or_unreachable")
    if any(item.state == "unknown" for item in fresh):
        return AdmissionDecision(False, "wait", "observation_unknown")

    idles = sorted(
        (item for item in fresh if item.state == "idle"),
        key=lambda item: item.observed_at,
    )
    if not _has_two_independent_idle(idles, payload.shared_min_gap):
        return AdmissionDecision(False, "wait", "need_two_idle")

    confirmation = payload.final_confirmation
    if confirmation is None or confirmation.state != "idle":
        return AdmissionDecision(False, "wait", "final_confirmation_missing")
    if _age(confirmation, payload.now) > payload.shared_max_age:
        return AdmissionDecision(False, "wait", "final_confirmation_stale")
    return AdmissionDecision(True, "dispatch", "shared_ok")


def _has_two_independent_idle(idles: list[ObservationSample], min_gap: float) -> bool:
    """存在一对独立 idle 即满足：取最早/最新样本判定间隔，不要求相邻样本。"""
    if len(idles) < 2:
        return False
    return idles[-1].observed_at - idles[0].observed_at >= min_gap


def _age(item: ObservationSample, now: float) -> float:
    return max(0.0, float(now) - float(item.observed_at))


__all__ = [
    "ACCESS_MODES",
    "AdmissionDecision",
    "AdmissionInput",
    "OBSERVED_STATES",
    "ObservationSample",
    "evaluate_admission",
]
