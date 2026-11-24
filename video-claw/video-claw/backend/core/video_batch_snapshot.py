"""已受理批次的不可变成员快照与当前成员交集。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple


@dataclass(frozen=True)
class BatchSnapshot:
    profile_id: str
    revision: int
    members: Tuple[str, ...]
    contracts: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "revision": self.revision,
            "members": list(self.members),
            "contracts": list(self.contracts),
        }


def effective_members(
    snapshot: BatchSnapshot,
    *,
    current_profile_members: Iterable[str],
    eligible_instances: Iterable[str],
) -> Tuple[str, ...]:
    """新派发可用集合 = 快照成员 ∩ 当前仍在该池的成员 ∩ 当前资格/启用有效。"""
    current = {str(item) for item in current_profile_members}
    eligible = {str(item) for item in eligible_instances}
    return tuple(
        member
        for member in snapshot.members
        if member in current and member in eligible
    )


def can_dispatch_new(snapshot: BatchSnapshot, member: str) -> bool:
    """快照后新增的成员不会自动扩进已受理批次。"""
    return member in snapshot.members


def in_flight_member_allowed(snapshot: BatchSnapshot, member: str) -> bool:
    """在途尝试按原快照继续查询/下载，即使成员后来被移除或禁用。"""
    return member in snapshot.members


def snapshot_from_mapping(value: dict) -> BatchSnapshot:
    return BatchSnapshot(
        profile_id=str(value.get("profile_id") or ""),
        revision=int(value.get("revision") or 0),
        members=tuple(str(item) for item in value.get("members") or []),
        contracts=tuple(str(item) for item in value.get("contracts") or []),
    )


__all__ = [
    "BatchSnapshot",
    "can_dispatch_new",
    "effective_members",
    "in_flight_member_allowed",
    "snapshot_from_mapping",
]
