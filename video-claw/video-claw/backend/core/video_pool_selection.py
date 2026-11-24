"""新执行的选池策略：显式偏好、管理员推荐、first_ready 与不兼容分离。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

REASONS = (
    "none",
    "no_default",
    "default_not_ready",
    "default_incompatible",
    "no_ready_pool",
    "no_compatible_pool",
)


@dataclass(frozen=True)
class PoolChoice:
    profile_id: Optional[str]
    source: Optional[str]  # user/admin_default/first_ready
    recommendation_reason: str
    error: str  # "" / selected_pool_not_ready / no_ready_pool / no_compatible_pool


@dataclass(frozen=True)
class PreparedVideoContract:
    """执行快照里的固定池与有效参数；不写回会话的 video_*_model。"""

    profile_id: Optional[str]
    revision: int
    mode: str
    selection_source: Optional[str]
    recommendation_reason: str
    parameters: Mapping[str, object]
    contract: Mapping[str, object] = field(default_factory=dict)

    def to_snapshot(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "revision": self.revision,
            "mode": self.mode,
            "selection_source": self.selection_source,
            "recommendation_reason": self.recommendation_reason,
            "parameters": dict(self.parameters),
            "contract": dict(self.contract),
        }


def choose_pool(
    *,
    mode: str,
    explicit_profile_id: Optional[str],
    default_profiles: Mapping[str, str],
    ready_profiles: Iterable[str],
    compatible_profiles: Iterable[str],
) -> PoolChoice:
    """只做纯选择；busy 不参与过滤，ready 池仍可被推荐并排队。"""
    ready = {str(item) for item in ready_profiles}
    compatible = {str(item) for item in compatible_profiles}

    if explicit_profile_id:
        chosen = str(explicit_profile_id)
        if chosen not in ready or chosen not in compatible:
            return PoolChoice(chosen, "user", "none", "selected_pool_not_ready")
        return PoolChoice(chosen, "user", "none", "")

    default_id = str(default_profiles.get(mode) or "")
    if default_id:
        if default_id in ready and default_id in compatible:
            return PoolChoice(default_id, "admin_default", "none", "")
        reason = "default_not_ready" if default_id not in ready else "default_incompatible"
        fallback = _first_ready_compatible(ready, compatible)
        if fallback:
            return PoolChoice(fallback, "first_ready", reason, "")
        return PoolChoice(None, None, reason, _error_for(reason))

    fallback = _first_ready_compatible(ready, compatible)
    if fallback:
        return PoolChoice(fallback, "first_ready", "no_default", "")
    if ready:
        return PoolChoice(None, None, "no_compatible_pool", "no_compatible_pool")
    return PoolChoice(None, None, "no_ready_pool", "no_ready_pool")


def _first_ready_compatible(
    ready: set[str],
    compatible: set[str],
) -> Optional[str]:
    candidates = sorted(ready & compatible)
    return candidates[0] if candidates else None


def _error_for(reason: str) -> str:
    if reason == "default_incompatible":
        return "no_compatible_pool"
    if reason == "default_not_ready":
        return "no_ready_pool"
    return "no_ready_pool"


__all__ = ["PoolChoice", "PreparedVideoContract", "REASONS", "choose_pool"]
