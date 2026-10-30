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
    assert CFG["pair_cost_cap"] == 0.95 and CFG["rung_minutes"] == 15.0 and CFG["units"] == ["C", "F"]
    assert CFG["lead_side"] == "YES" and CFG["stations"] == ["LEMD"]
    board = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    assert 'pair_maker: {name:"配对挂单线"' in board


def test_formation_needs_the_mode_two_sided_and_the_mids_in_range():
    form = P.formation(_formed_book(), CFG)
    assert form["formed"] and form["bucket"] == "20°C" and form["mid"] == pytest.approx(0.42)
    # Amendment five (user 10-30 「放宽成形门」): a far bucket without a YES bid
    # no longer holds the station-day back; its ask counts half.
    one_sided = _formed_book()
    one_sided[0] = _row("18°C", 0, None, 0.06)
    form = P.formation(one_sided, CFG)
    assert form["formed"] and form["bucket"] == "20°C" and form["mid_sum"] == pytest.approx(0.96)
    # The listing ladder: asks summing near 3 -> not formed, with or without its bids.
    listing = [_row(f"{t}°C", i, 0.16, 0.46) for i, t in enumerate(range(18, 25))]
    assert P.formation(listing, CFG)["formed"] is False
    bidless = [_row(f"{t}°C", i, 0.16 if i < 2 else None, 0.46) for i, t in enumerate(range(18, 25))]
    assert P.formation(bidless, CFG)["reason"].startswith("mid_sum_outside")
    thin = _formed_book()
    thin[2] = _row("20°C", 2, 0.40, 0.44, shares=2.0)
    assert P.formation(thin, CFG) == {"formed": False, "reason": "mode_one_sided"}, \
        "an ask under five shares is not a quote, and the mode needs one"
    nothing = [_row(f"{t}°C", i, None, 0.30) for i, t in enumerate(range(18, 21))]
    assert P.formation(nothing, CFG) == {"formed": False, "reason": "no_two_sided_bucket"}


def test_a_tie_on_the_mode_takes_the_lower_bucket():
    book = [_row("18°C", 0, 0.30, 0.34), _row("19°C", 1, 0.30, 0.34), _row("20°C", 2, 0.20, 0.24)]
    assert P.formation(book, CFG)["bucket"] == "18°C"


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
    # The candidate read seeks on the station list (query-plan guard).
    conn.execute("CREATE VIEW calibration_station_registry AS SELECT DISTINCT station_id"
                 " FROM basket_target_market_registry")
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
    cfg = {**CFG, "enabled": True, "stations": []}
    out = P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg)
    assert out["planned"] == 2, "C and F stations both (user 10-13 「全可以做」)"
    # Rule v2: the first pass rests the lead (YES) at its bid + 1 only; nothing to follow yet.
    assert {k["side"] for k in sent} == {"YES"} and {k["price"] for k in sent} == {0.401}
    assert all(k["intent_body"]["own_horizon"] is True and k["intent_body"]["post_only"] is True for k in sent)
    assert all(k["bucket"] == "20°C" for k in sent)
    sent_eglc = [k for k in sent if k["station_id"] == "EGLC"]
    assert sum(k["price"] * k["shares"] for k in sent_eglc) <= 15.0 + 1e-9
    assert {k["token_id"] for k in sent} == {"Y"}
    # Budget: max_cash_fraction 0.2 x 100 = 20 U across the lane.
    assert sum(k["price"] * k["shares"] for k in sent) <= 20.0 + 1e-9
    rows = conn.execute("SELECT plan_state, bucket FROM pair_maker_plans").fetchall()
    assert set(rows) == {("active", "20°C")}
    # A second pass plans nothing new for the same station-day.
    sent.clear()
    again = P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg)
    assert again["planned"] == 0 and sent == [] and again["refreshed"] == 2, "every rung still rests: nothing to re-post"


def test_an_unformed_book_waits(monkeypatch):
    conn = _ledger_db()
    conn.execute("CREATE TABLE basket_target_market_registry (station_id TEXT, event_date TEXT,"
                 " settlement_unit TEXT, settlement_timezone TEXT)")
    # The candidate read seeks on the station list (query-plan guard).
    conn.execute("CREATE VIEW calibration_station_registry AS SELECT DISTINCT station_id"
                 " FROM basket_target_market_registry")
    conn.execute("INSERT INTO basket_target_market_registry VALUES ('EGLC','2026-10-15','C','Europe/London')")
    listing = [_row(f"{t}°C", i, 0.16, 0.46) for i, t in enumerate(range(18, 25))]
    monkeypatch.setattr(P, "_book", lambda *a, **k: ({"buckets": listing}, None))
    monkeypatch.setattr(P, "_window_refusal", lambda *a, **k: (None, 60.0))
    monkeypatch.setattr(P, "_free_cash", lambda *a, **k: (100.0, None))
    monkeypatch.setattr(P, "_ws_manifest_root", lambda path: None)
    monkeypatch.setattr(P, "send_lane_buy", lambda *a, **k: pytest.fail("nothing is posted on an unformed book"))
    out = P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings={**CFG, "stations": []})
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


def _plan_with(rungs):
    return {"rungs": [{"side": s, "k": k, "price": p, "intent_id": i} for s, k, p, i in rungs]}


QUOTE = {"bid": 0.40, "ask": 0.44, "mid": 0.42, "tick": 0.001}




def _plan_with(rungs):
    return {"rungs": [{"side": s, "k": k, "price": p, "intent_id": i} for s, k, p, i in rungs]}


QUOTE = {"bid": 0.44, "ask": 0.49, "mid": 0.465, "tick": 0.001}


def test_the_lead_rests_at_its_bid_plus_one_and_nothing_follows_before_a_fill():
    rungs, status = P.lead_follow_plan(_plan_with([]), {}, QUOTE, CFG, min_size=5.0)
    assert rungs == [{"side": "YES", "k": 0, "price": 0.441, "shares": 5.0}]
    assert "follow" not in status


def test_after_a_lead_fill_the_follow_is_priced_under_the_leads_cost():
    # 5 YES filled at 0.441 -> NO at min(NO bid + 1 = 0.511, 0.95 - 0.441 = 0.509) = 0.509.
    plan = _plan_with([("YES", 0, 0.441, "a")])
    states = {"a": {"filled": 5.0, "notional": 2.205, "open": False}}
    rungs, _ = P.lead_follow_plan(plan, states, QUOTE, CFG, min_size=5.0)
    follow = next(r for r in rungs if r["side"] == "NO")
    assert follow["price"] == 0.509 and follow["shares"] == 5.0
    lead = next(r for r in rungs if r["side"] == "YES")
    assert lead["price"] == 0.441, "the lead keeps accumulating under its unmatched cap"


def test_on_a_wide_book_the_follow_takes_its_own_bid_plus_one():
    plan = _plan_with([("YES", 0, 0.30, "a")])
    states = {"a": {"filled": 5.0, "notional": 1.50, "open": False}}
    wide = {"bid": 0.30, "ask": 0.42, "mid": 0.36, "tick": 0.001}
    rungs, _ = P.lead_follow_plan(plan, states, wide, CFG, min_size=5.0)
    # NO bid = 1 - 0.42 = 0.58 -> 0.581, under the cap 0.95 - 0.30 = 0.65.
    assert next(r for r in rungs if r["side"] == "NO")["price"] == 0.581


def test_the_lead_stops_at_its_unmatched_cost_cap():
    plan = _plan_with([("YES", 0, 0.44, "a"), ("YES", 0, 0.44, "b"), ("YES", 0, 0.44, "c")])
    states = {i: {"filled": 5.0, "notional": 2.2, "open": False} for i in "abc"}
    rungs, status = P.lead_follow_plan(plan, states, QUOTE, CFG, min_size=5.0)
    assert not any(r["side"] == "YES" for r in rungs) and status["lead"].startswith("YES:unmatched_cap")
    assert next(r for r in rungs if r["side"] == "NO")["shares"] == 15.0


def test_a_lead_fill_beside_a_resting_follow_never_prices_a_pair_over_the_cap():
    # NO rests at 0.52: the lead may not bid over 0.95 - 0.52 = 0.43, even though its bid + 1 is 0.441.
    plan = _plan_with([("NO", 1, 0.52, "n")])
    states = {"n": {"filled": 0.0, "notional": 0.0, "open": True}}
    rungs, _ = P.lead_follow_plan(plan, states, QUOTE, CFG, min_size=5.0)
    assert next(r for r in rungs if r["side"] == "YES")["price"] == 0.43


def test_the_mvp_runs_on_its_one_station(monkeypatch):
    conn = _ledger_db()
    conn.execute("CREATE TABLE basket_target_market_registry (station_id TEXT, event_date TEXT,"
                 " settlement_unit TEXT, settlement_timezone TEXT)")
    # The candidate read seeks on the station list (query-plan guard).
    conn.execute("CREATE VIEW calibration_station_registry AS SELECT DISTINCT station_id"
                 " FROM basket_target_market_registry")
    P.ensure_lane_schema(conn)
    for st in ("LEMD", "EGLC"):
        conn.execute("INSERT INTO basket_target_market_registry VALUES (?, '2026-10-15', 'C', 'Europe/Madrid')", (st,))
    assert [c["station_id"] for c in P._candidates(conn, now_utc=NOW, cfg=CFG)] == ["LEMD"]


# Amendment three (user 10-13 「做 merge」): matched pairs back to pUSD.

def _merge_db(yes_filled=10.0, no_filled=10.0):
    conn = _ledger_db()
    P.ensure_lane_schema(conn)
    for iid, size, notional in (("y0", yes_filled, 0.40 * yes_filled), ("n0", no_filled, 0.54 * no_filled)):
        conn.execute("INSERT INTO basket_target_order_reconciliations VALUES (?,?,?)", (iid, size, notional))
        conn.execute("INSERT INTO basket_target_order_intents VALUES (?,?,?,?)", (iid, "filled", 0.5, size))
    plan = {"plan_id": "pairmk_plan_t", "station_id": "LEMD", "event_date": "2026-10-15", "bucket": "28°C",
            "settlement_timezone": "Europe/Madrid", "window_ends_utc": "2026-10-13T05:00:00Z",
            "tokens": {"YES": "Y", "NO": "N"}, "condition_id": "0x" + "ab" * 32,
            "rungs": [{"side": "YES", "k": 0, "intent_id": "y0"}, {"side": "NO", "k": 0, "intent_id": "n0"}]}
    conn.execute(f"INSERT INTO {P.PLAN_TABLE} VALUES (?,?,?,?,?,?,?,?,?)",
                 ("LEMD", "2026-10-15", "28°C", "active", "2026-10-12T17:00:00Z", plan["window_ends_utc"],
                  json.dumps(plan), None, None))
    return conn


def _merge_env(monkeypatch):
    monkeypatch.setattr(P, "_free_cash", lambda *a, **k: (100.0, None))
    monkeypatch.setattr(P, "_ws_manifest_root", lambda path: None)
    monkeypatch.setattr(P, "_candidates", lambda *a, **k: [])


def test_simulate_records_the_merge_once_and_counts_nothing(monkeypatch):
    _merge_env(monkeypatch)
    conn = _merge_db()
    calls = []

    def gw(path, payload):
        calls.append((path, payload))
        return {"ok": True, "simulated": True, "sent": False, "reason": "simulate_only", "gas_cost_pol": 0.15}

    cfg = {**CFG, "enabled": True, "stations": [], "merge": "simulate"}
    out = P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg, merge_transport=gw)
    assert [c[0] for c in calls] == ["/merge-positions"]
    assert calls[0][1]["shares"] == 10.0 and calls[0][1]["execute"] is False and calls[0][1]["neg_risk"] is True
    assert out["merges"][0]["state"] == "SIMULATED"
    P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg, merge_transport=gw)
    assert len(calls) == 1, "the same simulation is not repeated every pass"
    assert P.merged_shares_for_token(conn, "Y") == 0.0


def test_live_merge_is_sent_then_booked_from_its_receipt(monkeypatch):
    _merge_env(monkeypatch)
    conn = _merge_db(yes_filled=12.0, no_filled=10.0)
    replies = {"/merge-positions": {"ok": True, "simulated": True, "sent": True, "tx_hash": "0x" + "cd" * 32},
               "/merge-receipt": {"ok": True, "status": "pending"}}
    gw = lambda path, payload: replies[path]  # noqa: E731
    cfg = {**CFG, "enabled": True, "stations": [], "merge": "live"}
    out = P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg, merge_transport=gw)
    assert out["merges"][0]["state"] == "SENT" and out["merges"][0]["shares"] == 10.0, "only the matched pairs"
    # Pending receipt: the plan stays open and no second merge goes out.
    out = P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg, merge_transport=gw)
    assert out["merges"] == [] and out["waiting"].get("merge_pending") == 1
    assert conn.execute(f"SELECT plan_state FROM {P.PLAN_TABLE}").fetchone()[0] == "active"
    replies["/merge-receipt"] = {"ok": True, "status": "success"}
    P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg, merge_transport=gw)
    assert P.merged_shares_for_token(conn, "Y") == 10.0 and P.merged_shares_for_token(conn, "N") == 10.0
    plan = json.loads(conn.execute(f"SELECT plan_json FROM {P.PLAN_TABLE}").fetchone()[0])
    assert plan["merged_shares"] == 10.0 and plan["merged_cost_usdc"] == pytest.approx(10 * (0.40 + 0.54))
    assert conn.execute(f"SELECT plan_state FROM {P.PLAN_TABLE}").fetchone()[0] == "done"


def test_an_unknown_broadcast_holds_every_further_merge(monkeypatch):
    _merge_env(monkeypatch)
    conn = _merge_db()
    calls = []

    def gw(path, payload):
        calls.append(path)
        return {"ok": False, "sent": None, "error": "merge_send_unknown"}

    cfg = {**CFG, "enabled": True, "stations": [], "merge": "live"}
    P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg, merge_transport=gw)
    out = P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True, settings=cfg, merge_transport=gw)
    assert calls == ["/merge-positions"] and out["waiting"].get("merge_pending") == 1


def test_fewer_pairs_than_the_merge_minimum_wait(monkeypatch):
    _merge_env(monkeypatch)
    conn = _merge_db(yes_filled=5.0, no_filled=5.0)
    gw = lambda path, payload: pytest.fail("no merge under min_merge_shares")  # noqa: E731
    P.execute_pair_maker(conn, transport=None, now_utc=NOW, live_submit_authorized=True,
                         settings={**CFG, "enabled": True, "stations": [], "merge": "live"}, merge_transport=gw)


def test_the_departed_order_reconciliation_books_merged_shares_off_the_token():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_target_order_intents (order_intent_id TEXT, token_id TEXT, order_action TEXT)")
    conn.execute("CREATE TABLE basket_target_order_reconciliations (order_intent_id TEXT, filled_size REAL)")
    conn.execute("INSERT INTO basket_target_order_intents VALUES ('y0','Y','BUY')")
    conn.execute("INSERT INTO basket_target_order_reconciliations VALUES ('y0', 12.0)")
    assert R.recorded_fills_for_token(conn, "Y") == 12.0
    P.ensure_lane_schema(conn)
    conn.execute(f"INSERT INTO {P.MERGE_TABLE} VALUES ('m','p','LEMD','2026-10-15','28°C','0x','Y','N',10,'live','MERGED',"
                 "'0x',NULL,'t','t')")
    assert R.recorded_fills_for_token(conn, "Y") == 2.0


# Amendment four (user 10-13 「0.95 封顶。未配平控制：两个都用」).

def test_the_ratio_is_not_judged_on_a_small_book():
    # 10 YES vs 5 NO: ratio 33% but only 15 shares held -> the 6 U cost cap alone applies.
    plan = _plan_with([("YES", 0, 0.30, "a"), ("NO", 1, 0.60, "b")])
    states = {"a": {"filled": 10.0, "notional": 3.0, "open": False}, "b": {"filled": 5.0, "notional": 3.0, "open": False}}
    rungs, status = P.lead_follow_plan(plan, states, QUOTE, CFG, min_size=5.0)
    assert any(r["side"] == "YES" for r in rungs) and "ratio_warn" not in status


def test_the_lead_stops_at_a_ten_percent_unmatched_ratio_and_the_follow_keeps_pairing():
    # 30 YES vs 24 NO: ratio 6/54 = 11.1% above 20 shares -> no new lead, NO completes the 6.
    plan = _plan_with([("YES", 0, 0.30, "a"), ("NO", 1, 0.60, "b")])
    states = {"a": {"filled": 30.0, "notional": 9.0, "open": False}, "b": {"filled": 24.0, "notional": 14.4, "open": False}}
    cfg = {**CFG, "max_unmatched_usdc": 100.0}
    rungs, status = P.lead_follow_plan(plan, states, QUOTE, cfg, min_size=5.0)
    assert not any(r["side"] == "YES" for r in rungs) and status["lead"].startswith("YES:unmatched_ratio")
    assert next(r for r in rungs if r["side"] == "NO")["shares"] == 6.0
    assert status["ratio_warn"] is True


def test_under_ten_percent_the_lead_accumulates_with_a_warning_from_eight():
    # 52 YES vs 48 NO: 4%; 53.5 vs 46.5: 7% -> no warn; 54.5 vs 45.5: 9% -> warn, lead still posts.
    plan = _plan_with([("YES", 0, 0.30, "a"), ("NO", 1, 0.60, "b")])
    cfg = {**CFG, "max_unmatched_usdc": 100.0}
    for y, n, warn in ((52.0, 48.0, False), (53.5, 46.5, False), (54.5, 45.5, True)):
        states = {"a": {"filled": y, "notional": 0.3 * y, "open": False}, "b": {"filled": n, "notional": 0.6 * n, "open": False}}
        rungs, status = P.lead_follow_plan(plan, states, QUOTE, cfg, min_size=5.0)
        assert any(r["side"] == "YES" for r in rungs)
        assert status.get("ratio_warn", False) is warn


def test_a_partial_lead_fill_under_the_minimum_is_still_followed():
    # LEMD 10-14 live: YES filled 4.3478 for 2.347827 (avg 0.540003) -> NO for the gap under
    # 0.95 - 0.540003 = 0.409997, floored to 0.40 (lifted to 5 shares on send).
    plan = _plan_with([("YES", 0, 0.54, "a")])
    states = {"a": {"filled": 4.3478, "notional": 2.347827, "open": False}}
    book = {"bid": 0.51, "ask": 0.54, "mid": 0.525, "tick": 0.01}
    rungs, status = P.lead_follow_plan(plan, states, book, {**CFG, "max_unmatched_usdc": 2.0}, min_size=5.0)
    no = next(r for r in rungs if r["side"] == "NO")
    assert no["price"] == 0.40 and no["shares"] == 4.35
    assert P._shares(no["price"], {**CFG, "rung_shares": no["shares"]}, 5.0) == 5.0
    assert status["lead"].startswith("YES:unmatched_cap")


# ---------------------------------------------------------------------------
# The board (user 2026-10-30 「前端加开关和数据统计」).


def _board_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(P.PLAN_DDL)
    conn.execute(P.MERGE_DDL)
    conn.execute("CREATE TABLE basket_target_order_intents (order_intent_id TEXT, token_id TEXT,"
                 " order_action TEXT, state TEXT, live_submit_authorized INTEGER)")
    conn.execute("CREATE TABLE basket_target_order_reconciliations (order_intent_id TEXT,"
                 " filled_size REAL, filled_notional_usdc REAL)")

    def plan(day, bucket, state, yes, no):
        tokens = {"YES": f"y{day}", "NO": f"n{day}"}
        book = {"YES": {"shares": yes[0], "cost_usdc": yes[1], "resting": False},
                "NO": {"shares": no[0], "cost_usdc": no[1], "resting": state == "active"}}
        matched = min(yes[0], no[0])
        book.update(matched_shares=matched, unmatched_shares=abs(yes[0] - no[0]),
                    unmatched_side="YES" if yes[0] > no[0] else "NO" if no[0] > yes[0] else None,
                    pair_cost=(yes[1] / yes[0] + no[1] / no[0]) if yes[0] and no[0] else None)
        conn.execute(f"INSERT INTO {P.PLAN_TABLE} VALUES (?,?,?,?,?,?,?,?,?)",
                     ("LEMD", day, bucket, state, f"{day}T00:00:00Z", f"{day}T12:00:00Z",
                      json.dumps({"plan_id": f"p{day}", "tokens": tokens, "formation": {"mid_sum": 0.98},
                                  "rungs": []}), json.dumps(book), f"{day}T01:00:00Z"))
        for side, (shares, cost) in (("YES", yes), ("NO", no)):
            if shares:
                iid = f"{side}{day}"
                conn.execute("INSERT INTO basket_target_order_intents VALUES (?,?,?,?,1)",
                             (iid, tokens[side], "BUY", "resolved"))
                conn.execute("INSERT INTO basket_target_order_reconciliations VALUES (?,?,?)", (iid, shares, cost))

    plan("2026-10-15", "27°C", "done", (5.0, 2.55), (5.0, 2.20))   # a pair at 0.95, landed 26°C
    plan("2026-10-16", "27°C", "done", (5.0, 2.15), (0.0, 0.0))    # YES alone, landed 27°C
    plan("2026-10-31", "21°C", "active", (5.0, 2.25), (0.0, 0.0))  # still quoting
    return conn


def test_the_board_counts_pairs_and_settles_each_plan(monkeypatch):
    import scripts.calibration_dashboard as board

    monkeypatch.setattr(board, "_load_strategy_lanes",
                        lambda: {"lanes": {"pair_maker": {**CFG, "enabled": True}}})
    monkeypatch.setattr(board, "_live_settled_buckets", lambda conn, keys: {
        k: v for k, v in {("LEMD", "2026-10-15"): ("26°C", "label"),
                          ("LEMD", "2026-10-16"): ("27°C", "label")}.items() if k in keys})
    out = board.collect_pair_maker_lane(_board_conn())
    assert out["settings"]["enabled"] is True and out["settings"]["pair_cost_cap"] == 0.95
    T = out["plans"]
    assert (T["total"], T["active"], T["filled"], T["paired"]) == (3, 1, 3, 1)
    assert T["matched_shares"] == 5.0 and T["avg_pair_cost"] == pytest.approx(0.95)
    assert T["locked_usdc"] == pytest.approx(0.25)
    rows = {r["event_date"]: r for r in T["rows"]}
    # The pair pays 1 a pair whichever bucket lands; the lone YES rides the outcome.
    assert rows["2026-10-15"]["pnl_usdc"] == pytest.approx(5.0 - 4.75)
    assert rows["2026-10-16"]["pnl_usdc"] == pytest.approx(5.0 - 2.15)
    assert rows["2026-10-31"]["pnl_usdc"] is None and rows["2026-10-31"]["settled_bucket"] is None
    assert T["closed"] == 2 and T["pnl_usdc"] == pytest.approx(0.25 + 2.85)
    assert T["open_cost_usdc"] == pytest.approx(2.25) and T["unmatched_shares"] == 10.0


def test_the_board_draws_the_switch_and_the_page():
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    strip = source[source.index("function liveControlStrip(){"):source.index("const LC_LANES = {")]
    assert 'laneSwitch("pair_maker")' in strip
    assert '"pair_maker_lane": pair_maker_lane,' in source
    assert 'pairMakerLane(), "配对挂单线")' in source and "function pairMakerLane(){" in source
