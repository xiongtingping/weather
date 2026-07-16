from __future__ import annotations

from settlement_window_runtime import (
    build_settlement_window_state,
    load_approved_settlement_window_policy,
)


def _state(decision_as_of: str) -> dict:
    return build_settlement_window_state(
        resolution_local_date="2026-07-14",
        decision_as_of=decision_as_of,
        policy=load_approved_settlement_window_policy(),
    )


def test_window_opens_for_last_three_hours_of_resolution_local_day() -> None:
    assert _state("2026-07-14T12:59:59Z")["phase"] == "before_window"
    assert _state("2026-07-14T13:00:00Z")["settlement_window_open"] is True
    assert _state("2026-07-14T15:59:59Z")["settlement_window_open"] is True
    assert _state("2026-07-14T16:00:00Z")["phase"] == "closed"


def test_policy_tamper_and_naive_clock_fail_closed() -> None:
    policy = load_approved_settlement_window_policy()
    policy["window_open_local_time"] = "18:00:00"
    tampered = build_settlement_window_state(
        resolution_local_date="2026-07-14",
        decision_as_of="2026-07-14T13:00:00Z",
        policy=policy,
    )
    assert tampered["ok"] is False
    assert "settlement_window_policy_artifact_invalid" in tampered["block_reasons"]

    naive = build_settlement_window_state(
        resolution_local_date="2026-07-14",
        decision_as_of="2026-07-14T13:00:00",
        policy=load_approved_settlement_window_policy(),
    )
    assert naive["ok"] is False
    assert "settlement_window_decision_as_of_invalid" in naive["block_reasons"]
