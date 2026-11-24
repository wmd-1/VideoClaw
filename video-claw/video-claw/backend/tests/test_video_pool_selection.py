"""选池策略：默认 busy 仍等、fallback 不粘会话、无推荐正常。"""
from __future__ import annotations

from core.video_pool_selection import PreparedVideoContract, choose_pool


def _choose(**overrides):
    payload = {
        "mode": "first_frame",
        "explicit_profile_id": None,
        "default_profiles": {"first_frame": "default-pool"},
        "ready_profiles": {"default-pool", "other-pool"},
        "compatible_profiles": {"default-pool", "other-pool"},
    }
    payload.update(overrides)
    return choose_pool(**payload)


def test_explicit_ready_pool_is_user_source():
    choice = _choose(explicit_profile_id="other-pool")
    assert choice.profile_id == "other-pool"
    assert choice.source == "user"


def test_explicit_unavailable_pool_does_not_silently_fallback():
    choice = _choose(explicit_profile_id="offline-pool")
    assert choice.profile_id == "offline-pool"
    assert choice.error == "selected_pool_not_ready"


def test_default_ready_is_selected_even_if_busy():
    choice = _choose()
    assert choice.profile_id == "default-pool"
    assert choice.source == "admin_default"
    assert choice.error == ""


def test_default_not_ready_falls_back_to_first_ready_with_reason():
    choice = _choose(ready_profiles={"other-pool"})
    assert choice.profile_id == "other-pool"
    assert choice.source == "first_ready"
    assert choice.recommendation_reason == "default_not_ready"


def test_no_default_is_normal_not_an_error():
    choice = _choose(default_profiles={})
    assert choice.profile_id == "default-pool"
    assert choice.source == "first_ready"
    assert choice.recommendation_reason == "no_default"
    assert choice.error == ""


def test_no_ready_or_no_compatible_are_distinct():
    no_ready = _choose(ready_profiles=set(), compatible_profiles=set())
    assert no_ready.error == "no_ready_pool"
    assert no_ready.recommendation_reason == "default_not_ready"

    no_compatible = _choose(
        default_profiles={},
        ready_profiles={"other-pool"},
        compatible_profiles=set(),
    )
    assert no_compatible.error == "no_compatible_pool"
    assert no_compatible.recommendation_reason == "no_compatible_pool"


def test_prepared_contract_snapshot_is_self_contained():
    contract = PreparedVideoContract(
        profile_id="other-pool",
        revision=3,
        mode="first_frame",
        selection_source="first_ready",
        recommendation_reason="default_not_ready",
        parameters={"duration": 8, "resolution": "768P"},
    )
    snapshot = contract.to_snapshot()
    assert snapshot["profile_id"] == "other-pool"
    assert snapshot["parameters"] == {"duration": 8, "resolution": "768P"}
    assert "video_first_frame_model" not in snapshot


def test_fallback_is_not_sticky_for_next_execution():
    first = _choose(ready_profiles={"other-pool"})
    assert first.profile_id == "other-pool"
    second = _choose()
    assert second.profile_id == "default-pool"
    assert second.source == "admin_default"
