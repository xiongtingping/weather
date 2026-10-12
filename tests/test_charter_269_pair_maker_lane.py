"""Charter 269: the pair maker lane (user rulings 2026-10-12/13
「成形即挂、众数桶、允许 NO 单边、0.95 作挂价上限」)."""
from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path

import pytest

import bayes_basket_target.pair_maker_lane as P
import bayes_basket_target.resting_ladder_lifecycle as R
import bayes_basket_target.strategy_lanes as S

ROOT = Path(__file__).resolve().parents[1]
CFG = dict(S.PAIR_MAKER_DEFAULT_SETTINGS)
NOW = "2026-10-13T06:00:00Z"


def _row(bucket, ordinal, bid, ask, shares=50.0):
    return {"bucket": bucket, "ordinal": ordinal, "bid_available": bid is not None, "bid_price": bid or 0.0,
            "ask_levels": [] if ask is None else [{"price": ask, "shares": shares}], "price_tick_size": 0.001}


def _formed_book():
    # mids 0.05, 0.20, 0.42, 0.25, 0.06 -> sum 0.98, mode 20°C
    return [_row("18°C", 0, 0.04, 0.06), _row("19°C", 1, 0.18, 0.22), _row("20°C", 2, 0.40, 0.44),
            _row("21°C", 3, 0.23, 0.27), _row("22°C", 4, 0.05, 0.07)]


def test_the_lane_is_registered_and_off_by_default():
    assert S.LANE_PAIR_MAKER in S.KNOWN_LANES
    assert S.DEFAULT_LANES[S.LANE_PAIR_MAKER]["enabled"] is False
    assert CFG["pair_cost_cap"] == 0.95 and CFG["window_hours"] == 6.0 and CFG["units"] == ["C"]
    board = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    assert 'pair_maker: {name:"配对挂单线"' in board


def test_formation_needs_every_bucket_two_sided_and_the_mids_in_range():
    form = P.formation(_formed_book(), CFG)
    assert form["formed"] and form["bucket"] == "20°C" and form["mid"] == pytest.approx(0.42)
    one_sided = _formed_book()
    one_sided[0] = _row("18°C", 0, None, 0.06)
    assert P.formation(one_sided, CFG) == {"formed": False, "reason": "one_sided_bucket"}
    # The listing ladder: asks summing near 3 -> not formed.
    listing = [_row(f"{t}°C", i, 0.16, 0.46) for i, t in enumerate(range(18, 25))]
    assert P.formation(listing, CFG)["formed"] is False
    thin = _formed_book()
    thin[2] = _row("20°C", 2, 0.40, 0.44, shares=2.0)
    assert P.formation(thin, CFG)["reason"] == "one_sided_bucket", "an ask under five shares is not a quote"


def test_a_tie_on_the_mode_takes_the_lower_bucket():
    book = [_row("18°C", 0, 0.30, 0.34), _row("19°C", 1, 0.30, 0.34), _row("20°C", 2, 0.20, 0.24)]
    assert P.formation(book, CFG)["bucket"] == "18°C"


def test_any_yes_fill_with_any_no_fill_costs_at_most_the_cap():
    form = P.formation(_formed_book(), CFG)
    rungs = P.ladder(form, CFG)
    yes = [r["price"] for r in rungs if r["side"] == "YES"]
    no = [r["price"] for r in rungs if r["side"] == "NO"]
    assert yes and no
    assert max(y + n for y, n in itertools.product(yes, no)) <= 0.95 + 1e-9
    # mid 0.42, d 0.05: YES 0.37 / 0.35 / 0.32, NO 0.58 / 0.56 / 0.53.
    assert yes == [0.37, 0.35, 0.32] and no == [0.58, 0.56, 0.53]


def test_a_rung_that_would_cross_the_book_is_not_posted():
    form = {"mid": 0.42, "bid": 0.45, "ask": 0.36, "tick": 0.001}
    rungs = P.ladder(form, CFG)
    # YES must sit under the ask (0.36): 0.37 is dropped; NO must sit under 1 - bid = 0.55: 0.58/0.56 dropped.
    assert [r["price"] for r in rungs if r["side"] == "YES"] == [0.35, 0.32]
    assert [r["price"] for r in rungs if r["side"] == "NO"] == [0.53]


def _ledger_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_target_order_reconciliations (order_intent_id TEXT, filled_size REAL,"
                 " filled_notional_usdc REAL)")
    conn.execute("CREATE TABLE basket_target_order_intents (order_intent_id TEXT, state TEXT, limit_price REAL,"
                 " requested_size REAL)")
    return conn


def test_the_bucket_ledger_counts_pairs_cost_and_the_unmatched_side():
    conn = _ledger_db()
    for iid, size, notional, state in (("y0", 5, 1.85, "filled"), ("y1", 5, 1.75, "filled"),
                                       ("n0", 5, 2.90, "filled"), ("n1", 0, 0, "submitted")):
        conn.execute("INSERT INTO basket_target_order_reconciliations VALUES (?,?,?)", (iid, size, notional))
        conn.execute("INSERT INTO basket_target_order_intents VALUES (?,?,?,?)", (iid, state, 0.5, 5))
    plan = {"rungs": [{"side": "YES", "intent_id": "y0"}, {"side": "YES", "intent_id": "y1"},
                      {"side": "NO", "intent_id": "n0"}, {"side": "NO", "intent_id": "n1"}]}
    book = P.ledger(conn, plan)
    assert book["YES"]["shares"] == 10 and book["NO"]["shares"] == 5
    assert book["matched_shares"] == 5 and book["unmatched_side"] == "YES" and book["unmatched_shares"] == 5
    assert book["pair_cost"] == pytest.approx(0.36 + 0.58)
    assert book["NO"]["resting"] is True and book["YES"]["resting"] is False
    assert book["unmatched_ratio"] == pytest.approx(5 / 15, abs=1e-4)


def test_a_pass_posts_the_formed_mode_once_inside_the_budget(monkeypatch):
    conn = _ledger_db()
    conn.execute("CREATE TABLE basket_target_market_registry (station_id TEXT, event_date TEXT,"
                 " settlement_unit TEXT, settlement_timezone TEXT)")
    conn.execute("INSERT INTO basket_target_market_registry VALUES ('EGLC','2026-10-15','C','Europe/London')")
    conn.execute("INSERT INTO basket_target_market_registry VALUES ('KLGA','2026-10-15','F','America/New_York')")
    sent = []

    def fake_send(conn_, **kw):
        sent.append(kw)
        conn_.execute("INSERT INTO basket_target_order_intents VALUES (?,?,?,?)",
                      (kw["intent_id"], "submitted", kw["price"], kw["shares"]))
        return {"state": "submitted"}

    monkeypatch.setattr(P, "_book", lambda *a, **k: ({"buckets": _formed_book(), "snapshot_id": "b1",
                                                       "book_source": "ws"}, None))
    monkeypatch.setattr(P, "_window_refusal", lambda *a, **k: (None, 60.0))
    monkeypatch.setattr(P, "market_ids", lambda *a: {"yes_token": "Y", "no_token": "N", "min_size": 5.0})
    monkeypatch.setattr(P, "_free_cash", lambda *a, **k: (100.0, None))
    monkeypatch.setattr(P, "send_lane_buy", fake_send)
    monkeypatch.setattr(P, "_ws_manifest_root", lambda path: None)
    cfg = {**CFG, "enabled": True}
    out = P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg)
    assert out["planned"] == 1, "the F station is outside the lane's units"
    assert {k["side"] for k in sent} == {"YES", "NO"}
    assert all(k["intent_body"]["own_horizon"] is True and k["intent_body"]["post_only"] is True for k in sent)
    assert all(k["bucket"] == "20°C" for k in sent)
    assert {k["token_id"] for k in sent if k["side"] == "NO"} == {"N"}
    # Budget: max_cash_fraction 0.2 x 100 = 20 U, station-day cap 15 U.
    assert sum(k["price"] * k["shares"] for k in sent) <= 15.0 + 1e-9
    row = conn.execute("SELECT plan_state, bucket FROM pair_maker_plans").fetchone()
    assert row == ("active", "20°C")
    # A second pass plans nothing new for the same station-day.
    sent.clear()
    again = P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg)
    assert again["planned"] == 0 and sent == [] and again["refreshed"] == 1


def test_an_unformed_book_waits(monkeypatch):
    conn = _ledger_db()
    conn.execute("CREATE TABLE basket_target_market_registry (station_id TEXT, event_date TEXT,"
                 " settlement_unit TEXT, settlement_timezone TEXT)")
    conn.execute("INSERT INTO basket_target_market_registry VALUES ('EGLC','2026-10-15','C','Europe/London')")
    listing = [_row(f"{t}°C", i, 0.16, 0.46) for i, t in enumerate(range(18, 25))]
    monkeypatch.setattr(P, "_book", lambda *a, **k: ({"buckets": listing}, None))
    monkeypatch.setattr(P, "_window_refusal", lambda *a, **k: (None, 60.0))
    monkeypatch.setattr(P, "_free_cash", lambda *a, **k: (100.0, None))
    monkeypatch.setattr(P, "_ws_manifest_root", lambda path: None)
    monkeypatch.setattr(P, "send_lane_buy", lambda *a, **k: pytest.fail("nothing is posted on an unformed book"))
    out = P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings={**CFG})
    assert out["planned"] == 0 and any(k.startswith("not_formed") for k in out["waiting"])


def test_the_resting_pass_leaves_an_own_horizon_rung_to_its_window():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_target_order_intents (order_intent_id TEXT, cycle_id TEXT, bucket TEXT, token_id TEXT,"
        " limit_price REAL, requested_size REAL, idempotency_key TEXT, created_at TEXT, intent_json TEXT,"
        " order_action TEXT, state TEXT, side TEXT);"
        "CREATE TABLE basket_target_execution_cycles (cycle_id TEXT, station_id TEXT, event_date TEXT);"
        "CREATE TABLE basket_target_order_reconciliations (order_intent_id TEXT, exchange_order_id TEXT,"
        " filled_size REAL, filled_notional_usdc REAL, state TEXT);"
        "CREATE TABLE basket_target_order_events (order_intent_id TEXT, event_type TEXT, observed_at TEXT);"
    )
    for iid, body in (("pairmk_a", {"post_only": True, "order_type": "GTD", "own_horizon": True}),
                      ("proper_b", {"post_only": True, "order_type": "GTD"})):
        conn.execute("INSERT INTO basket_target_order_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (iid, "c1", "20°C", "T", 0.37, 5, iid, NOW, json.dumps(body), "BUY", "submitted", "YES"))
        conn.execute("INSERT INTO basket_target_order_reconciliations VALUES (?,?,?,?,?)", (iid, "x" + iid, 0, 0, "pending"))
    conn.execute("INSERT INTO basket_target_execution_cycles VALUES ('c1','EGLC','2026-10-15')")
    rungs = {r["order_intent_id"]: r["ladder_rung"] for r in R.load_resting_maker_intents(conn)}
    assert rungs == {"pairmk_a": False, "proper_b": True}


def test_a_rung_one_tick_inside_the_book_is_kept():
    # EFHK 2026-10-14 10°C (live, 10-12): bid 0.53, ask 0.54, tick 0.01 -> NO limit 0.46 exactly.
    form = {"mid": 0.535, "bid": 0.53, "ask": 0.54, "tick": 0.01}
    no = [r["price"] for r in P.ladder(form, CFG) if r["side"] == "NO"]
    assert no[0] == 0.46
