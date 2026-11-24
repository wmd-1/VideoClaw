"""排队期限：临时不可用不重置 60 分钟，显式下线立即结束。"""
from __future__ import annotations

from core.video_queue_policy import (
    POOL_UNAVAILABLE,
    QUEUE_TIMEOUT,
    WAITING,
    WAITING_POOL_UNAVAILABLE,
    evaluate_queued_job,
)


def test_temporary_unavailable_keeps_original_deadline():
    decision = evaluate_queued_job(
        now=100,
        queue_deadline_at=3700,
        pool_online=False,
        admin_offline=False,
    )
    assert decision.state == WAITING_POOL_UNAVAILABLE
    assert decision.deadline_unchanged is True


def test_recovery_does_not_reset_deadline_and_timeout_still_wins():
    before = evaluate_queued_job(
        now=3600,
        queue_deadline_at=3700,
        pool_online=True,
        admin_offline=False,
    )
    assert before.state == WAITING
    after = evaluate_queued_job(
        now=3700,
        queue_deadline_at=3700,
        pool_online=False,
        admin_offline=False,
    )
    assert after.state == QUEUE_TIMEOUT


def test_admin_offline_ends_immediately_even_before_deadline():
    decision = evaluate_queued_job(
        now=100,
        queue_deadline_at=3700,
        pool_online=False,
        admin_offline=True,
    )
    assert decision.state == POOL_UNAVAILABLE
