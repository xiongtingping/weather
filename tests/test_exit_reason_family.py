import unittest

import scan_weather_market as swm


class ExitReasonFamilyTests(unittest.TestCase):
    def test_legacy_exit_families_are_retired(self) -> None:
        self.assertEqual(
            swm._normalize_exit_reason_family_codes(
                [
                    "edge_min",
                    "hold_edge_realizable",
                    "lcb_q_peak_switch",
                    "lag",
                    "stop_loss",
                    "trail_stop",
                    "weather_repricing_target",
                ]
            ),
            [],
        )

    def test_target_realization_exit_family(self) -> None:
        self.assertEqual(
            swm._normalize_exit_reason_family_codes(
                ["target_realization_yes_target_invalidated"]
            ),
            ["target_realization"],
        )

    def test_prefix_spoof_and_retired_exit_codes_are_not_authorized(self) -> None:
        for code in (
            "target_realization_market_alpha_take_profit",
            "target_realization_switch_buy_exit_old",
            "target_realization_rebalance_over_target",
            "target_realization_no_capital_recycle",
            "weather_repricing_target",
        ):
            self.assertEqual(swm._exit_reason_family_for_code(code), "")
            self.assertEqual(
                swm._exit_order_policy_block_reason(
                    [code],
                    details={"exit_reason_family": "target_realization"},
                ),
                swm.EXIT_ORDER_POLICY_LEGACY_BLOCK_REASON,
            )

    def test_exit_authorization_requires_exact_single_declared_family(self) -> None:
        code = "target_realization_yes_target_invalidated"
        self.assertEqual(
            swm._exit_order_policy_block_reason([code], details={}),
            swm.EXIT_ORDER_POLICY_LEGACY_BLOCK_REASON,
        )
        self.assertEqual(
            swm._exit_order_policy_block_reason(
                [code],
                details={
                    "exit_reason_family_codes": [
                        "target_realization",
                        "market_alpha_take_profit",
                    ]
                },
            ),
            swm.EXIT_ORDER_POLICY_LEGACY_BLOCK_REASON,
        )
        self.assertEqual(
            swm._exit_order_policy_block_reason(
                [code],
                details={"exit_reason_family": "target_realization"},
            ),
            "",
        )

    def test_market_alpha_is_accepted_but_runtime_default_denied(self) -> None:
        code = "market_alpha_take_profit_top_yes_wave_completed"
        self.assertEqual(
            swm._exit_reason_family_for_code(code),
            "market_alpha_take_profit",
        )
        self.assertEqual(
            swm._exit_order_policy_block_reason(
                [code],
                details={"exit_reason_family": "market_alpha_take_profit"},
            ),
            swm.EXIT_ORDER_POLICY_LEGACY_BLOCK_REASON,
        )

    def test_mixed_exit_families_keep_only_bayes_triggers(self) -> None:
        self.assertEqual(
            swm._normalize_exit_reason_family_codes(
                [
                    "edge_min",
                    "target_realization_yes_target_invalidated",
                    "stop_loss",
                    "target_realization_no_invalidated",
                    "weather_repricing_target",
                ]
            ),
            ["target_realization"],
        )

    def test_operational_exit_codes_do_not_become_strategy_families(self) -> None:
        self.assertEqual(
            swm._normalize_exit_reason_family_codes(
                [
                    "hold",
                    "exit_holdoff",
                    "exit_wait_fill",
                    "exit_no_trigger",
                    "mirror_wait_source_exit",
                    "order_submitted",
                    "synced_exit_fill_without_decision",
                ]
            ),
            [],
        )

    def test_exit_trigger_codes_filter_operational_and_legacy_details_codes(self) -> None:
        self.assertEqual(
            swm._exit_trigger_reason_codes_from_decision(
                {
                    "exit_reason_codes": [
                        "hold",
                        "exit_wait_fill",
                        "edge_min",
                        "target_realization_yes_target_invalidated",
                    ]
                },
                decision_reason_codes=["exit_no_trigger"],
            ),
            ["target_realization_yes_target_invalidated"],
        )

    def test_synced_exit_fill_is_operational_not_strategy_family(self) -> None:
        details = swm._finalize_exit_decision_details(
            {"hold_edge_realizable": 0.04},
            decision_reason_codes=["synced_exit_fill_without_decision"],
        )

        self.assertEqual(details["exit_policy_primary_source"], "target_realization_binary_settlement")
        self.assertEqual(details["exit_edge_primary_source"], "")
        self.assertNotIn("exit_reason_family_codes", details)
        self.assertNotIn("exit_reason_primary_family", details)
        self.assertEqual(
            details["exit_operational_reason_codes"],
            ["synced_exit_fill_without_decision"],
        )
        self.assertFalse(details["exit_policy_strategy_triggered"])
        self.assertFalse(details["exit_policy_operational_blocked"])

    def test_target_realization_plan_context_copies_to_exit_details(self) -> None:
        plan = {
            "ok": True,
            "version": "target_realization_capital_plan_v2",
            "target_bucket": "29°C",
            "target_posterior": 0.3963,
            "second_target_bucket": "31°C",
            "second_target_posterior": 0.2196,
            "buy_new_leg": {"bucket": "29°C", "side": "YES"},
            "sell_old_buy_new_legs": [
                {"bucket": "30°C", "side": "YES", "execution_action": "SELL"},
                {"bucket": "29°C", "side": "YES", "execution_action": "BUY"},
            ],
        }
        payload = {
            "temperature_path_distribution": {
                "target_bucket": "29°C",
                "target_bucket_path_probability": 0.4411,
            },
            "target_switch_context": {
                "target_bucket": "29°C",
                "candidate_bucket": "31°C",
                "trajectory_momentum_impact": {
                    "label": "动量支持上切",
                    "target_path_probability_pct": 44.11,
                    "candidate_path_probability_pct": 6.66,
                },
            },
        }

        merged_plan = swm._merge_target_realization_plan_ssot_context(dict(plan), payload)
        details = swm._copy_target_realization_plan_context_to_details(
            {"decision_ssot": "auto_trade_target_bucket"},
            merged_plan,
        )

        self.assertEqual(details["target_bucket"], "29°C")
        self.assertEqual(details["ssot_target_bucket"], "29°C")
        self.assertEqual(details["capital_plan_target_bucket"], "29°C")
        self.assertEqual(details["target_realization_capital_plan"]["target_bucket"], "29°C")
        self.assertEqual(details["target_switch_context"]["candidate_bucket"], "31°C")
        self.assertEqual(details["trajectory_momentum_impact"]["label"], "动量支持上切")
        self.assertEqual(details["temperature_path_distribution"]["target_bucket"], "29°C")
        self.assertEqual(details["buy_new_leg"]["bucket"], "29°C")
        self.assertEqual(len(details["sell_old_buy_new_legs"]), 2)

    def test_dump_exit_reason_family_codes_for_edge_trades(self) -> None:
        self.assertEqual(
            swm._dump_exit_reason_family_codes('["stop_loss", "trail_stop"]'),
            None,
        )
        self.assertEqual(
            swm._dump_exit_reason_family_codes(
                '["edge_min", "target_realization_no_invalidated"]'
            ),
            '["target_realization"]',
        )

    def test_mixed_operational_and_strategy_codes_only_family_bayes_triggers(self) -> None:
        self.assertEqual(
            swm._normalize_exit_reason_family_codes(
                [
                    "exit_wait_fill",
                    "stop_loss",
                    "order_submitted",
                    "edge_min",
                    "target_realization_yes_target_invalidated",
                ]
            ),
            ["target_realization"],
        )

    def test_exit_trigger_codes_prefer_bayes_details_codes(self) -> None:
        self.assertEqual(
            swm._exit_trigger_reason_codes_from_decision(
                {
                    "exit_reason_codes": [
                        "weather_repricing_target",
                        "target_realization_no_invalidated",
                    ]
                },
                decision_reason_codes=["exit_no_trigger"],
            ),
            ["target_realization_no_invalidated"],
        )

    def test_exit_trigger_codes_ignore_operational_decision_codes(self) -> None:
        self.assertEqual(
            swm._exit_trigger_reason_codes_from_decision(
                {},
                decision_reason_codes=[
                    "exit_no_trigger",
                    "exit_quote_stale",
                    "mirror_wait_source_exit",
                    "order_submitted",
                    "order_already_live",
                    "place_order_failed",
                    "exit_marketable_immediate_confirm_pending",
                ],
            ),
            [],
        )

    def test_exit_trigger_codes_can_fallback_to_bayes_decision_codes(self) -> None:
        self.assertEqual(
            swm._exit_trigger_reason_codes_from_decision(
                {},
                decision_reason_codes=[
                    "edge_min",
                    "stop_loss",
                    "target_realization_yes_target_invalidated",
                ],
            ),
            ["target_realization_yes_target_invalidated"],
        )

    def test_exit_decision_details_use_bayes_primary_source(self) -> None:
        details = swm._finalize_exit_decision_details(
            {
                "hold_edge_realizable": -0.03,
                "hold_edge_realizable_trigger": -0.031,
                "prev_hold_edge_realizable": -0.028,
                "exit_hold_edge_realizable_min": 0.02,
                "exit_reason_codes": [
                    "edge_min",
                    "target_realization_yes_target_invalidated",
                    "weather_repricing_target",
                ],
            },
            decision_reason_codes=["order_submitted"],
        )

        self.assertEqual(details["exit_edge_primary_source"], "")
        self.assertEqual(details["exit_policy_primary_edge_source"], "")
        self.assertEqual(
            details["exit_policy_rule"],
            "target_realization_binary_settlement_plus_risk_constraints",
        )
        self.assertEqual(
            details["exit_original_reason_codes"],
            ["target_realization_yes_target_invalidated"],
        )
        self.assertEqual(details["exit_reason_family_codes"], ["target_realization"])
        self.assertEqual(details["exit_reason_primary_family"], "target_realization")
        self.assertEqual(details["exit_operational_reason_codes"], ["order_submitted"])
        self.assertEqual(
            details["exit_policy_trigger_reason_codes"],
            ["target_realization_yes_target_invalidated"],
        )
        self.assertEqual(details["exit_policy_family_codes"], ["target_realization"])
        self.assertEqual(details["exit_policy_primary_family"], "target_realization")
        self.assertEqual(details["exit_policy_operational_reason_codes"], ["order_submitted"])
        self.assertAlmostEqual(details["exit_policy_legacy_edge_value"], -0.03)
        self.assertAlmostEqual(details["exit_policy_legacy_edge_trigger_value"], -0.031)
        self.assertAlmostEqual(details["exit_policy_legacy_prev_edge_trigger_value"], -0.028)
        self.assertAlmostEqual(details["exit_policy_legacy_edge_min"], 0.02)
        self.assertIsNone(details["exit_policy_edge_min"])
        self.assertFalse(details["exit_policy_edge_triggered"])
        self.assertFalse(details["exit_policy_repricing_triggered"])
        self.assertFalse(details["exit_policy_risk_triggered"])
        self.assertTrue(details["exit_policy_target_realization_triggered"])
        self.assertTrue(details["exit_policy_strategy_triggered"])
        self.assertTrue(details["exit_policy_order_submitted"])
        self.assertFalse(details["exit_policy_operational_blocked"])

    def test_exit_decision_details_do_not_family_operational_codes(self) -> None:
        details = swm._finalize_exit_decision_details(
            {"hold_edge_realizable": 0.04},
            decision_reason_codes=["order_submitted", "exit_marketable_retry_submitted"],
        )

        self.assertEqual(details["exit_edge_primary_source"], "")
        self.assertEqual(details["exit_policy_primary_edge_source"], "")
        self.assertEqual(
            details["exit_operational_reason_codes"],
            ["order_submitted", "exit_marketable_retry_submitted"],
        )
        self.assertNotIn("exit_reason_family_codes", details)
        self.assertNotIn("exit_reason_primary_family", details)
        self.assertNotIn("exit_original_reason_codes", details)
        self.assertFalse(details["exit_policy_strategy_triggered"])
        self.assertFalse(details["exit_policy_operational_blocked"])
        self.assertTrue(details["exit_policy_order_submitted"])
        self.assertTrue(details["exit_policy_marketable_submitted"])

    def test_hard_zero_dead_market_is_explicit_operational_block_code(self) -> None:
        details = swm._finalize_exit_decision_details(
            {"hold_edge_realizable": -0.01},
            decision_reason_codes=["target_realization_hard_zero_dead_market"],
        )

        self.assertEqual(
            details["exit_operational_reason_codes"],
            ["target_realization_hard_zero_dead_market"],
        )
        self.assertTrue(details["exit_policy_operational_blocked"])
        self.assertEqual(
            details["exit_policy_operational_block_codes"],
            ["target_realization_hard_zero_dead_market"],
        )
        self.assertNotIn("exit_reason_family_codes", details)

    def test_exit_decision_details_ignore_legacy_decision_trigger_codes(self) -> None:
        details = swm._finalize_exit_decision_details(
            {"hold_edge_realizable": -0.02},
            decision_reason_codes=["stop_loss", "exit_wait_fill"],
        )

        self.assertNotIn("exit_original_reason_codes", details)
        self.assertNotIn("exit_reason_family_codes", details)
        self.assertNotIn("exit_reason_primary_family", details)
        self.assertEqual(details["exit_operational_reason_codes"], ["exit_wait_fill"])
        self.assertFalse(details["exit_policy_risk_triggered"])
        self.assertFalse(details["exit_policy_strategy_triggered"])
        self.assertTrue(details["exit_policy_operational_blocked"])
        self.assertEqual(details["exit_policy_operational_block_codes"], ["exit_wait_fill"])

    def test_exit_policy_audit_marks_operational_no_trigger_without_family(self) -> None:
        details = swm._finalize_exit_decision_details(
            {"hold_edge_realizable": 0.05},
            decision_reason_codes=["exit_no_trigger"],
        )

        self.assertNotIn("exit_reason_family_codes", details)
        self.assertTrue(details["exit_policy_no_trigger"])
        self.assertTrue(details["exit_policy_operational_blocked"])
        self.assertFalse(details["exit_policy_strategy_triggered"])
        self.assertEqual(details["exit_policy_operational_block_codes"], ["exit_no_trigger"])

    def test_exit_policy_audit_marks_hold_as_operational_wait(self) -> None:
        details = swm._finalize_exit_decision_details(
            {"hold_edge_realizable": 0.05, "exit_reason_codes": ["hold"]},
            decision_reason_codes=["exit_holdoff"],
        )

        self.assertNotIn("exit_original_reason_codes", details)
        self.assertNotIn("exit_reason_family_codes", details)
        self.assertEqual(details["exit_operational_reason_codes"], ["exit_holdoff"])
        self.assertTrue(details["exit_policy_operational_blocked"])
        self.assertFalse(details["exit_policy_strategy_triggered"])
        self.assertEqual(details["exit_policy_operational_block_codes"], ["exit_holdoff"])

    def test_exit_policy_audit_marks_mirror_wait_as_operational(self) -> None:
        details = swm._finalize_exit_decision_details(
            {"hold_edge_realizable": 0.05, "strategy": "mirror_follow"},
            decision_reason_codes=["mirror_wait_source_exit"],
        )

        self.assertNotIn("exit_reason_family_codes", details)
        self.assertNotIn("exit_reason_primary_family", details)
        self.assertEqual(
            details["exit_operational_reason_codes"],
            ["mirror_wait_source_exit"],
        )
        self.assertEqual(
            details["exit_policy_operational_block_codes"],
            ["mirror_wait_source_exit"],
        )
        self.assertTrue(details["exit_policy_operational_blocked"])
        self.assertFalse(details["exit_policy_strategy_triggered"])

    def test_exit_policy_audit_does_not_infer_edge_trigger_from_hold_edge(self) -> None:
        details = swm._finalize_exit_decision_details(
            {
                "hold_edge_realizable": -0.01,
                "hold_edge_realizable_trigger": 0.005,
                "prev_hold_edge_realizable": 0.01,
                "exit_hold_edge_realizable_min": 0.02,
            },
            decision_reason_codes=[],
        )

        self.assertNotIn("exit_original_reason_codes", details)
        self.assertNotIn("exit_reason_family_codes", details)
        self.assertTrue(details["exit_policy_legacy_edge_below_min"])
        self.assertTrue(details["exit_policy_legacy_edge_trigger_below_min"])
        self.assertTrue(details["exit_policy_legacy_prev_edge_trigger_below_min"])
        self.assertFalse(details["exit_policy_edge_below_min"])
        self.assertFalse(details["exit_policy_edge_triggered"])
        self.assertFalse(details["exit_policy_strategy_triggered"])

    def test_exit_policy_existing_legacy_repricing_code_is_ignored(self) -> None:
        details = swm._finalize_exit_decision_details(
            {
                "hold_edge_realizable": -0.01,
                "hold_edge_realizable_trigger": 0.005,
                "prev_hold_edge_realizable": 0.01,
                "exit_hold_edge_realizable_min": 0.02,
                "exit_reason_codes": ["weather_repricing_target"],
            },
            decision_reason_codes=[],
        )

        self.assertNotIn("exit_original_reason_codes", details)
        self.assertNotIn("exit_reason_family_codes", details)
        self.assertFalse(details["exit_policy_edge_triggered"])
        self.assertFalse(details["exit_policy_repricing_triggered"])
        self.assertFalse(details["exit_policy_strategy_triggered"])

    def test_exit_policy_does_not_infer_edge_family_from_single_below_min_edge(self) -> None:
        details = swm._finalize_exit_decision_details(
            {
                "hold_edge_realizable": -0.01,
                "hold_edge_realizable_trigger": 0.005,
                "prev_hold_edge_realizable": 0.05,
                "exit_hold_edge_realizable_min": 0.02,
            },
            decision_reason_codes=["exit_no_trigger"],
        )

        self.assertNotIn("exit_original_reason_codes", details)
        self.assertNotIn("exit_reason_family_codes", details)
        self.assertTrue(details["exit_policy_legacy_edge_below_min"])
        self.assertFalse(details["exit_policy_edge_below_min"])
        self.assertFalse(details["exit_policy_edge_triggered"])
        self.assertFalse(details["exit_policy_strategy_triggered"])
        self.assertTrue(details["exit_policy_no_trigger"])

    def test_exit_policy_audit_ignores_legacy_exec_edge_without_bayes_trigger(self) -> None:
        details = swm._finalize_exit_decision_details(
            {
                "hold_edge_realizable": 0.05,
                "hold_edge_realizable_trigger": 0.05,
                "prev_hold_edge_realizable": 0.05,
                "exit_hold_edge_realizable_min": 0.02,
                "exec_edge_exit": -0.50,
                "signal_edge_min": -0.50,
            },
            decision_reason_codes=[],
        )

        self.assertEqual(details["exit_edge_primary_source"], "")
        self.assertEqual(details["exit_policy_primary_edge_source"], "")
        self.assertFalse(details["exit_policy_edge_triggered"])
        self.assertFalse(details["exit_policy_strategy_triggered"])
        self.assertNotIn("exit_reason_family_codes", details)

    def test_exit_trigger_policy_removes_legacy_lag_flip_and_model_dir(self) -> None:
        result = swm._apply_exit_trigger_policy(
            reasons=["止损"],
            codes=["stop_loss"],
            triggers=["stop_loss"],
            exit_edge_realizable_only=True,
            hold_edge_realizable_trigger=-0.01,
            prev_hold_edge_realizable=0.01,
            edge_min=0.02,
            lag_exit_enabled=True,
            lag_value=0.95,
            lag_threshold=0.80,
            lag_consecutive=2,
            ask_hist=[0.90],
            bid_hist=[0.10],
            price_hist=[0.20],
            edge_flip_enabled=True,
            edge_flip_min=0.005,
            model_dir_enabled=True,
            model_dir_exit_ok=False,
        )

        self.assertEqual(result["codes"], [])
        self.assertEqual(result["triggers"], [])
        self.assertEqual(result["legacy_exit_codes_removed"], ["stop_loss"])
        self.assertTrue(result["edge_min_consecutive"])
        self.assertFalse(result["lag_triggered"])
        self.assertFalse(result["edge_flip_triggered"])
        self.assertFalse(result["model_dir_exit_block"])

    def test_exit_trigger_policy_keeps_only_bayes_codes(self) -> None:
        result = swm._apply_exit_trigger_policy(
            reasons=["stop loss", "SSOT目标仓位已超目标：执行减仓/再平衡"],
            codes=["stop_loss", "target_realization_no_invalidated"],
            triggers=["stop_loss", "target_realization_no_invalidated"],
            exit_edge_realizable_only=False,
            hold_edge_realizable_trigger=0.05,
            prev_hold_edge_realizable=0.05,
            edge_min=0.02,
            lag_exit_enabled=False,
            lag_value=None,
            lag_threshold=0.80,
            lag_consecutive=2,
            ask_hist=[],
            bid_hist=[],
            price_hist=[],
            edge_flip_enabled=True,
            edge_flip_min=0.005,
            model_dir_enabled=True,
            model_dir_exit_ok=False,
        )

        self.assertEqual(result["codes"], ["target_realization_no_invalidated"])
        self.assertEqual(result["triggers"], ["target_realization_no_invalidated"])
        self.assertEqual(result["legacy_exit_codes_removed"], ["stop_loss"])
        self.assertFalse(result["model_dir_exit_block"])


if __name__ == "__main__":
    unittest.main()
