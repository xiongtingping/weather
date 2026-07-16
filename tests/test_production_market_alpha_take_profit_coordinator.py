from __future__ import annotations

import json
import sqlite3

from dynamic_three_leg_position_lot_schema_contract import apply_dynamic_three_leg_position_lot_schema
from market_alpha_take_profit_activation import materialize_market_alpha_take_profit_activation
from market_alpha_take_profit_schema_contract import apply_market_alpha_take_profit_schema
from production_market_alpha_take_profit_coordinator import run_market_alpha_take_profit_once
from sequential_bayes_schema_contract import apply_forward_schema


NOW = "2026-07-14T08:00:00.500Z"
SLUG = "highest-temperature-in-chengdu-on-july-14-2026"
ARTIFACT = "strategy-approved"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    apply_dynamic_three_leg_position_lot_schema(conn, writer_quiescent=True)
    apply_market_alpha_take_profit_schema(conn, writer_quiescent=True)
    conn.executescript(
        """
        CREATE TABLE event_fact_history(
            id INTEGER PRIMARY KEY,slug TEXT,event_date TEXT,fact_json TEXT,
            posterior_run_id TEXT,fact_snapshot_id TEXT
        );
        CREATE TABLE event_fact_latest(slug TEXT,event_date TEXT,fact_id INTEGER);
        CREATE TABLE auto_trade_decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,run_date TEXT,slug TEXT,event_date TEXT,
            outcome TEXT,side TEXT,action TEXT,decided_state TEXT,will_order INTEGER,
            reason_codes_json TEXT,reasons_json TEXT,details_json TEXT,created_at TEXT,
            correlation_id TEXT,fact_snapshot_id TEXT,posterior_run_id TEXT,
            decision_id TEXT UNIQUE,order_intent_id TEXT UNIQUE
        );
        """
    )
    fact = {"posterior_ssot_fact": {
        "posterior_run_id": "post-current",
        "fact_snapshot_id": "fact-current",
        "realization_state_id": "realization-current",
        "target_bucket": "37°C",
    }}
    conn.execute(
        "INSERT INTO event_fact_history VALUES(1,?,?,?,?,?)",
        (SLUG, "2026-07-14", json.dumps(fact), "post-current", "fact-current"),
    )
    conn.execute("INSERT INTO event_fact_latest VALUES(?,?,1)", (SLUG, "2026-07-14"))
    conn.execute(
        """INSERT INTO dynamic_three_leg_position_lots(
           position_lot_id,position_lot_version,management_scope,slug,event_date,bucket,
           outcome_side,token_id,entry_strategy_leg,entry_strategy_artifact_sha256,
           entry_order_intent_id,entry_posterior_run_id,entry_price,acquired_size,
           remaining_size,remaining_cost_usdc,frozen_take_profit_price,state,
           opened_at,closed_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "lot-current", 2, "dynamic_three_leg_weather", SLUG, "2026-07-14", "37°C",
            "YES", "yes-37", "top_yes_wave", ARTIFACT, "entry-intent", "post-entry",
            0.40, 20.0, 20.0, 8.0, 0.90, "open", NOW, None, NOW,
        ),
    )
    conn.commit()
    return conn


def _activation() -> dict:
    return materialize_market_alpha_take_profit_activation({
        "schema": "market_alpha_take_profit_production_activation_v1",
        "state": "enabled",
        "strategy_artifact_sha256": ARTIFACT,
        "cutover_artifact_sha256": "cutover-approved",
        "live_decision_write_authorized": True,
        "live_submit_authorized": True,
        "reason_family": "market_alpha_take_profit",
        "reason_code": "market_alpha_take_profit_top_yes_wave_completed",
        "frozen_take_profit_price": 0.90,
    })


def _raw(_request, *, depth: float = 25.0) -> dict:
    return {
        "schema": "clob_execution_guard_snapshot_v1",
        "ok": True,
        "evidence_only": True,
        "authorizes_order": False,
        "token_id": "yes-37",
        "side": "SELL",
        "observed_at": "2026-07-14T08:00:00.550Z",
        "amount_decimals": 6,
        "order_book": {
            "asset_id": "yes-37",
            "timestamp": "1784016000550",
            "bids": [{"price": "0.91", "size": str(depth)}],
            "asks": [{"price": "0.92", "size": "10"}],
        },
        "conditional_balance_allowance": {"balance": "20000000", "allowance": "20000000"},
    }


def test_quote_trigger_writes_frozen_point_nine_limit_not_observed_bid(monkeypatch) -> None:
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    result = run_market_alpha_take_profit_once(
        conn,
        raw_guard_snapshot_fetcher=_raw,
        activation=_activation(),
        preliminary_as_of=NOW,
    )

    assert result["state"] == "intent_prepared"
    assert result["quote"]["price_for_requested_size"] == 0.91
    limit_price, size = conn.execute(
        "SELECT limit_price,size FROM posterior_execution_intents"
    ).fetchone()
    assert limit_price == 0.90
    assert size == 20.0


def test_quote_trigger_holds_without_full_depth_and_writes_nothing(monkeypatch) -> None:
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("PM_MARKET_ALPHA_TAKE_PROFIT_LIVE_SUBMIT_ENABLED", "1")
    conn = _conn()
    result = run_market_alpha_take_profit_once(
        conn,
        raw_guard_snapshot_fetcher=lambda request: _raw(request, depth=5.0),
        activation=_activation(),
        preliminary_as_of=NOW,
    )
    assert result["state"] == "hold_take_profit_not_executable"
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents").fetchone()[0] == 0
