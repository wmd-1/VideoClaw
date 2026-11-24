"""exclusive/shared 准入策略测试。"""
from __future__ import annotations

from core.video_instance_admission import AdmissionInput, ObservationSample, evaluate_admission

NOW = 1_000.0


def _base(**overrides):
    payload = {
        "access_mode": "exclusive",
        "enabled": True,
        "qualified": True,
        "boot_verified": True,
        "local_slot_free": True,
        "now": NOW,
    }
    payload.update(overrides)
    return AdmissionInput(**payload)


def test_exclusive_unknown_is_health_only_and_can_dispatch():
    decision = evaluate_admission(
        _base(observations=(ObservationSample("unknown", NOW - 30),))
    )
    assert decision.allowed is True
    assert decision.action == "dispatch"


def test_exclusive_reliable_busy_conflicts_with_empty_ledger():
    decision = evaluate_admission(
        _base(observations=(ObservationSample("busy", NOW - 2),))
    )
    assert decision.allowed is False
    assert decision.action == "quarantine"
    assert decision.reason == "exclusive_ledger_conflict"


def test_exclusive_stale_busy_is_not_reliable():
    decision = evaluate_admission(
        _base(observations=(ObservationSample("busy", NOW - 20),))
    )
    assert decision.allowed is True


def test_shared_requires_two_independent_recent_idle_and_confirmation():
    decision = evaluate_admission(
        _base(
            access_mode="shared",
            observations=(ObservationSample("idle", NOW - 10),),
            final_confirmation=ObservationSample("idle", NOW - 1),
        )
    )
    assert decision.allowed is False
    assert decision.reason == "need_two_idle"

    decision = evaluate_admission(
        _base(
            access_mode="shared",
            observations=(
                ObservationSample("idle", NOW - 10),
                ObservationSample("idle", NOW - 5),
            ),
            final_confirmation=ObservationSample("idle", NOW - 1),
        )
    )
    assert decision.allowed is True
    assert decision.action == "dispatch"


def test_shared_idle_gap_too_short_or_stale_is_rejected():
    too_close = evaluate_admission(
        _base(
            access_mode="shared",
            observations=(
                ObservationSample("idle", NOW - 9),
                ObservationSample("idle", NOW - 5),
            ),
            final_confirmation=ObservationSample("idle", NOW),
        )
    )
    assert too_close.allowed is False

    stale = evaluate_admission(
        _base(
            access_mode="shared",
            observations=(
                ObservationSample("idle", NOW - 30),
                ObservationSample("idle", NOW - 25),
            ),
            final_confirmation=ObservationSample("idle", NOW),
        )
    )
    assert stale.allowed is False


def test_shared_unknown_busy_and_final_failure_block_dispatch():
    for state in ("unknown", "busy", "unreachable"):
        decision = evaluate_admission(
            _base(
                access_mode="shared",
                observations=(
                    ObservationSample("idle", NOW - 10),
                    ObservationSample("idle", NOW - 5),
                    ObservationSample(state, NOW - 1),
                ),
                final_confirmation=ObservationSample("idle", NOW),
            )
        )
        assert decision.allowed is False
        assert decision.action == "wait"

    missing_final = evaluate_admission(
        _base(
            access_mode="shared",
            observations=(
                ObservationSample("idle", NOW - 10),
                ObservationSample("idle", NOW - 5),
            ),
        )
    )
    assert missing_final.allowed is False
    assert missing_final.reason == "final_confirmation_missing"


def test_common_gates_block_before_mode_policy():
    assert evaluate_admission(_base(enabled=False)).reason == "instance_disabled"
    assert evaluate_admission(_base(qualified=False)).reason == "not_qualified"
    assert evaluate_admission(_base(boot_verified=False)).reason == "boot_unverified"
    assert evaluate_admission(_base(local_slot_free=False)).reason == "slot_busy"
    assert evaluate_admission(_base(pending_attempt=True)).reason == "pending_attempt"
    assert evaluate_admission(_base(quarantined=True)).reason == "quarantined"
