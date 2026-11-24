"""统一视频调度错误 detail：HTTP 409/429 与任务 error_detail 共用同一内层结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DISPATCH_ERROR_CODE = "video_dispatch_error"
DISPATCH_STAGE = "video_generation"

POOL_NOT_READY = "pool_not_ready"
POOL_INCOMPATIBLE = "pool_incompatible"
SELECTED_POOL_NOT_READY = "selected_pool_not_ready"
POOL_UNAVAILABLE = "pool_unavailable"
DISPATCH_DISABLED = "dispatch_disabled"
QUEUE_FULL = "queue_full"
QUEUE_TIMEOUT = "queue_timeout"
POOL_CONFIGURATION_ERROR = "pool_configuration_error"
POOL_DRAINING = "pool_draining"

ACTION_CONFIGURE_POOL = "configure_pool"
ACTION_SELECT_POOL = "select_pool"
ACTION_EDIT_PARAMETERS = "edit_parameters"
ACTION_USE_MANUAL_ONCE = "use_manual_once"
ACTION_RETRY = "retry"

HTTP_STATUS_BY_ERROR = {
    POOL_NOT_READY: 409,
    POOL_INCOMPATIBLE: 409,
    SELECTED_POOL_NOT_READY: 409,
    POOL_UNAVAILABLE: 409,
    DISPATCH_DISABLED: 409,
    QUEUE_TIMEOUT: 409,
    QUEUE_FULL: 429,
    POOL_CONFIGURATION_ERROR: 400,
    POOL_DRAINING: 409,
}


@dataclass
class VideoDispatchDetail:
    """拒绝类错误的完整 detail；未受理时 execution_id 保持 None。"""

    error_code: str
    reason: str
    mode: str = "auto"
    stage: str = DISPATCH_STAGE
    field_name: Optional[str] = "video_profile_id"
    profile_id: Optional[str] = None
    execution_id: Optional[str] = None
    submission_id: Optional[str] = None
    missing: List[Dict[str, str]] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)

    def payload(self) -> Dict[str, Any]:
        return {
            "code": DISPATCH_ERROR_CODE,
            "error_code": self.error_code,
            "stage": self.stage,
            "mode": self.mode,
            "field": self.field_name,
            "profile_id": self.profile_id,
            "execution_id": self.execution_id,
            "submission_id": self.submission_id,
            "reason": self.reason,
            "missing": [dict(item) for item in self.missing],
            "actions": list(self.actions),
        }

    def http_detail(self) -> Dict[str, Any]:
        return {"detail": self.payload()}


def missing_item(field_name: str, code: str, reason: str = "") -> Dict[str, str]:
    return {"field": field_name, "code": code, "reason": reason}


def dispatch_error_payload(
    error_code: str,
    reason: str,
    *,
    mode: str = "auto",
    field_name: Optional[str] = "video_profile_id",
    profile_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    submission_id: Optional[str] = None,
    missing: Optional[List[Dict[str, str]]] = None,
    actions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """任务侧 error_detail：与 HTTP detail 内层保持同一字段集。"""
    return VideoDispatchDetail(
        error_code=error_code,
        reason=reason,
        mode=mode,
        field_name=field_name,
        profile_id=profile_id,
        execution_id=execution_id,
        submission_id=submission_id,
        missing=list(missing or []),
        actions=list(actions or []),
    ).payload()


__all__ = [
    "ACTION_CONFIGURE_POOL",
    "ACTION_EDIT_PARAMETERS",
    "ACTION_RETRY",
    "ACTION_SELECT_POOL",
    "ACTION_USE_MANUAL_ONCE",
    "DISPATCH_DISABLED",
    "DISPATCH_ERROR_CODE",
    "DISPATCH_STAGE",
    "HTTP_STATUS_BY_ERROR",
    "POOL_CONFIGURATION_ERROR",
    "POOL_INCOMPATIBLE",
    "POOL_NOT_READY",
    "POOL_UNAVAILABLE",
    "QUEUE_FULL",
    "QUEUE_TIMEOUT",
    "SELECTED_POOL_NOT_READY",
    "VideoDispatchDetail",
    "dispatch_error_payload",
    "missing_item",
]
