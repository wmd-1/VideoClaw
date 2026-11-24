"""批次快照：新增不扩批次、移除不接新片段、在途仍可查询。"""
from __future__ import annotations

from core.video_batch_snapshot import (
    BatchSnapshot,
    can_dispatch_new,
    effective_members,
    in_flight_member_allowed,
)


def _snapshot():
    return BatchSnapshot("pool", 1, ("a", "b"))


def test_added_member_does_not_expand_accepted_batch():
    snapshot = _snapshot()
    assert can_dispatch_new(snapshot, "c") is False
    assert effective_members(
        snapshot,
        current_profile_members=["a", "b", "c"],
        eligible_instances=["a", "b", "c"],
    ) == ("a", "b")


def test_removed_or_disabled_member_stops_new_dispatch():
    snapshot = _snapshot()
    assert effective_members(
        snapshot,
        current_profile_members=["a"],
        eligible_instances=["a", "b"],
    ) == ("a",)
    assert effective_members(
        snapshot,
        current_profile_members=["a", "b"],
        eligible_instances=["a"],
    ) == ("a",)


def test_in_flight_member_survives_membership_change():
    snapshot = _snapshot()
    assert in_flight_member_allowed(snapshot, "b") is True
    assert can_dispatch_new(snapshot, "b") is True
    # 当前池已移除 b 时，新派发用 effective_members 排除，但原在途查询仍用快照。
    assert effective_members(
        snapshot,
        current_profile_members=["a"],
        eligible_instances=["a", "b"],
    ) == ("a",)
