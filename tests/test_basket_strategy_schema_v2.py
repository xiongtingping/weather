import sqlite3

from basket_strategy.schema import apply_basket_strategy_schema


def test_policy_and_two_scale_audit_tables_materialize():
    conn = sqlite3.connect(":memory:")
    result = apply_basket_strategy_schema(conn)
    assert result["schema"] == "basket_strategy_schema_v2"
    row = conn.execute(
        "select policy_id,state,production_total_usdc,test_total_usdc,test_daily_loss_cap_usdc "
        "from basket_strategy_policy_state where singleton=1"
    ).fetchone()
    assert row == ("weather_basket_selection_execution_v2", "prepared", 1000.0, 100.0, 5.0)
    tables = {
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type='table' and name like 'basket_strategy_%'"
        )
    }
    assert {
        "basket_strategy_position_targets",
        "basket_strategy_parent_orders",
        "basket_strategy_child_orders",
    } <= tables
