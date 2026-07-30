from basket_strategy.policy import (
    allocate_profit_weighted,
    bucket_roles,
    compute_macro_topup,
    load_policy,
    split_micro_parent_order,
    validate_window_roles,
)


def _quotes(width=4):
    return [
        {"bucket": f"{34 + i}°C", "bucket_c": 34 + i, "yes_ask": 0.10, "yes_ask_size": 100.0}
        for i in range(width)
    ]


def test_fixed_roles_and_move_window_rule():
    assert bucket_roles(4) == ["non_core_edge_left", "primary_core", "secondary_core", "non_core_edge_right"]
    roles = allocate_profit_weighted(_quotes(), budget_usdc=10.0)["roles"]
    assert roles[0]["core"] is False
    assert roles[1]["core"] is True
    assert roles[2]["core"] is True
    assert roles[3]["core"] is False
    assert validate_window_roles(_quotes(), {34: 0.01, 35: 0.10, 36: 0.20, 37: 0.01}) == [
        "primary_core_probability_below_secondary_move_window"
    ]


def test_four_bucket_profit_allocation_is_nonnegative_and_main_secondary_two_to_one():
    result = allocate_profit_weighted(_quotes(), budget_usdc=10.0)
    assert result["status"] == "ready"
    profits = result["settlement_profit_by_bucket"]
    assert min(profits) > 0
    assert abs(profits[1] / profits[2] - 2.0) < 1e-6
    assert result["total_cost_usdc"] <= 10.0 + 1e-9


def test_macro_topup_is_additive_and_micro_does_not_recompute_target():
    macro = compute_macro_topup(
        target_quantities={"35°C": 10.0, "36°C": 5.0},
        filled_quantities={"35°C": 4.0},
        pending_quantities={"35°C": 2.0},
        available_budget_usdc=2.0,
        ask_by_bucket={"35°C": 0.10, "36°C": 0.10},
    )
    assert macro["delta_quantities"]["35°C"] == 4.0
    assert macro["delta_quantities"]["36°C"] == 5.0
    micro = split_micro_parent_order(
        bucket="35°C",
        quantity=6.0,
        asks=[{"price": 0.10, "size": 3.0}, {"price": 0.11, "size": 3.0}],
        child_max_notional_usdc=1.0,
    )
    assert micro["status"] == "ready"
    assert micro["target_recomputed"] is False
    assert sum(item["quantity"] for item in micro["child_orders"]) == 6.0


def test_policy_artifact_is_content_addressed():
    policy = load_policy()
    assert policy["policy_id"] == "weather_basket_selection_execution_v2"
    assert len(policy["policy_sha256"]) == 64
    assert policy["risk_budget"]["production_total_usdc"] == 1000.0
    assert policy["risk_budget"]["test_total_usdc"] == 100.0
