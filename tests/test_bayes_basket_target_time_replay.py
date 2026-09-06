from __future__ import annotations

import copy

import pytest

from bayes_basket_target.cash_phase import BasketCashPhasePolicyArtifact
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.time_replay import replay_materialized_target_chain


def _policy() -> BasketCashPhasePolicyArtifact:
    return BasketCashPhasePolicyArtifact.create(
        policy_version="time-replay-cash-v1",
        station_id="ZUUU",
        station_timezone="America/New_York",
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase={"HEAVY": 6.0, "STANDARD": 3.0, "TAIL": 1.0},
        valid_from_utc="2026-10-01T00:00:00Z",
        valid_until_utc="2026-12-01T00:00:00Z",
        evidence_status="OFFLINE_VALIDATED",
    )


def _event(
    event_id: str,
    sequence: int,
    observed_at: str,
    phase: str,
    stage: str,
    *,
    event_date_local: str = "2026-11-01",
    remaining_budget: float = 20.0,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "station_id": "ZUUU",
        "sequence": sequence,
        "observed_at_utc": observed_at,
        "event_date_local": event_date_local,
        "checkpoint_id": f"checkpoint-{event_id}",
        "probability_stage": stage,
        "posterior_version": f"posterior-{event_id}",
        "posterior_sha256": ("a" if sequence % 2 else "b") * 64,
        "snapshot_sha256": ("c" if sequence % 2 else "d") * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "quote_version": f"quote-{event_id}",
        "inventory_version": f"inventory-{event_id}",
        "order_state_version": f"orders-{event_id}",
        "source_observed_at_utc": observed_at,
        "current_phase": phase,
        "bucket": "38",
        "delta_shares": 20.0,
        "r_limit": 0.40,
        "remaining_budget_usdc": remaining_budget,
        "executable_depth_cash": 8.0,
        "candidate_present": True,
        "target_clip_notional_usdc": 5.0,
        "minimum_clip_notional_usdc": 0.5,
        "share_quantum": 0.01,
    }


def _events() -> list[dict[str, object]]:
    return [
        # 19:53 EDT, before the D-1 cutoff: HEAVY.
        _event("e1", 1, "2026-10-31T23:53:00Z", "HEAVY", "P0"),
        # After the 2026 fall-back transition: STANDARD, with UTC monotonicity.
        # Each event declares the budget the previous one left, and the
        # chain refuses any other number.  These moved on 2026-09-06 when
        # sizing began flooring to a whole share: 12.5 shares at 0.40 was
        # 5.00 USDC and twelve of them are 4.80, so every step down the
        # chain now leaves a little more behind.
        _event("e2", 2, "2026-11-01T06:05:00Z", "HEAVY", "P0-M", remaining_budget=15.2),
        _event("e3", 3, "2026-11-01T14:00:00Z", "STANDARD", "P0-M", remaining_budget=12.4),
        _event("e4", 4, "2026-11-01T15:00:00Z", "STANDARD", "P1", remaining_budget=9.6),
        # UTC crosses into the next day while the settlement date rolls too.
        _event("e5", 5, "2026-11-02T01:00:00Z", "TAIL", "P1", event_date_local="2026-11-02", remaining_budget=8.8),
    ]


def test_full_cross_chain_replay_equals_incremental_prefix_suffix() -> None:
    events = _events()
    policy = _policy()
    full = replay_materialized_target_chain(events, policy=policy, station_id="ZUUU")
    prefix = replay_materialized_target_chain(events[:2], policy=policy, station_id="ZUUU")
    suffix = replay_materialized_target_chain(
        events[2:], policy=policy, station_id="ZUUU", initial_state=prefix["continuation"]
    )
    assert full["rows"] == prefix["rows"] + suffix["rows"]
    assert full["continuation"] == suffix["continuation"]
    assert full["boundary"] == {
        "target_only": True,
        "places_orders": False,
        "orders_submitted": 0,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "network_access": False,
    }
    assert [row["phase_decision"]["next_phase"] for row in full["rows"]] == [
        "HEAVY", "STANDARD", "STANDARD", "TAIL", "TAIL"
    ]
    assert all(row["quote_source"] == "OFFICIAL_CLOB_WS" for row in full["rows"])


def test_replay_handles_duplicate_but_rejects_rewind_and_content_conflict() -> None:
    events = _events()
    policy = _policy()
    duplicate = replay_materialized_target_chain(
        [*events, copy.deepcopy(events[1])], policy=policy, station_id="ZUUU"
    )
    clean = replay_materialized_target_chain(events, policy=policy, station_id="ZUUU")
    assert duplicate["rows"] == clean["rows"]
    assert duplicate["continuation"] == clean["continuation"]
    assert duplicate["duplicate_event_count"] == 1

    rewind = copy.deepcopy(events[2])
    rewind["event_id"] = "e3-rewind"
    rewind["checkpoint_id"] = "checkpoint-e3-rewind"
    with pytest.raises(ContractError, match="sequence_not_strictly_increasing"):
        replay_materialized_target_chain([*events, rewind], policy=policy, station_id="ZUUU")

    conflicting = copy.deepcopy(events[0])
    conflicting["posterior_version"] = "tampered"
    with pytest.raises(ContractError, match="event_id_content_conflict"):
        replay_materialized_target_chain([events[0], conflicting], policy=policy, station_id="ZUUU")


def test_replay_rejects_backward_clock_future_source_and_non_ws_evidence() -> None:
    events = _events()
    policy = _policy()

    backward = _events()[:4]
    backward.append(_event("e5-back", 5, "2026-11-01T14:30:00Z", "TAIL", "P1"))
    with pytest.raises(ContractError, match="timestamp_moved_backwards"):
        replay_materialized_target_chain(backward, policy=policy, station_id="ZUUU")

    future_source = _events()
    future_source[2]["source_observed_at_utc"] = "2026-11-01T14:01:00Z"
    with pytest.raises(ContractError, match="future_source_leak"):
        replay_materialized_target_chain(future_source, policy=policy, station_id="ZUUU")

    rest_quote = _events()
    rest_quote[0]["quote_source"] = "REST"
    with pytest.raises(ContractError, match="official_ws_required"):
        replay_materialized_target_chain(rest_quote, policy=policy, station_id="ZUUU")

    budget_drift = _events()
    budget_drift[1]["remaining_budget_usdc"] = 20.0
    with pytest.raises(ContractError, match="budget_state_mismatch"):
        replay_materialized_target_chain(budget_drift, policy=policy, station_id="ZUUU")

    scope_drift = _events()
    scope_drift[0]["station_id"] = "ZBAA"
    with pytest.raises(ContractError, match="station_scope_mismatch"):
        replay_materialized_target_chain(scope_drift, policy=policy, station_id="ZUUU")


def test_replay_preserves_no_live_and_repeated_runs_are_identical() -> None:
    events = _events()
    policy = _policy()
    receipts = [replay_materialized_target_chain(events, policy=policy, station_id="ZUUU") for _ in range(5)]
    assert all(receipt == receipts[0] for receipt in receipts)
    assert receipts[0]["replay_sha256"]
    assert all(row["sizing_decision"]["c_order"] >= 0.0 for row in receipts[0]["rows"])
