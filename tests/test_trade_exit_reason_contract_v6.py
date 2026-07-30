from trade_exit_reason_contract import (
    MARKET_ALPHA_TAKE_PROFIT_REASON_CODES,
    TARGET_REALIZATION_EXIT_REASON_CODES,
    V6_MARKET_ALPHA_TAKE_PROFIT_REASON_CODES,
    V6_TARGET_REALIZATION_EXIT_REASON_CODES,
    is_executable_exit_reason_code,
    is_v6_executable_exit_reason_code,
    v6_exit_reason_family_for_code,
)


def test_v6_exact_exit_closed_set_is_generation_isolated():
    expected_target = {
        "target_realization_main_yes_upside_risk_stage_1",
        "target_realization_main_yes_upside_risk_stage_2",
        "target_realization_main_yes_upside_risk_stage_3",
        "target_realization_main_yes_upside_risk_stage_4",
        "target_realization_main_yes_invalidated",
    }
    expected_alpha = {
        "market_alpha_take_profit_main_yes_999",
        "market_alpha_take_profit_second_yes_999",
        "market_alpha_take_profit_trajectory_no_999",
        "market_alpha_take_profit_hard_exclusion_no_999",
    }
    assert set(V6_TARGET_REALIZATION_EXIT_REASON_CODES) == expected_target
    assert set(V6_MARKET_ALPHA_TAKE_PROFIT_REASON_CODES) == expected_alpha
    assert not (expected_target & set(TARGET_REALIZATION_EXIT_REASON_CODES))
    assert not (expected_alpha & set(MARKET_ALPHA_TAKE_PROFIT_REASON_CODES))
    assert all(is_v6_executable_exit_reason_code(code) for code in expected_target | expected_alpha)
    assert not is_v6_executable_exit_reason_code("target_realization_main_yes_upside_risk_stage_5")
    assert not is_v6_executable_exit_reason_code("market_alpha_take_profit_top_yes_wave_completed")
    assert not is_executable_exit_reason_code("market_alpha_take_profit_main_yes_999")
    assert v6_exit_reason_family_for_code("market_alpha_take_profit_main_yes_999") == "market_alpha_take_profit"
    assert v6_exit_reason_family_for_code("target_realization_main_yes_invalidated") == "target_realization"

