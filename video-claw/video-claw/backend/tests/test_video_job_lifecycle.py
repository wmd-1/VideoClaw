"""提交分类与槽位释放证据。"""
from __future__ import annotations

import httpx

from core.video_job_lifecycle import (
    SUBMIT_ACCEPTED,
    SUBMIT_REJECTED,
    SUBMIT_UNKNOWN,
    classify_submit_exception,
    classify_submit_response,
    job_terminal_state,
    may_release_slot,
    safe_retry_allowed,
)


def test_submission_classification_only_allows_safe_retry():
    assert classify_submit_exception(httpx.ConnectError("no route")) == SUBMIT_REJECTED
    assert classify_submit_exception(httpx.ConnectTimeout("timeout")) == SUBMIT_REJECTED
    assert classify_submit_exception(httpx.ReadTimeout("late")) == SUBMIT_UNKNOWN
    assert classify_submit_exception(httpx.RemoteProtocolError("cut")) == SUBMIT_UNKNOWN

    assert classify_submit_response(200, {"id": "job-1"}) == SUBMIT_ACCEPTED
    assert classify_submit_response(200, {"detail": "ok"}) == SUBMIT_UNKNOWN
    assert classify_submit_response(500, {"id": "job-1"}) == SUBMIT_UNKNOWN
    assert classify_submit_response(400, {"detail": "bad"}) == SUBMIT_REJECTED

    assert safe_retry_allowed(SUBMIT_REJECTED) is True
    assert safe_retry_allowed(SUBMIT_UNKNOWN) is False
    assert safe_retry_allowed(SUBMIT_ACCEPTED) is False


def test_failed_or_deleted_without_drain_never_releases_slot():
    assert job_terminal_state("completed") == "success"
    assert job_terminal_state("failed") == "failure"
    assert job_terminal_state("deleted") == "failure"
    assert job_terminal_state("unknown") == "pending"

    assert may_release_slot(status="completed") is True
    assert may_release_slot(status="failed") is False
    assert may_release_slot(status="deleted") is False
    assert may_release_slot(status="failed", drain_confirmed=True) is True
    assert may_release_slot(status="deleted", cancel_confirmed=True) is True
    assert may_release_slot(status="queued") is False
