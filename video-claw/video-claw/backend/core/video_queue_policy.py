"""排队期限与池临时/显式不可用的纯策略。"""
from __future__ import annotations

from dataclasses import dataclass

WAITING = "waiting"
WAITING_POOL_UNAVAILABLE = "waiting_pool_unavailable"
QUEUE_TIMEOUT = "queue_timeout"
POOL_UNAVAILABLE = "pool_unavailable"


@dataclass(frozen=True)
class QueueDecision:
    state: str
    deadline_unchanged: bool = True


def evaluate_queued_job(
    *,
    now: float,
    queue_deadline_at: float,
    pool_online: bool,
    admin_offline: bool,
) -> QueueDecision:
    """临时不可用保留原期限；管理员显式下线才提前结束未派发项。"""
    if admin_offline:
        return QueueDecision(POOL_UNAVAILABLE)
    if float(now) >= float(queue_deadline_at):
        return QueueDecision(QUEUE_TIMEOUT)
    if not pool_online:
        return QueueDecision(WAITING_POOL_UNAVAILABLE)
    return QueueDecision(WAITING)


__all__ = [
    "POOL_UNAVAILABLE",
    "QUEUE_TIMEOUT",
    "QueueDecision",
    "WAITING",
    "WAITING_POOL_UNAVAILABLE",
    "evaluate_queued_job",
]
