"""Charter 143: a broken basket is completed by how many legs it holds.

User ruling 2026-09-17 ("改").  Measured on the live book's 52 broken
station-days 2026-09-06..16: holding realised -0.66 a station-day; completing
multi-leg baskets in full and single-leg baskets on their cheap legs +0.45
(delta +1.12, 90% [+0.20, +2.14]).  Two held legs or more: every buyable
missing leg to the held level -- charter 112's floor-legal level when one
exists, otherwise the cheapest held size provided the worst in-window
settlement improves.  One held leg: only the legs that clear the one hurdle
(charter 130) on their own walked cost.  Never inside the completion cutoff,
never past the capital handed in.  ``off`` is the pre-143 book.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from bayes_basket_target import delta_v2_runtime as runtime
from bayes_basket_target.basket_kernel import delta_v2 as kernel
from bayes_basket_target.basket_kernel.contracts import ContractError
from tests import test_charter_84_ladder_balance as fixture
from tests import test_charter_136_completion_risk_budget as zgsz
from tests.charter_143_switch import set_charter_143

ROOT = Path(__file__).resolve().parents[1]
RULE = kernel.COMPLETION_143_RULE_VERSION


def _book(price: float, shares: float = 100.0):
    return ({"price": price, "shares": shares},)


def _targets(**overrides):
    kwargs = dict(
        held_by_bucket={"30": 5.0, "31": 5.0},
        support=("30", "31", "32"),
        held_cost_by_bucket={"30": 1.25, "31": 2.15},
        depth_by_bucket={"30": _book(0.20), "31": _book(0.45), "32": _book(0.24)},
        capital_available_usdc=10.0,
        claim_by_bucket={"30": 0.20, "31": 0.50, "32": 0.20},
        hours_to_cutoff=20.0,
    )
    kwargs.update(overrides)
    return kernel.completion_143_targets_v1(**kwargs)


def _short(result: dict) -> list[tuple[str, str]]:
    return [
        (row["bucket"], row["execution_candidate"])
        for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
    ]


def test_the_rule_ships_on() -> None:
    assert kernel.CHARTER_143_COMPLETION == "on"
    assert runtime.CHARTER_143_COMPLETION == "on"


@pytest.mark.parametrize("held, kind", [(0, None), (1, "single"), (2, "multi"), (4, "multi")])
def test_the_basket_type_is_the_held_count(held, kind) -> None:
    assert kernel.completion_143_basket_type_v1(held) == kind


def test_multi_completes_to_the_cheapest_held_size_and_records_the_floor() -> None:
    reading = _targets()
    assert reading["refused"] is None
    assert reading["level_rule"] == "cheapest_held_level_worst_improves"
    assert reading["targets"] == {"30": 5.0, "31": 5.0, "32": 5.0}
    assert reading["floor_legal_after"] is True


def test_multi_never_tops_up_a_shorter_held_rung() -> None:
    # RKSI 2026-09-18: 27C x9, 28C x5, 29C missing.  Charter 112's legal level
    # is 9, which raises 28C; the completion stops at 5 on 29C alone.
    reading = _targets(
        held_by_bucket={"27": 9.0, "28": 5.0},
        support=("27", "28", "29"),
        held_cost_by_bucket={"27": 1.44, "28": 2.70},
        depth_by_bucket={"27": _book(0.16), "28": _book(0.54), "29": _book(0.30)},
    )
    assert reading["buy_buckets"] == ["29"]
    assert reading["targets"] == {"27": 9.0, "28": 5.0, "29": 5.0}
    assert reading["floor_after"] > reading["floor_before"]


def test_multi_completes_past_the_floor_while_the_worst_improves() -> None:
    # Charter 120's thin book: five shares cost 3.75, no held level is legal.
    # Before: truth on 32 loses the 3.40 already paid.  After: it loses 2.15.
    reading = _targets(depth_by_bucket={
        "30": _book(0.20), "31": _book(0.45),
        "32": ({"price": 0.15, "shares": 1.0}, {"price": 0.90, "shares": 100.0}),
    })
    assert reading["refused"] is None
    assert reading["level_rule"] == "cheapest_held_level_worst_improves"
    assert reading["buy_buckets"] == ["32"]
    assert reading["spend_usdc"] == pytest.approx(3.75)
    assert reading["floor_before"] == pytest.approx(-3.40)
    assert reading["floor_after"] == pytest.approx(-2.15)


def test_multi_refuses_a_completion_that_leaves_the_window_worse() -> None:
    reading = _targets(depth_by_bucket={
        "30": _book(0.20), "31": _book(0.45), "32": _book(1.05),
    })
    assert reading["refused"] == "worst_not_improved"
    assert reading["targets"] == {}


def test_multi_reads_no_claim_and_prices_every_leg() -> None:
    assert _targets(claim_by_bucket=None)["refused"] is None
    unpriced = _targets(depth_by_bucket={"30": _book(0.20), "31": _book(0.45), "32": ()})
    assert unpriced["refused"] == "unpriced"


def _single(**overrides):
    kwargs = dict(
        held_by_bucket={"32": 5.0},
        support=("31", "32", "33"),
        held_cost_by_bucket={"32": 2.85},
        depth_by_bucket={"31": _book(0.34), "32": _book(0.59), "33": _book(0.09)},
        claim_by_bucket={"31": 0.319, "32": 0.398, "33": 0.157},
    )
    kwargs.update(overrides)
    return _targets(**kwargs)


def test_single_buys_only_the_legs_that_clear_the_hurdle() -> None:
    reading = _single()
    assert reading["refused"] is None
    assert reading["basket_type"] == "single"
    assert reading["level_rule"] == "cheap_legs_at_held_level"
    assert reading["buy_buckets"] == ["33"]
    assert reading["targets"] == {"31": 0.0, "32": 5.0, "33": 5.0}


def test_single_reads_the_hurdle_not_the_bare_claim() -> None:
    # 0.157 against 0.15 is under the claim but not 5% under it.
    assert _single(depth_by_bucket={
        "31": _book(0.34), "32": _book(0.59), "33": _book(0.15),
    })["refused"] == "no_leg_clears_hurdle"
    assert _single(depth_by_bucket={
        "31": _book(0.34), "32": _book(0.59), "33": _book(0.149),
    })["buy_buckets"] == ["33"]


def test_single_spends_the_ceiling_on_the_best_payout_per_dollar() -> None:
    both = dict(claim_by_bucket={"31": 0.60, "32": 0.398, "33": 0.157})
    assert _single(**both)["buy_buckets"] == ["31", "33"]
    # 0.45 buys 33 (0.157/0.09) and not 31 (0.60/0.34).
    assert _single(capital_available_usdc=0.45, **both)["buy_buckets"] == ["33"]
    assert _single(capital_available_usdc=0.40, **both)["refused"] == "capital"


def test_nothing_inside_the_completion_cutoff_and_nothing_when_off(monkeypatch) -> None:
    assert _targets(hours_to_cutoff=kernel.COMPLETION_CUTOFF_HOURS)["refused"] == (
        "past_completion_cutoff"
    )
    assert _targets(hours_to_cutoff=None)["refused"] is None
    set_charter_143(monkeypatch, "off")
    assert _targets()["refused"] == "off"


def test_a_dead_rung_is_neither_bought_nor_judged() -> None:
    reading = _targets(
        support=("29", "30", "31", "32"),
        depth_by_bucket={"29": _book(0.01), "30": _book(0.20), "31": _book(0.45),
                         "32": _book(0.24)},
        below_leg_floor_buckets=("29",),
    )
    assert reading["buy_buckets"] == ["32"]
    assert reading["targets"]["29"] == 0.0


def _exempt(**overrides):
    kwargs = dict(
        support=("30", "31", "32"),
        held_shares={"30": 5.0, "31": 5.0},
        held_cost_usdc={"30": 1.25, "31": 2.15},
        target_shares={"30": 5.0, "31": 5.0, "32": 5.0},
        target_cost_usdc={"30": 1.25, "31": 2.15, "32": 3.00},
        hours_to_cutoff=20.0,
    )
    kwargs.update(overrides)
    return kernel.completion_143_exemption_v1(**kwargs)


def test_the_exemption_is_read_on_what_goes_out() -> None:
    assert _exempt()["exempt"] is True
    assert _exempt(target_cost_usdc={"30": 1.25, "31": 2.15, "32": 5.10})["reason"] == (
        "worst_not_improved"
    )
    # A top-up is not a completion, whatever it adds to the missing leg.
    assert _exempt(target_shares={"30": 7.0, "31": 5.0, "32": 5.0},
                   target_cost_usdc={"30": 1.65, "31": 2.15, "32": 1.20})["reason"] == (
        "adds_to_held_leg"
    )
    assert _exempt(
        support=("29", "30", "31", "32"),
        target_shares={"29": 5.0, "30": 5.0, "31": 5.0, "32": 5.0},
        target_cost_usdc={"29": 0.05, "30": 1.25, "31": 2.15, "32": 3.00},
        below_leg_floor_buckets=("29",),
    )["reason"] == "buys_dead_leg"
    assert _exempt(hours_to_cutoff=2.0)["reason"] == "past_completion_cutoff"
    single = dict(
        held_shares={"31": 5.0}, held_cost_usdc={"31": 2.15},
        target_shares={"31": 5.0, "32": 5.0},
        target_cost_usdc={"31": 2.15, "32": 0.50},
    )
    assert _exempt(claim_by_bucket={"32": 0.20}, **single)["exempt"] is True
    assert _exempt(claim_by_bucket={"32": 0.10}, **single)["reason"] == "leg_below_hurdle"
    assert _exempt(**single)["reason"] == "leg_below_hurdle"


def test_the_solver_admits_a_certified_completion_past_the_floor() -> None:
    snapshot = fixture._held_snapshot(ask_32=0.60)
    rows = fixture._candidates(snapshot)
    balance = [r for r in rows if r["candidate_origin"] == kernel.LADDER_BALANCE_CANDIDATE_ORIGIN]
    assert len(balance) == 1 and balance[0]["completion_143"]["exempt"] is True
    probabilities = {row.bucket: row.probability for row in snapshot.buckets}
    solved = kernel.solve_target_inventory_v2(
        probabilities=probabilities,
        feasible_inventory_candidates=rows,
        protected_buckets=("30", "31", "32"),
        shortfall_buckets=(),
        breakthrough_buckets=(),
        weights=fixture._delta_policy().value_weights,
        objective_form=kernel.KELLY_OBJECTIVE_FORM,
        bankroll_usdc=514.29,
    )
    assert solved["winner_candidate_origin"] == kernel.LADDER_BALANCE_CANDIDATE_ORIGIN
    assert solved["winner_completion_rule"] == RULE
    baseline_only = kernel.solve_target_inventory_v2(
        probabilities=probabilities,
        feasible_inventory_candidates=[
            r for r in rows if r["candidate_kind"] == kernel.CANDIDATE_KIND_BASELINE
        ],
        protected_buckets=("30", "31", "32"),
        shortfall_buckets=(),
        breakthrough_buckets=(),
        weights=fixture._delta_policy().value_weights,
        objective_form=kernel.KELLY_OBJECTIVE_FORM,
        bankroll_usdc=514.29,
    )
    assert "winner_completion_rule" not in baseline_only


def test_a_broken_basket_is_completed_without_a_decision_moment() -> None:
    snapshot = fixture._held_snapshot(ask_32=0.60)

    def origins(**kwargs):
        return sorted(
            row["candidate_origin"] or row["candidate_kind"]
            for row in runtime._target_candidates(
                snapshot=snapshot, desired=("30", "31", "32"),
                artifact=fixture._delta_policy(), capital_available_usdc=10.0,
                minimum_clip_notional_usdc=0.01,
                bankroll_usdc=fixture._basket_policy().total_budget_usdc,
                probabilities_by_bucket={r.bucket: r.probability for r in snapshot.buckets},
                book_equity_usdc=514.29, decision_moment=False, **kwargs,
            )
        )

    assert kernel.LADDER_BALANCE_CANDIDATE_ORIGIN in origins(completion_143_admitted=True)
    assert kernel.LADDER_BALANCE_CANDIDATE_ORIGIN not in origins()


def test_the_support_contract_accepts_only_a_certified_partial_completion() -> None:
    base = {"candidate_id": "b", "candidate_kind": kernel.CANDIDATE_KIND_BASELINE,
            "shares_by_bucket": {"31": 0.0, "32": 5.0, "33": 0.0}}
    partial = {"candidate_id": "p", "candidate_kind": kernel.CANDIDATE_KIND_COMPLETE,
               "candidate_origin": kernel.LADDER_BALANCE_CANDIDATE_ORIGIN,
               "shares_by_bucket": {"31": 0.0, "32": 5.0, "33": 5.0}}
    certified = dict(partial, completion_143={
        "exempt": True, "basket_type": "single", "declined_buckets": ["31"],
    })
    runtime._assert_candidate_support_contract(
        [base, certified], ordered_support=("31", "32", "33"),
    )
    with pytest.raises(ContractError, match="complete_basket_incomplete"):
        runtime._assert_candidate_support_contract(
            [base, partial], ordered_support=("31", "32", "33"),
        )


def test_zgsz_multi_leg_is_completed_as_the_fok_and_carries_the_waiver() -> None:
    # The charter-136 book: 30/31 held, 32 missing.  Under the pre-143 rule
    # at 0.24 this held (``test_off_is_the_charter_130_book``).
    result = zgsz._compile(zgsz._snapshot(0.24))
    seat = result["basket_support_selection"]["seated"]
    assert seat["completion"]["admitted_by"] == RULE
    assert result["basket_target_activated"] is True
    assert _short(result) == [("32", "TAKER_FOK")]
    assert result["post_lift_edge_exempt_rule"] == RULE
    assert result["completion_repair"]["exempt"] is True
    # At 0.60 the floor is out of reach and the completion still goes: the
    # worst in-window settlement rises from -3.40 to -1.40.
    dear = zgsz._compile(zgsz._snapshot(0.60))
    assert _short(dear) == [("32", "TAKER_FOK")]
    assert dear["completion_143_after_lift"]["floor_after"] == pytest.approx(-1.40)
    assert dear["completion_repair"]["exempt_rule"] == RULE


def test_zgsz_holds_inside_the_cutoff() -> None:
    result = zgsz._compile(zgsz._snapshot(0.24), hours=2.0)
    assert result["basket_target_activated"] is False
    assert _short(result) == []


def test_ltac_single_leg_buys_the_cheap_rung_alone(monkeypatch) -> None:
    fixture._tilted_seat(monkeypatch)
    fixture._production_arms(monkeypatch)
    result = fixture._compile(fixture._ltac(ask_31=0.34, ask_33=0.09))
    completion = result["basket_support_selection"]["seated"]["completion"]
    assert completion["completion_143"]["basket_type"] == "single"
    assert completion["completion_143"]["hurdle_clearing_buckets"] == ["33"]
    assert [bucket for bucket, _ in _short(result)] == ["33"]
    assert result["completion_143_after_lift"]["exempt"] is True


def test_the_ledger_reads_the_same_exemption_on_its_legs() -> None:
    spec = importlib.util.spec_from_file_location(
        "ledger_143", ROOT / "scripts" / "paper_trading_ledger.py"
    )
    ledger = importlib.util.module_from_spec(spec)
    sys.modules["ledger_143"] = ledger
    spec.loader.exec_module(ledger)
    assert ledger._completion_143_exemption_v1 is kernel.completion_143_exemption_v1
    assert ledger._SELECTION_RULE_IDENTITY["charter_143_completion"] == RULE
    seat = {"buckets": ["30", "31", "32"], "completion": {"market_dead_buckets": []}}
    verdict = {"winner_completion_rule": RULE, "edge_probabilities": {},
               "hours_to_cutoff": 20.0}
    kwargs = dict(seat=seat, held={"30": 5.0, "31": 5.0},
                  held_cost={"30": 1.25, "31": 2.15},
                  leg_shares=[0.0, 0.0, 5.0], leg_cash=[0.0, 0.0, 3.0])
    assert ledger._completion_143_exempt_on_legs(verdict=verdict, **kwargs) is True
    assert ledger._completion_143_exempt_on_legs(
        verdict=dict(verdict, winner_completion_rule=None), **kwargs
    ) is False
    assert ledger._completion_143_exempt_on_legs(
        verdict=dict(verdict, hours_to_cutoff=1.0), **kwargs
    ) is False
    assert ledger._completion_143_exempt_on_legs(verdict=None, **kwargs) is False
