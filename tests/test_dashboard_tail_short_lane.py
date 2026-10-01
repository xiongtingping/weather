"""H1 dashboard counts fills, not resting orders, and scores NO correctly."""

from __future__ import annotations

import sqlite3
import json
import shutil
import subprocess

import pytest

from scripts import calibration_dashboard as dashboard


def test_h1_fills_and_recycle_sale_are_scored_from_their_own_token(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE tail_short_lane_evaluation(
          station_id TEXT,event_date TEXT,bucket TEXT,yes_bid REAL,market_q REAL,
          rungs_from_mode INTEGER,book_source TEXT,book_age_seconds REAL,
          order_intent_id TEXT,sent INTEGER,evaluated_at_utc TEXT,refusal TEXT);
        CREATE TABLE basket_target_execution_cycles(
          cycle_id TEXT,station_id TEXT,event_date TEXT);
        CREATE TABLE basket_target_order_intents(
          order_intent_id TEXT,cycle_id TEXT,bucket TEXT,token_id TEXT,side TEXT,
          order_action TEXT,limit_price REAL,requested_size REAL,state TEXT,
          created_at TEXT,live_submit_authorized INTEGER);
        CREATE TABLE basket_target_order_reconciliations(
          order_intent_id TEXT,filled_size REAL,filled_notional_usdc REAL,
          exchange_order_id TEXT,state TEXT);
    """)
    for station, bucket, token, size, cost in (
        ("KBKF", "62-63°F", "no_kbkf", 6, 5.76),
        ("RKPK", "30°C", "no_rkpk", 8, 7.84),
        ("RJTT", "25°C", "no_rjtt", 10, 9.80),
    ):
        day = "2026-09-29" if station == "KBKF" else "2026-09-30"
        conn.execute("INSERT INTO basket_target_execution_cycles VALUES(?,?,?)",
                     (station, station, day))
        conn.execute("INSERT INTO tail_short_lane_evaluation VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (station, day, bucket, .04, .04, 3, "official_clob_ws", 1.2,
                      "buy_" + station, 1, "2026-09-28T08:00:00Z", None))
        conn.execute("INSERT INTO basket_target_order_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     ("buy_" + station, station, bucket, token, "NO", "BUY",
                      cost / size, size, "filled", "2026-09-28T08:00:00Z", 1))
        conn.execute("INSERT INTO basket_target_order_reconciliations VALUES(?,?,?,?,?)",
                     ("buy_" + station, size, cost, "0x" + station, "resolved_filled"))
    conn.execute("INSERT INTO basket_target_order_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 ("recycle998_no", "KBKF", "62-63°F", "no_kbkf", "NO", "SELL",
                  .99, 2, "filled", "2026-09-28T09:00:00Z", 1))
    conn.execute("INSERT INTO basket_target_order_reconciliations VALUES(?,?,?,?,?)",
                 ("recycle998_no", 2, 1.98, "0xsell", "resolved_filled"))
    monkeypatch.setattr(dashboard, "_load_strategy_lanes", lambda: {
        "lanes": {"tail_short": {"enabled": True}}
    })
    monkeypatch.setattr(dashboard, "_render_live_book", lambda _conn: {
        "settled": {("KBKF", "2026-09-29"): ("64-65°F", "AWC"),
                    ("RKPK", "2026-09-30"): ("30°C", "AWC")}
    })
    result = dashboard.collect_tail_short_lane(conn)
    trades = result["trades"]
    assert (trades["sent"], trades["filled"], trades["filled_shares"], trades["spent_usdc"]) == (
        3, 3, 24, 23.4,
    )
    assert trades["unresolved"] == 0
    assert trades["recycle_order_count"] == 1
    assert trades["recycle_reconcile_required"] == 0
    by_station = {row["station_id"]: row for row in trades["rows"]}
    assert by_station["KBKF"]["payout_usdc"] == pytest.approx(5.98)
    assert round(by_station["KBKF"]["pnl_usdc"], 2) == .22
    assert by_station["KBKF"]["sale_realised_pnl_usdc"] == pytest.approx(.06)
    assert by_station["KBKF"]["close_reason"] == "settlement"
    assert by_station["RKPK"]["pnl_usdc"] == -7.84
    assert by_station["RJTT"]["pnl_usdc"] is None
    assert by_station["KBKF"]["exchange_order_id"] == "0xKBKF"

    monkeypatch.setattr(dashboard.live_book, "settled_buckets", lambda _conn, scopes: {
        ("RJTT", "2026-09-30"): ("26°C", "production_label")
    } if ("RJTT", "2026-09-30") in scopes else {})
    settled_later = dashboard.collect_tail_short_lane(conn)["trades"]
    assert settled_later["official_settled"] == 3
    assert settled_later["closed"] == 3
    assert next(row for row in settled_later["rows"] if row["station_id"] == "RJTT")["pnl_usdc"] == pytest.approx(.20)
    conn.execute("UPDATE basket_target_order_intents SET state='reconcile_required'"
                 " WHERE order_intent_id='recycle998_no'")
    conn.execute("UPDATE basket_target_order_reconciliations SET state='pending'"
                 " WHERE order_intent_id='recycle998_no'")
    unresolved_sale = dashboard.collect_tail_short_lane(conn)["trades"]
    assert unresolved_sale["recycle_reconcile_required"] == 1
    assert unresolved_sale["recycled_shares"] == 0
    assert next(row for row in unresolved_sale["rows"] if row["station_id"] == "KBKF")["pnl_usdc"] is None

    conn.execute("UPDATE basket_target_order_intents SET state='filled'"
                 " WHERE order_intent_id='recycle998_no'")
    conn.execute("UPDATE basket_target_order_reconciliations SET state='resolved_filled'"
                 " WHERE order_intent_id='recycle998_no'")
    monkeypatch.setattr(dashboard, "_render_live_book", lambda _conn: {"settled": {
        ("RKPK", "2026-09-30"): ("30°C", "AWC")
    }})
    monkeypatch.setattr(dashboard.live_book, "settled_buckets", lambda _conn, _scopes: {})
    partial_before_truth = dashboard.collect_tail_short_lane(conn)["trades"]
    partial = next(row for row in partial_before_truth["rows"] if row["station_id"] == "KBKF")
    assert partial["pnl_usdc"] is None
    assert partial["open_cost_usdc"] == pytest.approx(3.84)
    assert partial["realised_pnl_usdc"] == pytest.approx(.06)
    assert partial_before_truth["recycle_realised_pnl_usdc"] == pytest.approx(.06)
    assert partial_before_truth["closed"] == 1

    # A full, confirmed recycle sale closes the position and realizes P&L
    # even if official settlement truth has not yet arrived.
    conn.execute("UPDATE basket_target_order_reconciliations SET state='resolved_filled',"
                 " filled_size=6,filled_notional_usdc=5.988"
                 " WHERE order_intent_id='recycle998_no'")
    sold_before_truth = dashboard.collect_tail_short_lane(conn)["trades"]
    kbkf = next(row for row in sold_before_truth["rows"] if row["station_id"] == "KBKF")
    assert kbkf["close_reason"] == "sale"
    assert kbkf["pnl_usdc"] == pytest.approx(.228)
    assert kbkf["realised_pnl_usdc"] == pytest.approx(.228)
    assert sold_before_truth["closed"] == 2
    assert sold_before_truth["official_settled"] == 1
    assert sold_before_truth["wins"] == 1
    assert sold_before_truth["win_rate"] == pytest.approx(.5)
    assert sold_before_truth["net_odds"] == pytest.approx(
        (6 + 8 - 5.76 - 7.84) / (5.76 + 7.84)
    )

    node = shutil.which("node")
    if node:
        source = dashboard.HTML_TEMPLATE
        start = source.index("function tailShortLane(){")
        end = source.index("// 「Proper 下注线」", start)
        script = (
            "const D=" + json.dumps({"tail_short_lane": result}, ensure_ascii=False) + ";\n"
            + "const h=s=>String(s==null?'':s);\n"
            + source[start:end]
            + "\nconsole.log(tailShortLane());\n"
        )
        rendered = subprocess.run([node, "-e", script], capture_output=True,
                                  text=True, timeout=10, check=True).stdout
        assert "0xKBKF" not in rendered and "0xRKPK" not in rendered
        assert "交易所订单" not in rendered
        assert "NO 股数 / 实付" in rendered
        assert "胜率 / 净赔率" in rendered
        assert "已实现盈亏" in rendered
        assert 'class="scroll"' in rendered
        assert "FOK 吃单成交后没有当前挂单" in rendered
        assert "undefined" not in rendered
