"""Charter 180: the resting-rung pass asks its lookups at once, acts one rung at a time.

Fifteen resting orders asked one after another, ~1.3s a gateway round trip
each, were 19.9-20.0s of every cycle (2026-09-21).  With the lookups in
flight together the pass must still write, rung for rung, what the
one-at-a-time pass writes -- receipt, reconciliation rows, cancels -- and
the charter-174 breaker must still stop it at the same rung.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from bayes_basket_target import resting_ladder_lifecycle as lane
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.execution_reconciliation import record_order_event

POSTED = "2026-09-06T10:00:05Z"
NOW = "2026-09-06T12:00:00Z"
LIVE = {"ok": True, "status": "live", "response": {"size_matched": 0.0}}
DEAD_GATEWAY = {
    "ok": False, "error": "gateway_transport:TimeoutError", "transport_ambiguous": True,
}


def _ledger(n: int, *, production_schema: bool = False) -> sqlite3.Connection:
    # check_same_thread stays on: a lookup thread that touched the
    # connection would raise here rather than pass quietly.
    conn = sqlite3.connect(":memory:")
    if production_schema:
        from bayes_basket_target.ssot import apply_production_ssot_schema

        apply_production_ssot_schema(conn)
    else:
        apply_target_ledger_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("cycle_1", "plan_1", "KDAL", "2026-09-07", "America/Chicago", "basket",
         "a" * 64, 5.0, "submitted", "2026-09-06T10:00:00Z", "2026-09-06T10:00:00Z"),
    )
    for index in range(n):
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
            "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
            "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
            "intent_json,intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"intent_{index:02d}", "decision_1", "cycle_1", f"b{index:02d}",
             f"tok{index:02d}", "YES", "BUY", 0.30, 5.0, 1.5, "submitted", 1,
             f"key_{index:02d}",
             json.dumps({"post_only": True, "execution_candidate": "MAKER"}),
             "b" * 64, f"2026-09-06T10:00:{index:02d}Z"),
        )
        record_order_event(conn, order_intent_id=f"intent_{index:02d}", event={
            "event_type": "POST_ACK", "exchange_order_id": f"0x{index:02d}",
            "observed_at": POSTED, "matched_size": 0.0,
            "matched_notional_usdc": 0.0, "event_payload": {},
        })
    conn.commit()
    return conn


class _Client:
    """Answers by order id; a cancelled order answers ``cancelled`` after."""

    def __init__(self, answers, *, delay: float = 0.0) -> None:
        self.answers = dict(answers)
        self.delay = delay
        self.lock = threading.Lock()
        self.asked: list[str] = []
        self.cancels: list[str] = []
        self.cancelled: set[str] = set()
        self.in_flight = 0
        self.max_in_flight = 0

    def lookup_exchange_order(self, request):
        order = request["order_id"]
        with self.lock:
            self.asked.append(order)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            cancelled = order in self.cancelled
        try:
            if self.delay:
                time.sleep(self.delay)
            if cancelled:
                return {"ok": True, "status": "CANCELED", "response": {"size_matched": 0.0}}
            return dict(self.answers.get(order, LIVE))
        finally:
            with self.lock:
                self.in_flight -= 1

    def lookup_open_orders(self, request):
        return {"ok": True, "orders": []}

    def lookup_position(self, request):
        return {"ok": True, "position": {"size": 0.0}}

    def cancel_order(self, request):
        with self.lock:
            self.cancels.append(request["order_id"])
            self.cancelled.add(request["order_id"])
        return {"ok": True, "cancelled": True, "error": ""}


def _book(far: set[str]):
    # Buckets in ``far`` have a bid ten ticks above the 0.30 rung: cancelled.
    def load(conn, *, station_id, event_date):
        return {
            f"b{i:02d}": {
                "bid": 0.40 if f"b{i:02d}" in far else 0.29,
                "ask": 0.45, "tick": 0.01,
            }
            for i in range(40)
        }
    return load


def _states(conn):
    return conn.execute(
        "SELECT i.order_intent_id, i.state, r.state, r.filled_size"
        " FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " ORDER BY i.order_intent_id"
    ).fetchall()


def _run(n, answers, *, concurrency, far=frozenset(), start_index=0, delay=0.0):
    conn = _ledger(n)
    client = _Client(answers, delay=delay)
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc=NOW, client=client, book_loader=_book(set(far)),
        dwell_seconds=300.0, far_ticks=3.0, start_index=start_index,
        lookup_concurrency=concurrency,
    )
    receipt = dict(receipt)
    assert receipt.pop("lookup_concurrency") == max(1, concurrency)
    return receipt, _states(conn), client


MIXED = {
    "0x02": {"ok": True, "status": "MATCHED", "response": {"size_matched": 5.0}},
    "0x03": {"ok": True, "status": "CANCELED", "response": {"size_matched": 0.0}},
    "0x05": {"ok": False, "error": "gateway_http_error", "http_status": 404},
    "0x07": {"ok": True, "status": "live", "response": {"size_matched": 2.0}},
    "0x09": {"ok": True, "status": "EXPIRED", "response": {"size_matched": 0.0}},
}


def test_overlapping_lookups_write_what_the_one_at_a_time_pass_writes() -> None:
    far = {"b01", "b04", "b10"}
    for start in (0, 4):
        serial = _run(12, MIXED, concurrency=1, far=far, start_index=start)
        overlap = _run(12, MIXED, concurrency=6, far=far, start_index=start)
        assert overlap[0] == serial[0]
        assert overlap[1] == serial[1]
        assert overlap[2].cancels == serial[2].cancels
        # every rung asked once, plus one confirm per cancel
        assert sorted(overlap[2].asked) == sorted(serial[2].asked)
    assert serial[0]["cancel_requested"] == 3
    assert serial[0]["synced_filled"] == 1


def test_the_breaker_stops_the_overlapping_pass_at_the_same_rung() -> None:
    answers = {"0x04": DEAD_GATEWAY}
    serial = _run(12, answers, concurrency=1)
    overlap = _run(12, answers, concurrency=6)
    assert overlap[0] == serial[0]
    assert overlap[1] == serial[1]
    assert overlap[0]["deferred"] == [f"intent_{i:02d}" for i in range(5, 12)]
    assert overlap[0]["next_start_index"] == 5
    # nothing past the failed rung was acted on
    assert overlap[2].cancels == []


def test_a_first_lookup_that_trips_the_breaker_asks_nothing_else() -> None:
    receipt, _states_, client = _run(12, {"0x00": DEAD_GATEWAY}, concurrency=6)
    assert client.asked == ["0x00"]
    assert receipt["deferred_count"] == 11


def test_lookups_are_in_flight_together_up_to_the_bound() -> None:
    _r, _s, serial = _run(12, {}, concurrency=1, delay=0.05)
    assert serial.max_in_flight == 1
    _r, _s, overlap = _run(12, {}, concurrency=6, delay=0.05)
    assert 1 < overlap.max_in_flight <= 6


def test_production_passes_the_bound() -> None:
    assert lane.RESTING_LOOKUP_CONCURRENCY == 6


# --- the ledger fill rows ----------------------------------------------------------

def test_the_scoped_fill_read_starts_from_the_cycle_scope(tmp_path) -> None:
    from bayes_basket_target import inventory_producer as ip

    # The cycle scope index is declared with the production DDL.
    conn = _ledger(3, production_schema=True)
    conn.execute(
        "UPDATE basket_target_order_reconciliations SET state='resolved_filled',"
        " filled_size=5.0, filled_notional_usdc=1.5 WHERE order_intent_id='intent_01'"
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    rows = ip._ledger_fill_rows(conn, station_id="KDAL", event_date="2026-09-07")
    conn.set_trace_callback(None)
    assert [row["order_intent_id"] for row in rows] == ["intent_01"]
    sql = [s for s in statements if "basket_target_order_reconciliations r" in s][0]
    plan = " | ".join(str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "basket_target_execution_cycles_scope_idx" in plan
    # unscoped: the state filter is still the narrowing
    assert [row["order_intent_id"] for row in ip._ledger_fill_rows(conn)] == ["intent_01"]


# --- the cycle stack sampler ---------------------------------------------------------

def test_the_sampler_names_the_function_the_thread_is_in(monkeypatch) -> None:
    from pathlib import Path

    from bayes_basket_target import cycle_stack_sampler as sampler

    here = str(Path(__file__).resolve().parent) + "/"
    monkeypatch.setattr(sampler, "_REPO_DIRS", (*sampler._REPO_DIRS, here))
    stop = threading.Event()
    ready = threading.Event()
    ident: list[int] = []

    def hot_function_for_the_sampler() -> None:
        ident.append(threading.get_ident())
        ready.set()
        x = 0
        while not stop.is_set():
            x += 1

    worker = threading.Thread(target=hot_function_for_the_sampler)
    worker.start()
    ready.wait(1.0)
    probe = sampler.CycleStackSampler(ident[0], interval_sec=0.01).start()
    time.sleep(0.3)
    samples = probe.stop()
    stop.set()
    worker.join(1.0)
    assert len(samples) >= 5
    summary = sampler.summarize_samples(
        samples, marks=[("stage_a", time.perf_counter())], interval_sec=0.01,
    )
    top_key = summary["repo_self_top"][0][0]
    assert "hot_function_for_the_sampler" in top_key
    assert set(summary["by_stage"]) == {"stage_a"}


def test_samples_fall_in_the_first_stage_marked_after_them() -> None:
    from bayes_basket_target import cycle_stack_sampler as sampler

    samples = [
        (1.0, "leaf", "a.py:f:1", ("a.py:main:1", "a.py:f:1")),
        (2.0, "leaf", "a.py:f:1", ("a.py:main:1", "a.py:f:1")),
        (3.0, "leaf", "a.py:g:9", ("a.py:main:1", "a.py:g:9")),
        (9.0, "leaf", "", ()),
    ]
    summary = sampler.summarize_samples(
        samples, marks=[("second", 3.5), ("first", 2.5)], interval_sec=0.5,
    )
    assert summary["by_stage"]["first"]["sec"] == 1.0
    assert summary["by_stage"]["second"]["repo_self_top"] == [["a.py:g:9", 0.5]]
    assert summary["by_stage"]["after_last_mark"]["repo_self_top"] == [["<outside repo>", 0.5]]
    assert dict(map(tuple, summary["repo_cum_top"]))["a.py:main:1"] == 1.5


def test_marks_are_logged_and_the_history_keeps_the_python_top() -> None:
    from bayes_basket_target import production_worker as pw
    from scripts import weather_basket_yes_daemon as daemon

    worker = object.__new__(pw.TargetProductionWorker)
    worker._stage_mark_log = []
    worker._mark("activation")
    worker._mark("early_p1")
    assert [stage for stage, _ in worker._stage_mark_log] == ["activation", "early_p1"]

    top = {"interval_ms": 50.0, "samples": 2, "repo_self_top": [["x.py:f:1", 0.1]]}
    row = daemon.production_cycle_history_row(
        {"stage_timings_ms": {"cycle_total": 1.0}, "stage_span_ms": {},
         "python_sample_top": top},
        cycle_index=1, process_started_at_utc="t0", completed_at_utc="t1",
    )
    assert row["py_top"] == top
