"""Historical collector checks for P1 consensus lane records."""

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

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import p1_consensus_lane as lane  # noqa: E402
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402

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
    from bayes_basket_target.production import (  # noqa: PLC0415
        GTD_SIGNING_MARGIN_SECONDS, VENUE_GTD_MINIMUM_HORIZON_SECONDS,
    )
    assert out["settings"]["resting_horizon_seconds"] == (
        VENUE_GTD_MINIMUM_HORIZON_SECONDS + GTD_SIGNING_MARGIN_SECONDS
    )
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


def _intent(conn: sqlite3.Connection, intent: str, *, rule: str, order_type: str,
            state: str, blocking: str | None = None) -> None:
    conn.execute(
        "INSERT INTO basket_target_order_intents(order_intent_id,decision_id,cycle_id,bucket,"
        "token_id,side,order_action,limit_price,requested_size,requested_notional_usdc,state,"
        "live_submit_authorized,idempotency_key,intent_json,intent_sha256,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (intent, "p1_consensus_decision_ZSPD_2026-09-22", "cyc", "27°C", "tok", "YES", "BUY",
         0.6, 8.0, 4.8, state, 1, intent,
         json.dumps({"rule": rule, "order_type": order_type}), "s" * 64,
         "2026-09-22T08:00:00Z"),
    )
    if blocking is not None:
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations(order_intent_id,state,filled_size,"
            "filled_notional_usdc,last_event_id,blocking_reason,updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (intent, "resolved_not_placed", 0.0, 0.0, "ev", blocking, "2026-09-22T08:00:01Z"),
        )


def test_the_money_is_read_the_way_the_tiebreak_lanes_is(tmp_path, monkeypatch) -> None:
    """User ruling 2026-09-24 「同心线看不到订单和盈亏，做成和秤砣线一样的」: every
    order the lane sent, placed or not, scored and split by the rule that
    minted it -- the v1 taker and the v2 maker are two strategies."""

    conn = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    apply_production_ssot_schema(conn)
    lane.ensure_lane_schema(conn)
    _intent(conn, "p1cons_v1a", rule="p1_consensus_lane_v1", order_type="FOK", state="filled")
    _intent(conn, "p1cons_v1b", rule="p1_consensus_lane_v1", order_type="FOK", state="filled")
    _intent(conn, "p1cons_v2a", rule="p1_consensus_lane_v2", order_type="GTD", state="failed",
            blocking="gateway_refused_submission")
    _intent(conn, "p1cons_v2b", rule="p1_consensus_lane_v2", order_type="GTD", state="failed",
            blocking="gateway_refused_submission")
    # The evaluation row names only the newest repost's intent, with the
    # venue's own refusal; the older repost falls back to the reconciliation.
    _row(conn, "f", four=1, fired=1, refusal="send:expiration is less than 180 seconds in the future",
         intent="p1cons_v2b", day="2026-09-22")

    def leg(intent: str, day: str, bucket: str, filled: float, notional: float, state: str):
        return {"decision_id": f"p1_consensus_decision_ZSPD_{day}", "station_id": "ZSPD",
                "event_date": day, "stage": "P1", "bucket": bucket, "filled_size": filled,
                "filled_notional_usdc": notional, "limit_price": 0.6, "requested_size": 8.0,
                "requested_notional_usdc": 4.8, "state": state,
                "created_at": f"{day}T08:00:00Z", "order_intent_id": intent}

    legs = [
        leg("p1cons_v1a", "2026-09-21", "27°C", 8.0, 4.8, "filled"),
        leg("p1cons_v1b", "2026-09-22", "27°C", 8.0, 4.8, "filled"),
        leg("p1cons_v2a", "2026-09-22", "27°C", 0.0, 0.0, "failed"),
        leg("p1cons_v2b", "2026-09-22", "27°C", 0.0, 0.0, "failed"),
    ]
    monkeypatch.setattr(C.live_book, "build", lambda conn: {
        "legs": legs,
        "settled": {("ZSPD", "2026-09-21"): ("27°C",), ("ZSPD", "2026-09-22"): ("28°C",)},
    })
    out = C.collect_p1_consensus_lane(conn)
    R = out["trades"]
    assert (R["sent"], R["not_placed"], R["filled"], R["settled"], R["hits"]) == (4, 2, 2, 2, 1)
    assert R["stake_usdc"] == pytest.approx(9.6)
    assert R["pnl_usdc"] == pytest.approx(8.0 - 9.6)
    assert R["roi"] == pytest.approx((8.0 - 9.6) / 9.6)
    assert R["realised_minus_ask"] == pytest.approx(0.5 - 0.6)
    assert R["halves"]["first"] == pytest.approx(0.4)
    assert R["halves"]["second"] == pytest.approx(-0.6)
    by_intent = {t["order_intent_id"]: t for t in R["rows"]}
    assert by_intent["p1cons_v2b"]["not_placed"].startswith("send:expiration")
    assert by_intent["p1cons_v2a"]["not_placed"] == "gateway_refused_submission"
    assert by_intent["p1cons_v1a"]["not_placed"] is None
    versions = {v["rule_version"]: v for v in out["by_rule_version"]}
    assert versions["p1_consensus_lane_v1"]["resting"] is False
    assert (versions["p1_consensus_lane_v1"]["filled"], versions["p1_consensus_lane_v1"]["hits"]) == (2, 1)
    assert versions["p1_consensus_lane_v2"]["resting"] is True
    assert (versions["p1_consensus_lane_v2"]["sent"], versions["p1_consensus_lane_v2"]["not_placed"]) == (2, 2)
    assert [r["rule_version"] for r in out["evaluations"]["by_rule"]] == [lane.RULE_VERSION]
