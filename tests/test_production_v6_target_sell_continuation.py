import hashlib
import json
import sqlite3

from dynamic_three_leg_v6_schema_contract import initialize_dynamic_three_leg_v6_schema
from production_v6_target_sell_continuation import process_v6_target_sell_continuation_once
from sequential_bayes_schema_contract import TABLE_DDL
from target_realization_v6_runtime import build_target_realization_v6_plan


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


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
    conn.executescript(
        """
        CREATE TABLE event_fact_history(
            id INTEGER PRIMARY KEY,slug TEXT,event_date TEXT,fact_json TEXT,
            posterior_run_id TEXT,fact_snapshot_id TEXT
        );
        CREATE TABLE event_fact_latest(slug TEXT,event_date TEXT,fact_id INTEGER);
        CREATE TABLE market_tokens(
            slug TEXT,event_date TEXT,outcome TEXT,yes_token TEXT,no_token TEXT,
            market_active INTEGER,market_closed INTEGER,market_archived INTEGER,
            market_accepting_orders INTEGER,market_enable_orderbook INTEGER
        );
        """
    )
    initialize_dynamic_three_leg_v6_schema(conn)
    plan = build_target_realization_v6_plan(
        posterior_run_id="p1", realization_state_id="r1", canonical_observation_event_id="e1",
        posterior_buckets=[
            {"bucket": "35C", "ordinal": 35, "probability": 0.55},
            {"bucket": "36C", "ordinal": 36, "probability": 0.30},
            {"bucket": "34C", "ordinal": 34, "probability": 0.15},
        ],
        peak_timing_state={"entry_peak_at": "2026-07-16T09:00:00Z"},
        future_warming_or_rebound_risk=True, second_yes_entry_allowed=True,
        position_exposure_snapshot={"complete": True, "exposures": []},
        coverage_snapshot={"complete": True, "all_conflicting_sell_and_cancel_reconciled": True},
        prior_main_yes_cycle=None, available_capital_usdc=80.0,
        capital_snapshot_id="cap1", risk_snapshot_id="risk1", capital_epoch_id="epoch1",
        strategy_artifact_sha256="a" * 64, decision_as_of="2026-07-16T08:30:00Z",
    )
    conn.execute(
        """INSERT INTO target_realization_v6_plans(
            v6_trading_plan_id,posterior_run_id,realization_state_id,canonical_observation_event_id,
            strategy_artifact_sha256,main_yes_position_cycle_id,capital_allocation_id,main_yes_bucket,
            second_yes_bucket,no_mode,capital_scale_factor,plan_json,plan_sha256,state,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'committed',?,?)""",
        (
            plan["v6_trading_plan_id"], "p1", "r1", "e1", "a" * 64,
            plan["main_yes_position_cycle_id"], plan["capital_allocation"]["capital_allocation_id"],
            "35C", "36C", plan["no_mode"], plan["capital_allocation"]["capital_scale_factor"], _json(plan), _sha(plan),
            "2026-07-16T08:30:00Z", "2026-07-16T08:30:00Z",
        ),
    )
    cycle = plan["main_yes_cycle"]
    conn.execute(
        """INSERT INTO dynamic_three_leg_v6_main_yes_cycles(
            cycle_checkpoint_id,main_yes_position_cycle_id,v6_trading_plan_id,posterior_run_id,
            canonical_observation_event_id,bucket,main_yes_mode,cycle_generation,sell_stage_high_watermark,
            reentry_confirmation_count,cycle_json,cycle_sha256,state,created_at,updated_at
        ) VALUES('cp1',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            plan["main_yes_position_cycle_id"], plan["v6_trading_plan_id"], "p1", "e1", "35C",
            cycle["main_yes_mode"], cycle["cycle_generation"], 0, 0, _json(cycle), _sha(cycle),
            "active", "2026-07-16T08:30:00Z", "2026-07-16T08:30:00Z",
        ),
    )
    conn.execute(
        """INSERT INTO dynamic_three_leg_v6_position_lots(
            position_lot_id,position_lot_version,management_scope,slug,event_date,bucket,outcome_side,
            token_id,entry_strategy_leg,entry_strategy_artifact_sha256,entry_order_intent_id,
            entry_posterior_run_id,main_yes_position_cycle_id,entry_price,acquired_size,remaining_size,
            remaining_cost_usdc,standing_exit_plan_id,state,opened_at,closed_at,updated_at
        ) VALUES('lot1',2,'target_realization_v6','zuuu','2026-07-16','35C','YES','yes35','main_yes',
                 ?,'buy1','p1',?,0.4,10,10,4,'stand1','open','2026-07-16T08:00:00Z',NULL,'2026-07-16T08:31:00Z')""",
        ("a" * 64, plan["main_yes_position_cycle_id"]),
    )
    conn.execute(
        """INSERT INTO dynamic_three_leg_v6_standing_exit_plans(
            standing_exit_plan_id,strategy_artifact_sha256,posterior_run_id,realization_state_id,
            strategy_leg,position_lot_id,position_lot_version,token_id,bucket,outcome_side,buy_intent_id,
            fill_identity,target_size,covered_size,uncovered_size,stage_released_size,coverage_version,
            standing_sell_intent_id,standing_sell_order_id,exit_reason_code,state,plan_json,plan_sha256,
            created_at,updated_at
        ) VALUES('stand1',?,'p1','r1','main_yes','lot1','2','yes35','35C','YES','buy1','fill1',
                 10,5,0,5,2,'stand-replacement','order-replacement',
                 'market_alpha_take_profit_main_yes_999','covered','{}',?,'2026-07-16T08:00:00Z','2026-07-16T08:31:00Z')""",
        ("a" * 64, _sha({})),
    )
    conn.execute(
        """INSERT INTO dynamic_three_leg_v6_standing_exit_release_requests(
            standing_exit_release_request_id,standing_exit_plan_id,v6_trading_plan_id,
            main_yes_position_cycle_id,target_realization_reason_code,requested_release_size,
            standing_sell_order_id,idempotency_key,state,gateway_response_json,created_at,updated_at
        ) VALUES('rel1','stand1',?,?,'target_realization_main_yes_upside_risk_stage_2',5,
                 'order-old','cancel1','confirmed','{}','2026-07-16T08:30:00Z','2026-07-16T08:31:00Z')""",
        (plan["v6_trading_plan_id"], plan["main_yes_position_cycle_id"]),
    )
    conn.execute("INSERT INTO event_fact_history VALUES(1,'zuuu','2026-07-16','{}','p1','fact1')")
    conn.execute("INSERT INTO event_fact_latest VALUES('zuuu','2026-07-16',1)")
    conn.execute("INSERT INTO market_tokens VALUES('zuuu','2026-07-16','35C','yes35','no35',1,0,0,1,1)")
    conn.commit()
    return conn, plan


def _exchange(request, observed_at="2026-07-16T08:31:01Z"):
    books = {}
    for token in ("yes35", "no35"):
        books[token] = {
            "asset_id": token, "timestamp": observed_at,
            "bids": [{"price": "0.70", "size": "100"}],
            "asks": [{"price": "0.71", "size": "100"}],
        }
    return {
        "schema": "clob_dynamic_three_leg_snapshot_v1", "ok": True,
        "evidence_only": True, "authorizes_order": False, "request_id": request["request_id"],
        "observed_at": observed_at, "amount_decimals": 6,
        "order_books_by_token": books, "token_metadata_by_token": {
            "yes35": {"tick_size": "0.001"}, "no35": {"tick_size": "0.001"},
        },
        "conditional_balance_allowance_by_token": {
            "yes35": {"balance": "10000000", "allowance": "10000000"},
        },
        "collateral_balance_allowance": {"balance": "100000000", "allowance": "100000000"},
    }


def test_covered_residual_continues_exact_target_sell_without_replaying_buys():
    conn, plan = _setup()
    calls = []
    result = process_v6_target_sell_continuation_once(
        conn, action_as_of="2026-07-16T08:31:01Z",
        exchange_snapshot_fetcher=lambda request: calls.append(request) or _exchange(request),
    )
    assert result["state"] == "target_sell_intent_prepared"
    assert len(calls) == 1
    intent = conn.execute(
        "SELECT order_action,reason_family,reason_code,size,limit_price,intent_json FROM posterior_execution_intents"
    ).fetchone()
    assert intent[:5] == (
        "SELL", "target_realization", "target_realization_main_yes_upside_risk_stage_2", 5.0, 0.70,
    )
    assert json.loads(intent[5])["main_yes_position_cycle_id"] == plan["main_yes_position_cycle_id"]
    assert conn.execute("SELECT COUNT(*) FROM posterior_execution_intents WHERE order_action='BUY'").fetchone()[0] == 0


def test_expired_continuation_requotes_without_waiting_for_new_posterior():
    conn, _plan = _setup()
    first = process_v6_target_sell_continuation_once(
        conn, action_as_of="2026-07-16T08:31:01Z", exchange_snapshot_fetcher=_exchange,
    )
    conn.execute("UPDATE posterior_execution_intents SET state='expired' WHERE order_intent_id=?", (first["order_intent_id"],))
    conn.commit()
    second = process_v6_target_sell_continuation_once(
        conn, action_as_of="2026-07-16T08:31:03Z",
        exchange_snapshot_fetcher=lambda request: _exchange(request, "2026-07-16T08:31:03Z"),
    )
    assert second["state"] == "target_sell_intent_prepared"
    assert second["target_sell_continuation_id"] == first["target_sell_continuation_id"]
    assert second["order_intent_id"] != first["order_intent_id"]
    assert conn.execute("SELECT COUNT(*) FROM dynamic_three_leg_v6_target_sell_continuations").fetchone()[0] == 1
