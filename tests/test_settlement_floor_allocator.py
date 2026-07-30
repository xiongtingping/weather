from basket_strategy.settlement_floor_allocator import (
    build_role_rebalance_plan,
    build_staged_depth_slice,
    build_settlement_floor_plan,
    load_settlement_floor_policy,
)


def test_role_rebalance_sells_only_secondary_excess_and_recomputes_net_floor():
    result = build_role_rebalance_plan(
        buckets=["37", "38", "39", "40"],
        roles={"37": "F", "38": "M", "39": "S", "40": "F"},
        existing_positions={
            "37": {"shares": 500.0, "cost_usdc": 10.0},
            "38": {"shares": 594.2725, "cost_usdc": 10.0},
            "39": {"shares": 620.5178, "cost_usdc": 10.0},
            "40": {"shares": 500.0, "cost_usdc": 10.0},
        },
        sell_quotes={"39": {"bids": [{"price": 0.40, "size": 1000.0}]}},
    )

    assert result["status"] == "ready"
    assert result["rebalance_required"] is True
    assert result["sell_orders"][0]["role"] == "S"
    assert result["sell_orders"][0]["requested_sell_shares"] == 27.25
    assert result["final_shares_by_bucket"]["39"] == 593.2678
    assert abs(result["realized_proceeds_usdc"] - 10.9) < 1e-9
    assert abs(result["net_settlement_cost_usdc"] - 29.1) < 1e-9
    assert result["role_order_ok"] is True
    assert result["minimum_settlement_profit_usdc"] >= 0.01


def test_role_rebalance_does_not_sell_protected_floor_bucket():
    result = build_role_rebalance_plan(
        buckets=["37", "38", "39"],
        roles={"37": "F", "38": "M", "39": "S"},
        existing_positions={
            "37": {"shares": 200.0, "cost_usdc": 10.0},
            "38": {"shares": 100.0, "cost_usdc": 10.0},
            "39": {"shares": 90.0, "cost_usdc": 10.0},
        },
        sell_quotes={},
    )

    assert result["status"] == "needs_additive_topup"
    assert result["sell_orders"] == []
    assert result["reason_codes"] == ["role_order_requires_buy_not_protected_sell"]


def _quotes(prices=(0.10, 0.11, 0.12, 0.13, 0.14, 0.15)):
    buckets = ["33", "34", "35", "36", "37", "38"]
    return {
        bucket: {"asks": [{"price": price, "size": 1000.0}, {"price": price + 0.01, "size": 1000.0}]}
        for bucket, price in zip(buckets, prices)
    }


def test_complete_200u_plan_includes_lottery_in_floor_and_reports_profit():
    result = build_settlement_floor_plan(
        budget_usdc=200.0,
        buckets=["33", "34", "35", "36", "37", "38"],
        roles={"33": "L", "34": "L", "35": "B", "36": "A", "37": "S", "38": "M"},
        quotes=_quotes(),
        minimum_order_shares=1.0,
    )

    assert result["status"] == "ready"
    rows = {row["bucket"]: row for row in result["target_rows"]}
    assert rows["33"]["new_cost_usdc"] > 0.0
    assert rows["34"]["new_cost_usdc"] > 0.0
    assert rows["33"]["participates_in_settlement_floor"] is True
    assert result["lottery_reserve_usdc"] == 0.0
    assert result["total_cost_usdc"] <= 200.0 + 1e-7
    assert result["minimum_settlement_profit_usdc"] >= 0.01 - 1e-7
    assert result["maximum_settlement_profit_usdc"] >= result["minimum_settlement_profit_usdc"]
    assert rows["38"]["settlement_profit_usdc"] > rows["37"]["settlement_profit_usdc"]
    assert result["core_profit_sources"] == ["M", "S", "A"]
    assert result["profit_order_roles_present"] == ["M", "S", "A", "B"]


def test_missing_role_is_not_created_or_reassigned():
    result = build_settlement_floor_plan(
        budget_usdc=200.0,
        buckets=["36", "37", "38"],
        roles={"36": "M", "37": "S"},
        quotes={bucket: {"yes_ask": 0.20, "yes_ask_size": 1000.0} for bucket in ["36", "37", "38"]},
    )
    assert result["status"] == "ready"
    assert result["unassigned_buckets"] == ["38"]
    assert result["target_cost_by_role"].get("A", 0.0) == 0.0
    assert result["target_cost_by_role"].get("F", 0.0) == 0.0


def test_existing_positions_only_receive_additive_topup_and_floor_is_recomputed():
    result = build_settlement_floor_plan(
        budget_usdc=150.0,
        buckets=["36", "37", "38"],
        roles={"36": "M", "37": "S", "38": "F"},
        quotes={bucket: {"yes_ask": 0.20, "yes_ask_size": 1000.0} for bucket in ["36", "37", "38"]},
        existing_positions={
            "36": {"shares": 20.0, "cost_usdc": 4.0},
            "37": {"shares": 2.0, "cost_usdc": 0.4},
            "38": {"shares": 2.0, "cost_usdc": 0.4},
        },
    )
    assert result["status"] == "ready"
    rows = {row["bucket"]: row for row in result["target_rows"]}
    assert rows["36"]["new_shares"] >= 0.0
    assert rows["37"]["new_shares"] >= 0.0
    assert rows["38"]["new_shares"] >= 0.0
    assert rows["36"]["final_shares"] >= rows["36"]["existing_shares"]
    assert result["minimum_settlement_profit_usdc"] >= 0.01 - 1e-7


def test_real_depth_and_sum_of_best_asks_are_hard_inputs():
    insufficient = build_settlement_floor_plan(
        budget_usdc=200.0,
        buckets=["36", "37", "38", "39"],
        roles={"36": "M", "37": "S", "38": "A", "39": "F"},
        quotes={bucket: {"asks": [{"price": 0.20, "size": 0.01}]} for bucket in ["36", "37", "38", "39"]},
    )
    assert insufficient["status"] == "blocked"
    assert "settlement_floor_depth_insufficient" in insufficient["reason_codes"]

    sum_failed = build_settlement_floor_plan(
        budget_usdc=200.0,
        buckets=["36", "37", "38", "39"],
        roles={"36": "M", "37": "S", "38": "A", "39": "F"},
        quotes={bucket: {"yes_ask": 0.25, "yes_ask_size": 1000.0} for bucket in ["36", "37", "38", "39"]},
    )
    assert sum_failed["status"] == "blocked"
    assert "settlement_floor_best_ask_sum_ge_one" in sum_failed["reason_codes"]


def test_staged_depth_slice_separates_fak_from_gtc_and_reserves_only_pending_cost():
    result = build_staged_depth_slice(
        target_shares=8.0,
        quote={"asks": [{"price": 0.20, "size": 3.0}, {"price": 0.25, "size": 2.0}]},
        minimum_order_shares=1.0,
    )

    assert result["status"] == "pending"
    assert result["immediate_fak_shares"] == 5.0
    assert result["immediate_fak_cost_usdc"] == 1.1
    assert result["immediate_fak_limit_price"] == 0.25
    assert result["pending_gtc_shares"] == 3.0
    assert result["gtc_limit_price"] == 0.25
    assert result["pending_gtc_reserve_usdc"] == 0.75


def test_staged_plan_marks_pending_orders_outside_actual_floor_proof():
    buckets = ["37", "38", "39", "40"]
    quotes = {
        bucket: {"asks": [{"price": price, "size": 2.0}]}
        for bucket, price in zip(buckets, (0.15, 0.16, 0.17, 0.18))
    }
    result = build_settlement_floor_plan(
        budget_usdc=200.0,
        buckets=buckets,
        roles={"37": "M", "38": "S", "39": "A", "40": "F"},
        quotes=quotes,
        allow_staged_depth=True,
        minimum_order_shares=1.0,
        minimum_order_notional_usdc=1.0,
    )

    assert result["status"] == "ready"
    assert result["execution_mode"] == "staged_fak_then_gtc"
    assert result["pending_gtc_shares"] > 0.0
    assert result["pending_gtc_reserve_usdc"] > 0.0
    assert result["pending_orders_count_as_floor"] is False
    assert result["settlement_floor_proof_status"] == "pending_orders_excluded_not_proven"
    assert result["actual_minimum_settlement_profit_usdc"] is not None
    assert result["actual_minimum_settlement_profit_usdc"] != result["projected_minimum_settlement_profit_usdc"]
    assert all(row["pending_gtc_shares"] > 0.0 for row in result["target_rows"])


def test_staged_solver_uses_best_ask_cap_when_visible_tail_is_expensive():
    buckets = ["35", "36", "37", "38"]
    result = build_settlement_floor_plan(
        budget_usdc=200.0,
        buckets=buckets,
        roles={"35": "M", "36": "S", "37": "A", "38": "F"},
        quotes={
            "35": {"asks": [{"price": 0.10, "size": 1.0}, {"price": 0.70, "size": 1.0}]},
            "36": {"asks": [{"price": 0.10, "size": 1.0}, {"price": 0.70, "size": 1.0}]},
            "37": {"asks": [{"price": 0.10, "size": 1.0}, {"price": 0.70, "size": 1.0}]},
            "38": {"asks": [{"price": 0.10, "size": 1.0}, {"price": 0.70, "size": 1.0}]},
        },
        allow_staged_depth=True,
        minimum_order_shares=1.0,
        minimum_order_notional_usdc=1.0,
    )

    assert result["status"] == "ready"
    assert result["execution_mode"] == "staged_fak_then_gtc"
    assert result["pending_gtc_shares"] > 0.0


def test_policy_artifact_records_soft_ratios_and_calculation_only_boundary():
    policy = load_settlement_floor_policy()
    assert policy["policy_id"] == "weather_settlement_floor_biased_basket_v1"
    assert policy["allocation"]["M_to_S_funding_target"] == "2:1"
    assert policy["settlement_floor"]["hard"] is True
    assert policy["allocation"]["ratio_is_hard_gate"] is False
    assert policy["execution_boundary"]["submits_orders"] is False
    staged = policy["execution_boundary"]["manual_execution_modes"]["staged_fak_then_gtc"]
    assert staged["initial_order_type"] == "FAK"
    assert staged["remainder_order_type"] == "GTC"
    assert staged["pending_orders_count_as_floor"] is False
    assert staged["best_ask_sum_gate_for_pending_gtc"] is False
    assert staged["unknown_fill"] == "reconcile_required_stop"


def test_pending_gtc_exception_bypasses_ask_sum_gate_only_in_staged_mode():
    result = build_settlement_floor_plan(
        budget_usdc=30.0,
        buckets=["38", "39"],
        roles={"38": "M", "39": "F"},
        quotes={
            "38": {"asks": [{"price": 0.55, "size": 100.0}]},
            "39": {"asks": [{"price": 0.56, "size": 100.0}]},
        },
        existing_positions={
            "38": {"shares": 100.0, "cost_usdc": 10.0},
            "39": {"shares": 0.0, "cost_usdc": 0.0},
        },
        allow_staged_depth=True,
        allow_pending_only_when_ask_sum_ge_one=True,
        minimum_order_shares=1.0,
        minimum_order_notional_usdc=1.0,
    )

    assert result["status"] == "ready"
    assert result["best_ask_sum_floor_buckets"] > 1.0
    assert result["pending_only_gate_bypassed"] is True


def test_numeric_role_and_position_keys_match_degree_labeled_buckets():
    result = build_settlement_floor_plan(
        budget_usdc=20.0,
        buckets=["36°C", "37°C", "38°C"],
        roles={"36": "A", "37": "M", "38": "F"},
        quotes={bucket: {"yes_ask": 0.10, "yes_ask_size": 1000.0} for bucket in ["36°C", "37°C", "38°C"]},
        existing_positions={"38": {"shares": 2.0, "cost_usdc": 0.2}},
    )
    assert result["status"] == "ready"
    assert result["roles"] == {"36°C": "A", "37°C": "M", "38°C": "F"}
    assert result["target_rows"][-1]["existing_shares"] == 2.0


def test_cost_only_bucket_keeps_historical_cost_outside_floor():
    result = build_settlement_floor_plan(
        budget_usdc=20.0,
        buckets=["36°C", "37°C", "38°C"],
        roles={"36°C": "C", "37°C": "M", "38°C": "F"},
        quotes={bucket: {"yes_ask": 0.20, "yes_ask_size": 1000.0} for bucket in ["36°C", "37°C", "38°C"]},
        existing_positions={
            "36°C": {"shares": 10.0, "cost_usdc": 1.0},
            "37°C": {"shares": 10.0, "cost_usdc": 1.0},
            "38°C": {"shares": 0.0, "cost_usdc": 0.0},
        },
    )

    assert result["status"] == "ready"
    cost_only = next(row for row in result["target_rows"] if row["bucket"] == "36°C")
    assert cost_only["role"] == "C"
    assert cost_only["participates_in_settlement_floor"] is False
    assert cost_only["new_shares"] == 0.0
    assert result["settlement_floor_cost_usdc"] >= 2.0


def test_incremental_budget_allows_topup_when_historical_cost_exceeds_budget():
    result = build_settlement_floor_plan(
        budget_usdc=150.0,
        budget_is_incremental=True,
        buckets=["36", "37", "38"],
        roles={"36": "A", "37": "M", "38": "F"},
        quotes={bucket: {"yes_ask": 0.20, "yes_ask_size": 1000.0} for bucket in ["36", "37", "38"]},
        existing_positions={
            "36": {"shares": 100.0, "cost_usdc": 80.0},
            "37": {"shares": 101.0, "cost_usdc": 80.0},
            "38": {"shares": 1.0, "cost_usdc": 1.0},
        },
    )
    assert result["status"] == "ready"
    assert result["budget_is_incremental"] is True
    assert result["total_incremental_cost_usdc"] <= 150.0 + 1e-7


def test_existing_asymmetric_inventory_is_repaired_additively_in_declared_profit_order():
    result = build_settlement_floor_plan(
        budget_usdc=100.0,
        budget_is_incremental=True,
        buckets=["36", "37", "38", "39"],
        roles={"36": "A", "37": "M", "38": "S", "39": "F"},
        quotes={bucket: {"yes_ask": 0.20, "yes_ask_size": 1000.0} for bucket in ["36", "37", "38", "39"]},
        existing_positions={
            "36": {"shares": 526.0, "cost_usdc": 21.0},
            "37": {"shares": 520.0, "cost_usdc": 78.0},
            "38": {"shares": 394.0, "cost_usdc": 82.0},
            "39": {"shares": 267.0, "cost_usdc": 84.0},
        },
    )
    assert result["status"] == "ready"
    assert result["profit_order_ok"] is True
    rows = {row["bucket"]: row for row in result["target_rows"]}
    assert rows["37"]["new_shares"] > 0.0
    assert rows["38"]["new_shares"] > 0.0
    assert rows["39"]["new_shares"] > 0.0
    assert result["minimum_settlement_profit_usdc"] >= 0.01 - 1e-7


def test_multiple_floor_buckets_constrain_main_role_by_largest_floor_bucket():
    result = build_settlement_floor_plan(
        budget_usdc=60.0,
        budget_is_incremental=True,
        buckets=["38", "39", "40"],
        roles={"38": "F", "39": "M", "40": "F"},
        quotes={
            "38": {"asks": [{"price": 0.20, "size": 1000.0}]},
            "39": {"asks": [{"price": 0.30, "size": 1000.0}]},
            "40": {"asks": [{"price": 0.20, "size": 1000.0}]},
        },
        existing_positions={
            "38": {"shares": 150.0, "cost_usdc": 30.0},
            "39": {"shares": 100.0, "cost_usdc": 30.0},
            "40": {"shares": 0.0, "cost_usdc": 0.0},
        },
    )
    assert result["status"] == "ready"
    rows = {row["bucket"]: row for row in result["target_rows"]}
    assert rows["39"]["final_shares"] >= rows["38"]["final_shares"] + 1.0 - 1e-7
    assert result["profit_order_ok"] is True
