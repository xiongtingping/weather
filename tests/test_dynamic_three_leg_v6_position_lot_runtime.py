import sqlite3

import pytest

from dynamic_three_leg_v6_position_lot_runtime import apply_v6_reconciled_fill
from dynamic_three_leg_v6_schema_contract import initialize_dynamic_three_leg_v6_schema
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


def _intent():
    payload = {
        "slug": "zuuu",
        "event_date": "2026-07-16",
        "run_date": "2026-07-16",
        "correlation_id": "corr1",
    }
    import json
    return {
        "order_intent_id": "buy1",
        "order_action": "BUY",
        "position_lot_id": "lot1",
        "position_lot_version": "1",
        "bucket": "35C",
        "outcome_side": "YES",
        "token_id": "token1",
        "strategy_artifact_sha256": "a" * 64,
        "reason_family": "strategy_leg",
        "reason_code": "main_yes",
        "limit_price": 0.2,
        "posterior_run_id": "p1",
        "realization_state_id": "r1",
        "trading_plan_id": "plan1",
        "fact_snapshot_id": "fact1",
        "position_snapshot_id": "pos1",
        "market_snapshot_id": "market1",
        "capital_snapshot_id": "cap1",
        "risk_snapshot_id": "risk1",
        "capital_epoch_id": "epoch1",
        "risk_reservation_id": "reserve1",
        "quote_expires_at": "2026-07-16T09:00:00Z",
        "observation_to_order_hard_deadline_at": "2026-07-16T09:00:00Z",
        "intent_json": json.dumps(payload),
    }


def test_buy_fill_and_standing_0999_intent_share_one_transaction():
    conn = _conn()
    result = apply_v6_reconciled_fill(
        conn, intent=_intent(), cumulative_filled_size=5.0, fill_price=0.2,
        fill_identity="fill1", token_tick_size="0.001", applied_at="2026-07-16T08:00:00Z",
    )
    assert result["ok"] is True
    assert result["standing_exit"]["state"] == "standing_exit_intent_prepared"
    lot = conn.execute("SELECT entry_strategy_leg,remaining_size,state FROM dynamic_three_leg_v6_position_lots").fetchone()
    assert lot == ("main_yes", 5.0, "coverage_required")
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_v6_standing_exit_plans").fetchone()[0] == 1
    assert conn.execute("SELECT reason_code,limit_price FROM posterior_execution_intents").fetchone() == (
        "market_alpha_take_profit_main_yes_999", 0.999,
    )


@pytest.mark.parametrize("fault_after", ["lot", "coverage"])
def test_buy_fill_fault_rolls_back_lot_event_coverage_and_intent(fault_after):
    conn = _conn()
    with pytest.raises(RuntimeError):
        apply_v6_reconciled_fill(
            conn, intent=_intent(), cumulative_filled_size=5.0, fill_price=0.2,
            fill_identity="fill1", token_tick_size="0.001", applied_at="2026-07-16T08:00:00Z",
            fault_after=fault_after,
        )
    for table in (
        "dynamic_three_leg_v6_position_lots",
        "dynamic_three_leg_v6_position_lot_events",
        "dynamic_three_leg_v6_standing_exit_plans",
        "posterior_execution_intents",
        "auto_trade_decisions",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_buy_fill_tick_mismatch_rolls_back_the_lot_instead_of_leaving_naked_position():
    conn = _conn()
    with pytest.raises(RuntimeError):
        apply_v6_reconciled_fill(
            conn, intent=_intent(), cumulative_filled_size=5.0, fill_price=0.2,
            fill_identity="fill1", token_tick_size="0.01", applied_at="2026-07-16T08:00:00Z",
        )
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_v6_position_lots").fetchone()[0] == 0


def test_reconciled_target_sell_advances_executed_watermark_and_restores_residual_coverage():
    conn = _conn()
    buy = apply_v6_reconciled_fill(
        conn, intent=_intent(), cumulative_filled_size=10.0, fill_price=0.2,
        fill_identity="fill1", token_tick_size="0.001", applied_at="2026-07-16T08:00:00Z",
    )
    standing_id = buy["standing_exit"]["standing_exit_plan_id"]
    conn.execute(
        "UPDATE dynamic_three_leg_v6_standing_exit_plans SET state='released',target_size=10,covered_size=7.5,uncovered_size=0,stage_released_size=2.5 WHERE standing_exit_plan_id=?",
        (standing_id,),
    )
    plan_json = '{}'
    conn.execute(
        """INSERT INTO target_realization_v6_plans VALUES(
            'plan-v6','p1','r1','e1',?,'cycle1','cap1','35C',NULL,'trajectory_no',1,?,?,'committed',?,?
        )""",
        ("a" * 64, plan_json, "x" * 64, "2026-07-16T08:00:00Z", "2026-07-16T08:00:00Z"),
    )
    cycle = {
        "schema": "dynamic_three_leg_v6_main_yes_position_cycle_v1",
        "main_yes_position_cycle_id": "cycle1", "sell_stage_high_watermark": 0,
    }
    import json, hashlib
    cycle_json = json.dumps(cycle, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """INSERT INTO dynamic_three_leg_v6_main_yes_cycles VALUES(
            'cp1','cycle1','plan-v6','p1','e1','35C','upside_risk',1,0,0,?,?, 'active',?,?
        )""",
        (cycle_json, hashlib.sha256(cycle_json.encode()).hexdigest(), "2026-07-16T08:00:00Z", "2026-07-16T08:00:00Z"),
    )
    payload = {
        "schema": "target_realization_v6_target_sell_intent_v1",
        "standing_exit_plan_id": standing_id,
        "main_yes_position_cycle_id": "cycle1",
        "target_sell_stage": 1,
    }
    intent = {
        **_intent(), "order_intent_id": "sell1", "order_action": "SELL",
        "position_lot_version": "1", "reason_family": "target_realization",
        "reason_code": "target_realization_main_yes_upside_risk_stage_1",
        "limit_price": 0.3, "size": 2.5, "intent_json": json.dumps(payload),
    }
    result = apply_v6_reconciled_fill(
        conn, intent=intent, cumulative_filled_size=2.5, fill_price=0.3,
        fill_identity="target-fill1", token_tick_size="0.001", applied_at="2026-07-16T08:00:10Z",
    )
    assert result["ok"] is True
    assert tuple(conn.execute("SELECT remaining_size,position_lot_version FROM dynamic_three_leg_v6_position_lots").fetchone()) == (7.5, 2)
    assert tuple(conn.execute("SELECT target_size,covered_size,stage_released_size,state FROM dynamic_three_leg_v6_standing_exit_plans").fetchone()) == (7.5, 7.5, 0.0, "covered")
    assert conn.execute("SELECT sell_stage_high_watermark FROM dynamic_three_leg_v6_main_yes_cycles").fetchone()[0] == 1


def test_cycle_watermark_waits_for_every_lot_in_same_plan_stage():
    import hashlib
    import json

    conn = _conn()
    buys = []
    for suffix in ("1", "2"):
        buy_intent = {**_intent(), "order_intent_id": f"buy{suffix}", "position_lot_id": f"lot{suffix}"}
        buys.append(apply_v6_reconciled_fill(
            conn, intent=buy_intent, cumulative_filled_size=10.0, fill_price=0.2,
            fill_identity=f"fill{suffix}", token_tick_size="0.001", applied_at="2026-07-16T08:00:00Z",
        ))
    for buy in buys:
        conn.execute(
            """UPDATE dynamic_three_leg_v6_standing_exit_plans
               SET state='released',target_size=10,covered_size=7.5,uncovered_size=0,stage_released_size=2.5
               WHERE standing_exit_plan_id=?""",
            (buy["standing_exit"]["standing_exit_plan_id"],),
        )
    conn.execute(
        """INSERT INTO target_realization_v6_plans VALUES(
            'plan-multi','p1','r1','e1',?,'cycle-multi','cap1','35C',NULL,'trajectory_no',1,'{}',?,'committed',?,?
        )""",
        ("a" * 64, "x" * 64, "2026-07-16T08:00:00Z", "2026-07-16T08:00:00Z"),
    )
    cycle = {"schema": "dynamic_three_leg_v6_main_yes_position_cycle_v1", "main_yes_position_cycle_id": "cycle-multi", "sell_stage_high_watermark": 0}
    cycle_json = json.dumps(cycle, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """INSERT INTO dynamic_three_leg_v6_main_yes_cycles VALUES(
            'cp-multi','cycle-multi','plan-multi','p1','e1','35C','upside_risk',1,0,0,?,?,'active',?,?
        )""",
        (cycle_json, hashlib.sha256(cycle_json.encode()).hexdigest(), "2026-07-16T08:00:00Z", "2026-07-16T08:00:00Z"),
    )

    target_intents = []
    for suffix, buy in zip(("1", "2"), buys):
        standing_id = buy["standing_exit"]["standing_exit_plan_id"]
        payload = {
            "schema": "target_realization_v6_target_sell_intent_v1",
            "standing_exit_plan_id": standing_id,
            "main_yes_position_cycle_id": "cycle-multi",
            "target_sell_stage": 1,
        }
        target = {
            **_intent(), "order_intent_id": f"sell{suffix}", "order_action": "SELL",
            "position_lot_id": f"lot{suffix}", "position_lot_version": "1",
            "trading_plan_id": "plan-multi", "reason_family": "target_realization",
            "reason_code": "target_realization_main_yes_upside_risk_stage_1",
            "limit_price": 0.3, "size": 2.5, "notional": 0.75,
            "intent_json": json.dumps(payload, sort_keys=True),
        }
        target_intents.append(target)
        row = {
            **target,
            "decision_id": f"decision-sell{suffix}", "transition_plan_id": "plan-multi",
            "candidate_id": None, "state": "gateway_accepted", "live_submit_authorized": 1,
            "idempotency_key": f"sell-key-{suffix}",
            "intent_sha256": hashlib.sha256(target["intent_json"].encode()).hexdigest(),
            "gateway_response_json": "{}", "exchange_post_started_at": None,
            "expected_order_id": None, "exchange_order_id": f"exchange-{suffix}", "last_error": None,
            "created_at": "2026-07-16T08:00:01Z", "updated_at": "2026-07-16T08:00:01Z",
        }
        columns = [column[1] for column in conn.execute("PRAGMA table_info(posterior_execution_intents)")]
        conn.execute(
            f"INSERT INTO posterior_execution_intents({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(row.get(column) for column in columns),
        )

    apply_v6_reconciled_fill(
        conn, intent=target_intents[0], cumulative_filled_size=2.5, fill_price=0.3,
        fill_identity="target-fill-1", token_tick_size="0.001", applied_at="2026-07-16T08:00:10Z",
    )
    assert conn.execute("SELECT sell_stage_high_watermark FROM dynamic_three_leg_v6_main_yes_cycles").fetchone()[0] == 0
    conn.execute("UPDATE posterior_execution_intents SET state='filled' WHERE order_intent_id='sell1'")
    apply_v6_reconciled_fill(
        conn, intent=target_intents[1], cumulative_filled_size=2.5, fill_price=0.3,
        fill_identity="target-fill-2", token_tick_size="0.001", applied_at="2026-07-16T08:00:11Z",
    )
    assert conn.execute("SELECT sell_stage_high_watermark FROM dynamic_three_leg_v6_main_yes_cycles").fetchone()[0] == 1
