"""Charter 174: one transport failure ends the resting-rung pass.

Eleven resting sells at ~35s each -- every lookup waiting out the gateway's
timeout -- made this pass 388 seconds of a 650-second cycle on 2026-09-21,
in front of every decision.  The rungs the pass did not reach are named and
the next pass starts one past the rung that failed, so no order the exchange
cannot answer for is asked first every time.
"""

import json
import sqlite3

from bayes_basket_target import resting_ladder_lifecycle as lane
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.execution_reconciliation import record_order_event

POSTED = "2026-09-06T10:00:05Z"
NOW = "2026-09-06T12:00:00Z"
ORDERS = ("0xa", "0xb", "0xc")


def _ledger() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,station_id,"
        "event_date,settlement_timezone,strategy_lane,plan_sha256,"
        "requested_reserve_usdc,state,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("cycle_1", "plan_1", "KDAL", "2026-09-07", "America/Chicago", "basket",
         "a" * 64, 5.0, "submitted", "2026-09-06T10:00:00Z", "2026-09-06T10:00:00Z"),
    )
    for index, order in enumerate(ORDERS):
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,"
            "cycle_id,bucket,token_id,side,order_action,limit_price,requested_size,"
            "requested_notional_usdc,state,live_submit_authorized,idempotency_key,"
            "intent_json,intent_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"intent_{index}", "decision_1", "cycle_1", f"9{index}-9{index + 1}°F",
             f"tok{index}", "YES", "BUY", 0.30, 5.0, 1.5, "submitted", 1,
             f"key_{index}", json.dumps({"post_only": True, "execution_candidate": "MAKER"}),
             "b" * 64, f"2026-09-06T10:00:0{index}Z"),
        )
        record_order_event(conn, order_intent_id=f"intent_{index}", event={
            "event_type": "POST_ACK", "exchange_order_id": order,
            "observed_at": POSTED, "matched_size": 0.0,
            "matched_notional_usdc": 0.0, "event_payload": {},
        })
    conn.commit()
    return conn


class _Client:
    def __init__(self, answers) -> None:
        self.answers = dict(answers)
        self.asked: list[str] = []
        self.cancels: list[dict] = []

    def lookup_exchange_order(self, request):
        self.asked.append(request["order_id"])
        return dict(self.answers[request["order_id"]])

    def lookup_open_orders(self, request):
        return {"ok": True, "orders": []}

    def lookup_position(self, request):
        return {"ok": True, "position": {"size": 0.0}}

    def cancel_order(self, request):
        self.cancels.append(dict(request))
        return {"ok": True, "cancelled": True, "error": ""}


LIVE = {"ok": True, "status": "live", "response": {"size_matched": 0.0}}
DEAD_GATEWAY = {"ok": False, "error": "gateway_transport:TimeoutError", "transport_ambiguous": True}
BOOK = lambda conn, *, station_id, event_date: {  # noqa: E731 -- a fixture
    f"9{i}-9{i + 1}°F": {"bid": 0.29, "ask": 0.40, "tick": 0.01} for i in range(3)
}


def _states(conn):
    return conn.execute(
        "SELECT i.order_intent_id, i.state, r.state FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " ORDER BY i.order_intent_id"
    ).fetchall()


def test_a_transport_failure_defers_the_rest_and_names_them() -> None:
    conn = _ledger()
    client = _Client({"0xa": DEAD_GATEWAY, "0xb": LIVE, "0xc": LIVE})
    before = _states(conn)
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc=NOW, client=client, book_loader=BOOK,
        dwell_seconds=300.0, far_ticks=3.0,
    )
    assert client.asked == ["0xa"]
    assert receipt["deferred"] == ["intent_1", "intent_2"]
    assert receipt["deferred_count"] == 2
    assert "gateway_transport" in receipt["deferred_reason"]
    assert receipt["next_start_index"] == 1
    assert receipt["rungs"][0]["verdict"]["action"] == "deferred"
    assert client.cancels == []
    assert receipt["state"] == "no_change"
    assert _states(conn) == before


def test_the_next_pass_starts_past_the_failed_rung_and_wraps() -> None:
    conn = _ledger()
    client = _Client({"0xa": LIVE, "0xb": LIVE, "0xc": LIVE})
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc=NOW, client=client, book_loader=BOOK,
        dwell_seconds=300.0, far_ticks=3.0, start_index=1,
    )
    assert client.asked == ["0xb", "0xc", "0xa"]
    assert receipt["start_index"] == 1
    assert receipt["next_start_index"] == 0
    assert receipt["deferred"] == []
    assert receipt["kept"] == 3


def test_a_start_index_past_the_end_wraps_round() -> None:
    conn = _ledger()
    client = _Client({"0xa": LIVE, "0xb": LIVE, "0xc": LIVE})
    lane.reprice_resting_rungs(
        conn, as_of_utc=NOW, client=client, book_loader=BOOK,
        dwell_seconds=300.0, far_ticks=3.0, start_index=5,
    )
    assert client.asked == ["0xc", "0xa", "0xb"]


def test_an_answered_refusal_is_not_a_breaker() -> None:
    conn = _ledger()
    refused = {"ok": False, "error": "gateway_http_error", "http_status": 404}
    client = _Client({"0xa": refused, "0xb": LIVE, "0xc": LIVE})
    receipt = lane.reprice_resting_rungs(
        conn, as_of_utc=NOW, client=client, book_loader=BOOK,
        dwell_seconds=300.0, far_ticks=3.0,
    )
    assert client.asked == ["0xa", "0xb", "0xc"]
    assert receipt["deferred"] == []
    assert receipt["next_start_index"] == 0


def test_the_worker_hands_the_cursor_back_and_forth() -> None:
    import ast
    import inspect
    import textwrap

    from bayes_basket_target import production_worker as pw

    tree = ast.parse(textwrap.dedent(inspect.getsource(pw.TargetProductionWorker._reprice_resting_rungs)))
    keywords = {
        kw.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "reprice_resting_rungs"
        for kw in node.keywords
    }
    assert "start_index" in keywords
    stored = {
        target.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
    }
    assert "_resting_reprice_cursor" in stored
