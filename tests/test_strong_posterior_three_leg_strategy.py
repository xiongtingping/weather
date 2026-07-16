from strong_posterior_three_leg_strategy import build_strong_posterior_three_leg_strategy


def _evidence():
    return {
        "joint_feature_model_applied": True,
        "top_bucket": "31°C",
        "top_bucket_joint_strong_support": True,
        "safe_no_amsc_observation": True,
        "safe_no_monotonic_daily_high_floor": True,
        "safe_no_observation_source": "zuuu_metar_raw_amsc",
        "safe_no_observed_max_c": 31.0,
        "bucket_exclusions": [
            {
                "bucket": "30°C",
                "excluded": True,
                "kind": "observed_daily_high_exceeded",
                "composer_joint_strong_model": True,
                "confidence": 1.0,
                "settlement_exclusion_certificate_state": "valid",
            }
        ],
    }


def test_main_yes_second_yes_and_least_no_have_requested_caps_and_wave_target():
    result = build_strong_posterior_three_leg_strategy(
        posterior_buckets=[{"bucket":"30°C","probability":0.1,"yes_bid":0.08,"no_bid":0.09},{"bucket":"31°C","probability":0.8,"yes_bid":0.10,"no_bid":0.10},{"bucket":"32°C","probability":0.1,"yes_bid":0.07,"no_bid":0.09}],
        strong_physical_evidence=_evidence(), settlement_window_open=True, available_cash_usdc=100,
    )
    assert result["ok"] is True
    assert [row["strategy_leg"] for row in result["legs"]] == ["top_yes_wave", "second_yes", "strongly_excluded_no"]
    assert result["legs"][0]["target_notional_usdc"] == 20.0
    assert result["legs"][0]["dynamic_wave_exit_target_price"] == 0.90
    assert result["legs"][1]["target_notional_usdc"] == 4.99
    assert result["legs"][2]["target_notional_usdc"] == 100.0
    assert result["capital_policy_id"] == "dynamic_three_leg_capital_policy_v2_top20_second4_99_excluded_no100"
    assert result["legs"][2]["bucket"] == "30°C"
    assert result["legs"][2]["physical_exclusion_kind"] == "observed_daily_high_exceeded"


def test_missing_physical_evidence_or_settlement_window_fails_closed():
    result = build_strong_posterior_three_leg_strategy(
        posterior_buckets=[{"bucket":"30°C","probability":0.2,"yes_bid":0.08,"no_bid":0.08},{"bucket":"31°C","probability":0.8,"yes_bid":0.08,"no_bid":0.08}],
        strong_physical_evidence={}, settlement_window_open=False, available_cash_usdc=10,
    )
    assert result["ok"] is False
    assert "three_leg_composer_strong_physical_evidence_invalid" in result["block_reasons"]


def test_unreachable_upper_bucket_requires_joint_composer_exclusion() -> None:
    evidence = {
        "joint_feature_model_applied": True,
        "top_bucket": "32°C",
        "top_bucket_joint_strong_support": True,
        "bucket_exclusions": [
            {
                "bucket": "34°C",
                "excluded": True,
                "kind": "remaining_warming_unattainable",
                "composer_joint_strong_model": True,
                "confidence": 0.99,
            }
        ],
    }
    result = build_strong_posterior_three_leg_strategy(
        posterior_buckets=[
            {"bucket": "32°C", "probability": 0.7, "yes_bid": 0.20, "no_bid": 0.80},
            {"bucket": "33°C", "probability": 0.2, "yes_bid": 0.15, "no_bid": 0.85},
            {"bucket": "34°C", "probability": 0.1, "yes_bid": 0.10, "no_bid": 0.90},
        ],
        strong_physical_evidence=evidence,
        settlement_window_open=True,
        available_cash_usdc=100,
    )
    no_leg = result["legs"][2]
    assert no_leg["strategy_leg"] == "strongly_excluded_no"
    assert no_leg["bucket"] == "34°C"
    assert no_leg["action"] == "BUY"
    assert no_leg["physical_exclusion_kind"] == "remaining_warming_unattainable"


def test_no_leg_waits_until_settlement_window_and_never_rebuilds_x_to_b() -> None:
    evidence = _evidence()
    result = build_strong_posterior_three_leg_strategy(
        posterior_buckets=[
            {"bucket": "30°C", "probability": 0.1, "yes_bid": 0.1, "no_bid": 0.9},
            {"bucket": "31°C", "probability": 0.8, "yes_bid": 0.2, "no_bid": 0.8},
            {"bucket": "32°C", "probability": 0.1, "yes_bid": 0.1, "no_bid": 0.9},
        ],
        strong_physical_evidence=evidence,
        settlement_window_open=False,
        available_cash_usdc=100,
    )
    no_leg = result["legs"][2]
    assert no_leg["action"] == "HOLD"
    assert no_leg["block_reason"] == "strongly_excluded_no_settlement_window_not_open"
    assert result["boundary"]["physical_x_not_compared_directly_to_market_bucket_b"] is True


def test_top_and_second_yes_are_reselected_from_each_new_posterior() -> None:
    first_evidence = _evidence()
    first = build_strong_posterior_three_leg_strategy(
        posterior_buckets=[
            {"bucket": "31°C", "probability": 0.6, "yes_bid": 0.2, "no_bid": 0.8},
            {"bucket": "32°C", "probability": 0.3, "yes_bid": 0.2, "no_bid": 0.8},
            {"bucket": "33°C", "probability": 0.1, "yes_bid": 0.2, "no_bid": 0.8},
        ], strong_physical_evidence=first_evidence, settlement_window_open=True, available_cash_usdc=100,
    )
    second_evidence = {**_evidence(), "top_bucket": "32°C"}
    second = build_strong_posterior_three_leg_strategy(
        posterior_buckets=[
            {"bucket": "31°C", "probability": 0.2, "yes_bid": 0.2, "no_bid": 0.8},
            {"bucket": "32°C", "probability": 0.7, "yes_bid": 0.2, "no_bid": 0.8},
            {"bucket": "33°C", "probability": 0.1, "yes_bid": 0.2, "no_bid": 0.8},
        ], strong_physical_evidence=second_evidence, settlement_window_open=True, available_cash_usdc=100,
    )
    assert [row["bucket"] for row in first["legs"][:2]] == ["31°C", "32°C"]
    assert [row["bucket"] for row in second["legs"][:2]] == ["32°C", "31°C"]
