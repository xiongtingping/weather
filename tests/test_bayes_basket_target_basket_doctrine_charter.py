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
