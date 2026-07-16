from __future__ import annotations

from dynamic_three_leg_transition_runtime import (
    MANAGEMENT_SCOPE,
    build_dynamic_three_leg_transition_plan,
)


def _plan(*, top: str = "33C", second: str = "34C") -> dict:
    return {
        "ok": True,
        "trading_plan_id": "trplan-current",
        "posterior_run_id": "post-current",
        "realization_state_id": "rstate-current",
        "strategy_artifact_sha256": "strategy-current",
        "market_snapshot_id": "market-current",
        "capital_snapshot_id": "capital-current",
        "risk_snapshot_id": "risk-current",
        "capital_epoch_id": "capital-epoch-current",
        "risk_reservation_id": "risk-reservation-current",
        "second_yes_entry_window_state_id": "second-window-current",
        "candidates": [
            {"candidate_id": "top", "strategy_leg": "top_yes_wave", "bucket": top, "side": "YES", "notional": 20.0},
            {"candidate_id": "second", "strategy_leg": "second_yes", "bucket": second, "side": "YES", "notional": 4.99},
            {"candidate_id": "no", "strategy_leg": "strongly_excluded_no", "bucket": "31C", "side": "NO", "notional": 100.0},
        ],
    }


def _snapshot(positions=None, unresolved=None) -> dict:
    return {
        "position_snapshot_id": "positions-current",
        "as_of": "2026-07-13T00:00:00Z",
        "complete": True,
        "positions": positions or [],
        "unresolved_intents": unresolved or [],
    }


def _position(bucket: str, side: str, *, notional: float, size: float, cost: float) -> dict:
    return {
        "management_scope": MANAGEMENT_SCOPE,
        "position_lot_id": f"lot-{bucket}-{side}",
        "position_lot_version": "7",
        "token_id": f"token-{bucket}-{side}",
        "bucket": bucket,
        "side": side,
        "strategy_leg": "strongly_excluded_no" if side == "NO" else "top_yes_wave",
        "position_size": size,
        "entry_notional_usdc": notional,
        "average_cost": cost,
    }


def test_empty_managed_position_snapshot_emits_current_buy_deltas() -> None:
    result = build_dynamic_three_leg_transition_plan(desired_trading_plan=_plan(), position_snapshot=_snapshot(), decision_as_of="2026-07-13T00:00:00.500Z")
    assert result["ok"] is True
    assert result["phase"] == "BUY_NEW"
    assert all(row["expected_token_balance"] == 0.0 for row in result["buy_candidates"])
    assert [row["notional"] for row in result["buy_candidates"]] == [20.0, 4.99, 100.0]
    assert result["sell_intents"] == []


def test_posterior_role_change_sells_old_before_any_new_buy() -> None:
    positions = [_position("32C", "YES", notional=20.0, size=40.0, cost=0.5)]
    result = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_plan(top="33C", second="34C"),
        position_snapshot=_snapshot(positions),
        decision_as_of="2026-07-13T00:00:00.500Z",
    )
    assert result["phase"] == "SELL_OLD"
    assert result["buy_candidates"] == []
    assert result["sell_intents"][0]["bucket"] == "32C"
    assert result["sell_intents"][0]["reason_code"] == "target_realization_top_yes_invalidated"
    assert result["requires_latest_posterior_refresh_after_sell"] is True


def test_top_to_second_role_change_trims_below_five_dollars() -> None:
    positions = [_position("34C", "YES", notional=20.0, size=40.0, cost=0.5)]
    result = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_plan(top="33C", second="34C"),
        position_snapshot=_snapshot(positions),
        decision_as_of="2026-07-13T00:00:00.500Z",
    )
    sell = result["sell_intents"][0]
    assert result["phase"] == "SELL_OLD"
    assert sell["full_exit"] is False
    assert sell["requested_sell_size"] == 30.02
    assert sell["target_notional_after_fill_usdc"] == 4.99


def test_trimmed_former_top_counts_toward_current_second_without_duplicate_buy() -> None:
    positions = [_position("34C", "YES", notional=4.99, size=9.98, cost=0.5)]
    result = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_plan(top="33C", second="34C"),
        position_snapshot=_snapshot(positions),
        decision_as_of="2026-07-13T00:00:00.500Z",
    )

    assert result["phase"] == "BUY_NEW"
    assert not any(row["bucket"] == "34C" for row in result["buy_candidates"])
    assert any(row["bucket"] == "33C" and row["strategy_leg"] == "top_yes_wave" for row in result["buy_candidates"])


def test_second_entry_promoted_to_top_must_exit_before_top_wave_reentry() -> None:
    position = _position("33C", "YES", notional=5.0, size=10.0, cost=0.5)
    position["strategy_leg"] = "second_yes"
    result = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_plan(top="33C", second="34C"),
        position_snapshot=_snapshot([position]),
        decision_as_of="2026-07-13T00:00:00.500Z",
    )

    assert result["phase"] == "SELL_OLD"
    assert result["sell_intents"][0]["full_exit"] is True
    assert result["sell_intents"][0]["strategy_leg"] == "second_yes"
    assert result["buy_candidates"] == []


def test_continuing_position_only_buys_remaining_allocation() -> None:
    positions = [_position("33C", "YES", notional=12.0, size=24.0, cost=0.5)]
    result = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_plan(), position_snapshot=_snapshot(positions), decision_as_of="2026-07-13T00:00:00.500Z"
    )
    top = next(row for row in result["buy_candidates"] if row["strategy_leg"] == "top_yes_wave")
    assert result["phase"] == "BUY_NEW"
    assert top["notional"] == 8.0
    assert top["target_notional_usdc"] == 20.0


def test_unresolved_intent_and_incomplete_snapshot_fail_closed() -> None:
    unresolved = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_plan(), position_snapshot=_snapshot(unresolved=[{"intent_id": "pending"}]), decision_as_of="2026-07-13T00:00:00.500Z"
    )
    incomplete = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_plan(),
        position_snapshot={"position_snapshot_id": "x", "as_of": "2026-07-13T00:00:00Z", "complete": False},
        decision_as_of="2026-07-13T00:00:00.500Z",
    )
    assert unresolved["phase"] == "BLOCKED"
    assert "dynamic_transition_unresolved_intent_requires_reconcile" in unresolved["block_reasons"]
    assert incomplete["phase"] == "BLOCKED"


def test_stale_position_snapshot_fails_closed() -> None:
    result = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_plan(),
        position_snapshot=_snapshot(),
        decision_as_of="2026-07-13T00:00:01.001Z",
    )
    assert result["phase"] == "BLOCKED"
    assert "dynamic_transition_position_snapshot_stale" in result["block_reasons"]


def test_closed_second_yes_window_retains_same_bucket_without_buy_or_forced_sell() -> None:
    plan = _plan()
    second = plan["candidates"].pop(1)
    plan["retention_candidates"] = [second]
    position = _position("34C", "YES", notional=4.99, size=9.98, cost=0.5)
    position["strategy_leg"] = "second_yes"

    result = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=plan,
        position_snapshot=_snapshot([position]),
        decision_as_of="2026-07-13T00:00:00.500Z",
    )

    assert result["ok"] is True
    assert not any(row["strategy_leg"] == "second_yes" for row in result["buy_candidates"])
    assert not any(row["strategy_leg"] == "second_yes" for row in result["sell_intents"])
