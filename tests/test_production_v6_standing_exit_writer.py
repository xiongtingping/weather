import sqlite3

import pytest

from dynamic_three_leg_v6_schema_contract import initialize_dynamic_three_leg_v6_schema
from production_v6_standing_exit_writer import (
    acknowledge_v6_standing_exit_coverage,
    commit_v6_buy_fill_standing_exit,
)
from sequential_bayes_schema_contract import TABLE_DDL


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE auto_trade_decisions(
            id INTEGER PRIMARY KEY,run_id TEXT,run_date TEXT,slug TEXT,event_date TEXT,outcome TEXT,
            side TEXT,action TEXT,decided_state TEXT,will_order INTEGER,reason_codes_json TEXT,
            reasons_json TEXT,details_json TEXT,created_at TEXT,correlation_id TEXT,fact_snapshot_id TEXT,
            posterior_run_id TEXT,decision_id TEXT UNIQUE,order_intent_id TEXT
        )"""
    )
    conn.execute(TABLE_DDL["posterior_execution_intents"])
    initialize_dynamic_three_leg_v6_schema(conn)
    return conn


def _fill():
    return {
        "strategy_leg": "main_yes",
        "filled_size": 10.0,
        "posterior_run_id": "p1",
        "realization_state_id": "r1",
        "position_lot_id": "lot1",
        "position_lot_version": "v1",
        "token_id": "token1",
        "bucket": "35C",
        "side": "YES",
        "buy_intent_id": "buy1",
        "fill_identity": "fill1",
        "trading_plan_id": "plan1",
        "fact_snapshot_id": "fact1",
        "position_snapshot_id": "positions1",
        "market_snapshot_id": "market1",
        "capital_snapshot_id": "capital1",
        "risk_snapshot_id": "risk1",
        "capital_epoch_id": "epoch1",
        "risk_reservation_id": "reserve1",
        "quote_expires_at": "2026-07-16T09:00:00Z",
        "observation_to_order_hard_deadline_at": "2026-07-16T09:00:00Z",
        "run_date": "2026-07-16",
        "slug": "zuuu",
        "event_date": "2026-07-16",
        "correlation_id": "corr1",
    }


def _commit(conn, **kwargs):
    args = dict(
        fill=_fill(),
        strategy_artifact_sha256="a" * 64,
        tick_size="0.001",
        created_at="2026-07-16T08:00:00Z",
    )
    args.update(kwargs)
    return commit_v6_buy_fill_standing_exit(conn, **args)


def test_fill_atomically_prepares_exact_0999_coverage_intent():
    conn = _conn()
    result = _commit(conn)
    assert result["ok"] is True
    plan = conn.execute(
        "SELECT state,target_size,covered_size,uncovered_size,exit_reason_code FROM dynamic_three_leg_v6_standing_exit_plans"
    ).fetchone()
    assert plan == ("intent_prepared", 10.0, 0.0, 10.0, "market_alpha_take_profit_main_yes_999")
    intent = conn.execute(
        "SELECT order_action,limit_price,size,reason_code,state FROM posterior_execution_intents"
    ).fetchone()
    assert intent == ("SELL", 0.999, 10.0, "market_alpha_take_profit_main_yes_999", "prepared")
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1
    replay = _commit(conn)
    assert replay["state"] == "already_prepared"
    ack = acknowledge_v6_standing_exit_coverage(
        conn,
        standing_exit_plan_id=result["standing_exit_plan_id"],
        order_intent_id=result["order_intent_id"],
        exchange_order_id="order-999",
        acknowledged_at="2026-07-16T08:00:01Z",
    )
    assert ack["state"] == "covered"
    assert conn.execute("SELECT covered_size,uncovered_size,state FROM dynamic_three_leg_v6_standing_exit_plans").fetchone() == (10.0, 0.0, "covered")


@pytest.mark.parametrize("fault_after", ["coverage", "intent", "decision"])
def test_fill_coverage_faults_never_leave_partial_local_state(fault_after):
    conn = _conn()
    with pytest.raises(RuntimeError):
        _commit(conn, fault_after=fault_after)
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_v6_standing_exit_plans").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_v6_standing_exit_coverage_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0


def test_fill_fails_closed_when_market_tick_cannot_represent_0999():
    conn = _conn()
    result = _commit(conn, tick_size="0.01")
    assert result["ok"] is False
    assert "standing_exit_999_tick_unsupported" in result["block_reasons"]
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents").fetchone()[0] == 0
