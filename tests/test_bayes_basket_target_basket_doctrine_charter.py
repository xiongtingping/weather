"""Charter clauses 35-41 held against the running code.

The doctrine these clauses state was arrived at by measurement, not taste: 21
consecutive production plans were scored and came back at a median 29% win
probability, 7.3x best-case odds and a 0.92 spike ratio -- a faithful
mispricing harvester where a basket machine was intended.  Writing the ruling
into the charter without a test would leave the same gap the measurement
exposed, so each clause here is asserted against the symbols that implement
it.  A clause that cannot be checked is a clause that will drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError as DomainContractError
from bayes_basket_target.basket_kernel.delta_v2 import (
    CANDIDATE_KIND_BASELINE,
    CURVE_OBJECTIVE_FORM,
    DEFAULT_KELLY_FRACTION,
    OBJECTIVE_FORMS,
    SUPPORT_SCENARIO_FLOOR_FRACTION,
    VALUE_OBJECTIVE_FORM,
    solve_target_inventory_v2,
)
from tests.test_bayes_basket_target_delta_v2 import _value_weights


CHARTER = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "constitution"
    / "SYSTEM_CHARTER.md"
)


def _clause(number: int) -> str:
    text = CHARTER.read_text(encoding="utf-8")
    match = re.search(
        rf"^{number}\. (.+?)(?=^\d+\. |\Z)",
        text,
        re.M | re.S,
    )
    assert match is not None, f"charter clause {number} missing"
    return match.group(1)


def test_clause_35_objective_form_matches_the_running_kernel() -> None:
    clause = _clause(35)
    assert CURVE_OBJECTIVE_FORM in clause
    assert VALUE_OBJECTIVE_FORM in clause
    assert OBJECTIVE_FORMS == {VALUE_OBJECTIVE_FORM, CURVE_OBJECTIVE_FORM}
    assert f"f={DEFAULT_KELLY_FRACTION}" in clause
    # The bankroll is the stable risk capital, never the depleting balance:
    # as remaining budget approaches zero the penalty diverges and a
    # zero-budget station fails closed instead of trading.
    assert "total_budget_usdc" in clause
    assert "剩余预算" in clause


def test_clause_31_no_longer_claims_the_linear_score_is_the_argmax() -> None:
    """The superseded objective must be labelled, not silently left standing.

    ``value_v2`` is still computed and still audited; what changed is that it
    stopped being what the argmax reads.  A charter that still described it as
    the objective would be the first implementation gap.
    """

    clause = _clause(31)
    assert "审计读数" in clause
    assert "第 35 条" in clause


def test_clause_37_floor_is_a_legality_gate_not_a_preference() -> None:
    clause = _clause(37)
    assert "SUPPORT_SCENARIO_FLOOR_FRACTION" in clause
    assert "_floor_legal" in clause
    assert SUPPORT_SCENARIO_FLOOR_FRACTION == -0.10

    lottery = {
        "candidate_id": "lottery",
        "shares_by_bucket": {"a": 9.6},
        "cost_by_bucket": {"a": 0.5},
    }
    hold = {
        "candidate_id": "hold",
        "shares_by_bucket": {},
        "cost_by_bucket": {},
        "candidate_kind": CANDIDATE_KIND_BASELINE,
    }
    # Clause 37: the no-new-action baseline is always legal, so a book
    # offering only floor-violating shapes resolves to HOLD rather than
    # degrading into the lottery.
    result = solve_target_inventory_v2(
        probabilities={"a": 0.2, "b": 0.8},
        feasible_inventory_candidates=[hold, lottery],
        protected_buckets=["a", "b"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=_value_weights(),
    )
    assert result["winner_candidate_id"] == "hold"
    assert result["floor_rejected_candidate_count"] == 1


def test_clause_37_baseline_is_exempt_because_holdings_cannot_be_unbought(
) -> None:
    """The floor governs what a decision adds, never what it inherited.

    Every position opened before this floor existed was sized by the
    unconstrained objective, so a station carrying one arrives with a shape
    the floor condemns.  Judging the baseline by it makes "hold, add nothing"
    -- the one answer that is always available -- illegal, and the solver
    raises instead of holding: the rule written to protect these stations
    would have been the thing that blocked them.
    """

    clause = _clause(37)
    assert "恒合法且豁免地板" in clause

    inherited = {
        "candidate_id": "no-new-action",
        "shares_by_bucket": {"a": 9.6, "b": 0.0},
        "cost_by_bucket": {"a": 0.5, "b": 0.0},
        "candidate_kind": CANDIDATE_KIND_BASELINE,
    }
    add_on = {
        "candidate_id": "add-to-the-skew",
        "shares_by_bucket": {"a": 9.6, "b": 0.2},
        "cost_by_bucket": {"a": 0.5, "b": 0.03},
        "candidate_kind": "COMPLETE_BASKET_TARGET",
    }
    result = solve_target_inventory_v2(
        probabilities={"a": 0.2, "b": 0.8},
        feasible_inventory_candidates=[inherited, add_on],
        protected_buckets=["a", "b"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=_value_weights(),
    )
    assert result["winner_candidate_id"] == "no-new-action"
    # The spending candidate is still judged: only the baseline is exempt.
    assert result["floor_rejected_candidate_count"] == 1
    # And the inherited shape is reported honestly rather than hidden.
    assert result["target_support_floor_pnl"] == pytest.approx(-0.5)


def test_clause_38_floor_relaxes_as_the_support_collapses() -> None:
    """封下尾不封上凸, and the floor dissolves at a one-bucket support.

    This is the continuous dial between the basket machine and the certainty
    machine: with four live buckets the floor forces the basket to protect all
    of them; once the hard lower bound has killed everything but one, going
    all-in on the survivor is fully legal, because that is no longer a bet.
    """

    clause = _clause(38)
    assert "封下尾" in clause and "不封上凸" in clause

    concentrated = {
        "candidate_id": "all-in",
        "shares_by_bucket": {"a": 9.6},
        "cost_by_bucket": {"a": 0.5},
    }
    hold = {
        "candidate_id": "hold",
        "shares_by_bucket": {},
        "cost_by_bucket": {},
        "candidate_kind": CANDIDATE_KIND_BASELINE,
    }
    # Same candidate, support collapsed to the single surviving bucket.
    result = solve_target_inventory_v2(
        probabilities={"a": 1.0},
        feasible_inventory_candidates=[hold, concentrated],
        protected_buckets=["a"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=_value_weights(),
    )
    assert result["winner_candidate_id"] == "all-in"
    assert result["floor_rejected_candidate_count"] == 0
    # Unbounded upside: the winning scenario is not capped by the floor.
    assert result["target_scenario_pnl"]["a"] == pytest.approx(9.1)


def test_clause_38_floor_scope_is_live_evidence_not_the_chosen_window(
) -> None:
    """The optimizer picks the window; it must not pick the floor's scope.

    Clause 38 relaxes the floor as the support collapses, and that clause is
    about the *hard bound* removing outcomes -- certainty arriving.  Once the
    objective also chooses the window, reading the floor off its choice makes
    the constrained party the author of its own constraint: narrow to one
    bucket and the floor protects nothing, while the outcomes walked away
    from still cost the whole premium.  The scope is therefore the live
    evidence set, and a window that abandons a live bucket is judged on it.
    """

    clause = _clause(38)
    assert "硬下界" in clause

    live = ["a", "b"]
    # Seats only "b" while "a" is still live: "a" settling costs the premium.
    narrow = {
        "candidate_id": "abandon-a",
        "shares_by_bucket": {"a": 0.0, "b": 5.0},
        "cost_by_bucket": {"a": 0.0, "b": 0.5},
        "candidate_kind": "COMPLETE_BASKET_TARGET",
    }
    hold = {
        "candidate_id": "hold",
        "shares_by_bucket": {},
        "cost_by_bucket": {},
        "candidate_kind": CANDIDATE_KIND_BASELINE,
    }
    narrow_scope = solve_target_inventory_v2(
        probabilities={"a": 0.2, "b": 0.8},
        feasible_inventory_candidates=[hold, narrow],
        protected_buckets=["b"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=_value_weights(),
    )
    live_scope = solve_target_inventory_v2(
        probabilities={"a": 0.2, "b": 0.8},
        feasible_inventory_candidates=[hold, narrow],
        protected_buckets=live,
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=_value_weights(),
    )
    # Scoped to its own window the abandoning basket looks clean and is
    # seated; scoped to the live evidence the floor sees the -100% leg.
    assert narrow_scope["winner_candidate_id"] == "abandon-a"
    assert narrow_scope["floor_rejected_candidate_count"] == 0
    assert live_scope["winner_candidate_id"] == "hold"
    assert live_scope["floor_rejected_candidate_count"] == 1


def test_clause_40_dials_are_reported_on_every_solution() -> None:
    clause = _clause(40)
    for symbol in (
        "target_win_probability",
        "target_odds_ratio",
        "target_support_floor_pnl",
        "floor_rejected_candidate_count",
    ):
        assert symbol in clause

    result = solve_target_inventory_v2(
        probabilities={"a": 0.2, "b": 0.8},
        feasible_inventory_candidates=[
            {
                "candidate_id": "flat",
                "shares_by_bucket": {"a": 1.0, "b": 1.4},
                "cost_by_bucket": {"a": 0.55, "b": 0.55},
            },
        ],
        protected_buckets=["a", "b"],
        shortfall_buckets=[],
        breakthrough_buckets=[],
        weights=_value_weights(),
    )
    assert result["target_win_probability"] == pytest.approx(0.8)
    assert result["target_odds_ratio"] == pytest.approx(3.0)
    assert result["target_support_floor_pnl"] == pytest.approx(-0.1)
    assert result["support_scenario_floor_fraction"] == -0.10
    # Clause 40's break-even arithmetic, stated so a future edit to the win
    # target cannot silently leave the odds floor behind: at w the blend is
    # break-even when mean-win/mean-loss equals (1-w)/w.
    assert (1 - 0.55) / 0.55 == pytest.approx(0.818, abs=1e-3)


def test_clause_36_forbids_a_second_entry_price_gate() -> None:
    """No price threshold beyond "whole basket beats holding".

    A live support routinely prices above 0.50; a "support must be cheaper
    than X" or "odds must exceed Y" gate would not shape the curve, it would
    delete ordinary trading days.  The kernel must therefore expose exactly
    two objective forms and no admission threshold constant beside them.
    """

    clause = _clause(36)
    assert "整篮" in clause
    assert "单桶 EV 永不构成买入门槛" in clause

    from bayes_basket_target.basket_kernel import delta_v2

    banned = [
        name
        for name in dir(delta_v2)
        if name.isupper()
        and (
            "MIN_ODDS" in name
            or "MAX_SUPPORT_PRICE" in name
            or "MIN_EDGE" in name
        )
    ]
    assert banned == []


def test_clause_41_breaker_can_only_stop_new_intents() -> None:
    clause = _clause(41)
    assert "STOP_NEW_INTENTS_ONLY" in clause
    assert "station_regional_blocks_v1.json" in clause

    import json

    from bayes_basket_target.portfolio_inventory import (
        load_portfolio_capital_policy,
    )

    policy_path = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "trading"
        / "portfolio_capital_policy_v1.json"
    )
    policy, _sha = load_portfolio_capital_policy(policy_path)
    assert policy["drawdown_circuit_breaker"]["action"] == (
        "STOP_NEW_INTENTS_ONLY"
    )

    # Unwinding realises, at the current book, exactly the loss the breaker is
    # reacting to -- the same philosophy as the P1 hard bound only ever
    # constraining new exposure.  The loader must refuse it rather than trust
    # the config file to stay well-behaved.
    tampered = json.loads(policy_path.read_text(encoding="utf-8"))
    tampered["drawdown_circuit_breaker"]["action"] = "UNWIND_ALL"
    tampered_path = policy_path.parent / "_charter_audit_tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    try:
        with pytest.raises(DomainContractError):
            load_portfolio_capital_policy(tampered_path)
    finally:
        tampered_path.unlink()


def test_clause_42_probability_core_is_price_blind() -> None:
    """The evidence layers must not see the book.

    The probability core answers "where could the day land" from evidence
    alone; the objective answers "where is it worth buying" from prices.
    Measured over 37 station-days the two disagree on 92% of windows, so a
    price leak into the core would silently move seat selection out of the
    objective's hands -- the exact drift clause 42 exists to prevent.  Same
    probabilities under wildly different books must yield the same core.
    """

    clause = _clause(42)
    assert "永不读价格" in clause
    assert "席位唯一权威" in clause

    from bayes_basket_target.basket_kernel.contracts import (
        TargetBasketPolicy,
        TargetBasketSnapshot,
    )
    from bayes_basket_target.basket_kernel.support import _choose_support

    def snapshot(prices: dict[str, float]) -> TargetBasketSnapshot:
        return TargetBasketSnapshot.from_mapping({
            "schema": "target_basket_snapshot_v6",
            "snapshot_id": "doctrine-price-blind",
            "sequence": 1,
            "observed_at": "2026-08-13T03:00:00Z",
            "market_id": "doctrine-market",
            "station_id": "ZUUU",
            "event_date": "2026-08-13",
            "quote_version": "quote-v1",
            "quote_observed_at": "2026-08-13T03:00:00Z",
            "quote_source": "OFFICIAL_CLOB_WS",
            "inventory_version": "inventory-v1",
            "order_state_version": "order-v1",
            "evidence": {
                "probability_stage": "P0",
                "posterior_version": "p0-v1",
                "posterior_sha256": "a" * 64,
            },
            "buckets": [
                {
                    "bucket": str(35 + index),
                    "ordinal": 35 + index,
                    "probability": probability,
                    "bid_price": max(0.01, prices[str(35 + index)] - 0.01),
                    "bid_levels": [{
                        "price": max(0.01, prices[str(35 + index)] - 0.01),
                        "shares": 10.0,
                    }],
                    "ask_levels": [{
                        "price": prices[str(35 + index)],
                        "shares": 100.0,
                    }],
                    "price_tick_size": 0.01,
                    "maker_queue_ahead_shares": 2.0,
                    "maker_queue_known": True,
                }
                for index, probability in enumerate((0.2, 0.4, 0.3, 0.1))
            ],
            "checkpoint_changed": True,
            "quote_changed": True,
            "fill_changed": True,
            "time_gate_changed": True,
            "evidence_fresh": True,
            "contract_matches": True,
            "order_state_known": True,
            "risk_inputs_complete": True,
            "time_left_minutes": 1440.0,
        })

    policy = TargetBasketPolicy.from_mapping({
        "total_budget_usdc": 5.0,
        "base_coverage": 0.6,
        "maximum_coverage": 0.8,
        "entropy_coverage_gain": 0.0,
        "clip_min_notional_usdc": 0.01,
        "clip_max_notional_usdc": 1.0,
        "protected_floor_usdc": -5.0,
        "max_outside_loss_usdc": 5.0,
    })
    cheap = _choose_support(
        snapshot({"35": 0.03, "36": 0.04, "37": 0.03, "38": 0.02}), policy
    )
    dear = _choose_support(
        snapshot({"35": 0.55, "36": 0.75, "37": 0.65, "38": 0.45}), policy
    )
    # base (the probability core) is return slot 2 in both calls.
    assert cheap[2] == dear[2]


def test_clause_43_no_weather_branch_in_the_strategy_layer() -> None:
    """Direction lives inside p(t); the seat path must stay weather-blind.

    Measured on 173 settled station-days the calibrated centres carry the
    same residual on rainy days (+0.63 mean, 52% high) as on clear ones
    (+0.55, 49%): the models and calibration have already priced the rain.
    A strategy-layer weather branch would count that evidence twice, crush
    the calibration this measurement just validated, and do it with a binary
    shove where the probability already speaks in degrees.
    """

    clause = _clause(43)
    assert "双重计数" in clause
    # The approved successor is no longer a plan: the conditional tail layer
    # is live inside p(t), with its measurements and its four rejected
    # candidates recorded in the clause itself.
    assert "conditional_tail_skew_v1" in clause
    assert "同日气候余量" in clause

    from bayes_basket_target.cross_consensus import (
        CLIMATOLOGY_HEADROOM_BANDS,
        TAF_DIRECTION_COLD,
        TAF_DIRECTION_WARM,
    )

    # Width: the band a day falls in must matter, and a day at the top of its
    # own record must widen more than an ordinary one.
    factors = [factor for _bound, factor in CLIMATOLOGY_HEADROOM_BANDS]
    assert factors[0] > factors[-1]
    # Skew: the two TAF directions must be mirror images, or the layer is
    # scaling rather than pointing.
    assert TAF_DIRECTION_WARM[0] > 1.0 > TAF_DIRECTION_WARM[1]
    assert TAF_DIRECTION_COLD[1] > 1.0 > TAF_DIRECTION_COLD[0]
    assert TAF_DIRECTION_WARM == TAF_DIRECTION_COLD[::-1]

    strategy_files = (
        "bayes_basket_target/basket_kernel/delta_v2.py",
        "bayes_basket_target/basket_kernel/support.py",
        "bayes_basket_target/delta_v2_runtime.py",
    )
    weather_identifiers = (
        "precipitation_present",
        "cloud_cover_fraction",
        "dewpoint_depression",
        "wind_speed_mps",
        "wind_gust_mps",
    )
    root = Path(__file__).resolve().parents[1]
    for rel in strategy_files:
        source = (root / rel).read_text(encoding="utf-8")
        leaked = [name for name in weather_identifiers if name in source]
        assert leaked == [], f"{rel} reads weather features: {leaked}"


def test_clause_44_truth_exclusions_reach_every_scoring_path() -> None:
    """A config nobody reads is a comment.

    The six excluded station-days were settled before their own local
    afternoon and cannot be revised, so both places that score a model
    against truth must skip them: the OOS gate that decides which systems
    are seated, and the trajectory folds that teach the day's shape.  One
    without the other leaves the defect alive in whichever path was missed.
    """

    clause = _clause(44)
    assert "最新 `revision_no`" in clause
    assert "retired_ingest_truth_exclusions_v1.json" in clause

    from bayes_basket_target.truth_exclusions import (
        is_excluded,
        load_truth_exclusions,
    )

    excluded = load_truth_exclusions()
    assert ("KSFO", "2026-08-01") in excluded
    assert is_excluded("ksfo", "2026-08-01"), "station id must be case-folded"
    # Repaired-by-revision station-days are *not* excluded: they were fixed
    # the ordinary way and their latest revision is correct.
    assert not is_excluded("KMIA", "2026-08-01")

    root = Path(__file__).resolve().parents[1]
    for rel in (
        "bayes_basket_target/native_ensemble_oos.py",
        "scripts/build_bayes_basket_target_metar_trajectory_training_packet.py",
    ):
        source = (root / rel).read_text(encoding="utf-8")
        assert "is_excluded" in source, f"{rel} does not apply exclusions"


def _wide_snapshot(probabilities: tuple[float, ...], ask: float) -> object:
    from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot

    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "doctrine-domain",
        "sequence": 1,
        "observed_at": "2026-08-13T03:00:00Z",
        "market_id": "doctrine-market",
        "station_id": "ZUUU",
        "event_date": "2026-08-13",
        "quote_version": "quote-v1",
        "quote_observed_at": "2026-08-13T03:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-v1",
        "order_state_version": "order-v1",
        "evidence": {
            "probability_stage": "P0",
            "posterior_version": "p0-v1",
            "posterior_sha256": "a" * 64,
        },
        "buckets": [
            {
                "bucket": str(32 + index),
                "ordinal": 32 + index,
                "probability": probability,
                "bid_price": round(ask - 0.01, 4),
                "bid_levels": [{"price": round(ask - 0.01, 4), "shares": 10.0}],
                "ask_levels": [{"price": ask, "shares": 100.0}],
                "price_tick_size": 0.01,
                "maker_queue_ahead_shares": 2.0,
                "maker_queue_known": True,
            }
            for index, probability in enumerate(probabilities)
        ],
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
        "time_left_minutes": 1440.0,
    })


def test_clause_35_candidate_domain_is_the_reachable_day_not_the_core() -> None:
    """Seat selection must be allowed to look outside the probability core.

    Drawing candidates from the core makes the argmax a formality: the core is
    chosen by mass, and mass is exactly what the book prices richest, so every
    sub-window of the core is expensive and only the incumbent survives the
    rails.  That is what production showed -- 34 of 40 plans had a single
    admissible window, their own.  The domain is instead every bucket the
    evidence can still reach; the hard lower bound zeroes what it excludes, so
    nothing unreachable can be a candidate.
    """

    clause = _clause(35)
    assert "定义域不得收窄为概率核" in clause
    assert "入围只排序、不排除" in clause

    from bayes_basket_target.basket_kernel.contracts import TargetBasketPolicy
    from bayes_basket_target.delta_v2_runtime import _support_windows

    policy = TargetBasketPolicy.from_mapping({
        "total_budget_usdc": 5.0,
        "base_coverage": 0.6,
        "maximum_coverage": 0.8,
        "entropy_coverage_gain": 0.0,
        "clip_min_notional_usdc": 0.01,
        "clip_max_notional_usdc": 1.0,
        "protected_floor_usdc": -5.0,
        "max_outside_loss_usdc": 5.0,
    })
    probabilities = (0.05, 0.10, 0.62, 0.13, 0.07, 0.03)
    snapshot = _wide_snapshot(probabilities, ask=0.10)
    by_bucket = {
        row.bucket: float(row.probability) for row in snapshot.buckets
    }
    windows = _support_windows(
        snapshot=snapshot,
        desired={"34"},
        probabilities=by_bucket,
        fee_rate=0.0,
        maximum_support_buckets=policy.maximum_support_buckets,
        center_bucket="36",
    )
    seats = [tuple(window["buckets"]) for window in windows]
    assert ("34",) in seats, "the incumbent is always available"
    outside = [seat for seat in seats if any(b != "34" for b in seat)]
    assert outside, "the core must not be the whole candidate domain"


def test_clause_37_window_enumeration_respects_the_four_bucket_cap() -> None:
    """A wider domain must not become a wider basket.

    Clause 37 makes "contiguous and at most four" a structural legality, not a
    preference, so it binds the enumeration itself -- a six-bucket window that
    scores well must never be offered to the objective in the first place.
    """

    assert "至多 4 桶" in _clause(37)

    from bayes_basket_target.basket_kernel.contracts import TargetBasketPolicy
    from bayes_basket_target.delta_v2_runtime import _support_windows

    policy = TargetBasketPolicy.from_mapping({
        "total_budget_usdc": 5.0,
        "base_coverage": 0.6,
        "maximum_coverage": 0.8,
        "entropy_coverage_gain": 0.0,
        "clip_min_notional_usdc": 0.01,
        "clip_max_notional_usdc": 1.0,
        "protected_floor_usdc": -5.0,
        "max_outside_loss_usdc": 5.0,
    })
    probabilities = (0.16, 0.17, 0.17, 0.17, 0.17, 0.16)
    snapshot = _wide_snapshot(probabilities, ask=0.02)
    by_bucket = {
        row.bucket: float(row.probability) for row in snapshot.buckets
    }
    windows = _support_windows(
        snapshot=snapshot,
        desired={"33", "34"},
        probabilities=by_bucket,
        fee_rate=0.0,
        maximum_support_buckets=policy.maximum_support_buckets,
        center_bucket="36",
    )
    assert windows, "cheap uniform book must admit something"
    for window in windows:
        buckets = list(window["buckets"])
        assert len(buckets) <= policy.maximum_support_buckets
        ordinals = [int(bucket) for bucket in buckets]
        assert ordinals == list(range(ordinals[0], ordinals[0] + len(ordinals)))


def test_clause_37_incumbent_survives_a_domain_it_cannot_be_enumerated_in() -> None:
    """"Hold what we hold" must never fall off the menu.

    Enumeration only produces contiguous runs of at most four buckets, but an
    inherited support is under no such obligation -- a live ZHHH core sat on
    {31C, 33C or higher} with 32C alive in between.  If the incumbent is not
    offered, the optimizer cannot elect to keep the current position, and the
    only remaining way to "keep" it is to narrow into a sub-window, which is
    exactly the floor escape clause 38 scopes the floor to live buckets to
    prevent.
    """

    from bayes_basket_target.basket_kernel.contracts import TargetBasketPolicy
    from bayes_basket_target.delta_v2_runtime import _support_windows

    policy = TargetBasketPolicy.from_mapping({
        "total_budget_usdc": 5.0,
        "base_coverage": 0.6,
        "maximum_coverage": 0.8,
        "entropy_coverage_gain": 0.0,
        "clip_min_notional_usdc": 0.01,
        "clip_max_notional_usdc": 1.0,
        "protected_floor_usdc": -5.0,
        "max_outside_loss_usdc": 5.0,
    })
    snapshot = _wide_snapshot((0.10, 0.15, 0.20, 0.25, 0.20, 0.10), ask=0.90)
    by_bucket = {
        row.bucket: float(row.probability) for row in snapshot.buckets
    }

    # Gapped: 33 is held, 34 is not, 35 is held.
    gapped = _support_windows(
        snapshot=snapshot,
        desired={"33", "35"},
        probabilities=by_bucket,
        fee_rate=0.0,
        maximum_support_buckets=policy.maximum_support_buckets,
        center_bucket="36",
    )
    assert ("33", "35") in [tuple(row["buckets"]) for row in gapped]

    # Wider than the structural cap, because it was inherited from a policy
    # that allowed more.
    oversized = _support_windows(
        snapshot=snapshot,
        desired={"32", "33", "34", "35", "36"},
        probabilities=by_bucket,
        fee_rate=0.0,
        maximum_support_buckets=policy.maximum_support_buckets,
        center_bucket="36",
    )
    seats = [tuple(row["buckets"]) for row in oversized]
    assert ("32", "33", "34", "35", "36") in seats
    assert all(
        len(seat) <= policy.maximum_support_buckets
        for seat, row in zip(seats, oversized)
        if not row["is_incumbent"]
    ), "only the incumbent may exceed the cap, and only because it already does"


def test_clause_45_market_center_outside_envelope_blocks_new_money_only() -> None:
    """The gate stops new intents; it never touches p or holdings.

    ZUUU 2026-08-13 is the type specimen: a starved two-system root centred
    at 35 against a book centred at 37.  Every "edge" the optimizer saw in
    the low buckets was phantom belief, and the one supervisor positioned to
    notice -- the market's own center leaving the model envelope -- was
    diagnostic-only.  Clause 45 makes it a capital gate: same snapshot, same
    probabilities, and the only thing that changes when the market center
    walks out of the envelope is that no new buy is authorized.
    """

    clause = _clause(45)
    assert "只挡新钱" in clause
    assert "market_center_outside_model_envelope" in clause

    from bayes_basket_target.basket_kernel.contracts import (
        TargetBasketPolicy,
        TargetBasketSnapshot,
    )
    from bayes_basket_target.delta_v2_runtime import (
        compile_delta_v2_runtime_plan,
    )
    from tests.test_bayes_basket_target_delta_v2_runtime import (
        _basket_policy,
        _delta_policy,
        _fill_resolver,
    )

    def plan_with_market_center(outside: bool) -> dict:
        raw = TargetBasketSnapshot.from_mapping({
            "schema": "target_basket_snapshot_v6",
            "snapshot_id": "doctrine-envelope-gate",
            "sequence": 1,
            "observed_at": "2026-08-13T03:00:00Z",
            "market_id": "doctrine-market",
            "station_id": "RKPK",
            "event_date": "2026-08-13",
            "quote_version": "quote-v1",
            "quote_observed_at": "2026-08-13T03:00:00Z",
            "quote_source": "OFFICIAL_CLOB_WS",
            "inventory_version": "inventory-v1",
            "order_state_version": "order-v1",
            "evidence": {
                "probability_stage": "P0",
                "posterior_version": "p0-v1",
                "posterior_sha256": "a" * 64,
            },
            "buckets": [
                {
                    "bucket": "35",
                    "ordinal": 35,
                    "probability": 0.3,
                    "bid_price": 0.04,
                    "bid_levels": [{"price": 0.04, "shares": 10.0}],
                    "ask_levels": [{"price": 0.20, "shares": 100.0}],
                    "price_tick_size": 0.01,
                    "maker_queue_ahead_shares": 2.0,
                    "maker_queue_known": True,
                },
                {
                    "bucket": "36",
                    "ordinal": 36,
                    "probability": 0.7,
                    "bid_price": 0.05,
                    "bid_levels": [{"price": 0.05, "shares": 10.0}],
                    "ask_levels": [{"price": 0.20, "shares": 100.0}],
                    "price_tick_size": 0.01,
                    "maker_queue_ahead_shares": 2.0,
                    "maker_queue_known": True,
                },
                {
                    # An evidence-dead bucket the model gives nothing; when
                    # the book prices it richest, the market center has left
                    # the model envelope.
                    "bucket": "39",
                    "ordinal": 39,
                    "probability": 0.0,
                    "bid_price": 0.80 if outside else 0.01,
                    "bid_levels": [{
                        "price": 0.80 if outside else 0.01,
                        "shares": 10.0,
                    }],
                    "ask_levels": [{
                        "price": 0.90 if outside else 0.02,
                        "shares": 100.0,
                    }],
                    "price_tick_size": 0.01,
                    "maker_queue_ahead_shares": 2.0,
                    "maker_queue_known": True,
                },
            ],
            "checkpoint_changed": True,
            "quote_changed": True,
            "fill_changed": True,
            "time_gate_changed": True,
            "evidence_fresh": True,
            "contract_matches": True,
            "order_state_known": True,
            "risk_inputs_complete": True,
            "time_left_minutes": 1440.0,
        })
        return compile_delta_v2_runtime_plan(
            snapshot=raw,
            basket_policy=_basket_policy(),
            delta_policy=_delta_policy(),
            fill_resolver=_fill_resolver,
            capital_available_usdc=5.0,
            minimum_profit_floor=-5.0,
            resource_probe_ok=True,
        )

    inside = plan_with_market_center(outside=False)
    outside = plan_with_market_center(outside=True)

    assert inside["envelope_entry_gate"]["blocked"] is False
    assert inside["delta_v2_action_selection"]["selected_actions"]

    gate = outside["envelope_entry_gate"]
    assert gate["blocked"] is True
    assert gate["blocks_new_intents_only"] is True
    assert outside["market_center_bucket"] == "39"
    assert outside["delta_v2_action_selection"]["selected_actions"] == []
    assert outside["delta_v2_audit"]["authorized"] is False
    assert "market_center_outside_model_envelope" in (
        outside["delta_v2_action_selection"]["blockers"]
    )
    # Probabilities are untouched: the gate is a capital gate, not a p(t)
    # channel.
    assert outside["candidate_support"] == inside["candidate_support"]


def test_clause_42_taf_is_mandatory_envelope_evidence_and_fails_closed() -> None:
    """TAF reaches the envelope by evidence, never by price.

    The envelope's only legal widener is evidence; a swallowed query error
    used to compile a narrower envelope that forbade the forecaster's bucket
    for the rest of the root's life.  Absence (no table, no TX issued) stays
    a legitimate None; an unanswerable question fails closed.
    """

    clause = _clause(42)
    assert "TAF 是强制包络证据" in clause
    assert "不得为容纳市场中心而扩张" in clause

    import sqlite3 as _sqlite3

    from bayes_basket_target.native_ensemble_p0 import (
        _station_issues_taf_tx,
        _taf_envelope_witness,
    )

    # Absence: a deployment without the TAF lane has no TAF evidence.
    bare = _sqlite3.connect(":memory:")
    assert _taf_envelope_witness(
        bare,
        station_id="ZUUU",
        event_date="2026-08-13",
        decision_at_utc="2026-08-13T00:00:00Z",
    ) is None
    assert _station_issues_taf_tx(bare, station_id="ZUUU") is False

    # Failure: the table exists but cannot be read -> fail closed, both ways.
    broken = _sqlite3.connect(":memory:")
    broken.execute(
        "CREATE TABLE calibration_forward_taf_capture (wrong_shape TEXT)"
    )
    with pytest.raises(
        DomainContractError, match="taf_envelope_witness_query_failed"
    ):
        _taf_envelope_witness(
            broken,
            station_id="ZUUU",
            event_date="2026-08-13",
            decision_at_utc="2026-08-13T00:00:00Z",
        )
    with pytest.raises(
        DomainContractError, match="taf_station_capability_query_failed"
    ):
        _station_issues_taf_tx(broken, station_id="ZUUU")


def test_clause_42_envelope_compiler_source_is_price_blind() -> None:
    """The evidence compiler must not even know the book's vocabulary.

    Clause 42 says the envelope never reads prices and clause 45 says the
    market center may legitimately sit outside it; both collapse if a later
    edit lets the P0 compiler peek at quotes to "fix" a disagreement.  Same
    enforcement idiom as the strategy layer's weather-blindness: the source
    may not contain book identifiers at all.
    """

    source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "native_ensemble_p0.py"
    ).read_text(encoding="utf-8")
    for token in (
        "bid_price",
        "ask_price",
        "bid_levels",
        "ask_levels",
        "order_book",
        "market_center",
    ):
        assert token not in source, (
            f"price vocabulary {token!r} leaked into the envelope compiler"
        )


def test_clause_35_objective_owns_seat_quantity_price_and_dials() -> None:
    """The model nominates; the objective decides all five powers.

    Fixture: the inherited core is {36} but both buckets offer at 0.20
    against 0.30/0.70 of belief, so the pair is strictly better.  Every
    decision the plan carries must then come from the objective's seated
    solution, not from the core's nomination: the support is the pair, each
    seated bucket gets a positive objective-solved quantity, every limit
    price is a legal post bounded by the book, and the clause-40 dials are
    published for the seated basket.
    """

    clause = _clause(35)
    assert "模型选桶仅是参考与提名" in clause
    assert "五项全部唯一归本条目标函数" in clause

    from bayes_basket_target.basket_kernel.contracts import (
        TargetBasketSnapshot,
    )
    from bayes_basket_target.delta_v2_runtime import (
        compile_delta_v2_runtime_plan,
    )
    from tests.test_bayes_basket_target_delta_v2_runtime import (
        _basket_policy,
        _delta_policy,
        _fill_resolver,
        _snapshot,
    )

    raw = _snapshot().to_dict()
    raw["current_core_buckets"] = ["36"]
    plan = compile_delta_v2_runtime_plan(
        snapshot=TargetBasketSnapshot.from_mapping(raw),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )

    seated = plan["basket_support_selection"]["seated"]
    # Power 1: the seat, not the core's nomination.
    assert seated["buckets"] == ["35", "36"]
    assert plan["candidate_support"] == seated["buckets"]
    # Power 2: quantities are the objective's solve, positive on every
    # seated bucket.
    shares = {
        row["bucket"]: float(row["target_shares"])
        for row in plan["targets"]
    }
    for bucket in seated["buckets"]:
        assert shares.get(bucket, 0.0) > 0.0
    # Power 3: every offered price is a legal post bounded by the book --
    # never the static reservation grid floor, never a crossing price.
    actions = plan["delta_v2_action_selection"]["selected_actions"]
    assert actions
    by_bucket = {row.bucket: row for row in TargetBasketSnapshot.from_mapping(
        raw
    ).buckets}
    for entry in actions:
        action = entry["action"]
        row = by_bucket[str(action["bucket"])]
        assert float(action["limit_price"]) < float(row.ask_price) + 1e-9
        assert float(action["limit_price"]) > 0.0
    # Powers 4 and 5: EV and the clause-40 dials are published for the
    # seated basket.
    assert isinstance(seated["edge"], float)
    assert plan["basket_win_probability"] == pytest.approx(
        seated["win_probability"]
    )
    # A full-partition basket has no losing scenario, so the odds ratio is
    # legitimately undefined (None) -- that is the certainty-harvest shape,
    # not a missing dial.
    assert plan["basket_odds_ratio"] is None or plan["basket_odds_ratio"] > 0.0
    assert plan["basket_support_floor_pnl_usdc"] > 0.0


def test_clause_46_p0_opens_positions_without_waiting_for_p1() -> None:
    """Stage-open is the first path; preliminary is a recorded failure.

    The drift this clause kills: treating ``preliminary_no_live_p1_pending``
    as a stage gate ("P0 waits for P1") when it is the fallback receipt for
    a stage-open that concretely failed.  The worker source must attempt the
    atomic open before it may write the preliminary record, and the fallback
    must record why the open failed.
    """

    clause = _clause(46)
    assert "P0 建仓、P0-M 确认调仓、P1 动态调仓" in clause
    assert "永不允许作为先验门" in clause

    source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "production_worker.py"
    ).read_text(encoding="utf-8")
    open_at = source.index("commit_production_candidate_atomic(")
    preliminary_at = source.index("commit_preliminary_scope_decision(")
    assert open_at < preliminary_at, (
        "the atomic stage-open must be attempted before the preliminary "
        "fallback record"
    )
    assert "stage_open_blocker" in source, (
        "a failed stage-open must record why, or the fallback becomes an "
        "unexplained gate again"
    )


def test_clause_48_listing_window_opens_positions() -> None:
    """Listing-at-entry: subscribe rootless, cold-start virgin leads, serve
    split packets with named riders.

    The drifts this clause kills: an empty probability claim treated as an
    expired one (catalog circuit-break on rootless listing scopes), a virgin
    lead bucket starving its own listing window while its forward window
    fills, and any gate that rebuilds its expected cycle map from the
    packet's current runs instead of the activation's fitted pairing.
    """

    clause = _clause(48)
    assert "订阅面 = 概率信封 ∪ registry 挂牌面" in clause
    assert "虚桶冷启动" in clause
    assert "分裂包照常服务" in clause

    from bayes_basket_target.native_ensemble_p0 import (
        HOUR_LEAD_BUCKET_FRESHNESS_ORDER,
        HOUR_LEAD_FALLBACK_CHAIN,
    )

    # Every chain starts at its own bucket (exact bucket auto-takes-over)
    # and borrows only strictly shorter trained groups.
    order = {
        bucket: index
        for index, bucket in enumerate(HOUR_LEAD_BUCKET_FRESHNESS_ORDER)
    }
    for bucket, chain in HOUR_LEAD_FALLBACK_CHAIN.items():
        assert chain[0] == bucket
        assert all(
            order[candidate] <= order[bucket] for candidate in chain
        )

    source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "native_ensemble_p0.py"
    ).read_text(encoding="utf-8")
    for lineage_key in (
        "provisional_lead_fallback_json",
        "hour_bucket_split_riders_json",
        "activation_cycle_riders_json",
    ):
        assert lineage_key in source, (
            "every borrow must be named in lineage: " + lineage_key
        )

    publisher = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "resident_target_ws_publisher.py"
    ).read_text(encoding="utf-8")
    assert "set(current_probability) | set(rows_by_scope)" in publisher, (
        "the catalog must union envelope scopes with registry listing scopes"
    )


def test_clause_38_floor_scope_follows_coverability() -> None:
    """The floor protects the whole live set exactly when a window can span it.

    The drift this kills in both directions: scoping the floor to the
    optimizer's window while the live set is coverable (self-written
    constraints, the original clause 38 concern), and scoping it to a live
    set wider than any legal window (a ban dressed as a floor -- eleven
    listing-day live buckets against a four-bucket support made every
    spending candidate illegal and the opening stage structurally unable
    to buy, colliding with charters 46 and 48).
    """

    clause = _clause(38)
    assert "可覆盖性" in clause
    assert "支持窗自身" in clause

    source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "delta_v2_runtime.py"
    ).read_text(encoding="utf-8")
    conditional = (
        "protected_buckets=(\n"
        "                    live_buckets\n"
        "                    if len(live_buckets)\n"
        "                    <= max(1, int(basket_policy.maximum_support_buckets))\n"
        "                    else window\n"
        "                ),"
    )
    assert conditional in source, (
        "the window solver must scope the floor by coverability, not "
        "unconditionally by the live set"
    )


def test_uncalibrated_member_never_serves_identity_parameters() -> None:
    """A provisional member is uncertified, not neutral.

    Measured 2026-08-14 across 6,933 station-day-system cases: served with
    identity parameters (no bias, no spread change) ICON ran a z standard
    deviation of 2.88 with its 95% interval covering 61% of outcomes and a
    +0.85 sigma cold bias, while holding a third of ZUUU's combination
    weight.  Every system in this fleet needs 2.5x-4.7x spread inflation and
    runs cold by 0.3C-1.9C, so identity parameters assert the opposite of
    both measurements.  The provisional member borrows its station's own
    certified peers' median instead, and never serves narrower than the raw
    member cloud.
    """

    source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "native_ensemble_p0.py"
    ).read_text(encoding="utf-8")

    assert '"spread_factor": 1.0' not in source, (
        "an uncalibrated member must not serve an identity spread"
    )
    assert '"bias_c": 0.0' not in source, (
        "an uncalibrated member must not serve an identity bias"
    )
    assert "PEER_MEDIAN_STATION_LOCAL" in source
    # The borrow is station-local (the weights ruling) and floored at the raw
    # cloud, and it stays named as provisional rather than becoming a
    # certificate.
    assert "provisional_parameter_sources_json" in source
    assert "provisional_uncalibrated_system_ids_json" in source

    from bayes_basket_target.native_ensemble_oos import (
        build_native_ensemble_oos,  # noqa: F401
    )

    oos_source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "native_ensemble_oos.py"
    ).read_text(encoding="utf-8")
    # The inflation cap must clear the measured requirement (4.7x worst
    # case); a cap that binds is a distribution knowingly served too narrow.
    assert "spread_factor = min(\n        5.0," in oos_source


def test_borrowed_bias_needs_a_real_median_and_a_measured_ceiling() -> None:
    """Borrowing spread and borrowing bias are not the same act.

    Spread widens an uncertified member, which makes it claim less -- borrow
    it freely.  Bias moves the centre, which moves seats, and a median over
    two peers is their midpoint with no breakdown point.  ZUUU 2026-08-14:
    two peers handed ICON +3.09C where ICON's own measured offset at that
    station was +1.29C, and the served 39C+ bucket went 14.7% -> 30.8% on
    borrowed arithmetic rather than evidence.
    """

    from bayes_basket_target.native_ensemble_p0 import (
        PROVISIONAL_BIAS_ABS_CAP_C,
        PROVISIONAL_BIAS_MIN_PEERS,
    )

    assert PROVISIONAL_BIAS_MIN_PEERS >= 3
    # The ceiling is the largest per-system offset measured anywhere in the
    # fleet; a borrow beyond it is arithmetic, not evidence.
    assert 1.94 <= PROVISIONAL_BIAS_ABS_CAP_C <= 2.5

    source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "native_ensemble_p0.py"
    ).read_text(encoding="utf-8")
    # Refusing to borrow a bias must leave the member at its raw centre,
    # never silently at some other number, and must say so in the receipt.
    assert "bias_refused_peer_count_" in source
    assert "bias_capped_from_" in source


def test_clause_43_synoptic_cooling_is_a_centre_shift_inside_p_of_t() -> None:
    """Cloud, rain and mixing enter p(t), never the strategy layer.

    This is the one physical-evidence direction term that survived: the three
    fields the airport forecaster publishes beside the TX, standardised and
    averaged, correlate -0.170 with the standardised residual and keep -0.154
    of that as an increment over the TX gap itself -- a second reading of the
    day, not the same one.  Every sign is physical: more cloud, rain or wind
    and the day runs colder than the models say.
    """

    clause = _clause(43)
    assert "synoptic_cooling_shift_v1" in clause
    assert "双重计数" in clause

    from bayes_basket_target.cross_consensus import (
        SYNOPTIC_COOLING_NORMALISATION,
        SYNOPTIC_COOLING_SHIFT_SIGMA,
        synoptic_cooling_index,
    )

    assert set(SYNOPTIC_COOLING_NORMALISATION) == {
        "cloud_cover_fraction",
        "precipitation_probability",
        "wind_speed_mps",
    }
    assert 0.0 < SYNOPTIC_COOLING_SHIFT_SIGMA <= 0.25

    # A cloudy, wet, windy day reads warm-side-negative; a clear calm one the
    # other way.  The shift is the index times a negative coefficient, so the
    # index itself must rise with cooling.
    wet, _receipt = synoptic_cooling_index(
        cloud_cover_fraction=0.9,
        precipitation_probability=0.6,
        wind_speed_mps=7.0,
    )
    dry, _receipt = synoptic_cooling_index(
        cloud_cover_fraction=0.0,
        precipitation_probability=0.0,
        wind_speed_mps=2.0,
    )
    assert wet > 0.0 > dry

    # All three fields are required: an average of standardised parts
    # silently reweights itself when one is missing.
    missing, receipt = synoptic_cooling_index(
        cloud_cover_fraction=None,
        precipitation_probability=0.0,
        wind_speed_mps=2.0,
    )
    assert missing is None
    assert receipt["state"] == "INCOMPLETE_FIELDS"

    # The strategy layer still may not read any of it (clause 43's original
    # prohibition, unchanged).
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "bayes_basket_target/basket_kernel/delta_v2.py",
        "bayes_basket_target/basket_kernel/support.py",
        "bayes_basket_target/delta_v2_runtime.py",
    ):
        source = (root / rel).read_text(encoding="utf-8")
        for name in SYNOPTIC_COOLING_NORMALISATION:
            assert name not in source, f"{rel} reads {name}"


def test_clause_43_forecast_radiation_leads_the_cooling_shift() -> None:
    """The models' own radiation forecast, not the forecaster's proxy fields.

    Measured over 5,230 station-days: forecast daily shortwave radiation
    correlates +0.189 with the standardised residual against +0.191 for the
    reanalysis actual -- the forecast loses essentially nothing, because the
    models get the radiation right and mis-translate it to screen
    temperature.  It gains 2.12% CRPS over every station-day, twice what the
    TAF fields reach over the seventeen stations that publish a TX, and
    adding those fields on top of it gains nothing.
    """

    clause = _clause(43)
    assert "辐射领跑" in clause

    from bayes_basket_target.cross_consensus import (
        RADIATION_MIN_HISTORY_SAMPLES,
        RADIATION_SHIFT_SIGMA,
        forecast_radiation_shift_sigma,
    )

    assert 0.0 < RADIATION_SHIFT_SIGMA <= 0.25
    assert RADIATION_MIN_HISTORY_SAMPLES >= 30

    history = [20.0 + (index % 11) - 5 for index in range(60)]
    bright, bright_receipt = forecast_radiation_shift_sigma(
        radiation_sum=40.0, station_history=history
    )
    dull, _receipt = forecast_radiation_shift_sigma(
        radiation_sum=5.0, station_history=history
    )
    # A brighter day than this station usually gets runs warmer than the
    # models say, and a duller one colder.
    assert bright > 0.0 > dull
    assert bright_receipt["state"] == "RESOLVED"

    # A station whose history is too thin has no scale to standardise
    # against, and must not be shifted on a guess.
    thin, thin_receipt = forecast_radiation_shift_sigma(
        radiation_sum=40.0, station_history=[20.0, 21.0, 19.0]
    )
    assert thin is None
    assert thin_receipt["state"] == "HISTORY_TOO_THIN"

    # Reference covariates only: the capture must never write the truth
    # table, and the strategy layer must never read the field.
    capture = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "capture_station_forecast_physics.py"
    ).read_text(encoding="utf-8")
    # The name may appear in prose saying it is never written; what must not
    # appear is a write to it.
    assert "INSERT" in capture
    for statement in capture.split("INSERT")[1:]:
        assert "calibration_station_daily_high_truth" not in statement[:200]
    assert "reference_only" in capture
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "bayes_basket_target/basket_kernel/delta_v2.py",
        "bayes_basket_target/delta_v2_runtime.py",
    ):
        source = (root / rel).read_text(encoding="utf-8")
        assert "shortwave_radiation" not in source


def test_off_centre_windows_are_deflated_not_forbidden() -> None:
    """A window without the model's centre is over-claimed, not illegal.

    Measured over 5,230 station-days on four-bucket contiguous windows:
    windows holding the centre realize 1.01 of their claimed mass on the
    bulk band, windows without it run 0.78-0.85 across four adjacent bands
    and 0.48 above 0.55 claimed.  The spread inflation that fixes the tails
    moves mass outward faster than the truth does.

    The answer is a corrected probability, not a rail.  Rails were retired
    because they price nothing: a cheap far window can be genuinely worth
    buying and still wins after the correction, while a window claiming the
    majority of the mass from off centre goes negative on its own arithmetic.
    """

    from bayes_basket_target.basket_kernel.delta_v2 import (
        off_center_claim_factor,
    )

    # Deflation across the measured plateau, sharper where the over-claim is.
    assert off_center_claim_factor(0.30) < 1.0
    assert off_center_claim_factor(0.45) < 1.0
    assert off_center_claim_factor(0.70) < off_center_claim_factor(0.30)
    # Shrunk toward one rather than served at the point estimate.
    assert off_center_claim_factor(0.70) > 0.48

    source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "delta_v2_runtime.py"
    ).read_text(encoding="utf-8")
    # Ranking and sizing must see the same corrected belief, or the seat and
    # the stake disagree about what is being bought.
    assert "off_center_claim_factor(raw_win)" in source
    assert "window_probabilities" in source
    # It corrects, it does not forbid: no admission branch may key on it.
    assert "if not holds_center:\n                continue" not in source


def test_clause_43_neighbour_direction_leans_p_of_t_not_the_objective() -> None:
    """The one static directional regularity, served where clause 43 puts it.

    Measured over 5,233 station-days with a bootstrap over days (stations on
    one day share weather, so the day is the independent unit and there are
    47 of them): the truth leans one bucket warm and falls away faster on the
    cold side than the served distribution says.  Offsets minus two, minus
    one and plus one each exclude 1.0; plus two does not and therefore
    serves 1.0.  The centre is excluded on purpose -- it is the factor that
    drifts (1.149 fitted early, 1.005 late) and carrying it costs 0.67% CRPS
    out of sample, against +0.17% for the four offsets around it.
    """

    clause = _clause(43)
    assert "neighbour_direction_v1" in clause

    from bayes_basket_target.cross_consensus import (
        NEIGHBOUR_DIRECTION_BANDS,
        apply_neighbour_direction_tilt,
        neighbour_direction_band,
    )

    # The centre is never reweighted here; position is the radiation shift's
    # job and the centre factor does not hold still.
    for factors in NEIGHBOUR_DIRECTION_BANDS.values():
        assert 0 not in factors
    # A station's tilt follows its latitude band, and an unknown latitude
    # serves the neutral one rather than a guess.
    assert neighbour_direction_band(None) == "lat_00_25"
    assert NEIGHBOUR_DIRECTION_BANDS["lat_00_25"] == {
        -2: 1.0, -1: 1.0, 1: 1.0, 2: 1.0
    }
    assert neighbour_direction_band(-52.0) == neighbour_direction_band(52.0)
    # High latitude leans warm hard; the mid band does not.  This is the
    # structure both pipelines agree on and the reason the prior is banded.
    high = NEIGHBOUR_DIRECTION_BANDS["lat_45_plus"]
    mid = NEIGHBOUR_DIRECTION_BANDS["lat_25_35"]
    assert high[-1] < 1.0 < high[1]
    assert (high[1] / high[-1]) > (mid[1] / mid[-1])

    raw = {
        "33": 0.04, "34": 0.09, "35": 0.19, "36": 0.34,
        "37": 0.26, "38": 0.06, "39": 0.02,
    }
    ordinals = {key: index for index, key in enumerate(sorted(raw))}
    tilted, receipt = apply_neighbour_direction_tilt(
        raw, ordinals=ordinals, center_bucket="36", latitude=52.3
    )
    assert receipt["state"] == "APPLIED"
    assert receipt["band"] == "lat_45_plus"
    assert abs(sum(tilted.values()) - 1.0) < 1e-9
    # The high band's lean lives in the inner pair: one bucket above gains
    # against one below.  It is deliberately not asserted that this makes a
    # [-1,+2] window beat a [-2,+1] one -- the banded fit says that is
    # band-dependent and mostly the other way, because the high band's outer
    # pair leans cold.  The tilt calibrates shape; placement stays priced.
    ratio_before = raw["37"] / raw["35"]
    ratio_after = tilted["37"] / tilted["35"]
    assert ratio_after > ratio_before

    # It lives in p(t).  The objective must not learn a placement rule.
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "bayes_basket_target/basket_kernel/delta_v2.py",
        "bayes_basket_target/delta_v2_runtime.py",
    ):
        source = (root / rel).read_text(encoding="utf-8")
        assert "NEIGHBOUR_DIRECTION" not in source
        assert "neighbour_direction" not in source

    # The banded prior is fitted off-pipeline on years of data and must say
    # so: it is a reference covariate pair, never settlement truth.
    capture = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "capture_station_forecast_tmax_history.py"
    ).read_text(encoding="utf-8")
    # The name may appear in prose saying it is never written; what must not
    # appear is a write to it.
    assert "INSERT" in capture
    for statement in capture.split("INSERT")[1:]:
        assert "calibration_station_daily_high_truth" not in statement[:200]
    assert "reference_only" in capture
