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


def test_solver_at_effective_bankroll_is_the_curve_score_optimum() -> None:
    # The generator hands the solver the ranking's effective bankroll W/f and
    # asks for fraction one, so its first candidate is not an approximation
    # of what curve_score wants -- it is that argmax.  Pin the equivalence on
    # a position with inventory, where the two formulations would otherwise
    # be free to disagree.
    import random

    from bayes_basket_target.basket_kernel.delta_v2 import (
        DEFAULT_KELLY_FRACTION,
        ValueWeightsV2,
        curve_score,
        evaluate_basket_value_v2,
    )

    seat = ("a", "b", "c", "d")
    probabilities = [0.10, 0.28, 0.34, 0.16]
    prices = [0.06, 0.22, 0.37, 0.13]
    held = [0.0, 40.0, 25.0, 0.0]
    sunk = 40 * 0.20 + 25 * 0.34
    cap = 1e7
    bankroll = 200.0
    full = {**dict(zip(seat, probabilities)), "rest": 0.12}
    weights = ValueWeightsV2.from_mapping({
        "lambda_profit_floor": 0.0, "lambda_cvar": 0.0,
        "lambda_shortfall": 0.0, "lambda_breakthrough": 0.0,
        "lambda_overfill": 0.0, "lambda_excess_hhi": 0.0,
        "cvar_alpha": 0.25, "hhi_max": 0.75,
        "expected_pnl_scale": 1.0, "profit_floor_scale": 1.0,
        "cvar_scale": 1.0, "coverage_scale": 1.0,
        "overfill_scale": 1.0, "excess_hhi_scale": 1.0,
    })

    def score(bought: list[float]) -> float:
        shares = {
            bucket: held[index] + bought[index]
            for index, bucket in enumerate(seat)
        }
        cost = {
            bucket: (sunk if index == 0 else 0.0)
            + bought[index] * prices[index]
            for index, bucket in enumerate(seat)
        }
        return curve_score(
            evaluate_basket_value_v2(
                probabilities=full,
                shares_by_bucket={**shares, "rest": 0.0},
                cost_by_bucket={**cost, "rest": 0.0},
                protected_buckets=seat, shortfall_buckets=(),
                breakthrough_buckets=(), weights=weights,
            ),
            bankroll_usdc=bankroll,
            kelly_fraction=DEFAULT_KELLY_FRACTION,
        )

    optimum = solve_fractional_kelly(
        probabilities=probabilities, prices=prices, depth_caps=[cap] * 4,
        budget_usdc=1e7, bankroll_usdc=bankroll / DEFAULT_KELLY_FRACTION,
        kelly_fraction=1.0, held_shares=held, sunk_cost_usdc=sunk,
    )
    best = score(optimum)
    random.seed(0)
    for _ in range(2000):
        spread = random.choice([0.02, 0.2, 2.0])
        nearby = [
            max(0.0, min(cap, value + random.gauss(0.0, spread)))
            for value in optimum
        ]
        assert score(nearby) <= best + 1e-12


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


def test_the_budget_face_is_split_by_equal_marginal_growth() -> None:
    # Coordinate ascent cannot travel along the budget face -- raising one
    # leg there requires lowering another, and no single-coordinate step
    # does both -- so the solver stalled the moment the budget bound, which
    # is every day worth trading.  Measured over 900 adversarial positions,
    # every allocation it got wrong was on that face; the worst put dollars
    # where the marginal growth per dollar was 129x lower than elsewhere.
    #
    # The condition is exact and holds budget-bound or not: differentiating
    # the growth gives p_i / (c_i * A_i) equal across every leg held
    # strictly inside its bounds.
    probabilities = [0.194, 0.202, 0.210, 0.282]
    prices = [0.19, 0.17, 0.57, 0.23]
    depth_caps = [135.8, 228.6, 318.4, 97.4]
    held = [0.0, 9.9, 0.0, 0.0]
    sunk = 13.1
    bankroll = 120.0
    budget = 9.8

    stakes = solve_fractional_kelly(
        probabilities=probabilities, prices=prices, depth_caps=depth_caps,
        budget_usdc=budget, bankroll_usdc=bankroll, kelly_fraction=1.0,
        held_shares=held, sunk_cost_usdc=sunk,
    )
    spend = sum(q * c for q, c in zip(stakes, prices))
    # The budget really does bind here, which is the case under test.
    assert spend == pytest.approx(budget, abs=1e-6)

    reserve = bankroll - sunk - spend
    assert reserve > 0.0
    interior = [
        index for index in range(len(prices))
        if stakes[index] > 1e-9 and stakes[index] + 1e-9 < depth_caps[index]
    ]
    assert len(interior) >= 3
    marginals = [
        probabilities[index]
        / (prices[index] * (reserve + held[index] + stakes[index]))
        for index in interior
    ]
    assert max(marginals) / min(marginals) - 1.0 < 1e-6

    # A leg pinned at zero is allowed to sit below that level, never above:
    # if it were above, dollars belong there and the solve left them behind.
    common = max(marginals)
    for index in range(len(prices)):
        if index in interior or stakes[index] > 1e-9:
            continue
        pinned = probabilities[index] / (
            prices[index] * (reserve + held[index])
        )
        assert pinned <= common * (1.0 + 1e-6)
