"""Charter 132 stage D: the live attribution's arithmetic.

The report is only worth reading if its pieces add up to the cash and its
order is the one it states; and an order whose exchange outcome is unknown
must never be booked as filled or unfilled.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import edge_attribution as A  # noqa: E402


@pytest.mark.parametrize("y", [0.0, 1.0])
@pytest.mark.parametrize("paid", [0.30, 0.45])
def test_the_four_pieces_add_up_to_the_cash(y, paid):
    parts = A.decompose(shares=12.5, paid=paid, ask=0.40, p=0.55, m=0.35, y=y)
    assert sum(parts[k] for k in A.COMPONENTS) == pytest.approx(parts["total"])
    assert parts["total"] == pytest.approx(12.5 * (y - paid))
    assert parts["claimed_edge"] == pytest.approx(12.5 * (0.55 - 0.40))
    assert parts["execution"] == pytest.approx(12.5 * (0.40 - paid))
    assert parts["market_correction"] == pytest.approx(12.5 * (0.35 - 0.55))


def _leg(**over):
    base = {
        "intent": "i", "station": "S", "date": "2026-09-01", "stage": "P0",
        "decided_at": "2026-09-01T00:00:00Z", "bucket": "a", "filled": 5.0,
        "notional": 2.0, "limit_price": 0.4, "requested": 5.0, "ask": 0.42,
        "p": 0.5, "m": 0.4, "y": 0.0,
    }
    base.update(over)
    return base


def test_selection_is_measured_against_the_market_reference():
    legs = [
        _leg(intent="f1", filled=5.0, m=0.4, y=0.0),
        _leg(intent="f2", filled=5.0, m=0.2, y=0.0),
        _leg(intent="u1", filled=0.0, notional=0.0, m=0.4, y=1.0),
        _leg(intent="u2", filled=0.0, notional=0.0, m=0.2, y=0.0),
    ]
    got = A.attribute(legs)["selection"]
    assert got["filled"]["outcome_minus_market"] == pytest.approx(-0.3)
    assert got["unfilled"]["outcome_minus_market"] == pytest.approx(0.2)
    assert got["adverse_selection_points"] == pytest.approx(0.5)


def test_only_filled_legs_carry_cash():
    legs = [_leg(intent="f", filled=5.0), _leg(intent="u", filled=0.0, notional=0.0)]
    whole = A.attribute(legs)["by_stage"]["all"]
    assert whole["legs"] == 1 and whole["shares"] == 5.0


def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_target_order_intents(order_intent_id TEXT,"
        " decision_id TEXT, bucket TEXT, limit_price REAL, requested_size REAL,"
        " side TEXT, order_action TEXT);"
        "CREATE TABLE basket_target_order_reconciliations(order_intent_id TEXT,"
        " state TEXT, filled_size REAL, filled_notional_usdc REAL);"
        "CREATE TABLE basket_target_order_events(order_intent_id TEXT, event_type TEXT);"
        "CREATE TABLE basket_policy_training_fact(decision_id TEXT, station_id TEXT,"
        " event_date TEXT, stage TEXT, decision_at_utc TEXT,"
        " probability_envelope_id TEXT, ws_snapshot_id TEXT);"
        "CREATE TABLE basket_probability_envelope(envelope_id TEXT, probabilities_json TEXT);"
        "CREATE TABLE basket_official_ws_snapshot(snapshot_id TEXT, buckets_json TEXT);"
        "CREATE TABLE paper_trade_candidate_window(station_id TEXT, event_date TEXT,"
        " settled_bucket TEXT, settled_at_utc TEXT);"
    )
    book = [
        {"bucket": "a", "ask_levels": [{"price": 0.42, "shares": 10}], "ask_available": True,
         "bid_levels": [{"price": 0.38, "shares": 10}], "bid_price": 0.38},
        {"bucket": "b", "ask_levels": [{"price": 0.62, "shares": 10}], "ask_available": True,
         "bid_levels": [{"price": 0.58, "shares": 10}], "bid_price": 0.58},
    ]
    conn.execute("INSERT INTO basket_policy_training_fact VALUES('d','S','2026-09-01','P0','2026-09-01T00:00:00Z','e','w')")
    conn.execute("INSERT INTO basket_probability_envelope VALUES('e',?)", (json.dumps({"a": 0.5, "b": 0.5}),))
    conn.execute("INSERT INTO basket_official_ws_snapshot VALUES('w',?)", (json.dumps(book),))
    conn.execute("INSERT INTO paper_trade_candidate_window VALUES('S','2026-09-01','a','2026-09-02T00:00:00Z')")
    return conn


def test_an_unknown_exchange_outcome_is_never_booked():
    conn = _db()
    conn.execute("INSERT INTO basket_target_order_intents VALUES('k','d','a',0.42,5,'YES','BUY')")
    conn.execute("INSERT INTO basket_target_order_events VALUES('k','UNKNOWN_POST')")
    conn.execute("INSERT INTO basket_target_order_reconciliations VALUES('k','pending',0,0)")
    conn.execute("INSERT INTO basket_target_order_intents VALUES('f','d','a',0.42,5,'YES','BUY')")
    conn.execute("INSERT INTO basket_target_order_reconciliations VALUES('f','resolved_filled',5,2.0)")
    legs, counts = A.load_legs(conn)
    assert [leg["intent"] for leg in legs] == ["f"]
    assert counts["unknown_outcome"] == 1
    assert legs[0]["y"] == 1.0 and legs[0]["ask"] == pytest.approx(0.42)
