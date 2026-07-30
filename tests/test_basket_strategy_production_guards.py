import sqlite3

from basket_strategy.policy import allocate_profit_weighted
from basket_strategy.production import BasketProductionEngine
from basket_strategy.schema import apply_basket_strategy_schema


def _activation(mode="production"):
    return {"basket": {"risk_budget_mode": mode}}


def test_profit_weighted_allocation_enforces_parent_order_cap():
    rows = [
        {"bucket": f"{34 + i}°C", "bucket_c": 34 + i, "yes_ask": price, "yes_ask_size": 1000.0}
        for i, price in enumerate((0.10, 0.20, 0.15, 0.10))
    ]
    result = allocate_profit_weighted(rows, budget_usdc=10.0, max_leg_notional_usdc=2.0)
    assert result["status"] == "ready"
    assert max(q * p for q, p in zip(result["quantities"], result["prices"])) <= 2.0 + 1e-9
    assert result["total_cost_usdc"] <= 10.0 + 1e-9


def test_global_city_event_and_test_loss_budget_guards():
    conn = sqlite3.connect(":memory:")
    apply_basket_strategy_schema(conn)
    engine = BasketProductionEngine(conn, gateway=object())
    activation = _activation("production")

    assert engine._capital_gate(
        slug="event-a", event_date="2026-07-20", city_slug="chengdu", activation=activation, proposed_usdc=5.0
    )["status"] == "available"
    assert engine._capital_gate(
        slug="event-a", event_date="2026-07-20", city_slug="chengdu", activation=activation, proposed_usdc=5.01
    )["reason_codes"] == ["single_event_cap_exhausted"]

    conn.execute(
        """INSERT INTO basket_strategy_cycles(
             cycle_id,strategy_id,slug,event_date,city_slug,plan_sha256,plan_json,state,
             total_cost_usdc,quantity_per_bucket,created_at,updated_at)
           VALUES('c1','basket','event-a','2026-07-20','chengdu','p','{}','submitted',4,1,'now','now')"""
    )
    conn.commit()
    assert "single_event_cap_exhausted" in engine._capital_gate(
        slug="event-a", event_date="2026-07-20", city_slug="chengdu", activation=activation, proposed_usdc=2.0
    )["reason_codes"]

    test_activation = _activation("test")
    conn.execute(
        """INSERT INTO basket_strategy_capital_ledger(
             ledger_id,cycle_id,slug,event_date,city_slug,entry_type,amount_usdc,reason,idempotency_key,created_at)
           VALUES('loss','c1','event-a','2026-07-20','chengdu','realized_loss',5,'test','loss-key','now')"""
    )
    conn.commit()
    assert "daily_loss_cap_exhausted" in engine._capital_gate(
        slug="event-b", event_date="2026-07-20", city_slug="chengdu", activation=test_activation, proposed_usdc=0.1
    )["reason_codes"]
    conn.close()


def test_known_partial_fill_reopens_submitted_cycle_for_reconcile():
    conn = sqlite3.connect(":memory:")
    apply_basket_strategy_schema(conn)
    engine = BasketProductionEngine(conn, gateway=object())
    conn.execute(
        """INSERT INTO basket_strategy_cycles(
             cycle_id,strategy_id,slug,event_date,city_slug,plan_sha256,plan_json,state,
             total_cost_usdc,quantity_per_bucket,created_at,updated_at)
           VALUES('cycle-partial','basket','event-a','2026-07-23','chengdu','p','{}','submitted',5,1,'now','now')"""
    )
    conn.execute(
        """INSERT INTO basket_strategy_legs(
             leg_id,cycle_id,order_intent_id,idempotency_key,bucket,token_id,side,
             limit_price,requested_size,requested_notional_usdc,order_type,state,
             exchange_order_id,size_matched,created_at,updated_at)
           VALUES('leg-partial','cycle-partial','intent-partial','idem-partial','39°C','token',
                  'YES',0.37,19.86,7.34,'FOK','exchange_accepted','order-partial',19.135134,'now','now')"""
    )
    conn.execute(
        """INSERT INTO basket_strategy_position_targets(
             target_id,cycle_id,snapshot_id,target_version,bucket,role,target_profit_usdc,
             target_quantity,filled_quantity,pending_quantity,delta_quantity,state,created_at,updated_at)
           VALUES('target-partial','cycle-partial','',1,'39°C','floor',20.1,19.0,19.135134,0,0,'satisfied','now','now')"""
    )
    conn.execute(
        """INSERT INTO basket_strategy_parent_orders(
             parent_order_id,cycle_id,target_version,snapshot_id,bucket,requested_quantity,
             requested_notional_usdc,state,idempotency_key,created_at,updated_at)
           VALUES('parent-partial','cycle-partial',1,'','39°C',19.86,7.34,'filled','parent-idem','now','now')"""
    )
    conn.execute(
        """INSERT INTO basket_strategy_child_orders(
             child_order_id,parent_order_id,leg_id,price_level,limit_price,target_quantity,
             target_notional_usdc,state,idempotency_key,exchange_order_id,size_matched,created_at,updated_at)
           VALUES('child-partial','parent-partial','leg-partial',0,0.37,19.86,7.34,'filled',
                  'child-idem','order-partial',19.135134,'now','now')"""
    )
    conn.commit()

    result = engine.reconcile_known_fills()
    assert result["state"] == "reconcile_required"
    assert abs(result["cycles"][0]["partial_legs"][0]["remaining_size"] - 0.724866) < 1e-9
    assert conn.execute("SELECT state FROM basket_strategy_cycles WHERE cycle_id='cycle-partial'").fetchone()[0] == "reconcile_required"
    target = conn.execute(
        "SELECT target_quantity,filled_quantity,delta_quantity,state FROM basket_strategy_position_targets WHERE target_id='target-partial'"
    ).fetchone()
    assert abs(target[0] - 19.86) < 1e-9
    assert abs(target[1] - 19.135134) < 1e-9
    assert abs(target[2] - 0.724866) < 1e-9
    assert target[3] == "reconcile_required"
    conn.close()
