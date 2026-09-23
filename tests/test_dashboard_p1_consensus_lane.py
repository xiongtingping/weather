"""The P1 consensus lane's page (charter 203; user ruling 2026-09-23
「前端增加新上的策略线的显式」).

The collector reads the lane's own evaluation table and the live book's legs
under its own decision rows; the page draws the dials, the four-centre base
rate, the overlap with the tiebreak lane, the fired decisions and the trades.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import p1_consensus_lane as lane  # noqa: E402
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402

SOURCE = C.HTML_TEMPLATE


EVIDENCE_HEAD = "// Dated readings (config/ui/board_evidence_v1.json"
EVIDENCE_TAIL = "const evCi = "


def _evidence_helpers() -> str:
    """The page's evidence helpers (EV/withEv/evStamp/...): a section that
    prints a dated reading calls them, and a section rendered alone in node
    needs them defined the way the page defines them."""
    start = SOURCE.index(EVIDENCE_HEAD)
    tail = SOURCE.index(EVIDENCE_TAIL, start)
    return SOURCE[start:SOURCE.index("\n", tail) + 1]


def _function(name: str) -> str:
    begin = SOURCE.index(f"function {name}(")
    depth = 0
    for index in range(SOURCE.index("{", begin), len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
        elif SOURCE[index] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[begin:index + 1]
    raise AssertionError(f"function {name} is unbalanced")


def _row(conn: sqlite3.Connection, fact: str, *, four: int, fired: int, refusal, intent,
         held=None, bucket="27°C", day="2026-09-23") -> None:
    conn.execute(
        f"INSERT INTO {lane.EVALUATION_TABLE}(evaluation_id,fact_id,station_id,event_date,"
        "settlement_timezone,decision_at_utc,envelope_id,rule_version,remaining_hours,"
        "model_mode,model_mean,market_mode,market_mean,four_same,market_share,model_share,"
        "best_ask,best_bid,held_buckets,station_day_buys,fired,refusal,order_intent_id,"
        "evaluated_at_utc,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"e_{fact}", fact, "ZSPD", day, "Asia/Shanghai", f"{day}T08:00:00.000Z", f"env_{fact}",
         lane.RULE_VERSION, 8.0, bucket, bucket, bucket if four else "28°C", bucket, four,
         0.61, 0.58, 0.62, 0.60, held, 0, fired, refusal, intent,
         f"{day}T08:01:00.000Z", "c" * 64),
    )


def test_the_collector_reads_the_lanes_own_rows(tmp_path, monkeypatch) -> None:
    conn = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    apply_production_ssot_schema(conn)
    lane.ensure_lane_schema(conn)
    _row(conn, "a", four=0, fired=0, refusal="rule_not_met:centres_split", intent=None)
    _row(conn, "b", four=1, fired=0, refusal="rule_not_met:ask_outside_band:0.720", intent=None)
    _row(conn, "c", four=1, fired=1, refusal=None, intent="p1cons_1", held="27°C")
    _row(conn, "d", four=1, fired=1, refusal=None, intent="p1cons_2", held="26°C")
    _row(conn, "e", four=1, fired=1, refusal="send_book_stale:80s", intent=None)
    monkeypatch.setattr(C.live_book, "build", lambda conn: {"legs": [], "settled": {}})
    out = C.collect_p1_consensus_lane(conn)
    E = out["evaluations"]
    assert out["available"] is True and E["available"] is True
    assert (E["total"], E["four_same"], E["fired"], E["sent"]) == (5, 4, 3, 2)
    # One send bought the bucket the account already held, one a different one.
    assert (E["overlap_tiebreak_same_bucket"], E["overlap_tiebreak_other_bucket"]) == (1, 1)
    assert len(E["fired_rows"]) == 3
    assert out["settings"]["stake_usdc"] == 5.0
    assert out["trades"]["available"] is True and out["trades"]["count"] == 0


def test_the_collector_counts_only_its_own_legs(tmp_path, monkeypatch) -> None:
    conn = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    apply_production_ssot_schema(conn)
    legs = [
        {"decision_id": "p1_consensus_decision_ZSPD_2026-09-22", "station_id": "ZSPD",
         "event_date": "2026-09-22", "bucket": "27°C", "filled_size": 8.0,
         "filled_notional_usdc": 4.96, "limit_price": 0.62, "state": "filled",
         "created_at": "2026-09-22T08:00:00Z", "order_intent_id": "p1cons_x"},
        {"decision_id": "tiebreak_decision_ZSPD_2026-09-22", "station_id": "ZSPD",
         "event_date": "2026-09-22", "bucket": "27°C", "filled_size": 10.0,
         "filled_notional_usdc": 5.0, "limit_price": 0.50, "state": "filled",
         "created_at": "2026-09-22T00:00:00Z", "order_intent_id": "tb_x"},
    ]
    monkeypatch.setattr(C.live_book, "build", lambda conn: {
        "legs": legs, "settled": {("ZSPD", "2026-09-22"): ("27°C",)},
    })
    R = C.collect_p1_consensus_lane(conn)["trades"]
    assert (R["count"], R["filled"], R["settled"], R["hits"]) == (1, 1, 1, 1)
    assert R["pnl_usdc"] == pytest.approx(8.0 - 4.96)
    assert R["avg_ask"] == pytest.approx(0.62)


def test_the_page_draws_the_lane() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the renderer")
    payload = {"p1_consensus_lane": {
        "available": True,
        "settings": {"enabled": True, "stake_usdc": 5.0, "rule": lane.RULE_V1,
                     "min_remaining_hours": 6.0, "max_remaining_hours": 10.0,
                     "min_ask": 0.5, "max_ask": 0.7, "max_paid_price": 0.7,
                     "fact_fresh_seconds": 1200.0, "max_buys_per_station_day": 4},
        "evaluations": {"available": True, "total": 5, "four_same": 4, "fired": 3, "sent": 2,
                        "event_days": 1, "last_at_utc": "2026-09-23T08:01:00.000Z",
                        "refusals": [{"refusal": "sent", "count": 2, "fired": 2}],
                        "fired_rows": [{"station_id": "ZSPD", "event_date": "2026-09-23",
                                        "decision_at_utc": "2026-09-23T08:00:00.000Z",
                                        "remaining_hours": 8.0, "bucket": "27°C",
                                        "best_ask": 0.62, "market_share": 0.61,
                                        "model_share": 0.58, "station_day_buys": 0,
                                        "held_buckets": "26°C", "open_order_lane": None,
                                        "refusal": None}],
                        "by_day": [{"event_date": "2026-09-23", "evaluated": 5, "four_same": 4,
                                    "fired": 3, "sent": 2}],
                        "overlap_tiebreak_same_bucket": 1, "overlap_tiebreak_other_bucket": 1,
                        "overlap_open_order": 0},
        "trades": {"available": True, "rows": [], "count": 0, "filled": 0, "open": 0,
                   "settled": 0},
    }}
    script = (
        "const D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        "const h = (s) => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;');\n"
        + _evidence_helpers() + "\n"
        + _function("p1ConsensusLane") + "\nconsole.log(p1ConsensusLane());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    html = result.stdout
    for text in ("同心线", "每笔 $5.00", "窗口 剩 6.00–10.00h", "卖一 0.50–0.70",
                 "四中心同桶", "与秤砣线重叠", "ZSPD", "27°C", "已送出", "还没有成交"):
        assert text in html, text


def test_an_absent_reading_is_said_not_drawn() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the renderer")
    script = ("const D = {};\nconst h = (s) => String(s);\n"
              + _function("p1ConsensusLane") + "\nconsole.log(p1ConsensusLane());\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "这一轮没有生成" in result.stdout
