"""A tiebreak sale belongs in its realised cash flow, even if the bucket loses."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import calibration_dashboard as dashboard  # noqa: E402
import live_book  # noqa: E402
from bayes_basket_target.tiebreak_lane import RULE_V21, RULE_V23, rule_version_for  # noqa: E402


def _leg(station: str, day: str, intent: str, *, decision: str | None = None,
         bucket: str = "20°C", shares: float = 10.0, cost: float = 5.0) -> dict:
    return {
        "station_id": station, "event_date": day, "bucket": bucket,
        "order_intent_id": intent, "decision_id": decision or f"tiebreak_decision_{station}_{day}",
        "side": "YES", "stage": "P0-M", "filled_size": shares,
        "filled_notional_usdc": cost, "state": "filled", "limit_price": cost / shares,
    }


def _sell(station: str, day: str, *, bucket: str = "20°C",
          shares: float = 10.0, proceeds: float = 9.9, side: str = "YES") -> dict:
    return {
        "station_id": station, "event_date": day, "bucket": bucket,
        "sold_size": shares, "proceeds_usdc": proceeds, "side": side,
    }


def test_sells_are_allocated_to_intents_without_claiming_another_lane() -> None:
    legs = [
        _leg("KATL", "2026-09-01", "tie"),
        _leg("KATL", "2026-09-01", "basket", decision="basket_decision_a"),
    ]
    no_leg = _leg("KATL", "2026-09-01", "proper_no")
    no_leg["side"] = "NO"
    legs.append(no_leg)
    sells = [_sell("KATL", "2026-09-01")]
    assert live_book.allocate_sells_by_intent(legs, sells) == {
        "tie": pytest.approx((5.0, 4.95)),
        "basket": pytest.approx((5.0, 4.95)),
    }
    # External inventory and a NO sale cannot turn into a YES-lane receipt.
    sells = [_sell("KATL", "2026-09-01", shares=25.0, proceeds=24.75),
             _sell("KATL", "2026-09-01", shares=10.0, proceeds=9.9, side="NO")]
    assert live_book.allocate_sells_by_intent(legs, sells) == {
        "tie": pytest.approx((10.0, 9.9)),
        "basket": pytest.approx((10.0, 9.9)),
    }


def test_tiebreak_collector_uses_sales_and_current_rule_for_acceptance(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE auto_trade_decisions(decision_id TEXT, details_json TEXT)")
    conn.execute(
        "CREATE TABLE tiebreak_lane_evaluation("
        "station_id TEXT,event_date TEXT,stage TEXT,rule_version TEXT,fired INTEGER,"
        "refusal TEXT,order_intent_id TEXT,evaluated_at_utc TEXT,model_mode TEXT,"
        "market_mode TEXT,market_share_on_model_mode REAL,model_mode_share REAL)"
    )
    cases = [
        ("WIN", "2026-09-01", RULE_V23, "P0-M", "20°C"),
        ("LOSE", "2026-09-02", RULE_V23, "P0-M", "21°C"),
        ("PART", "2026-09-03", RULE_V23, "P0-M", "20°C"),
        ("OLD", "2026-09-04", RULE_V21, "P0-M", "21°C"),
        ("P0HIST", "2026-09-05", RULE_V23, "P0", "20°C"),
    ]
    legs = []
    settled = {}
    for station, day, rule, stage, landed in cases:
        decision = f"tiebreak_decision_{station}_{day}"
        conn.execute("INSERT INTO auto_trade_decisions VALUES (?,?)", (
            decision, json.dumps({"decision_signature": {"rule": rule_version_for(rule)}}),
        ))
        leg = _leg(station, day, f"intent_{station}")
        leg["stage"] = stage
        legs.append(leg)
        settled[(station, day)] = (landed, "production_label")
    # The evaluation row is allowed to point at a later repost: the decision
    # signature still labels the filled intent's rule version.
    conn.execute(
        "INSERT INTO tiebreak_lane_evaluation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("WIN", "2026-09-01", "P0-M", rule_version_for(RULE_V23), 1,
         None, "later_unfilled_repost", "2026-09-01T10:00:00Z", "20°C",
         "20°C", 0.5, 0.5),
    )
    sells = [
        _sell("WIN", "2026-09-01"),
        _sell("LOSE", "2026-09-02"),
        _sell("PART", "2026-09-03", shares=4.0, proceeds=3.2),
    ]
    book = {
        "legs": legs, "settled": settled, "exit_sells": sells,
        "sold_by_intent": live_book.allocate_sells_by_intent(legs, sells),
    }
    monkeypatch.setattr(dashboard, "_render_live_book", lambda _conn: book)
    monkeypatch.setattr(dashboard, "_load_strategy_lanes", lambda: {
        "lanes": {"tiebreak": {"enabled": True, "rule": RULE_V23,
                              "opening_stages": ["P0-M", "P1"]}},
    })
    monkeypatch.setattr(dashboard, "_tiebreak_registered_hypotheses", lambda _conn: {})

    out = dashboard.collect_tiebreak_lane(conn)
    trades = {t["station_id"]: t for t in out["trades"]["rows"]}
    assert trades["WIN"]["pnl_usdc"] == pytest.approx(4.9)
    assert trades["LOSE"]["hit"] is False
    assert trades["LOSE"]["pnl_usdc"] == pytest.approx(4.9)
    assert trades["PART"]["sold_size"] == pytest.approx(4.0)
    assert trades["PART"]["payout_usdc"] == pytest.approx(9.2)
    assert trades["PART"]["pnl_usdc"] == pytest.approx(4.2)
    assert out["trades"]["pnl_usdc"] == pytest.approx(14.0)
    assert out["trades"]["acceptance"]["rule_version"] == rule_version_for(RULE_V23)
    assert out["trades"]["acceptance"]["settled"] == 3
    assert out["trades"]["acceptance"]["pnl_usdc"] == pytest.approx(14.0)
    assert {row["rule_version"]: row["settled"] for row in out["by_rule_version"]} == {
        rule_version_for(RULE_V21): 1, rule_version_for(RULE_V23): 4,
    }


def test_all_filled_orders_survive_newer_unfilled_reposts_and_repeat_exactly(monkeypatch):
    """A 60-intent slice hid older fills; repeated reads must reconcile to money."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE auto_trade_decisions(decision_id TEXT, details_json TEXT)")
    legs, settled = [], {}
    for n in range(75):
        station, day = f"S{n:03}", f"2026-09-{1+n%10:02}"
        decision = f"tiebreak_decision_{station}_{day}"
        version = rule_version_for(RULE_V21 if n<20 else RULE_V23)
        conn.execute("INSERT INTO auto_trade_decisions VALUES (?,?)", (
            decision, json.dumps({"decision_signature": {"rule": version}})))
        leg = _leg(station, day, f"fill_{n}")
        if n%2: leg['state']='cancelled'  # Actual fills remain visible.
        legs.append(leg)
        if n<70: settled[(station,day)]=(leg['bucket'],'production_label')
        repost = dict(leg,order_intent_id=f"repost_{n}",filled_size=0,
                      filled_notional_usdc=0,state='submitted',event_date='2026-10-01')
        legs.append(repost)
    monkeypatch.setattr(dashboard,'_render_live_book',lambda c: {
        'legs':legs,'settled':settled,'sold_by_intent':{}})
    monkeypatch.setattr(dashboard,'_load_strategy_lanes',lambda: {
        'lanes':{'tiebreak':{'rule':RULE_V23,'opening_stages':['P0-M','P1']}}})
    monkeypatch.setattr(dashboard,'_tiebreak_registered_hypotheses',lambda c: {})
    first=dashboard.collect_tiebreak_lane(conn)['trades']
    second=dashboard.collect_tiebreak_lane(conn)['trades']
    assert first==second
    assert len(first['rows'])==first['filled']==75
    assert first['open']==5
    assert all(r['filled_size']>0 and r['rule_version'] for r in first['rows'])
    assert {r['rule_version'] for r in first['rows']}=={
        rule_version_for(RULE_V21),rule_version_for(RULE_V23)}
    assert sum(r['pnl_usdc'] for r in first['rows'] if r['pnl_usdc'] is not None)==pytest.approx(first['pnl_usdc'])
    assert first['roi']==pytest.approx(first['pnl_usdc']/first['stake_usdc'])
