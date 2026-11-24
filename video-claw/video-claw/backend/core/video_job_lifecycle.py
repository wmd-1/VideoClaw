"""提交与终态分类：只有可证明未受理才允许换形态/重发。"""
from __future__ import annotations

from typing import Any, Mapping

import httpx

SUBMIT_ACCEPTED = "accepted"
SUBMIT_REJECTED = "rejected"
SUBMIT_UNKNOWN = "unknown"

SUCCESS_STATES = {"completed", "succeeded", "success", "done"}
FAILURE_STATES = {"failed", "error", "cancelled", "canceled", "deleted"}


def classify_submit_exception(exc: BaseException) -> str:
    """连接失败可证明未发出；读超时/写超时/协议中断可能已被受理。"""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return SUBMIT_REJECTED
    return SUBMIT_UNKNOWN


def classify_submit_response(status_code: int, payload: Any) -> str:
    if 200 <= int(status_code) < 300:
        if isinstance(payload, Mapping) and any(
            payload.get(key) for key in ("id", "video_id", "task_id", "job_id")
        ):
            return SUBMIT_ACCEPTED
        return SUBMIT_UNKNOWN
    if int(status_code) >= 500:
        return SUBMIT_UNKNOWN
    return SUBMIT_REJECTED


def job_terminal_state(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value in SUCCESS_STATES:
        return "success"
    if value in FAILURE_STATES:
        return "failure"
    if value:
        return "pending"
    return "unknown"


def may_release_slot(
    *,
    status: Any,
    drain_confirmed: bool = False,
    cancel_confirmed: bool = False,
) -> bool:
    """failed/deleted/404 本身不能证明 GPU 已停止；必须有排空/取消确认。"""
    terminal = job_terminal_state(status)
    if terminal == "success":
        return True
    if terminal == "failure":
        return bool(drain_confirmed or cancel_confirmed)
    return False


def safe_retry_allowed(outcome: str) -> bool:
    return outcome == SUBMIT_REJECTED


__all__ = [
    "FAILURE_STATES",
    "SUBMIT_ACCEPTED",
    "SUBMIT_REJECTED",
    "SUBMIT_UNKNOWN",
    "SUCCESS_STATES",
    "classify_submit_exception",
    "classify_submit_response",
    "job_terminal_state",
    "may_release_slot",
    "safe_retry_allowed",
]
