import os
import unittest

from weather_strategy_runtime import apply_weather_strategy_profile


class WeatherStrategyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = {
            key: os.environ.get(key)
            for key in (
                "PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED",
                "PM_WEATHER_STRATEGY_STAGE_SIZE_ENABLED",
                "PM_WEATHER_STRATEGY_VARIANCE_SKIP_ENABLED",
                "PM_WEATHER_STRATEGY_VARIANCE_SKIP_SIGMA_MAX",
            )
        }
        os.environ["PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED"] = "0"
        os.environ["PM_WEATHER_STRATEGY_STAGE_SIZE_ENABLED"] = "0"
        os.environ["PM_WEATHER_STRATEGY_VARIANCE_SKIP_ENABLED"] = "1"
        os.environ["PM_WEATHER_STRATEGY_VARIANCE_SKIP_SIGMA_MAX"] = "0.08"

    def tearDown(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_high_confidence_consensus_uses_positive_ev_without_price_cap(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.62,
            "q_quote": 0.69,
            "edge_tradeability_profit_value": 0.018,
            "edge_tradeability_profit_min": 0.0,
            "edge_tradeability_pass": True,
            "lcb_consensus_yes_top_auto_label": "25C",
            "lcb_consensus_yes_top_market_label": "25C",
            "lcb_consensus_yes_top_model_label": "25C",
            "lcb_consensus_yes_top_model": "ecmwf_ifs025",
            "lcb_consensus_yes_top_auto_q": 0.44,
            "lcb_consensus_yes_top_auto_second_q": 0.30,
            "priority": 1000,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertEqual(profile["weather_strategy_kind"], "high_confidence_consensus")
        self.assertTrue(profile["weather_strategy_positive_ev"])
        self.assertTrue(profile["weather_strategy_ev_gate_pass"])
        self.assertAlmostEqual(float(profile["weather_strategy_price"]), 0.62, places=9)
        self.assertEqual(candidate["strategy_variant"], "posterior_settlement")
        self.assertEqual(candidate["priority"], 1200)

    def test_consensus_negative_tradeability_ev_is_not_positive(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.40,
            "q_quote": 0.46,
            "edge_tradeability_profit_value": -0.002,
            "edge_tradeability_profit_min": 0.0,
            "edge_tradeability_pass": False,
            "lcb_consensus_yes_top_auto_label": "25C",
            "lcb_consensus_yes_top_market_label": "25C",
            "lcb_consensus_yes_top_model_label": "25C",
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertFalse(profile["weather_strategy_positive_ev"])
        self.assertFalse(profile["weather_strategy_ev_gate_pass"])
        self.assertNotEqual(profile["weather_strategy_kind"], "high_confidence_consensus")

    def test_adjacent_bucket_risk_scales_size_once(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.28,
            "q_quote": 0.34,
            "edge_tradeability_profit_value": 0.012,
            "edge_tradeability_profit_min": 0.0,
            "edge_tradeability_pass": True,
            "lcb_consensus_yes_top_auto_label": "24C",
            "lcb_consensus_yes_top_market_label": "24C",
            "lcb_consensus_yes_top_model_label": "24C",
            "lcb_consensus_yes_top_auto_q": 0.36,
            "lcb_consensus_yes_top_auto_second_q": 0.33,
            "strategy_size_ratio_extra": 1.0,
        }

        apply_weather_strategy_profile(candidate)
        apply_weather_strategy_profile(candidate)

        self.assertTrue(candidate["weather_strategy_adjacent_risk"])
        self.assertTrue(candidate["weather_strategy_size_ratio_applied"])
        self.assertAlmostEqual(float(candidate["strategy_size_ratio_extra"]), 0.0, places=9)

    def test_untrusted_settlement_source_blocks_rules_ok(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.31,
            "q_quote": 0.40,
            "edge_tradeability_profit_value": 0.02,
            "edge_tradeability_profit_min": 0.0,
            "edge_tradeability_pass": True,
            "rules_contract_ok": True,
            "source_kind": "wunderground_aux",
            "source_usable_for_settlement": False,
            "source_na_reason": "wu_non_history_source",
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertFalse(profile["weather_strategy_rules_ok"])
        self.assertEqual(profile["weather_strategy_rules_na_reason"], "wu_non_history_source")
        self.assertFalse(profile["weather_strategy_source_usable_for_settlement"])
        self.assertNotEqual(profile["weather_strategy_kind"], "high_confidence_consensus")

    def test_station_mismatch_blocks_settlement_eligibility(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.31,
            "q_quote": 0.46,
            "edge_tradeability_profit_value": 0.12,
            "edge_tradeability_pass": True,
            "rules_contract_ok": True,
            "settlement_station": "ZBAA",
            "source_kind": "wunderground_history",
            "source_fit_tier": "station_mismatch",
            "source_usable_for_settlement": False,
            "source_station_id": "ZSPD",
            "source_station_expected": "ZBAA",
            "source_station_match": False,
            "source_station_match_reason": "expected_station_does_not_match_resolution_source",
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertFalse(profile["weather_strategy_rules_ok"])
        self.assertFalse(profile["weather_strategy_settlement_eligible"])
        self.assertEqual(
            profile["weather_strategy_rules_na_reason"],
            "expected_station_does_not_match_resolution_source",
        )
        self.assertFalse(profile["weather_strategy_source_station_match"])

    def test_profile_accepts_weather_strategy_aliases_from_ssot_snapshot(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "price": 0.27,
            "q_entry": 0.79,
            "edge_tradeability_profit_value": 0.20,
            "edge_tradeability_profit_min": 0.0,
            "edge_tradeability_pass": True,
            "weather_strategy_rules_ok": True,
            "weather_strategy_settlement_station": "MMMX",
            "weather_strategy_source_kind": "wunderground_history",
            "weather_strategy_source_confidence": 0.95,
            "weather_strategy_source_fit_tier": "settlement_source",
            "weather_strategy_source_usable_for_settlement": True,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertTrue(profile["weather_strategy_rules_ok"])
        self.assertEqual(profile["weather_strategy_settlement_station"], "MMMX")
        self.assertEqual(profile["source_fit_tier"], "settlement_source")
        self.assertEqual(profile["source_trust_tier"], "settlement_source")
        self.assertEqual(profile["weather_strategy_source_kind"], "wunderground_history")
        self.assertEqual(profile["weather_strategy_source_fit_tier"], "settlement_source")
        self.assertEqual(profile["weather_strategy_source_trust_tier"], "settlement_source")
        self.assertTrue(profile["weather_strategy_source_usable_for_settlement"])

    def test_repricing_signal_combines_edge_liquidity_and_observation(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.24,
            "q_quote": 0.31,
            "edge_tradeability_profit_value": 0.015,
            "edge_tradeability_profit_min": 0.0,
            "edge_tradeability_pass": True,
            "spread": 0.04,
            "rules_contract_ok": True,
            "source_kind": "wunderground_history",
            "source_confidence": 0.95,
            "source_usable_for_settlement": True,
            "observed_nowcast_value": 73.0,
            "observed_nowcast_source": "wunderground_history",
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertEqual(profile["weather_strategy_kind"], "repricing_maker")
        self.assertGreater(float(profile["weather_strategy_repricing_signal"]), 0.015)

    def test_near_zero_yes_ask_is_blocked_as_dead_bucket(self) -> None:
        os.environ["PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED"] = "1"
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.004,
            "q_quote": 0.95,
            "edge_tradeability_profit_value": 0.30,
            "edge_tradeability_pass": True,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertFalse(profile["weather_strategy_entry_allowed"])
        self.assertEqual(
            profile["weather_strategy_entry_block_reason"], "entry_ask_too_low_for_entry"
        )
    
    def test_near_zero_no_ask_is_blocked_as_dead_bucket(self) -> None:
        os.environ["PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED"] = "1"
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "price": 0.004,
            "q_entry": 0.84,
            "edge_tradeability_profit_value": 0.13,
            "edge_tradeability_pass": True,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertFalse(profile["weather_strategy_entry_allowed"])
        self.assertEqual(
            profile["weather_strategy_entry_block_reason"], "entry_ask_too_low_for_entry"
        )

    def test_bayes_q_takes_precedence_over_static_q_for_raw_ev(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.35,
            "q_entry": 0.72,
            "q_quote": 0.69,
            "edge_tradeability_bayes_q": 0.28,
            "edge_tradeability_profit_value": -0.004,
            "edge_tradeability_profit_min": 0.0,
            "edge_tradeability_pass": True,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertAlmostEqual(float(profile["weather_strategy_raw_ev"]), -0.07, places=6)
        self.assertFalse(profile["weather_strategy_positive_ev"])

    def test_context_falls_back_to_bayes_side_key_when_missing_generic(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "price": 0.22,
            "q_quote": 0.30,
            "q_entry": 0.55,
            "edge_tradeability_profit_value": 0.012,
            "edge_tradeability_profit_min": 0.0,
            "edge_tradeability_pass": True,
        }
        context = {"edge_tradeability_bayes_q_no": 0.88}

        profile = apply_weather_strategy_profile(candidate, context=context)

        self.assertAlmostEqual(float(profile["weather_strategy_raw_ev"]), 0.66, places=6)
        self.assertTrue(profile["weather_strategy_positive_ev"])

    def test_exit_plan_does_not_fallback_to_exec_edge_exit(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.30,
            "q_quote": 0.38,
            "edge_tradeability_profit_value": 0.02,
            "edge_tradeability_profit_min": 0.0,
            "edge_tradeability_pass": True,
            "exec_exit_price": 0.35,
            "exec_edge_exit": 0.08,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertEqual(
            profile["weather_strategy_exit_plan"],
            "hold_to_settlement",
        )
        self.assertIsNone(profile["weather_strategy_target_exit_price"])

    def test_stage_mix_marks_d2_as_edge_dominant(self) -> None:
        os.environ["PM_WEATHER_STRATEGY_STAGE_SIZE_ENABLED"] = "1"
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.30,
            "q_quote": 0.45,
            "edge_tradeability_profit_value": 0.12,
            "edge_tradeability_pass": True,
            "days_to_event": 2,
            "strategy_size_ratio_extra": 1.0,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertEqual(profile["weather_strategy_stage"], "d2_plus")
        self.assertEqual(profile["weather_strategy_mode"], "edge_dominant")
        self.assertAlmostEqual(float(profile["weather_strategy_settlement_weight"]), 0.25, places=9)
        self.assertEqual(profile["strategy_family"], "posterior_settlement")
        self.assertAlmostEqual(float(candidate["strategy_size_ratio_extra"]), 0.0, places=9)

    def test_d0_locked_without_settlement_source_blocks_new_edge(self) -> None:
        os.environ["PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED"] = "1"
        os.environ["PM_WEATHER_STRATEGY_STAGE_SIZE_ENABLED"] = "1"
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.30,
            "q_quote": 0.45,
            "edge_tradeability_profit_value": 0.14,
            "edge_tradeability_pass": True,
            "q_bayes_z_stage": "d0_afternoon",
            "strategy_size_ratio_extra": 1.0,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertEqual(profile["weather_strategy_mode"], "settlement_only")
        self.assertFalse(profile["weather_strategy_settlement_eligible"])
        self.assertFalse(profile["weather_strategy_entry_allowed"])
        self.assertEqual(
            profile["weather_strategy_entry_block_reason"],
            "d0_locked_no_new_entry_without_settlement_source",
        )
        self.assertAlmostEqual(float(candidate["strategy_size_ratio_extra"]), 0.0, places=9)

    def test_tail_no_positive_edge_is_reverse_edge_strategy(self) -> None:
        os.environ["PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED"] = "1"
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "outcome": "38C or higher",
            "price": 0.18,
            "q_entry": 0.84,
            "edge_tradeability_profit_value": 0.13,
            "edge_tradeability_pass": True,
            "days_to_event": 1,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertEqual(profile["weather_strategy_kind"], "tail_no_mispricing")
        self.assertTrue(profile["weather_strategy_reverse_edge"])
        self.assertTrue(profile["weather_strategy_market_overpriced_yes"])
        self.assertEqual(
            profile["weather_strategy_reverse_edge_action"],
            "buy_no_equivalent_to_sell_yes",
        )

    def test_thin_edge_is_shadow_only_under_strict_threshold(self) -> None:
        os.environ["PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED"] = "1"
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.30,
            "q_quote": 0.34,
            "edge_tradeability_profit_value": 0.03,
            "edge_tradeability_pass": True,
            "days_to_event": 1,
            "strategy_size_ratio_extra": 1.0,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertEqual(profile["weather_strategy_edge_tier"], "diagnostic_only_not_entry_gate")
        self.assertFalse(profile["weather_strategy_entry_allowed"])
        self.assertEqual(profile["weather_strategy_entry_block_reason"], "target_bucket_missing")
        self.assertAlmostEqual(float(candidate["strategy_size_ratio_extra"]), 0.0, places=9)

    def test_exec_edge_entry_fallback_prevents_stale_shadow_tier(self) -> None:
        os.environ["PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED"] = "1"
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.03,
            "q_entry": 0.17,
            "exec_edge_entry": 0.13,
            "edge_tradeability_pass": True,
            "q_bayes_z_stage": "d0_morning",
            "rules_contract_ok": True,
            "source_usable_for_settlement": True,
            "settlement_station": "KMIA",
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertEqual(profile["weather_strategy_edge_tier"], "diagnostic_only_not_entry_gate")
        self.assertEqual(profile["weather_strategy_edge_tier_reason"], "edge_lcb_meets_strict_min")
        self.assertFalse(profile["weather_strategy_entry_allowed"])
        self.assertEqual(profile["weather_strategy_entry_block_reason"], "target_bucket_missing")

    def test_target_realization_hold_allows_initial_entry_without_active_yes(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "outcome": "33°C",
            "target_bucket": "33°C",
            "target_posterior": 0.42,
            "target_realization_reliability": 0.72,
            "target_adjustment_action": "HOLD",
            "target_adjustment_triggered": False,
            "has_active_yes_position": False,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertTrue(profile["posterior_settlement_entry_allowed"])
        self.assertTrue(profile["weather_strategy_entry_allowed"])
        self.assertEqual(profile["weather_strategy_entry_style"], "settlement_yes_target")

    def test_target_realization_hold_blocks_duplicate_active_yes_add(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "outcome": "33°C",
            "target_bucket": "33°C",
            "target_posterior": 0.42,
            "target_realization_reliability": 0.72,
            "target_adjustment_action": "HOLD",
            "target_adjustment_triggered": False,
            "has_active_yes_position": True,
            "held_target_bucket": "33°C",
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertTrue(profile["posterior_settlement_entry_allowed"])
        self.assertFalse(profile["weather_strategy_entry_allowed"])
        self.assertEqual(profile["weather_strategy_entry_block_reason"], "target_adjustment_hold_no_rebalance")

    def test_target_realization_switch_buy_allows_new_target_entry(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "outcome": "34°C",
            "target_bucket": "34°C",
            "target_posterior": 0.44,
            "target_realization_reliability": 0.74,
            "target_adjustment_action": "SWITCH_BUY",
            "target_adjustment_triggered": True,
            "switch_buy_allowed": True,
            "has_active_yes_position": True,
            "held_target_bucket": "33°C",
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertTrue(profile["posterior_settlement_entry_allowed"])
        self.assertTrue(profile["weather_strategy_entry_allowed"])

    def test_autumn_market_lowers_size(self) -> None:
        os.environ["PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED"] = "1"
        os.environ["PM_WEATHER_STRATEGY_STAGE_SIZE_ENABLED"] = "0"
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.30,
            "q_quote": 0.45,
            "edge_tradeability_profit_value": 0.12,
            "edge_tradeability_pass": True,
            "event_date": "2026-10-12",
            "strategy_size_ratio_extra": 1.0,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertEqual(profile["weather_strategy_season"], "autumn")
        self.assertEqual(profile["weather_strategy_season_size_reason"], "autumn_lower_size")
        self.assertAlmostEqual(float(candidate["strategy_size_ratio_extra"]), 0.0, places=9)

    def test_high_bayes_sigma_blocks_entry_as_variance_skip(self) -> None:
        os.environ["PM_WEATHER_STRATEGY_STRICT_EDGE_ENABLED"] = "1"
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.30,
            "q_quote": 0.48,
            "q_bayes_sigma": 0.09,
            "edge_tradeability_profit_value": 0.14,
            "edge_tradeability_pass": True,
            "days_to_event": 1,
            "strategy_size_ratio_extra": 1.0,
        }

        profile = apply_weather_strategy_profile(candidate)

        self.assertTrue(profile["weather_strategy_variance_skip"])
        self.assertEqual(profile["weather_strategy_variance_skip_reason"], "q_sigma_high")
        self.assertFalse(profile["weather_strategy_entry_allowed"])
        self.assertEqual(profile["weather_strategy_entry_block_reason"], "variance_high")
        self.assertAlmostEqual(float(candidate["strategy_size_ratio_extra"]), 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
