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

from bayes_basket_target import delta_v2_runtime

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



@pytest.fixture(autouse=True)
def _certainty_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """These fixtures are sharp on purpose; the gate is tested separately.

    The certainty gate refuses to open into a market that has finished
    pricing the day, and a hand-built fixture distribution is almost always
    sharp -- so leaving it on would make every test in this file assert
    "the gate fired" instead of the property it was written for.  Its own
    behaviour is pinned in test_certainty_gate_* below.
    """

    monkeypatch.setattr(
        delta_v2_runtime, "CERTAINTY_GATE_SIGNALS", ()
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
    windows, _ = _support_windows(
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
    windows, _ = _support_windows(
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
    gapped, _ = _support_windows(
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
    oversized, _ = _support_windows(
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


def test_clause_45_market_center_outside_envelope_is_diagnostic_only() -> None:
    """The disagreement is recorded; it no longer locks new money.

    The capital gate this once was (2026-08-13, the ZUUU starved-root
    specimen) fired exactly once in its life -- RCSS 2026-08-15, unsettled --
    and retired by the 2026-08-15 ruling with no measurable protective
    record.  The objective is allowed to read market prices and the market
    center; what guards against buying phantom edge is the clause-42 seat
    touch rule and the off-centre claim deflation, both of which were
    measured.  Same snapshot, same probabilities: when the market center
    walks out of the envelope the diagnostic flips and nothing else does.
    """

    clause = _clause(45)
    assert "退役" in clause
    assert "不再锁新钱" in clause
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

    assert inside["envelope_entry_gate"]["outside"] is False
    assert inside["delta_v2_action_selection"]["selected_actions"]

    gate = outside["envelope_entry_gate"]
    assert gate["outside"] is True
    assert gate["enforced"] is False
    assert outside["market_center_bucket"] == "39"
    # The ruling: reading the market center is allowed, locking new money on
    # it is not.  Bucket 39 carries no probability, so the touch rule keeps
    # it unseatable either way; the 35/36 book is identical in both plans and
    # must authorize identically.
    assert outside["delta_v2_action_selection"]["selected_actions"]
    assert outside["delta_v2_audit"]["authorized"] is True
    assert "market_center_outside_model_envelope" not in (
        outside["delta_v2_action_selection"]["blockers"]
    )
    assert "market_center_outside_model_envelope" not in (
        outside.get("reason_codes") or ()
    )
    # Probabilities are untouched: the diagnostic is not a p(t) channel.
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
    # (d) 2026-09-02: the ledger's stage-zero write boundary admits a listed
    # day that has not started.  The behaviour itself is pinned in
    # test_bayes_basket_target_atomic_ledger
    # (test_stage_zero_execution_admits_a_listed_day_that_has_not_started).
    assert "不得以\"日未开始\"拒绝挂牌日的 stage-zero 决策" in clause
    ledger_source = (
        Path(__file__).resolve().parents[1]
        / "bayes_basket_target"
        / "atomic_ledger.py"
    ).read_text(encoding="utf-8")
    assert 'if day_state not in {"open", "not_started"}:' in ledger_source
    assert "elif stage_zero_anchor is None:" in ledger_source

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
    # Charter 62: the constant is the FLOOR of the borrow cap; the working
    # cap is the largest offset this station's own certified peers have
    # measured.  A borrow beyond the station's own evidence is arithmetic,
    # not evidence -- and clamping a tropical borrow to a temperate fleet
    # constant was itself a measured 1.5-3.8C cold error.
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
    # The station-local cap and the frozen certified-peer lists (a borrowed
    # value must never feed back into the median it was borrowed from).
    assert "provisional_bias_cap_c = max(" in source
    assert "certified_peer_biases" in source


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
    # the stake disagree about what is being bought.  The call carries the
    # centre distance since 2026-08-27; the assertion is about which mass it
    # corrects, which is still the window's own raw claim.
    assert "off_center_claim_factor(raw_win, gap_buckets=" in source
    # Containment is the zero of that distance, so the two-value rule is the
    # ramp's own default rather than a separate branch.
    assert "center_gap = (" in source
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
        NEIGHBOUR_DIRECTION_BY_BAND,
        NEIGHBOUR_DIRECTION_FLEET,
        apply_neighbour_direction_tilt,
        neighbour_direction_band,
    )

    # The centre is never reweighted here; position is the radiation shift's
    # job and the centre factor does not hold still.
    for cells in NEIGHBOUR_DIRECTION_BY_BAND.values():
        assert 0 not in cells
        assert set(cells) == set(NEIGHBOUR_DIRECTION_FLEET)

    # A cell that did not clear both hurdles serves the fleet factor, so most
    # of the table is the fleet and only what earned its own reading differs.
    differing = [
        (band, offset)
        for band, cells in NEIGHBOUR_DIRECTION_BY_BAND.items()
        for offset, value in cells.items()
        if value != NEIGHBOUR_DIRECTION_FLEET[offset]
    ]
    assert len(differing) == 4, differing
    # Every served cell must lean the same way as the fleet -- a band may
    # sharpen the fleet's reading, never reverse it.
    for band, cells in NEIGHBOUR_DIRECTION_BY_BAND.items():
        for offset, value in cells.items():
            fleet = NEIGHBOUR_DIRECTION_FLEET[offset]
            if fleet == 1.0:
                assert value == 1.0
            else:
                assert (value - 1.0) * (fleet - 1.0) > 0
                assert abs(value - 1.0) >= abs(fleet - 1.0)

    # High latitude is where the lean is strongest, on both neighbours.
    high = NEIGHBOUR_DIRECTION_BY_BAND["lat_45_plus"]
    for other in ("lat_00_25", "lat_25_35", "lat_35_45"):
        assert high[-1] < NEIGHBOUR_DIRECTION_BY_BAND[other][-1]
        assert high[1] > NEIGHBOUR_DIRECTION_BY_BAND[other][1]

    # Hemispheres share a band; an unknown latitude is not evidence.
    assert neighbour_direction_band(-52.0) == neighbour_direction_band(52.0)
    assert neighbour_direction_band(None) == "lat_25_35"

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

    # The research lane that failed to transfer is kept for re-measurement,
    # and must never write the truth table.
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


def test_clause_49_ladder_seat_must_contain_the_centre_bucket() -> None:
    """The seat hangs from the rung: every window must hold the centre bucket.

    The Ladder ruling (clause 49) supersedes clause 42's touch-the-core gate
    with a strictly stronger one -- containing the centre implies touching
    the run grown from it.  The domain stays the whole day (clause 35's
    EV -0.195 measurement stands); direction is checked nowhere, because
    which flank the ladder extends toward is the automatic outcome of the
    curve-score argmax over calibrated mass versus price, never a rule.

    The incumbent -- the continuous core's own support, not held inventory --
    is exempt, because it was admitted on evidence and this filter is about
    price.
    """

    assert "包含核心中心桶" in _clause(49)
    assert "定义域仍是全域" in _clause(49)
    assert "自动" in _clause(49)

    from bayes_basket_target.basket_kernel.contracts import TargetBasketPolicy
    from bayes_basket_target.basket_kernel.delta_v2 import probability_core
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
    # Buckets 32..37.  The mass sits on 34/35; 37 is the penny tail, and the
    # book is cheap everywhere so price alone would happily seat it.
    probabilities = (0.04, 0.06, 0.32, 0.30, 0.17, 0.11)
    snapshot = _wide_snapshot(probabilities, ask=0.02)
    by_bucket = {
        row.bucket: float(row.probability) for row in snapshot.buckets
    }
    core = probability_core(
        by_bucket,
        ordinals={row.bucket: int(row.ordinal) for row in snapshot.buckets},
    )
    assert core == {"34", "35"}, "the core is the run p(t) stands behind"

    windows, audit = _support_windows(
        snapshot=snapshot,
        desired={"34", "35"},
        probabilities=by_bucket,
        fee_rate=0.0,
        maximum_support_buckets=policy.maximum_support_buckets,
        center_bucket="34",
    )
    seats = [tuple(row["buckets"]) for row in windows]
    assert audit["probability_core"] == ["34", "35"]
    assert audit["ladder_center_bucket"] == "34"
    assert audit["center_refused_window_count"] > 0, "the rule must have bitten"
    assert (
        audit["core_refused_window_count"]
        == audit["center_refused_window_count"]
    ), "the retained key mirrors the rung count"

    # The detached penny tail is gone even though it is cheap ...
    assert ("37",) not in seats
    assert ("36", "37") not in seats
    assert ("32",) not in seats
    # ... and so is a lean that touches the core but lets go of the rung:
    # under clause 42 {35,36,37} held, under the ladder it does not.
    assert ("35", "36", "37") not in seats
    # Every surviving window holds the rung; both flanks and the straddle
    # are all on the menu, and the curve score alone picks among them.
    assert ("34",) in seats
    assert ("33", "34") in seats, "the lower flank is enumerated"
    assert ("34", "35", "36") in seats, "the upper flank is enumerated"
    assert ("33", "34", "35") in seats, "the straddle is enumerated"
    assert all("34" in seat for seat in seats)


def test_clause_49_negative_ev_centre_is_still_the_anchor() -> None:
    """A centre bucket priced above its own probability still anchors the ladder.

    Ruling (2026-08-15): the centre is the anchor and one leg of a complete
    basket -- it is never refused for being individually negative EV.  The
    charter has said since 2026-08-13 that the positive-EV test lives at the
    whole-basket level only and that atomicity means negative legs get bought
    (clause 35); this pins the interaction with the ladder rung, where the
    temptation to drop an expensive centre is strongest.

    The mass is where it is; price decides how far the ladder extends from
    the rung, never whether the rung itself is bought.
    """

    from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot
    from bayes_basket_target.delta_v2_runtime import _support_windows

    labels = ("30", "31", "32", "33", "34")
    probabilities = (0.05, 0.10, 0.40, 0.30, 0.15)
    # 32 is the centre and holds the most mass, and the book charges 0.55 for
    # it: -0.15 of edge on its own.  Its neighbours are cheap.
    asks = (0.02, 0.03, 0.55, 0.10, 0.04)
    snapshot = TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "negative-ev-centre",
        "sequence": 1,
        "observed_at": "2026-08-15T12:00:00Z",
        "market_id": "m",
        "station_id": "ZUUU",
        "event_date": "2026-08-16",
        "quote_version": "v",
        "quote_observed_at": "2026-08-15T12:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "i",
        "order_state_version": "o",
        "evidence": {
            "probability_stage": "P0",
            "posterior_version": "p",
            "posterior_sha256": "a" * 64,
        },
        "buckets": [
            {
                "bucket": bucket,
                "ordinal": 30 + index,
                "probability": probability,
                "bid_price": max(0.0, ask - 0.01),
                "bid_levels": [
                    {"price": max(0.001, ask - 0.01), "shares": 50.0}
                ],
                "ask_levels": [{"price": ask, "shares": 200.0}],
                "price_tick_size": 0.001,
                "maker_queue_ahead_shares": 2.0,
                "maker_queue_known": True,
            }
            for index, (bucket, probability, ask) in enumerate(
                zip(labels, probabilities, asks)
            )
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
    by_bucket = {
        row.bucket: float(row.probability) for row in snapshot.buckets
    }
    windows, audit = _support_windows(
        snapshot=snapshot,
        desired={"32"},
        probabilities=by_bucket,
        fee_rate=0.0,
        maximum_support_buckets=4,
        center_bucket="32",
    )

    assert audit["ladder_center_bucket"] == "32"
    # The expensive centre anchors every seat on the menu ...
    assert windows, "a negative-EV centre must not empty the menu"
    assert all("32" in row["buckets"] for row in windows)
    # ... including baskets whose whole-window edge is positive precisely
    # because the cheap neighbours carry the leg the centre cannot.
    best = windows[0]
    assert best["edge"] > 0
    assert set(best["buckets"]) > {"32"}, "the ladder extends off the rung"
    # And the lone-centre window stays enumerated at its honest negative
    # edge: it is ranked out by the curve score, never filtered out by a
    # per-bucket EV rule.
    solo = next(row for row in windows if row["buckets"] == ("32",))
    assert solo["edge"] < 0


def test_clause_49_ladder_rule_evicts_an_off_rung_incumbent() -> None:
    """The centre rung has no exemption left, not even for the incumbent.

    ``desired`` is what ``_choose_support`` admitted on evidence, not filled
    inventory, and it used to ride onto the menu whether or not it held the
    rung.  The 2026-08-16 ruling states the requirement with no exemption
    attached: a seated window must contain the centre bucket, full stop.  A
    coverage target that has drifted off the rung is now visible as a refusal
    rather than as a seat, which is the same fault reported one layer earlier.
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
    probabilities = (0.04, 0.06, 0.32, 0.30, 0.17, 0.11)
    snapshot = _wide_snapshot(probabilities, ask=0.02)
    by_bucket = {
        row.bucket: float(row.probability) for row in snapshot.buckets
    }
    # Held: {37}, which shares nothing with the {34,35} core.
    windows, audit = _support_windows(
        snapshot=snapshot,
        desired={"37"},
        probabilities=by_bucket,
        fee_rate=0.0,
        maximum_support_buckets=policy.maximum_support_buckets,
        center_bucket="34",
    )
    seats = [tuple(row["buckets"]) for row in windows]
    assert ("37",) not in seats, "an off-rung incumbent is refused like any other"
    assert seats, "refusing it must not empty the menu"
    # No exemption survives: every seat holds the rung, incumbent or not.
    assert all(audit["ladder_center_bucket"] in seat for seat in seats)
    assert audit["center_refused_window_count"] > 0, "the rule must bite"


def test_clause_42_p0_membership_is_won_never_granted() -> None:
    """The P0 combination is numerical models only, and every seat is won.

    The 2026-08-15 ruling clarified eligibility versus mandate.  Two
    contest-free paths existed and both are gone: ICON's equal-family-share
    graft (its fingerprint: exactly 1/n on every selection row it touched,
    beside fitted peers) and TAF's candidacy in the cross-system contest
    (the forecaster's stage is P0-M and the envelope witness, never a P0
    member -- ZUUU's bound train_end 08-08 row had five candidates, five
    selected, TAF at 0.216: a contest that never eliminated anyone).
    """

    clause = _clause(42)
    assert "P0 组合成员资格" in clause
    assert "有权入选 ≠ 必须入选" in clause
    assert "永不作 P0 组合成员" in clause
    assert "p0_membership_demotions_json" in clause

    import inspect

    import bayes_basket_target.native_ensemble_oos as oos
    from bayes_basket_target import native_ensemble_p0

    # Fit side: no graft, and point forecasts never reach the contest.
    assert not hasattr(oos, "_provisional_family_share_admission")
    fit_source = inspect.getsource(oos.build_native_ensemble_oos)
    ready = fit_source.split("ready_rows = [", 1)[1][:600]
    assert "POINT_FORECAST_SYSTEMS" in ready

    # Serve side: a point-forecast weight in an old selection row is demoted
    # to a rider and named in the lineage, not silently served.
    serve_source = inspect.getsource(
        native_ensemble_p0.compile_native_ensemble_p0
    )
    assert "p0_membership_demotions" in serve_source
    assert "p0_membership_demotions_json" in serve_source


def test_clause_42_core_is_seeded_on_the_point_estimate_not_the_argmax() -> None:
    """One anchor for the run and the deflation, so they cannot disagree.

    An open-ended edge bucket spans an unbounded interval and collects the
    whole tail, so it wins the argmax on shape rather than on conviction --
    the exact anchor ``_center`` refuses.  Seeding the core there let the
    gate bless what it exists to stop: LIMC and VILK 2026-08-16 both seated
    a lone edge bucket, legitimised by a core the edge bucket had itself
    seeded.  Grown from the point estimate, both seats move onto three
    bounded buckets that reach the core.
    """

    clause = _clause(42)
    assert "点估计" in clause

    from bayes_basket_target.basket_kernel.delta_v2 import probability_core

    # Buckets 32..37, mass piled on the open-ended top while the stage's own
    # estimate sits at 33.
    probabilities = {
        "32": 0.05, "33": 0.14, "34": 0.12,
        "35": 0.10, "36": 0.11, "37+": 0.48,
    }
    ordinals = {b: i for i, b in enumerate(probabilities)}

    # Argmax seeding anchors on the unbounded bucket and never reaches 33.
    assert probability_core(probabilities, ordinals=ordinals) == {"36", "37+"}

    # Point-estimate seeding grows from 33 and holds the buckets between.
    seeded = probability_core(
        probabilities, ordinals=ordinals, center_bucket="33"
    )
    assert "33" in seeded
    assert seeded == {"33", "34", "35", "36", "37+"}

    # An edge bucket is still a legitimate seed when the estimate is in it.
    assert probability_core(
        probabilities, ordinals=ordinals, center_bucket="37+"
    ) == {"36", "37+"}

    # A caller with no anchor keeps the argmax fallback.
    assert probability_core(
        probabilities, ordinals=ordinals, center_bucket="not-a-bucket"
    ) == {"36", "37+"}


def test_clause_50_a_seat_needs_at_least_three_rungs() -> None:
    """A one- or two-bucket window is enumerated, audited, and not seated.

    Clause 49 put the ladder on the rung; clause 50 says how long it has to
    be.  ZUUU 2026-08-16 is the case: the curve score preferred the single
    bucket {35C or higher} at 0.1985 over {34, 35+} at 0.1922, which is
    arithmetically right and strategically wrong -- basket completeness, the
    envelopes and the TAF direction correction only mean something across a
    spread of rungs.

    The floor refuses seating, never search: narrow windows stay in the
    enumeration so the audit can see what was refused.
    """

    clause = _clause(50)
    assert "至少 3 桶" in clause
    assert "枚举不收窄" in clause

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
    assert policy.minimum_support_buckets == 3

    probabilities = (0.04, 0.06, 0.32, 0.30, 0.17, 0.11)
    snapshot = _wide_snapshot(probabilities, ask=0.02)
    by_bucket = {
        row.bucket: float(row.probability) for row in snapshot.buckets
    }
    windows, audit = _support_windows(
        snapshot=snapshot,
        desired={"34", "35"},
        probabilities=by_bucket,
        fee_rate=0.0,
        maximum_support_buckets=policy.maximum_support_buckets,
        minimum_support_buckets=policy.minimum_support_buckets,
        center_bucket="34",
    )
    seats = [
        tuple(row["buckets"]) for row in windows if not row["is_incumbent"]
    ]
    assert seats, "the floor must not empty the menu"
    assert all(len(seat) >= 3 for seat in seats)
    assert all(len(seat) <= 4 for seat in seats)
    assert ("34",) not in seats
    assert ("34", "35") not in seats
    assert audit["minimum_support_buckets"] == 3
    assert audit["short_refused_window_count"] > 0, "the rule must bite"
    # The rung rule from clause 49 still holds on top of the length rule.
    assert all(audit["ladder_center_bucket"] in seat for seat in seats)
    # The floor binds the incumbent too.  A two-bucket core is a two-rung
    # ladder however it arrived, and clause 50's five hard requirements are
    # equally unreachable from it -- so the evidence support stays on the
    # menu only when it is long enough to hold a seat.
    assert all(len(row["buckets"]) >= 3 for row in windows)
    assert ("34", "35") not in [tuple(row["buckets"]) for row in windows]


def test_clause_50_floor_is_not_waived_for_an_inherited_two_rung_ladder() -> (
    None
):
    """A short incumbent must lose its seat to a long window, not keep it.

    The exemption was written for a market that can only quote two live
    buckets.  Measured on 2026-08-17 that was not the case it was serving:
    RKSI, RPLL and ZHHH each seated a two-bucket incumbent while three-bucket
    windows sat admissible on the same plan, so the waiver was not keeping a
    starved day tradeable -- it was letting the curve score buy the narrower
    ladder it prefers.  The seat has to go to a window that can carry basket
    completeness, the envelopes, the TAF correction and whole-basket EV.
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
    probabilities = (0.04, 0.06, 0.32, 0.30, 0.17, 0.11)
    snapshot = _wide_snapshot(probabilities, ask=0.02)
    by_bucket = {
        row.bucket: float(row.probability) for row in snapshot.buckets
    }
    windows, audit = _support_windows(
        snapshot=snapshot,
        # The two-bucket support the continuous core resolved: the incumbent.
        desired={"34", "35"},
        probabilities=by_bucket,
        fee_rate=0.0,
        maximum_support_buckets=policy.maximum_support_buckets,
        minimum_support_buckets=policy.minimum_support_buckets,
        center_bucket="34",
    )
    incumbents = [row for row in windows if row["is_incumbent"]]
    assert not incumbents, "a two-rung incumbent may not hold a seat"
    assert windows, "refusing it must not empty the menu when longer windows exist"
    assert min(len(row["buckets"]) for row in windows) >= 3
    assert audit["short_refused_window_count"] > 0


def test_clause_51_a_fading_leg_is_held_never_topped_up_never_sold() -> None:
    """Rebalancing follows p(t)'s direction, and only in one direction.

    The objective re-solves from scratch every cycle, so it sees today's
    probability without its history and would top up a leg the evidence has
    just moved away from.  Clause 51 clamps that add to zero while leaving
    the existing size alone -- the clamp is one-sided, so a faded leg is
    held, not sold.
    """

    clause = _clause(51)
    assert "不加仓" in clause
    assert "永不卖出" in clause

    from bayes_basket_target.delta_v2_runtime import (
        compile_delta_v2_runtime_plan,
    )
    import inspect

    source = inspect.getsource(compile_delta_v2_runtime_plan)
    # The clamp exists, and it is a floor at the current size rather than a
    # rewrite of the target: min() with zero can only remove a purchase.
    assert "declining_buckets" in source
    assert "gap_by_bucket[bucket] = min(0.0, gap_by_bucket[bucket])" in source
    # Selling stays impossible: the execution set is positive gaps only.
    assert "gap_by_bucket[bucket] > EPS" in source
    # No previous reading means no direction, and every bucket stays eligible.
    assert "if previous_probabilities:" in source

    signature = inspect.signature(compile_delta_v2_runtime_plan)
    assert "previous_probabilities" in signature.parameters
    assert signature.parameters["previous_probabilities"].default is None


def _rung_snapshot(
    rung_width_c: float | None,
    *,
    dispersion_c: float = 1.0502,
) -> "TargetBasketSnapshot":
    """A six-rung book whose core is one bucket and whose envelope is wide.

    Everything except the ladder's Celsius width is held fixed, so any
    difference in the candidate window is attributable to the conversion.
    """

    from bayes_basket_target.basket_kernel.contracts import (
        TargetBasketSnapshot,
    )

    evidence = {
        "probability_stage": "P0",
        "posterior_version": "p0-v1",
        "posterior_sha256": "a" * 64,
        # Defaults to KLGA 2026-08-13's measured model envelope span, which
        # sits inside the band (1.0, 1.111] where the two readings disagree.
        "forecast_dispersion_c": dispersion_c,
        "model_envelope_lower_ordinal": 35,
        "model_envelope_upper_ordinal": 40,
    }
    if rung_width_c is not None:
        evidence["bucket_rung_width_c"] = rung_width_c
    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "doctrine-rung-width",
        "sequence": 1,
        "observed_at": "2026-08-13T03:00:00Z",
        "market_id": "doctrine-market",
        "station_id": "KLGA",
        "event_date": "2026-08-13",
        "quote_version": "quote-v1",
        "quote_observed_at": "2026-08-13T03:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-v1",
        "order_state_version": "order-v1",
        "evidence": evidence,
        "buckets": [
            {
                "bucket": str(35 + index),
                "ordinal": 35 + index,
                "probability": probability,
                "bid_price": 0.02,
                "bid_levels": [{"price": 0.02, "shares": 10.0}],
                "ask_levels": [{"price": 0.03, "shares": 100.0}],
                "price_tick_size": 0.01,
                "maker_queue_ahead_shares": 2.0,
                "maker_queue_known": True,
            }
            for index, probability in enumerate(
                (0.02, 0.03, 0.80, 0.10, 0.03, 0.02)
            )
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


def test_clause_53_expansion_layers_convert_at_the_station_rung_width() -> None:
    """A Celsius dispersion must be spent at the station's own exchange rate.

    Clause 53 splits the two kinds of quantity: boundary (a) puts every
    Celsius number in the Celsius domain before it is compared across
    stations, boundary (b) leaves rung counts alone because a rung is market
    structure.  The disagreement expansion straddles them -- it reads a
    Celsius span and answers in rungs -- so it is boundary (a) that governs
    the division, and the divisor is the ladder's own width.

    Dividing by a fixed 1.0 read the Fahrenheit ladder at the Celsius rate.
    Measured 2026-08-18 over ``basket_probability_envelope`` from 2026-08-01:
    25,033 Fahrenheit envelopes, of which 1,453 (5.8%) bought two layers where
    the station's own 10/9 C rung buys one; 63,998 Celsius envelopes, none
    divergent.  Only two rung widths exist in production -- 1.0 and 1.111111 --
    so the divergent band is exactly span in (1.0, 1.111] and the cap of two
    bounds it.  Charter clause 53 named the Celsius-hardcoded divisor shape as
    the defect it exists to prevent; this is the second case found.
    """

    clause = _clause(53)
    assert "桶宽禁止按单位硬编码" in clause
    assert "1.111" in clause

    from bayes_basket_target.basket_kernel.contracts import (
        ContractError as KernelContractError,
        TargetBasketPolicy,
    )
    from bayes_basket_target.basket_kernel.support import (
        _choose_support,
        _disagreement_expansion_layers,
    )

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

    celsius = _rung_snapshot(1.0)
    fahrenheit = _rung_snapshot(10.0 / 9.0)

    # The same physical disagreement, read on each book's own ladder.
    assert _disagreement_expansion_layers(celsius, policy) == 2
    assert _disagreement_expansion_layers(fahrenheit, policy) == 1

    # And it reaches the candidate window, not just the counter: slot 9 is the
    # layer budget, slots 7 and 8 the window it opens around the one-bucket
    # core at ordinal 37.
    celsius_support = _choose_support(celsius, policy)
    fahrenheit_support = _choose_support(fahrenheit, policy)
    assert (celsius_support[7], celsius_support[8]) == (35, 39)
    assert (fahrenheit_support[7], fahrenheit_support[8]) == (36, 38)
    assert celsius_support[9] == 2
    assert fahrenheit_support[9] == 1

    # Boundary (b) is untouched: the cap is a rung count and stays synonymous
    # across units.  A Fahrenheit book still reaches two layers -- it just has
    # to disagree by 10/9 C per rung to get there, which is the point.
    assert _disagreement_expansion_layers(fahrenheit, policy) == 1
    assert _disagreement_expansion_layers(
        _rung_snapshot(10.0 / 9.0, dispersion_c=2.3), policy
    ) == 2

    # A dispersion with no ladder to spend it on is refused, not silently
    # converted at 1.0 -- that fallback is the defect wearing a default.
    with pytest.raises(
        KernelContractError,
        match="bucket_rung_width_required_for_disagreement_expansion",
    ):
        _disagreement_expansion_layers(_rung_snapshot(None), policy)

    # The knob is denominated in rungs now.  The Celsius name is refused
    # rather than aliased: the same number means different things under the
    # two readings, so accepting it would carry the confusion forward.
    with pytest.raises(
        KernelContractError,
        match="legacy_dispersion_celsius_per_expansion_layer_forbidden",
    ):
        TargetBasketPolicy.from_mapping({
            "clip_max_notional_usdc": 30.0,
            "dispersion_c_per_expansion_layer": 1.0,
        })
