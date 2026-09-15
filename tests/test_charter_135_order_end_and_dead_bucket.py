"""Charter 135: an order that ends unfilled re-decides; a dead bucket's bid comes down.

Two user rulings of 2026-09-14.

(一) "撤单后不会重挂：策略重新决策" -- a cancel, a GTD expiry or a killed FOK is
a decision moment, and what runs is the whole strategy.  Before it, the
resting lane cancelled and placed nothing, the next compile asked only about
the model, admissibility and fills, and the rung was simply gone: 37 of 195
cancels ahead of their event day were re-quoted within the hour.

(二) "死桶挂单立即撤单" -- a resting buy on a bucket the day's observed high has
already ruled out is cancelled on the first pass, ahead of any book.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from bayes_basket_target import delta_v2_runtime
from bayes_basket_target import resting_ladder_lifecycle as lane
from bayes_basket_target.basket_kernel.delta_v2 import (
    ORDER_END_EVENT_TYPES,
    is_decision_moment_v1,
    order_end_marker_v1,
)
from bayes_basket_target.execution_reconciliation import record_order_event
from bayes_basket_target.market_registry import apply_target_market_registry_schema
from bayes_basket_target.production_io import (
    load_last_decision_signature,
    load_order_end_count,
)
from bayes_basket_target.production_worker import TargetProductionWorker
from bayes_basket_target.ssot import apply_production_ssot_schema

from tests import test_charter_84_ladder_balance as fixture
from tests import test_resting_ladder_lifecycle as lifecycle_fixture


# ---------------------------------------------------------------------------
# (一) the kernel arm


def _moment(**kwargs):
    base = {
        "probability_identity": "p-1",
        "edge_state": "admissible",
        "last_probability_identity": "p-1",
        "last_edge_state": "admissible",
        "basket_holding": "34:5",
        "last_basket_holding": "34:5",
    }
    return is_decision_moment_v1(**{**base, **kwargs})


def test_an_order_that_ended_unfilled_is_a_moment_on_its_own() -> None:
    assert not _moment(order_ends="ended:2", last_order_ends="ended:2")
    assert _moment(order_ends="ended:3", last_order_ends="ended:2")
    # The first compile under the rule after a decision that saw none.
    assert _moment(order_ends="ended:1", last_order_ends=None)


def test_a_caller_with_no_order_store_keeps_the_arm_off() -> None:
    """The paper ledger's judge attaches no count; that may not read as
    "an order ended"."""

    assert not _moment()
    assert not _moment(order_ends=None, last_order_ends="ended:4")


def test_only_ends_count_and_a_refusal_is_not_an_end() -> None:
    """A barred station refuses every cycle.  Counting refusals would make
    every cycle a moment that mints the same refused order again."""

    assert set(ORDER_END_EVENT_TYPES) == {"CANCEL_ACK", "EXPIRED"}
    assert order_end_marker_v1(0) == "ended:0"


# ---------------------------------------------------------------------------
# (一) the store reading


def _store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("cycle_1", "plan_1", "KDAL", "2026-09-15", "America/Chicago", "basket",
         "a" * 64, 5.0, "submitted", "2026-09-14T10:00:00Z", "2026-09-14T10:00:00Z"),
    )
    for intent_id in ("intent_1", "intent_2"):
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
            "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
            "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
            "intent_json,intent_sha256,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent_id, "decision_1", "cycle_1", "98-99°F", "tok", "YES", "BUY",
             0.30, 5.0, 1.5, "submitted", 1, f"key_{intent_id}",
             json.dumps({"post_only": True, "execution_candidate": "MAKER"}),
             "b" * 64, "2026-09-14T10:00:00Z"),
        )
    conn.commit()
    return conn


def _event(conn, intent_id: str, event_type: str, at: str) -> None:
    record_order_event(conn, order_intent_id=intent_id, event={
        "event_type": event_type, "exchange_order_id": f"0x{intent_id}",
        "observed_at": at, "matched_size": 0.0,
        "matched_notional_usdc": 0.0, "event_payload": {},
    })
    conn.commit()


def _decision(conn, *, at: str, signature: dict) -> None:
    conn.execute(
        "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,decided_state,will_order,"
        "target_execution_hard_deadline_at,reason_codes_json,details_json,"
        "decision_sha256,decision_scope_key,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"decision_{at}", f"cycle_{at}", f"plan_{at}", "KDAL", "2026-09-15",
         "America/Chicago", "basket", "prepared", 0, None, "[]",
         json.dumps({"decision_signature": signature}), "c" * 64,
         f"scope_{at}", at),
    )
    conn.commit()


SIGNATURE = {
    "stage": "P0",
    "probability_identity": "p-1",
    "edge_state": "admissible",
    "basket_holding": "",
    "is_decision_moment": True,
}


def test_the_store_counts_cancels_and_expiries_and_nothing_else() -> None:
    conn = _store()
    _event(conn, "intent_1", "POST_ACK", "2026-09-14T10:00:05Z")
    _event(conn, "intent_2", "POST_ACK", "2026-09-14T10:00:06Z")
    assert load_order_end_count(conn, station_id="KDAL", event_date="2026-09-15") == 0
    _event(conn, "intent_1", "CANCEL_ACK", "2026-09-14T10:30:00Z")
    _event(conn, "intent_2", "EXPIRED", "2026-09-14T11:00:00Z")
    assert load_order_end_count(conn, station_id="KDAL", event_date="2026-09-15") == 2
    assert load_order_end_count(
        conn, station_id="KDAL", event_date="2026-09-15",
        observed_at_or_before="2026-09-14T10:45:00Z",
    ) == 1
    # Another scope's orders are not this scope's.
    assert load_order_end_count(conn, station_id="KDAL", event_date="2026-09-16") == 0


def test_a_cancel_after_the_recorded_decision_makes_the_next_compile_a_moment() -> None:
    conn = _store()
    _event(conn, "intent_1", "POST_ACK", "2026-09-14T10:00:05Z")
    _decision(conn, at="2026-09-14T10:00:00Z", signature={**SIGNATURE, "order_ends": "ended:0"})

    quiet = load_last_decision_signature(
        conn, station_id="KDAL", event_date="2026-09-15", stage="P0",
    )
    assert quiet["current_order_ends"] == quiet["order_ends"] == "ended:0"

    _event(conn, "intent_1", "CANCEL_ACK", "2026-09-14T10:30:00Z")
    loud = load_last_decision_signature(
        conn, station_id="KDAL", event_date="2026-09-15", stage="P0",
    )
    assert (loud["order_ends"], loud["current_order_ends"]) == ("ended:0", "ended:1")
    assert is_decision_moment_v1(
        probability_identity="p-1", edge_state="admissible",
        last_probability_identity=loud["probability_identity"],
        last_edge_state=loud["edge_state"],
        basket_holding="", last_basket_holding="",
        order_ends=loud["current_order_ends"], last_order_ends=loud["order_ends"],
    )


def test_a_decision_from_before_the_rule_is_read_at_its_last_real_moment() -> None:
    """Compiles that were not moments are recorded too, and before this arm
    they were written straight past every cancel.  The count is rebuilt at
    the last row that WAS a moment, so a scope whose order ended after the
    strategy last decided re-decides once, and nothing else does."""

    conn = _store()
    _event(conn, "intent_1", "POST_ACK", "2026-09-14T10:00:05Z")
    _decision(conn, at="2026-09-14T10:00:00Z", signature=dict(SIGNATURE))
    _event(conn, "intent_1", "CANCEL_ACK", "2026-09-14T10:30:00Z")
    # A later compile that was not a moment -- the cancel is behind it.
    _decision(conn, at="2026-09-14T10:40:00Z",
              signature={**SIGNATURE, "is_decision_moment": False})

    stuck = load_last_decision_signature(
        conn, station_id="KDAL", event_date="2026-09-15", stage="P0",
    )
    assert (stuck["order_ends"], stuck["current_order_ends"]) == ("ended:0", "ended:1")

    # Had the strategy decided after the cancel, nothing is new.
    _decision(conn, at="2026-09-14T10:50:00Z", signature=dict(SIGNATURE))
    settled = load_last_decision_signature(
        conn, station_id="KDAL", event_date="2026-09-15", stage="P0",
    )
    assert settled["order_ends"] == settled["current_order_ends"] == "ended:1"


def test_with_no_decision_of_the_stage_the_count_still_rides() -> None:
    conn = _store()
    _event(conn, "intent_1", "EXPIRED", "2026-09-14T10:30:00Z")
    assert load_last_decision_signature(
        conn, station_id="KDAL", event_date="2026-09-15", stage="P1",
    ) == {"current_order_ends": "ended:1"}


def test_the_compile_records_the_count_it_compared_and_re_decides_on_a_new_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        delta_v2_runtime, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp")
    )
    kw = dict(
        snapshot=fixture._held_snapshot(),
        basket_policy=fixture._basket_policy(),
        delta_policy=fixture._delta_policy(),
        fill_resolver=fixture._fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )
    first = fixture.compile_delta_v2_runtime_plan(
        **kw, last_decision_signature={"current_order_ends": "ended:2"},
    )
    recorded = dict(first["delta_v2_audit"]["decision_moment"])
    assert recorded["is_decision_moment"] is True
    assert recorded["order_ends"] == "ended:2"

    same = fixture.compile_delta_v2_runtime_plan(
        **kw, last_decision_signature={**recorded, "current_order_ends": "ended:2"},
    )
    assert same["delta_v2_audit"]["decision_moment"]["is_decision_moment"] is False

    ended = fixture.compile_delta_v2_runtime_plan(
        **kw, last_decision_signature={**recorded, "current_order_ends": "ended:3"},
    )
    moment = ended["delta_v2_audit"]["decision_moment"]
    assert moment["is_decision_moment"] is True
    assert (moment["last_order_ends"], moment["order_ends"]) == ("ended:2", "ended:3")


# ---------------------------------------------------------------------------
# (二) the dead bucket's bid


def _dead(buckets, high=37.5):
    return lambda conn, *, station_id, event_date, as_of_utc: (tuple(buckets), high)


def test_a_bid_on_a_dead_bucket_is_cancelled_at_once_without_a_book() -> None:
    conn = lifecycle_fixture._ledger()
    client = lifecycle_fixture._Client([
        lifecycle_fixture._live(),
        {"ok": True, "status": "cancelled", "response": {"size_matched": 0}},
    ])
    receipt = lane.reprice_resting_rungs(
        conn,
        # Five seconds after the post: no dwell is waited out.
        as_of_utc="2026-09-06T10:00:10Z",
        client=client,
        book_loader=lambda conn, *, station_id, event_date: {},
        dead_bucket_loader=_dead(["98-99°F"]),
    )
    assert receipt["cancel_requested"] == 1
    assert receipt["cancel_requested_dead_bucket"] == 1
    assert receipt["cancel_confirmed"] == 1
    verdict = receipt["rungs"][0]["verdict"]
    assert verdict["reason"] == "bucket_dead_by_observed_high"
    assert verdict["observed_running_high_c"] == 37.5
    assert client.cancels[0]["idempotency_key"].endswith(
        ":cancel:" + lane.DEAD_BUCKET_CANCEL_RULE_VERSION
    )
    assert lifecycle_fixture._state(conn) == ("cancelled", "resolved_cancelled", 0.0)


def test_a_live_bucket_still_answers_to_the_book() -> None:
    conn = lifecycle_fixture._ledger()
    client = lifecycle_fixture._Client([lifecycle_fixture._live()])
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc="2026-09-06T12:00:00Z", client=client,
        book_loader=lifecycle_fixture._book(0.29),
        dead_bucket_loader=_dead(["96-97°F"]),
    )
    assert receipt["cancel_requested"] == 0 and receipt["kept"] == 1
    assert receipt["rungs"][0]["verdict"]["reason"] == "on_anchor"


def test_a_failed_thermometer_read_does_not_stop_the_book_rule() -> None:
    def broken(conn, **_kw):
        raise RuntimeError("canonical down")

    conn = lifecycle_fixture._ledger()
    client = lifecycle_fixture._Client([
        lifecycle_fixture._live(),
        {"ok": True, "status": "cancelled", "response": {"size_matched": 0}},
    ])
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc="2026-09-06T10:00:30Z", client=client,
        book_loader=lifecycle_fixture._book(0.34),
        dead_bucket_loader=broken,
    )
    assert any("dead_bucket_read" in row["reason"] for row in receipt["failures"])
    assert receipt["cancel_requested"] == 1
    assert receipt["cancel_requested_dead_bucket"] == 0
    assert receipt["rungs"][0]["verdict"]["reason"] == "bid_rose_far"


def _registry(conn, rows) -> None:
    apply_target_market_registry_schema(conn)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(basket_target_market_registry)")]
    for outcome, lower, upper, snapshot, refreshed in rows:
        values = {
            "snapshot_sha256": snapshot, "slug": f"slug-{outcome}",
            "event_date": "2026-09-15", "outcome": outcome, "city_slug": "paris",
            "station_id": "LFPB", "settlement_timezone": "Europe/Paris",
            "settlement_unit": "C", "source_lane": "awc", "source_eligible": 1,
            "source_block_reason": None, "resolution_url": "u", "rules_sha256": "r" * 64,
            "yes_token": f"y-{outcome}", "no_token": f"n-{outcome}",
            "market_active": 1, "market_accepting_orders": 1,
            "registry_json": json.dumps({"outcome": outcome, "lower": lower, "upper": upper}),
            "refreshed_at": refreshed,
        }
        present = [c for c in columns if c in values]
        conn.execute(
            f"INSERT INTO basket_target_market_registry({','.join(present)})"
            f" VALUES({','.join('?' for _ in present)})",
            [values[c] for c in present],
        )
    conn.commit()


def test_the_worker_reads_charter_125s_predicate_off_the_latest_registry() -> None:
    conn = sqlite3.connect(":memory:")
    ladder = [("26°C or below", None, 26), ("27°C", 27, 27), ("28°C", 28, 28), ("29°C or higher", 29, None)]
    # An older snapshot that must not be mixed in.
    _registry(conn, [(o, lo, up, "old", "2026-09-14T00:00:00Z") for o, lo, up in ladder])
    _registry(conn, [(o, lo, up, "new", "2026-09-14T12:00:00Z") for o, lo, up in ladder])

    def worker(high):
        return SimpleNamespace(_observed_running_high_c=lambda **_kw: high)

    read = TargetProductionWorker._resting_dead_buckets
    # 28°C observed: every bucket ending at or below 27.5°C is dead.
    assert read(worker(28.0), conn, station_id="LFPB", event_date="2026-09-15",
                as_of_utc="2026-09-15T12:00:00Z") == (("26°C or below", "27°C"), 28.0)
    # 27°C observed: 27°C itself can still be the day's high.
    assert read(worker(27.0), conn, station_id="LFPB", event_date="2026-09-15",
                as_of_utc="2026-09-15T12:00:00Z") == (("26°C or below",), 27.0)
    # No formal observation yet: nothing is dead.
    assert read(worker(None), conn, station_id="LFPB", event_date="2026-09-15",
                as_of_utc="2026-09-15T06:00:00Z") == ((), None)


def test_the_worker_hands_the_lane_its_thermometer() -> None:
    import inspect

    source = inspect.getsource(TargetProductionWorker._reprice_resting_rungs)
    assert "dead_bucket_loader=self._resting_dead_buckets" in source
