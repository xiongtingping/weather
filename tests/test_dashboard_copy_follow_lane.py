"""跟单线 board (charters 256/257): the collector reads the lane's own rows and
nothing else (user ruling 2026-10-09 「前端增加对应的跟单线数据统计看板」)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import copy_follow_lane as lane  # noqa: E402
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402

NOW = "2026-10-08T16:30:00Z"


def _source(conn, key, *, user, refusal, ts=1791476800, bucket="30°C", price=0.49, our_p=0.447, cap=0.397):
    conn.execute(
        f"INSERT INTO {lane.SOURCE_TABLE}(source_key, source_user, source_ts, source_at_utc, transaction_hash, token_id,"
        " condition_id, order_action, outcome_side, size, price, usdc_size, event_slug, title, station_id, event_date,"
        " bucket, settlement_timezone, ratio, our_equity_usdc, source_value_usdc, scaled_shares, refusal, ingested_at_utc,"
        " taker, model_stage, our_p, price_cap, model_margin)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, user, ts, NOW, "0x" + key, f"yes_{bucket}", "0xc", "BUY", "YES", 5.0, price, 5.0 * price,
         "highest-temperature-in-lucknow-on-october-9-2026", "t", "VILK", "2026-10-09", bucket, "Asia/Kolkata",
         None, None, None, None if refusal else round(5.0 / cap, 6), refusal, NOW, 1, "P0-M", our_p, cap, our_p - price),
    )


def test_the_collector_reads_the_lanes_rows_and_names_refusals(tmp_path, monkeypatch) -> None:
    conn = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    apply_production_ssot_schema(conn)
    monkeypatch.setattr(C, "_load_strategy_lanes", lambda: {"lanes": {"copy_follow": {
        "enabled": True, "rule": lane.RULE_V2, "stake_usdc": 5.0, "max_daily_cost_usdc": 40.0,
        "model_margin": 0.05, "lead_hours": [12.0, 48.0], "station_units": ["C"], "model_stages": ["P0", "P0-M"],
        "source_users": ["0xa", "0xb"]}}})
    # No tables yet: the page still renders the switch, with the readings marked unavailable.
    out = C.collect_copy_follow_lane(conn)
    assert out["available"] is True and out["settings"]["enabled"] is True and out["source_wallets"] == 2
    assert out["sources"]["available"] is False and out["trades"]["available"] is False
    lane.ensure_lane_schema(conn)
    _source(conn, "k1", user="0xa", refusal="model_margin_short:0.447<0.540")
    _source(conn, "k2", user="0xb", refusal="model_margin_short:0.447<0.550", ts=1791476801)
    _source(conn, "k3", user="0xa", refusal="station_unit_not_followed:F", ts=1791476802, bucket="78-79°F")
    _source(conn, "k4", user="0xb", refusal=None, ts=1791476803, bucket="29°C", price=0.30, our_p=0.45, cap=0.36)
    monkeypatch.setattr(C, "_render_live_book", lambda conn: {"settled": {}})
    out = C.collect_copy_follow_lane(conn)
    S = out["sources"]
    assert (S["total"], S["admitted"], S["wallets"]) == (4, 1, 2)
    assert {r["reason"]: r["count"] for r in S["refusals"]} == {"model_margin_short": 2, "station_unit_not_followed": 1, "admitted": 1}
    assert {w["source_user"]: (w["seen"], w["admitted"]) for w in S["by_wallet"]} == {"0xa": (2, 0), "0xb": (2, 1)}
    assert S["recent"][0]["bucket"] == "29°C" and S["recent"][0]["refusal"] is None and S["recent"][0]["our_p"] == 0.45
    E = out["evaluations"]
    assert E["available"] is True and E["total"] == 0
    assert out["trades"]["available"] is True and out["trades"]["filled"] == 0


def test_the_board_lists_the_lane_and_not_the_retired_p1_tail() -> None:
    src = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "function copyFollowLane()" in src and '"copy_follow_lane"' in src
    assert 'copy_follow:"跟单线"' in src and '["copy_follow","跟单线"]' in src
    # The P1 tail board and its money-grid column are gone; the lane's name stays in the charter 259
    # reliability diagrams' label map (legs it filled before it was switched off are still drawn).
    assert "def collect_p1_tail_lane" not in src and '["p1_tail","P1 尾段"]' not in src
