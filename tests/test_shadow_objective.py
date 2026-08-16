"""The shadow log-wealth objective: solver theory, curve, and receipts."""

from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.basket_kernel.delta_v2 import CURVE_OBJECTIVE_FORM
from bayes_basket_target.shadow_objective import (
    build_shadow_objective_score,
    persist_shadow_objective_score,
    solve_fractional_kelly,
)


def test_solver_matches_kelly_theory() -> None:
    # Binary case, p=0.5 at price 0.2: full Kelly stakes (p-c)/(c(1-c))*W
    # = 375 shares on W=200; the quarter fraction serves 93.75.
    single = solve_fractional_kelly(
        probabilities=[0.5], prices=[0.2], depth_caps=[1e6],
        budget_usdc=1e6, bankroll_usdc=200.0,
    )
    assert single[0] == pytest.approx(93.75, rel=1e-3)

    # A binding budget moves to the budget face exactly.
    capped = solve_fractional_kelly(
        probabilities=[0.5], prices=[0.2], depth_caps=[1e6],
        budget_usdc=50.0, bankroll_usdc=200.0,
    )
    assert capped[0] == pytest.approx(62.5, rel=1e-3)

    # No edge, no bet: prices equal probabilities.
    flat = solve_fractional_kelly(
        probabilities=[0.2, 0.3], prices=[0.2, 0.3],
        depth_caps=[100.0, 100.0], budget_usdc=10.0,
        bankroll_usdc=200.0,
    )
    assert all(abs(value) < 1e-6 for value in flat)

    # Negative-edge buckets are refused even when budget is ample.
    mixed = solve_fractional_kelly(
        probabilities=[0.18, 0.12], prices=[0.05, 0.52],
        depth_caps=[500.0, 500.0], budget_usdc=100.0,
        bankroll_usdc=200.0,
    )
    assert mixed[0] > 0.0
    assert mixed[1] == pytest.approx(0.0, abs=1e-6)

    # Depth caps bind.
    shallow = solve_fractional_kelly(
        probabilities=[0.5], prices=[0.2], depth_caps=[10.0],
        budget_usdc=1e6, bankroll_usdc=200.0,
    )
    assert shallow[0] <= 10.0 * 0.25 + 1e-9

    with pytest.raises(ContractError):
        solve_fractional_kelly(
            probabilities=[0.5], prices=[1.2], depth_caps=[10.0],
            budget_usdc=10.0, bankroll_usdc=200.0,
        )


def test_inventory_is_the_initial_condition_not_an_afterthought() -> None:
    # Two symmetric outcomes, same price, same probability.  From flat the
    # log-optimal answer is symmetric; holding one of them already, it is not
    # -- the covered outcome's marginal share is worth less, so the solve
    # buys the bare one.  Solving from flat and adding to inventory would
    # keep the symmetric answer and re-buy coverage the station owns.
    flat = solve_fractional_kelly(
        probabilities=[0.4, 0.4], prices=[0.25, 0.25],
        depth_caps=[1e5, 1e5], budget_usdc=100.0, bankroll_usdc=200.0,
        kelly_fraction=1.0,
    )
    assert flat[0] == pytest.approx(flat[1], rel=1e-6)

    positioned = solve_fractional_kelly(
        probabilities=[0.4, 0.4], prices=[0.25, 0.25],
        depth_caps=[1e5, 1e5], budget_usdc=100.0, bankroll_usdc=200.0,
        kelly_fraction=1.0,
        held_shares=[80.0, 0.0], sunk_cost_usdc=20.0,
    )
    assert positioned[1] > positioned[0] + 1e-6

    # An outcome held outside the buyable set is scored, not written off as
    # paying nothing.  Half the mass settling on shares the station already
    # owns is half the downside, so the seated leg can carry more -- treating
    # that mass as "pays minus the cost" is what shrinks a rebalance for a
    # loss it is not exposed to.  A zero-share row is exactly the residual.
    ignored = solve_fractional_kelly(
        probabilities=[0.3], prices=[0.2], depth_caps=[1e5],
        budget_usdc=100.0, bankroll_usdc=200.0, kelly_fraction=1.0,
    )
    scored = solve_fractional_kelly(
        probabilities=[0.3], prices=[0.2], depth_caps=[1e5],
        budget_usdc=100.0, bankroll_usdc=200.0, kelly_fraction=1.0,
        held_outside=[(0.5, 400.0)],
    )
    assert scored[0] > ignored[0] + 1e-6
    assert solve_fractional_kelly(
        probabilities=[0.3], prices=[0.2], depth_caps=[1e5],
        budget_usdc=100.0, bankroll_usdc=200.0, kelly_fraction=1.0,
        held_outside=[(0.5, 0.0)],
    )[0] == pytest.approx(ignored[0], rel=1e-6)

    # Defaults are the from-flat problem exactly.
    assert solve_fractional_kelly(
        probabilities=[0.5], prices=[0.2], depth_caps=[1e6],
        budget_usdc=1e6, bankroll_usdc=200.0,
        held_shares=[0.0], sunk_cost_usdc=0.0, held_outside=[],
    )[0] == pytest.approx(93.75, rel=1e-3)

    with pytest.raises(ContractError):
        solve_fractional_kelly(
            probabilities=[0.5, 0.2], prices=[0.2, 0.3],
            depth_caps=[10.0, 10.0], budget_usdc=10.0, bankroll_usdc=200.0,
            held_shares=[1.0],
        )


def test_shadow_score_receipt_and_idempotent_persist() -> None:
    rows = [
        {"bucket": "34", "eligible": True, "r_limit": 0.011,
         "depth_shares_cap": 253.4, "q_order": 49.86},
        {"bucket": "35", "eligible": True, "r_limit": 0.051,
         "depth_shares_cap": 200.0, "q_order": 33.33},
        {"bucket": "37", "eligible": True, "r_limit": 0.521,
         "depth_shares_cap": 100.0, "q_order": 1.85},
        {"bucket": "38", "eligible": False, "r_limit": 0.9,
         "depth_shares_cap": 0.0, "q_order": 0.0},
    ]
    score = build_shadow_objective_score(
        station_id="ZUUU", event_date="2026-08-12",
        probability_stage="P0", checkpoint_id="p0-native-test",
        decision_at_utc="2026-08-11T11:43:27.081Z",
        candidate_rows=rows,
        probabilities_by_bucket={"34": 0.1849, "35": 0.1509, "37": 0.1241},
        budget_usdc=4.25, bankroll_usdc=200.0,
        created_at_utc="2026-08-12T04:00:00Z",
    )
    assert score is not None
    # The ineligible bucket stayed out of the experiment.
    assert score["buckets"] == ["34", "35", "37"]
    # Optimality: on the identical feasible set the full-Kelly optimum
    # bounds the production allocation's growth.  The fractional stakes sit
    # deliberately below that bound -- variance bought with growth.
    assert (
        score["full_kelly_curve"]["expected_log_growth"]
        >= score["actual_curve"]["expected_log_growth"] - 1e-9
    )
    # The shadow refuses the negative-edge 37 bucket the plan bought.
    assert score["shadow_quantities"][2] == pytest.approx(0.0, abs=1e-6)

    conn = sqlite3.connect(":memory:")
    assert persist_shadow_objective_score(conn, score) == 1
    assert persist_shadow_objective_score(conn, score) == 0
    row = conn.execute(
        "SELECT COUNT(*), objective_form "
        "FROM basket_shadow_objective_score"
    ).fetchone()
    # One name for one objective: the shadow scores the same functional form
    # the live selector maximises, so it records that form's identity rather
    # than minting a parallel one.
    assert row == (1, CURVE_OBJECTIVE_FORM)


def test_shadow_score_none_when_nothing_admitted() -> None:
    assert build_shadow_objective_score(
        station_id="ZUUU", event_date="2026-08-12",
        probability_stage="P0", checkpoint_id="c",
        decision_at_utc="t", candidate_rows=[],
        probabilities_by_bucket={}, budget_usdc=1.0,
        bankroll_usdc=200.0, created_at_utc="t",
    ) is None
