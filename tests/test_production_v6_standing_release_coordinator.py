import sqlite3

from dynamic_three_leg_v6_schema_contract import initialize_dynamic_three_leg_v6_schema
from production_v6_standing_exit_writer import acknowledge_v6_standing_exit_coverage, commit_v6_buy_fill_standing_exit
from production_v6_standing_release_coordinator import process_v6_standing_release_once
from sequential_bayes_schema_contract import TABLE_DDL


def _setup():
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
    fill = {
        "strategy_leg": "main_yes", "filled_size": 10.0, "posterior_run_id": "p1",
        "realization_state_id": "r1", "position_lot_id": "lot1", "position_lot_version": "1",
        "token_id": "token1", "bucket": "35C", "side": "YES", "buy_intent_id": "buy1",
        "fill_identity": "fill1", "trading_plan_id": "plan1", "fact_snapshot_id": "fact1",
        "position_snapshot_id": "pos1", "market_snapshot_id": "market1", "capital_snapshot_id": "cap1",
        "risk_snapshot_id": "risk1", "capital_epoch_id": "epoch1", "risk_reservation_id": "reserve1",
        "quote_expires_at": "2026-07-16T09:00:00Z", "observation_to_order_hard_deadline_at": "2026-07-16T09:00:00Z",
        "run_date": "2026-07-16", "slug": "zuuu", "event_date": "2026-07-16", "correlation_id": "corr1",
    }
    standing = commit_v6_buy_fill_standing_exit(conn, fill=fill, strategy_artifact_sha256="a" * 64, tick_size="0.001", created_at="2026-07-16T08:00:00Z")
    conn.execute("UPDATE posterior_execution_intents SET state='gateway_accepted',exchange_order_id='order999' WHERE order_intent_id=?", (standing["order_intent_id"],))
    acknowledge_v6_standing_exit_coverage(conn, standing_exit_plan_id=standing["standing_exit_plan_id"], order_intent_id=standing["order_intent_id"], exchange_order_id="order999", acknowledged_at="2026-07-16T08:00:01Z")
    conn.execute(
        """INSERT INTO dynamic_three_leg_v6_standing_exit_release_requests(
            standing_exit_release_request_id,standing_exit_plan_id,main_yes_position_cycle_id,
            v6_trading_plan_id,
            target_realization_reason_code,requested_release_size,standing_sell_order_id,idempotency_key,
            state,gateway_response_json,created_at,updated_at
        ) VALUES('rel1',?,'cycle1','plan1','target_realization_main_yes_upside_risk_stage_1',2.5,'order999','cancel-key','prepared','{}',?,?)""",
        (standing["standing_exit_plan_id"], "2026-07-16T08:00:02Z", "2026-07-16T08:00:02Z"),
    )
    conn.commit()
    return conn


def test_release_cancels_reconciles_then_replaces_residual_before_target_sell():
    conn = _setup()
    cancel_calls = []
    first = process_v6_standing_release_once(
        conn, action_as_of="2026-07-16T08:00:02Z",
        gateway_cancel_order=lambda request: cancel_calls.append(request) or {"ok": True, "cancelled": True, "orderID": "order999"},
        gateway_lookup_exchange_order=lambda _request: (_ for _ in ()).throw(AssertionError()),
    )
    assert first["state"] == "reconcile_required"
    assert len(cancel_calls) == 1
    second = process_v6_standing_release_once(
        conn, action_as_of="2026-07-16T08:00:03Z",
        gateway_cancel_order=lambda _request: (_ for _ in ()).throw(AssertionError()),
        gateway_lookup_exchange_order=lambda _request: {"ok": True, "status": "cancelled", "orderID": "order999"},
    )
    assert second["state"] == "confirmed"
    assert second["released_size"] == 2.5
    assert second["replacement_size"] == 7.5
    plan = conn.execute("SELECT state,covered_size,uncovered_size,stage_released_size FROM dynamic_three_leg_v6_standing_exit_plans").fetchone()
    assert tuple(plan) == ("intent_prepared", 0.0, 7.5, 2.5)
    assert tuple(conn.execute("SELECT size,limit_price,state FROM posterior_execution_intents WHERE order_intent_id=?", (second["replacement_order_intent_id"],)).fetchone()) == (7.5, 0.999, "prepared")
