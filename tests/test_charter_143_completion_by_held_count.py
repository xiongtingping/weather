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
    # Fixture built under the 0.35 sigma gate; the gate moved to 0.20 (user
    # 2026-10-16), the completion rule under test did not.
    from bayes_basket_target import delta_v2_runtime as _rt
    monkeypatch.setattr(_rt, "SKEW_GAP_THRESHOLD_SIGMA", 0.35)
    fixture._tilted_seat(monkeypatch)
    fixture._production_arms(monkeypatch)
    result = fixture._compile(fixture._ltac(ask_31=0.34, ask_33=0.09))
    completion = result["basket_support_selection"]["seated"]["completion"]
    assert completion["completion_143"]["basket_type"] == "single"
    assert completion["completion_143"]["hurdle_clearing_buckets"] == ["33"]
    assert [bucket for bucket, _ in _short(result)] == ["33"]
    assert result["completion_143_after_lift"]["exempt"] is True


# --- v2 (2026-09-17, first live morning): only legs the chain sent, at most at
# the size they were sent at.


def test_v2_a_leg_nobody_sent_is_not_missing() -> None:
    # ZGGG 2026-09-17: 33/34/35C sent and held; the label moved the seat to
    # 32/33/34C and v1 bought 32C.
    reading = _targets(
        held_by_bucket={"33": 5.0, "34": 5.0},
        support=("32", "33", "34"),
        held_cost_by_bucket={"33": 1.30, "34": 1.85},
        depth_by_bucket={"32": _book(0.07), "33": _book(0.40), "34": _book(0.40)},
        sent_shares_by_bucket={"33": 5.0, "34": 5.0, "35": 5.0},
    )
    assert reading["refused"] == "no_sent_leg_missing"
    assert reading["unsent_buckets"] == ["32"]
    assert reading["targets"] == {}


def test_v2_the_basket_is_the_held_and_sent_legs() -> None:
    # 33/34 held, 35 sent and missing, 32 never sent: buy 35 alone, and the
    # in-window reading is over 33/34/35 -- 32 paying nothing is not a
    # settlement this basket was ever built to cover.
    reading = _targets(
        held_by_bucket={"33": 5.0, "34": 5.0},
        support=("32", "33", "34", "35"),
        held_cost_by_bucket={"33": 1.30, "34": 1.85},
        depth_by_bucket={"32": _book(0.07), "33": _book(0.40), "34": _book(0.40),
                         "35": _book(0.24)},
        sent_shares_by_bucket={"33": 5.0, "34": 5.0, "35": 5.0},
    )
    assert reading["refused"] is None
    assert reading["buy_buckets"] == ["35"]
    assert reading["targets"]["32"] == 0.0
    assert reading["floor_before"] == pytest.approx(-3.15)
    assert reading["floor_after"] == pytest.approx(5.0 - 3.15 - 1.20)


def test_v2_a_completion_buys_no_more_than_the_leg_was_sent_at() -> None:
    # SBGR 2026-09-18: 20C held x34 (a 0.03 rung the dollar minimum lifted),
    # 21C sent at 13; v1 bought 34 @0.14.
    reading = _targets(
        held_by_bucket={"20": 34.0},
        support=("20", "21", "22"),
        held_cost_by_bucket={"20": 1.02},
        depth_by_bucket={"20": _book(0.03), "21": _book(0.14), "22": _book(0.28)},
        claim_by_bucket={"20": 0.137, "21": 0.239, "22": 0.182},
        sent_shares_by_bucket={"20": 34.0, "21": 13.0, "22": 5.0},
    )
    assert reading["buy_buckets"] == ["21"]
    assert reading["targets"]["21"] == 13.0
    assert reading["spend_usdc"] == pytest.approx(13 * 0.14)
    multi = _targets(sent_shares_by_bucket={"30": 5.0, "31": 5.0, "32": 3.0})
    assert multi["targets"]["32"] == 3.0


def test_v2_the_exemption_refuses_an_unsent_leg() -> None:
    reading = _exempt(sent_shares_by_bucket={"30": 5.0, "31": 5.0})
    assert reading["reason"] == "buys_unsent_leg"
    assert _exempt(sent_shares_by_bucket={"30": 5.0, "31": 5.0, "32": 1.0})["exempt"] is True


def test_v2_the_snapshot_carries_the_sent_sizes_and_hashes_as_before_without() -> None:
    from dataclasses import replace

    snapshot = fixture._held_snapshot(ask_32=0.15)
    assert "chain_sent_shares" not in snapshot.to_dict()
    sent = replace(snapshot, chain_seat_buckets=("30", "31"),
                   chain_sent_shares={"30": 5.0, "31": 5.0})
    again = type(snapshot).from_mapping(sent.to_dict())
    assert dict(again.chain_sent_shares) == {"30": 5.0, "31": 5.0}
    with pytest.raises(Exception, match="snapshot_chain_sent_bucket_unknown"):
        type(snapshot).from_mapping({**snapshot.to_dict(), "chain_sent_shares": {"99": 1.0}})
    assert runtime._completion_143_sent_shares(snapshot) is None
    assert runtime._completion_143_sent_shares(sent) == {"30": 5.0, "31": 5.0}
    seat_only = replace(snapshot, chain_seat_buckets=("30",))
    assert runtime._completion_143_sent_shares(seat_only) == {
        "30": runtime.COMPLETION_143_UNSIZED_SENT_SHARES
    }


def test_v2_the_loader_reads_the_last_sent_size() -> None:
    import json
    import sqlite3

    from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
    from bayes_basket_target.execution_reconciliation import record_order_event
    from bayes_basket_target.production_io import (
        load_chain_seat_buckets,
        load_chain_sent_shares,
    )

    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES('c1','plan_1','SBGR','2026-09-18','America/Sao_Paulo','basket',?,1,"
        "'submitted','2026-09-16T15:00:00Z','2026-09-16T15:00:00Z')",
        ("a" * 64,),
    )
    for n, (bucket, size, at, event) in enumerate((
        ("21°C", 7.0, "2026-09-16T15:38:04Z", "CANCEL_ACK"),
        ("21°C", 13.0, "2026-09-16T17:10:01Z", "CANCEL_ACK"),
        ("21°C", 40.0, "2026-09-16T18:00:00Z", "NEVER_REACHED_EXCHANGE"),
        ("20°C", 34.0, "2026-09-16T23:42:14Z", "FILL"),
    )):
        intent = f"i{n}"
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
            "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
            "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
            "intent_json,intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent, "d1", "c1", bucket, f"t{n}", "YES", "BUY", 0.1, size, 0.5,
             "submitted", 1, f"k{n}", json.dumps({}), "b" * 64, at),
        )
        record_order_event(conn, order_intent_id=intent, event={
            "event_type": event, "exchange_order_id": f"0x{n}",
            "observed_at": at, "matched_size": size if event == "FILL" else 0.0,
            "matched_notional_usdc": 1.0 if event == "FILL" else 0.0, "event_payload": {},
        })
    assert load_chain_sent_shares(conn, station_id="SBGR", event_date="2026-09-18") == {
        "21°C": 13.0, "20°C": 34.0,
    }
    assert load_chain_seat_buckets(conn, station_id="SBGR", event_date="2026-09-18") == (
        frozenset({"20°C", "21°C"})
    )


def test_v2_zgsz_holds_on_a_leg_it_never_sent_and_completes_one_it_did() -> None:
    from dataclasses import replace

    unsent = replace(zgsz._snapshot(0.24), chain_seat_buckets=("30", "31"),
                     chain_sent_shares={"30": 5.0, "31": 5.0})
    result = zgsz._compile(unsent)
    assert result["basket_target_activated"] is False
    assert _short(result) == []
    sent = replace(unsent, chain_seat_buckets=("30", "31", "32"),
                   chain_sent_shares={"30": 5.0, "31": 5.0, "32": 5.0})
    assert _short(zgsz._compile(sent)) == [("32", "TAKER_FOK")]


# --- second review, 2026-09-17 evening (user: "再次全面检查是否还有bug和漏洞").


def test_v2_the_taf_entry_window_needs_no_send_history() -> None:
    """Charter 90: the TX leg entering a held basket has never been sent --
    that is what entering means.  v2's send-history bound must not refuse
    the one leg charter 90 exists to buy."""
    from dataclasses import replace

    snapshot = replace(
        fixture._held_snapshot(ask_32=0.24),
        chain_seat_buckets=("30", "31"), chain_sent_shares={"30": 5.0, "31": 5.0},
    )

    def balance(**kwargs):
        rows = runtime._target_candidates(
            snapshot=snapshot, desired=("30", "31", "32"),
            artifact=fixture._delta_policy(), capital_available_usdc=10.0,
            minimum_clip_notional_usdc=0.01,
            bankroll_usdc=fixture._basket_policy().total_budget_usdc,
            probabilities_by_bucket={r.bucket: r.probability for r in snapshot.buckets},
            book_equity_usdc=514.29, **kwargs,
        )
        return [r for r in rows if r["candidate_origin"] == kernel.LADDER_BALANCE_CANDIDATE_ORIGIN]

    assert balance() == []
    entry = balance(taf_tx_entry=True)
    assert len(entry) == 1 and entry[0]["completion_143"]["exempt"] is True
    assert entry[0]["shares_by_bucket"]["32"] == 5.0


def test_a_kelly_top_up_the_chain_law_refuses_does_not_take_the_completion_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kernel seats a Kelly row ahead of the completion (charter 72);
    the chain law then judges it as the top-up it is.  Refused, the window
    used to fall with it and the certified completion was never asked.

    On this fixture the Kelly row is floor-legal and takes the seat first;
    with every adding chain read at par, 66(A) refuses it, the solve is
    asked again without it, and the completion is seated."""
    monkeypatch.setattr(runtime, "CHARTER_66A_CHAIN_PAR", "on")
    monkeypatch.setattr(runtime, "_chain_blended_cost", lambda **kw: 1.5)
    # The fixture's own 0.45 on 31: the whole window keeps an edge, so the
    # Kelly row exists to take the seat first (charter 230).
    result = zgsz._compile(zgsz._snapshot(0.24, ask_31=0.45))
    solution = result["target_solution"]
    assert solution["winner_candidate_origin"] == kernel.LADDER_BALANCE_CANDIDATE_ORIGIN
    assert solution["winner_completion_rule"] == RULE
    assert _short(result) == [("32", "TAKER_FOK")]
    excluded = result["basket_support_selection"]["seated"]["chain_gate_excluded_candidate_ids"]
    assert len(excluded) == 1 and excluded[0].startswith("kelly-")
    # Without the retry the same window is refused outright.
    set_charter_143(monkeypatch, "off")
    held = zgsz._compile(zgsz._snapshot(0.24, ask_31=0.45))
    assert held["basket_target_activated"] is False


def test_an_uncertified_shape_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    # The build and the rule read the same ladder; force them apart and the
    # candidate is withheld rather than sent to the support contract.
    real = kernel.completion_143_exemption_v1

    def refused(**kwargs):
        return {**real(**kwargs), "exempt": False, "reason": "forced"}

    monkeypatch.setattr(runtime, "completion_143_exemption_v1", refused)
    snapshot = fixture._held_snapshot(ask_32=0.60)
    rows = fixture._candidates(snapshot)
    assert not [r for r in rows if r["candidate_origin"] == kernel.LADDER_BALANCE_CANDIDATE_ORIGIN]


# --- 2026-09-17 08:42Z: the stage lanes read no send history at all.


def test_one_assembler_attaches_the_station_day_fields_for_both_sources() -> None:
    """Two sources of a book (materialized SSOT rows; live WS quotes on a
    forward day), one assembly.  Until 2026-09-17 each source had its own
    builder attaching its own idea of the chain-level fields: the send
    history (charters 136 五 / 143) and the real account (charter 141)
    reached only ``load_production_market_snapshot`` while every P0-M and
    P1 decision came through ``p0_preselection`` -- the completion rule read
    no history and bought legs nobody had sent, and the whole-book exposure
    bound never bound a stage-lane decision."""
    import inspect
    import json
    import re
    import sqlite3
    from pathlib import Path

    from bayes_basket_target import p0_preselection, production_io
    from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
    from bayes_basket_target.execution_reconciliation import record_order_event

    # Both builders end in the assembler, and nothing else attaches the
    # send history: one place to add a station-day field.
    assert "assemble_market_snapshot(" in inspect.getsource(
        production_io.load_production_market_snapshot
    )
    # The forward source splits into reading the book and assembling it
    # (2026-09-24), so a recorded book reaches the same assembler.
    assert "assemble_market_snapshot(" in inspect.getsource(
        p0_preselection._forward_market_snapshot
    )
    for forward_builder in (
        p0_preselection._market_snapshot,
        p0_preselection._bind_scope_market_evidence,
    ):
        assert "_forward_market_snapshot(" in inspect.getsource(forward_builder)
    package = Path(production_io.__file__).parent
    callers = {
        path.name
        for path in package.glob("*.py")
        if re.search(r"(?<!def )chain_send_history_fields\(", path.read_text(encoding="utf-8"))
    }
    assert callers == {"production_io.py"}
    # The account is read through the chain state (the real one in
    # production, the simulated one on paper), which reads it with
    # ``account_capital_for_snapshot``.
    assert "account_capital=chain_state.account_capital(" in inspect.getsource(
        p0_preselection.compile_p0_preselection
    )
    assert "account_capital_for_snapshot(" in inspect.getsource(
        production_io.SsotChainState.account_capital
    )
    assert "account_capital=None" in inspect.getsource(
        p0_preselection.compile_p0_market_evidence
    )

    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES('c1','plan_1','ZUUU','2026-09-18','Asia/Shanghai','basket',?,1,"
        "'submitted','2026-09-16T22:15:14Z','2026-09-16T22:15:14Z')",
        ("a" * 64,),
    )
    for n, (bucket, size) in enumerate((("24°C", 15.0), ("25°C", 6.0))):
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
            "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
            "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
            "intent_json,intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"i{n}", "d1", "c1", bucket, f"t{n}", "YES", "BUY", 0.1, size, 1.0,
             "submitted", 1, f"k{n}", json.dumps({}), "b" * 64, "2026-09-16T22:15:14Z"),
        )
        record_order_event(conn, order_intent_id=f"i{n}", event={
            "event_type": "FILL", "exchange_order_id": f"0x{n}",
            "observed_at": "2026-09-16T22:15:39Z", "matched_size": size,
            "matched_notional_usdc": 1.0, "event_payload": {},
        })
    buckets = [
        {"bucket": b, "ordinal": i, "ask_levels": [{"price": 0.2, "shares": 10.0}],
         "bid_levels": [], "bid_price": 0.0, "filled_shares": 0.0, "filled_cost_usdc": 0.0,
         "pending_shares": 0.0, "pending_notional_usdc": 0.0}
        for i, b in enumerate(["24°C", "25°C", "26°C", "27°C"])
    ]
    identity = {
        "snapshot_id": "s1", "sequence": 0, "observed_at": "2026-09-17T08:42:00Z",
        "market_id": "m1", "settlement_timezone": "Asia/Shanghai", "quote_version": "q1",
        "quote_observed_at": "2026-09-17T08:42:00Z", "quote_expires_at": "2026-09-17T08:52:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "inventory_version": "i1", "order_state_version": "o1",
    }
    account = {"book_equity_usdc": 88.0, "basket_open_exposure_usdc": 50.0,
               "portfolio_exposure_fraction": 0.9}
    snapshot = production_io.assemble_market_snapshot(
        conn, station_id="ZUUU", event_date="2026-09-18",
        evaluation_at_utc="2026-09-17T08:42:57Z", identity=identity, buckets=buckets,
        remaining_budget_usdc=5.0, cash_phase_input=None, account_capital=account,
        extra={"book_equity_usdc": 1.0},
    )
    assert snapshot["chain_seat_buckets"] == ["24°C", "25°C"]
    assert snapshot["chain_sent_shares"] == {"24°C": 15.0, "25°C": 6.0}
    # The real account overrides the source's planning equity.
    assert snapshot["book_equity_usdc"] == 88.0
    assert snapshot["basket_open_exposure_usdc"] == 50.0
    assert snapshot["portfolio_exposure_fraction"] == 0.9
    assert snapshot["current_core_buckets"] == [] and snapshot["current_direction"] is None
    # A chain that sent nothing carries neither field and hashes as before.
    empty = production_io.assemble_market_snapshot(
        conn, station_id="ZGSZ", event_date="2026-09-18",
        evaluation_at_utc="2026-09-17T08:42:57Z", identity=identity, buckets=buckets,
        remaining_budget_usdc=5.0, cash_phase_input=None, account_capital=None,
    )
    assert "chain_seat_buckets" not in empty and "chain_sent_shares" not in empty
    assert "basket_open_exposure_usdc" not in empty
    # And the rule reads them: 26/27 were never sent, so neither is missing.
    assert runtime._completion_143_sent_shares(
        type(fixture._held_snapshot()).from_mapping({
            **fixture._held_snapshot().to_dict(),
            "chain_seat_buckets": ["30", "31"], "chain_sent_shares": {"30": 5.0, "31": 5.0},
        })
    ) == {"30": 5.0, "31": 5.0}
