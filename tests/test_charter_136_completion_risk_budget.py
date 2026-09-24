"""Charter 136: a broken seat is completed when its exposure outweighs the edge.

User rulings 2026-09-15: "补单的负边际权重判断要小于篮子不完整的暴露风险"; "篮子不
完整的风险暴露的成本、已建仓篮子的成本、整篮不命中的成本，要综合考虑，既要降低风险
暴露，又不能放大风险暴露"; "落地".

Settled live station-days 2026-09-06..14: 15 of the 27 broken seats settled on
the missing leg; completing all 27 at the price posted would have turned
-23.05 into +5.89, and at ten cents over it into -29.90.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from bayes_basket_target import delta_v2_runtime as runtime
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.basket_kernel import delta_v2 as kernel
from bayes_basket_target.execution_reconciliation import record_order_event
from bayes_basket_target.production_io import load_chain_seat_buckets
from tests import test_charter_84_ladder_balance as fixture
from tests.charter_143_switch import set_charter_143

ROOT = Path(__file__).resolve().parents[1]
RULE = kernel.COMPLETION_RISK_BUDGET_RULE_VERSION


def _book(price: float, shares: float = 100.0):
    return ({"price": price, "shares": shares},)


def _budget(**overrides):
    kwargs = dict(
        window=("18°C", "19°C"),
        held_shares_by_bucket={"18°C": 5.0},
        held_cost_by_bucket={"18°C": 1.65},
        chain_cost_usdc=1.65,
        ask_by_bucket={"18°C": 0.34, "19°C": 0.52},
        depth_by_bucket={"18°C": _book(0.34), "19°C": _book(0.52)},
        claim_by_bucket={"18°C": 0.362, "19°C": 0.505},
        budget_usdc=4.30,
        hours_to_cutoff=26.0,
        chain_seat_buckets=("18°C", "19°C"),
        chain_held_buckets=("18°C",),
    )
    kwargs.update(overrides)
    return kernel.completion_risk_budget_v1(**kwargs)


def test_the_budget_is_the_station_days_ceiling_less_the_chain() -> None:
    # Charter 141: one number for everything that goes out -- 10% of the real
    # book less the chain.  It was 1.5x the 2% x stage-share allowance, a
    # second number for the same question.
    budget = kernel.completion_budget_usdc_v1(
        book_equity_usdc=99.80, stage="P0", committed_usdc=2.15,
    )
    assert budget == pytest.approx(99.80 * kernel.STATION_DAY_HARD_FRACTION - 2.15)
    assert budget == pytest.approx(kernel.station_day_ceiling_usdc(
        book_equity_usdc=99.80, committed_usdc=2.15,
    ))
    # Every stage reads the same ceiling.
    assert kernel.completion_budget_usdc_v1(
        book_equity_usdc=99.80, stage="P1", committed_usdc=2.15,
    ) == pytest.approx(budget)
    assert kernel.completion_budget_usdc_v1(
        book_equity_usdc=100.0, stage="P0", committed_usdc=50.0,
    ) == 0.0


def test_a_leg_the_market_took_is_completed_on_the_exposure() -> None:
    # EHAM 19C: 0.52 against a 0.505 claim -> conditioned 0.808.
    reading = _budget()
    assert (reading["admits"], reading["reason"]) == (True, "exposure_outweighs_edge")
    assert reading["spend_usdc"] == pytest.approx(2.60)
    assert reading["delta_ev_usdc"] == pytest.approx(5 * 0.808 - 2.60)
    assert reading["worst_in_window_before_usdc"] == pytest.approx(-1.65)
    assert reading["worst_in_window_after_usdc"] == pytest.approx(0.75)
    assert reading["delta_exposure_usdc"] == pytest.approx(2.40)
    assert reading["miss_loss_after_usdc"] == pytest.approx(4.25)


def test_a_leg_the_market_handed_us_is_completed_only_when_the_exposure_pays_for_it() -> None:
    # LFPB 2026-09-14 29C at 2026-09-13 09:47Z: 27C and 28C held at five for
    # 2.95, 29C offered at 0.09 against a 0.1845 claim -> conditioned 0.0517.
    # Gives up 0.19 of expectation to remove 4.55 of exposure: allowed 0.23.
    lfpb = _budget(
        window=("27°C", "28°C", "29°C"),
        held_shares_by_bucket={"27°C": 5.0, "28°C": 5.0},
        held_cost_by_bucket={"27°C": 1.80, "28°C": 1.15},
        chain_cost_usdc=2.95,
        ask_by_bucket={"27°C": 0.41, "28°C": 0.27, "29°C": 0.09},
        depth_by_bucket={"27°C": _book(0.41), "28°C": _book(0.27), "29°C": _book(0.09, 72.88)},
        claim_by_bucket={"27°C": 0.187, "28°C": 0.326, "29°C": 0.18452334679774293},
        budget_usdc=10.0,
        chain_seat_buckets=("27°C", "28°C", "29°C"),
        chain_held_buckets=("27°C", "28°C"),
    )
    assert lfpb["admits"] is True
    assert lfpb["delta_ev_usdc"] == pytest.approx(-0.192, abs=1e-3)
    assert lfpb["delta_exposure_usdc"] == pytest.approx(4.55)
    # The same hand-me-down at a dear price removes less than it gives up.
    dear = _budget(
        ask_by_bucket={"18°C": 0.34, "19°C": 0.40},
        depth_by_bucket={"18°C": _book(0.34), "19°C": _book(0.40)},
        claim_by_bucket={"18°C": 0.362, "19°C": 0.50},
    )
    assert (dear["admits"], dear["reason"]) == (False, "edge_given_up_exceeds_exposure_removed")


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"held_cost_by_bucket": {"18°C": 3.0}, "chain_cost_usdc": 3.0,
          "ask_by_bucket": {"18°C": 0.34, "19°C": 0.42},
          "depth_by_bucket": {"18°C": _book(0.34), "19°C": _book(0.42)},
          "claim_by_bucket": {"18°C": 0.362, "19°C": 0.40}, "budget_usdc": 10.0},
         "no_floor_legal_level"),
        ({"chain_seat_buckets": ("18°C",)}, "leg_outside_the_seat"),
        ({"chain_held_buckets": ("17°C", "18°C")}, "window_drops_a_held_leg"),
        ({"hours_to_cutoff": 3.0}, "inside_completion_cutoff"),
        ({"budget_usdc": 0.5}, "no_level_inside_the_budget"),
        ({"held_shares_by_bucket": {}}, "not_a_broken_seat"),
        ({"ask_by_bucket": {"18°C": 0.34, "19°C": None}}, "short_leg_unpriced"),
    ],
)
def test_each_guard_refuses_by_name(overrides, reason) -> None:
    reading = _budget(**overrides)
    assert (reading["admits"], reading["reason"]) == (False, reason)


@pytest.fixture
def armed(monkeypatch):
    # Charter 143 replaced this admission for completions; the budget is
    # read only under its rollback.
    set_charter_143(monkeypatch, "off")
    monkeypatch.setattr(runtime, "CHARTER_136_COMPLETION", "on")


def _snapshot(ask_32: float, *, seat=("30", "31", "32"), hold_30: bool = True):
    snapshot = fixture._held_snapshot(ask_32=ask_32)
    rows = snapshot.buckets
    if not hold_30:
        rows = tuple(
            replace(row, filled_shares=0.0, filled_cost_usdc=0.0)
            if row.bucket == "30" else row
            for row in rows
        )
    return replace(snapshot, buckets=rows, chain_seat_buckets=tuple(seat))


def _compile(snapshot, *, hours=None, budget=10.0) -> dict:
    return fixture.compile_delta_v2_runtime_plan(
        snapshot=snapshot,
        basket_policy=fixture._basket_policy(),
        delta_policy=fixture._delta_policy(),
        fill_resolver=fixture._fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
        hours_to_cutoff=hours,
        completion_budget_usdc=budget,
    )


def _short(result: dict) -> list[tuple[str, str]]:
    return [
        (row["bucket"], row["execution_candidate"])
        for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
    ]


def test_the_arm_ships_off() -> None:
    assert runtime.CHARTER_136_COMPLETION == "off"


def test_off_is_the_charter_130_book(monkeypatch) -> None:
    set_charter_143(monkeypatch, "off")
    result = _compile(_snapshot(0.24))
    assert result["basket_target_activated"] is False
    assert _short(result) == []


def test_armed_the_taken_leg_is_bought_as_the_completion_fok(armed) -> None:
    result = _compile(_snapshot(0.24))
    seat = result["basket_support_selection"]["seated"]
    assert seat["buckets"] == ["30", "31", "32"]
    assert seat["completion"]["admitted_by"] == RULE
    assert result["post_lift_edge_exempt_rule"] == RULE
    assert result["completion_risk_budget"]["reason"] == "exposure_outweighs_edge"
    assert _short(result) == [("32", "TAKER_FOK")]


@pytest.mark.parametrize(
    "snapshot_kwargs, compile_kwargs",
    [
        ({"ask_32": 0.18}, {}),
        ({"ask_32": 0.24, "seat": ("30", "31")}, {}),
        ({"ask_32": 0.24}, {"hours": 2.0}),
        ({"ask_32": 0.24}, {"budget": 0.5}),
    ],
    ids=["handed_us_and_not_worth_it", "outside_the_seat", "inside_cutoff", "over_budget"],
)
def test_armed_refusals_hold_the_seat(armed, snapshot_kwargs, compile_kwargs) -> None:
    ask = snapshot_kwargs.pop("ask_32")
    result = _compile(_snapshot(ask, **snapshot_kwargs), **compile_kwargs)
    assert result["basket_target_activated"] is False
    assert _short(result) == []


def test_armed_a_relabelled_shape_does_not_strand_the_seat(armed) -> None:
    # ZGSZ 2026-09-16: held 32C, missing 31C, the label turned SYMMETRIC and
    # allowed only {30,31,32}.  Here 31 is held, 32 missing, the fixture's
    # label allows only {30,31,32}: the seat {31,32} completes anyway.
    result = _compile(_snapshot(0.24, seat=("31", "32"), hold_30=False))
    seat = result["basket_support_selection"]["seated"]
    assert seat["buckets"] == ["31", "32"]
    assert seat["completion"]["admitted_by"] == RULE
    assert _short(result) == [("32", "TAKER_FOK")]


def test_armed_the_hurdle_path_is_unchanged(armed) -> None:
    completion = _compile(_snapshot(0.15))["basket_support_selection"]["seated"]["completion"]
    assert completion["admitted_by"] == "hurdle"


def test_the_snapshot_hashes_as_before_when_the_chain_has_sent_nothing() -> None:
    snapshot = fixture._held_snapshot(ask_32=0.15)
    assert "chain_seat_buckets" not in snapshot.to_dict()
    seated = replace(snapshot, chain_seat_buckets=("30", "31", "32"))
    assert list(seated.to_dict()["chain_seat_buckets"]) == ["30", "31", "32"]
    assert seated.content_sha256 != snapshot.content_sha256
    raw = {**snapshot.to_dict(), "chain_seat_buckets": ["99"]}
    with pytest.raises(Exception, match="snapshot_chain_seat_bucket_unknown"):
        type(snapshot).from_mapping(raw)


def test_the_seat_is_what_reached_the_venue() -> None:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES('c1','plan_1','LFPB','2026-09-14','Europe/Paris','basket',?,1,'submitted',"
        "'2026-09-13T06:00:00Z','2026-09-13T06:00:00Z')",
        ("a" * 64,),
    )
    for n, (bucket, event) in enumerate(
        (("25°C", "NEVER_REACHED_EXCHANGE"), ("27°C", "POST_ACK"),
         ("28°C", "FILL"), ("29°C", "CANCEL_ACK"), ("30°C", "SUBMIT_REFUSED")),
    ):
        intent = f"i{n}"
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
            "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
            "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
            "intent_json,intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent, "d1", "c1", bucket, f"t{n}", "YES", "BUY", 0.1, 5.0, 0.5,
             "submitted", 1, f"k{n}", json.dumps({"post_only": True}), "b" * 64,
             "2026-09-13T06:00:00Z"),
        )
        record_order_event(conn, order_intent_id=intent, event={
            "event_type": event, "exchange_order_id": f"0x{n}",
            "observed_at": "2026-09-13T06:00:01Z", "matched_size": 5.0 if event == "FILL" else 0.0,
            "matched_notional_usdc": 0.5 if event == "FILL" else 0.0, "event_payload": {},
        })
    assert load_chain_seat_buckets(
        conn, station_id="LFPB", event_date="2026-09-14",
    ) == frozenset({"27°C", "28°C", "29°C"})


def test_the_ledger_judges_under_the_same_rule_and_hashes_it() -> None:
    spec = importlib.util.spec_from_file_location(
        "ledger_136", ROOT / "scripts" / "paper_trading_ledger.py"
    )
    ledger = importlib.util.module_from_spec(spec)
    sys.modules["ledger_136"] = ledger
    spec.loader.exec_module(ledger)
    assert ledger._CHARTER_136_COMPLETION is runtime.CHARTER_136_COMPLETION
    assert ledger._completion_budget_usdc_v1 is kernel.completion_budget_usdc_v1
    assert ledger._SELECTION_RULE_IDENTITY["charter_136_completion"] == "off"
    import inspect
    assert "chain_seat_buckets" in inspect.signature(ledger._production_judge).parameters
    assert "chain_seat_buckets" in inspect.signature(ledger._paper_snapshot_mapping).parameters
