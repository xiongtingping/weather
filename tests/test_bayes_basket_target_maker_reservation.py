from __future__ import annotations

import random

import pytest

from bayes_basket_target.basket_kernel.contracts import ContractError
from bayes_basket_target.basket_kernel.maker_reservation import (
    canonicalize_quote_path,
    elapsed_seconds_with_wrap,
    maker_repricing_decision,
    solve_maker_reservation,
)
WEIGHTS = {
    "lambda_profit_floor": 0.0,
    "lambda_downside": 0.0,
    "lambda_shortfall": 0.0,
    "lambda_breakthrough": 0.0,
    "lambda_concentration": 0.0,
    "lambda_execution": 0.20,
}


def _solve(**overrides):
    payload = {
        "probabilities": {"35°C": 0.82, "36°C": 0.18},
        "filled_shares_by_bucket": {"35°C": 0.0, "36°C": 0.0},
        "filled_cost_by_bucket": {"35°C": 0.0, "36°C": 0.0},
        "bucket": "35°C",
        "requested_shares": 10.0,
        "protected_buckets": ["35°C", "36°C"],
        "weights": WEIGHTS,
        "concentration_scale_usdc": 10.0,
        "price_tick_size": 0.01,
        "best_ask_price": 0.20,
        "maximum_limit_price": 0.99,
        "timeout_candidates_seconds": [30.0, 60.0, 120.0],
        "formal_activation_eligible": False,
        "cost_terms": {
            "fill_probability": 0.82,
            "queue_cost_per_filled_share": 0.001,
            "nonfill_opportunity_cost_per_share": 0.02,
            "adverse_selection_cost_per_filled_share": 0.001,
            "wait_cost_per_filled_share": 0.001,
        },
    }
    payload.update(overrides)
    return solve_maker_reservation(**payload)


def test_formal_missing_oos_is_immediate_hold_and_no_gateway():
    result = _solve(formal_activation_eligible=True)

    assert result["status"] == "HOLD"
    assert result["reason_codes"] == ["maker_grid_oos_formal_artifact_required"]
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_single_exact_scope_oos_artifact_cannot_cover_whole_price_grid():
    expected_scope = {
        "station_id": "RKPK",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "issuance": "00Z",
        "liquidity_bin": "liquid",
        "price_distance_bin": "near",
        "queue_bin": "q0_q5",
    }
    artifact = {
        "schema": "target_maker_fill_queue_risk_oos_artifact_v1",
        "content_sha256": "a" * 64,
        "metrics": {},
    }

    result = _solve(
        formal_activation_eligible=True,
        artifact=artifact,
        expected_scope=expected_scope,
        as_of_utc="2026-07-15T00:00:00Z",
        cost_terms=None,
    )

    assert result["status"] == "HOLD"
    assert result["reason_codes"] == [
        "maker_grid_oos_formal_artifact_required"
    ]
    assert result["authorized"] is False
    assert result["orders_submitted"] == 0


def test_counterfactual_cost_terms_never_become_promotion_or_order():
    result = _solve()

    assert result["status"] == "COUNTERFACTUAL_ONLY"
    assert result["authorized"] is False
    assert result["will_order"] is False
    assert result["promotion_eligible"] is False
    assert result["orders_submitted"] == 0


def test_objective_selects_grid_price_not_fixed_ask_minus_tick_or_bid_copy():
    result = _solve(best_ask_price=0.20, best_bid_price=0.13)

    assert result["reservation_price"] != pytest.approx(0.19)
    assert result["reservation_price"] != pytest.approx(0.13)
    assert result["fixed_tick_subtract"] is False
    assert result["copied_best_bid"] is False
    assert result["selected"]["delta_basket_score"] > 0.0


def test_timeout_terms_are_jointly_optimized_with_fill_and_wait_cost():
    result = _solve(
        timeout_candidates_seconds=[30.0, 60.0, 120.0],
        cost_terms_by_timeout={
            30.0: {"fill_probability": 0.40, "wait_cost_per_filled_share": 0.0001},
            60.0: {"fill_probability": 0.78, "wait_cost_per_filled_share": 0.001},
            120.0: {"fill_probability": 0.82, "wait_cost_per_filled_share": 0.40},
        },
    )

    assert result["timeout_seconds"] == 60.0
    assert result["selected"]["fill_probability"] == pytest.approx(0.78)
    assert result["selected"]["wait_cost_per_filled_share"] == pytest.approx(0.001)


def test_confirmed_pending_never_enters_scenario_pnl_or_floor():
    result = _solve(
        confirmed_pending_shares=99.0,
        confirmed_pending_notional=19.80,
    )
    selected = result["selected"]

    assert selected["confirmed_pending_shares"] == 99.0
    assert selected["confirmed_pending_notional"] == 19.80
    assert result["confirmed_pending"]["enters_scenario_pnl"] is False
    assert result["confirmed_pending"]["enters_profit_floor"] is False
    # The objective sees only expected filled action shares, not 99 pending.
    assert max(selected["scenario_pnl"].values()) < 10.0


def test_low_high_low_quote_path_and_empty_poll_are_deterministic():
    path = [
        {"sequence": 3, "best_ask_price": 0.20, "quote_version": "q3"},
        {"sequence": 4, "empty_poll": True},
        {"sequence": 5, "best_ask_price": 0.31, "quote_version": "q5"},
        {"sequence": 6, "best_ask_price": 0.20, "quote_version": "q6"},
    ]
    folded = canonicalize_quote_path(path)
    assert folded["empty_poll_count"] == 1
    assert folded["current_quote"]["best_ask_price"] == pytest.approx(0.20)
    assert folded["transition_prices"] == pytest.approx([0.11, -0.11])
    assert maker_repricing_decision(path[0], path[1], empty_poll=True)["reprice"] is False

    first = _solve(quote_path=path)
    second = _solve(quote_path=list(reversed(path)))
    assert first["content_sha256"] == second["content_sha256"]
    assert first["reservation_price"] == second["reservation_price"]


def test_time_wrap_is_real_date_or_modulo_clock_and_rejects_backwards_timestamp():
    assert elapsed_seconds_with_wrap(86_390, 10) == pytest.approx(20.0)
    assert elapsed_seconds_with_wrap(
        "2026-07-27T23:59:50Z", "2026-07-28T00:00:10Z"
    ) == pytest.approx(20.0)
    with pytest.raises(ContractError, match="timestamp_moved_backwards"):
        elapsed_seconds_with_wrap("2026-07-28T00:00:10Z", "2026-07-27T23:59:50Z")


def test_empty_poll_keeps_price_and_same_quote_is_hold():
    quote = {"best_ask_price": 0.20, "best_bid_price": 0.13, "quote_version": "q"}
    decision = maker_repricing_decision(quote, quote)
    empty = maker_repricing_decision(quote, None, empty_poll=True)

    assert decision["action"] == "HOLD"
    assert decision["reprice"] is False
    assert empty["action"] == "HOLD"
    assert empty["reprice"] is False


def test_randomized_replay_is_byte_deterministic_and_post_only():
    rng = random.Random(20260727)
    for _ in range(250):
        ask = round(rng.uniform(0.11, 0.75), 2)
        bid = round(max(0.01, ask - rng.uniform(0.01, 0.10)), 2)
        terms = {
            "fill_probability": rng.uniform(0.35, 0.95),
            "queue_cost_per_filled_share": rng.uniform(0.0, 0.01),
            "nonfill_opportunity_cost_per_share": rng.uniform(0.0, 0.05),
            "adverse_selection_cost_per_filled_share": rng.uniform(0.0, 0.01),
            "wait_cost_per_filled_share": rng.uniform(0.0, 0.01),
        }
        first = _solve(
            best_ask_price=ask,
            best_bid_price=bid,
            cost_terms=terms,
        )
        second = _solve(
            best_ask_price=ask,
            best_bid_price=bid,
            cost_terms=terms,
        )
        assert first["content_sha256"] == second["content_sha256"]
        if first["status"] != "HOLD":
            assert first["reservation_price"] < ask
            assert first["reservation_price"] <= 0.99
            assert first["orders_submitted"] == 0
