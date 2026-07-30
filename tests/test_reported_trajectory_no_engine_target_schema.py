from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from reported_trajectory_market_registry import load_config
from reported_trajectory_no_engine import ReportedTrajectoryNoEngine
from reported_trajectory_no_schema import apply_reported_trajectory_no_schema


def _target_schema_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        """CREATE TABLE market_tokens(
          slug TEXT,event_date TEXT,outcome TEXT,yes_token TEXT,no_token TEXT,outcome_volume REAL,
          liquidity REAL,market_active INTEGER,market_closed INTEGER,market_archived INTEGER,
          market_accepting_orders INTEGER,market_enable_orderbook INTEGER,updated_at TEXT,
          PRIMARY KEY(slug,event_date,outcome)
        )"""
    )
    apply_reported_trajectory_no_schema(conn)
    for outcome, lower, upper, kind, no_token in (
        ("29°C or below", None, 29, "lower_tail", "no29"),
        ("30°C", 30, 30, "single", "no30"),
        ("31°C or higher", 31, None, "upper_tail", "no31"),
    ):
        registry = {
            "slug": "highest-temperature-in-beijing-on-july-18-2026",
            "event_date": "2026-07-18",
            "city_slug": "beijing",
            "station_id": "ZBAA",
            "settlement_timezone": "Asia/Shanghai",
            "settlement_unit": "C",
            "source_lane": "amsc",
            "source_eligible": True,
            "outcome": outcome,
            "lower": lower,
            "upper": upper,
            "kind": kind,
            "yes_token": "yes" + no_token,
            "no_token": no_token,
        }
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry(
              slug,event_date,outcome,city_slug,station_id,settlement_timezone,settlement_unit,
              source_lane,source_eligible,source_block_reason,resolution_url,rules_sha256,
              yes_token,no_token,market_active,market_accepting_orders,registry_json,refreshed_at
            ) VALUES(?,?,?,?,?,?,?,?,1,'','url','sha',?,?,1,1,?,?)""",
            (
                registry["slug"], registry["event_date"], outcome, "beijing", "ZBAA", "Asia/Shanghai", "C",
                "amsc", registry["yes_token"], no_token, json.dumps(registry),
                datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            ),
        )
    conn.commit()
    return conn


def test_hard_no_decision_writes_current_target_ssot_schema_atomically() -> None:
    conn = _target_schema_db()
    with patch.dict(
        os.environ, {"PM_TARGET_EXECUTION_MODE": "production_no_live"}, clear=False
    ):
        engine = ReportedTrajectoryNoEngine(conn, config=load_config())
        result = engine.process_source_poll(
            {
                "source_lane": "amsc",
                "station_ids": ["ZBAA"],
                "status": 200,
                "response_started_at": "2026-07-18T02:00:00Z",
                "station_errors": [],
                "taf_requested": False,
                "reports": [{
                    "station_id": "ZBAA",
                    "raw_text": "METAR ZBAA 180200Z 18004MPS CAVOK 31/20 Q1000",
                    "source_report_time": "2026-07-18T02:00:00Z",
                }],
            },
            request_started_at="2026-07-18T02:00:00Z",
            response_completed_at="2026-07-18T02:00:00.100Z",
        )

    assert result["new_observation_count"] == 1
    decision = conn.execute(
        "SELECT decision_id,cycle_id,plan_id,station_id,event_date,"
        "settlement_timezone,strategy_lane,"
        "decided_state,will_order,reason_codes_json,details_json,decision_sha256 "
        "FROM auto_trade_decisions"
    ).fetchone()
    assert decision is not None
    assert decision[3:9] == (
        "ZBAA", "2026-07-18", "Asia/Shanghai", "hard_no", "blocked", 0
    )
    assert json.loads(decision[9]) == ["production_no_live"]
    assert json.loads(decision[10])["taf_consumed"] is False
    assert json.loads(decision[10])["settlement_timezone"] == "Asia/Shanghai"
    assert len(decision[11]) == 64
    assert conn.execute("SELECT COUNT(*) FROM reported_trajectory_observations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM reported_trajectory_station_day_bounds").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM reported_trajectory_no_intents").fetchone()[0] == 0
