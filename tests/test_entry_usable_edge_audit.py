import unittest
from unittest.mock import patch

from scan_weather_market import (
    _build_candidate_decision_context,
    _build_entry_usable_edge_audit,
    _candidate_sort_score_value,
    _entry_order_policy_block_reason,
    _entry_strategy_family_from_details,
    _exit_order_policy_allow_reason,
    _exit_order_policy_block_reason,
    _exit_order_policy_guard_details,
    _filter_executable_exit_payload,
    _normalize_exit_reason_family_codes,
    _normalize_entry_block_reason_codes,
)


class EntryUsableEdgeAuditTest(unittest.TestCase):
    def test_target_switch_non_target_suppression_remains_hard_in_realizable_only_mode(self):
        hard, soft = _normalize_entry_block_reason_codes(
            ["target_switch_non_target_entry_suppressed"],
            entry_only_realizable=True,
        )

        self.assertEqual(hard, ["target_switch_non_target_entry_suppressed"])
        self.assertEqual(soft, [])

    def test_posterior_settlement_audit_uses_strategy_gate_not_edge(self):
        audit = _build_entry_usable_edge_audit(
            {
                "strategy_family": "posterior_settlement",
                "strategy_lane": "yes_realized_target",
                "posterior_settlement_entry_allowed": True,
                "posterior_settlement_sort_score": 76.5,
                "posterior_settlement_size_scale": 0.74,
            }
        )

        self.assertEqual(audit["entry_usable_edge_base_source"], "posterior_settlement")
        self.assertEqual(audit["entry_alpha_gate_source"], "posterior_settlement")
        self.assertEqual(audit["strategy_lane"], "yes_realized_target")
        self.assertAlmostEqual(audit["entry_usable_edge_sort_score"], 76.5)
        self.assertFalse(audit["take_profit_enabled"])
        self.assertFalse(audit["stop_loss_enabled"])
        self.assertTrue(audit["settlement_exit_only"])

    def test_posterior_settlement_order_guard_uses_posterior_block_reason(self):
        with patch(
            "scan_weather_market.validate_sequential_bayes_execution_context",
            return_value={"ok": True, "blockers": []},
        ):
            allowed = _entry_order_policy_block_reason(
                {
                    "strategy_family": "posterior_settlement",
                    "posterior_settlement_entry_allowed": True,
                    "decision_ssot": "posterior_ssot_fact",
                    "target_probability_field": "posterior_ssot_fact.target_probability",
                }
            )
            blocked = _entry_order_policy_block_reason(
                {
                    "strategy_family": "posterior_settlement",
                    "posterior_settlement_entry_allowed": False,
                    "posterior_settlement_entry_block_reason": "target_realization_reliability_below_min",
                    "decision_ssot": "posterior_ssot_fact",
                    "target_probability_field": "posterior_ssot_fact.target_probability",
                }
            )

        self.assertEqual(allowed, "")
        self.assertEqual(blocked, "target_realization_reliability_below_min")

    def test_posterior_settlement_order_guard_blocks_zero_ssot_plan_notional(self):
        with patch(
            "scan_weather_market.validate_sequential_bayes_execution_context",
            return_value={"ok": True, "blockers": []},
        ):
            blocked = _entry_order_policy_block_reason(
                {
                    "strategy_family": "posterior_settlement",
                    "posterior_settlement_entry_allowed": True,
                    "target_realization_plan_notional": 0.0,
                    "target_realization_plan_notional_source": "ui_snapshot_ssot",
                    "decision_ssot": "posterior_ssot_fact",
                    "target_probability_field": "posterior_ssot_fact.target_probability",
                }
            )

        self.assertEqual(blocked, "target_realization_plan_notional_nonpositive")

    def test_legacy_exit_codes_are_not_bayes_exit_triggers(self):
        reasons, codes, removed = _filter_executable_exit_payload(
            ["旧退出诊断", "Bayes目标桶切换：旧YES目标桶退出"],
            [
                "stop_loss",
                "trail_stop",
                "edge_min",
                "weather_repricing_exit",
                "target_realization_switch_buy_exit_old",
            ],
            side="YES",
        )

        self.assertEqual(codes, [])
        self.assertEqual(
            sorted(removed),
            [
                "edge_min",
                "stop_loss",
                "target_realization_switch_buy_exit_old",
                "trail_stop",
                "weather_repricing_exit",
            ],
        )
        self.assertEqual(
            _normalize_exit_reason_family_codes(
                [
                    "stop_loss",
                    "trail_stop",
                    "edge_min",
                    "target_realization_yes_target_invalidated",
                ]
            ),
            ["target_realization"],
        )
        self.assertEqual(reasons, [])

    def test_exit_order_policy_blocks_non_bayes_exit_submit(self):
        block_reason = _exit_order_policy_block_reason(
            ["stop_loss", "trail_stop", "edge_min", "weather_repricing_exit"]
        )
        details = _exit_order_policy_guard_details(
            ["stop_loss", "trail_stop", "edge_min", "weather_repricing_exit"],
            reasons=["旧退出诊断"],
            block_reason=block_reason,
            side="YES",
        )

        self.assertEqual(block_reason, "non_bayes_exit_candidate_disabled")
        self.assertFalse(details["exit_order_policy_allowed"])
        self.assertEqual(details["exit_order_policy_allow_reason"], "")
        self.assertEqual(details["exit_order_policy_allowed_reason_codes"], [])
        self.assertEqual(
            sorted(details["exit_order_policy_removed_legacy_codes"]),
            ["edge_min", "stop_loss", "trail_stop", "weather_repricing_exit"],
        )

    def test_exit_order_policy_allows_target_realization_exit_submit(self):
        codes = ["target_realization_yes_target_invalidated"]
        family = {"exit_reason_family": "target_realization"}
        block_reason = _exit_order_policy_block_reason(codes, details=family)
        details = _exit_order_policy_guard_details(
            codes,
            reasons=["Bayes目标桶切换：旧YES目标桶退出"],
            details=family,
            block_reason=block_reason,
            side="YES",
        )

        self.assertEqual(block_reason, "")
        self.assertEqual(
            _exit_order_policy_allow_reason(codes, details=family),
            "target_realization",
        )
        self.assertTrue(details["exit_order_policy_allowed"])
        self.assertEqual(details["exit_order_policy_allow_reason"], "target_realization")
        self.assertEqual(
            details["exit_order_policy_allowed_reason_codes"],
            ["target_realization_yes_target_invalidated"],
        )
        self.assertEqual(details["exit_order_policy_removed_legacy_codes"], [])

    def test_posterior_settlement_sort_score_takes_priority(self):
        self.assertAlmostEqual(
            _candidate_sort_score_value(
                {
                    "strategy_family": "posterior_settlement",
                    "posterior_settlement_entry_allowed": True,
                    "posterior_settlement_sort_score": 88.0,
                    "edge_score": -10.0,
                }
            ),
            88.0,
        )

    def test_uses_tradeability_profit_as_base_without_penalties(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.08,
                "exec_entry_price": 0.14,
                "spread": 0.01,
                "penalty": 0.0,
                "time_factor": 1.0,
                "structure_factor": 1.0,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "MMMX",
            }
        )

        self.assertAlmostEqual(audit["entry_usable_edge"], 0.08)
        self.assertEqual(
            audit["entry_usable_edge_base_source"],
            "edge_tradeability_profit_value",
        )
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 1.0)

    def test_autumn_event_reduces_entry_size_scale(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.08,
                "exec_entry_price": 0.14,
                "spread": 0.01,
                "penalty": 0.0,
                "time_factor": 1.0,
                "structure_factor": 1.0,
                "event_date": "2026-10-15",
                "slug": "highest-temperature-in-atlanta-on-october-15-2026",
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KATL",
            }
        )

        self.assertEqual(audit["weather_strategy_season"], "autumn")
        self.assertAlmostEqual(audit["weather_strategy_season_size_scale"], 0.40)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale_before_season"], 1.0)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.40)

    def test_high_bayes_sigma_blocks_entry_usable_edge(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.20,
                "edge_tradeability_pass": True,
                "q_bayes_sigma": 0.20,
                "q_bayes_lcb": 0.35,
                "q_bayes_ucb": 0.75,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KATL",
            }
        )

        self.assertEqual(audit["entry_usable_edge_na_reason"], "q_bayes_sigma_high")
        self.assertFalse(audit["q_bayes_variance_gate_pass"])
        self.assertAlmostEqual(audit["q_bayes_sigma_for_entry_gate"], 0.20)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_high_bayes_interval_width_blocks_entry_usable_edge(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.20,
                "edge_tradeability_pass": True,
                "q_bayes_lcb": 0.25,
                "q_bayes_ucb": 0.60,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KATL",
            }
        )

        self.assertEqual(
            audit["entry_usable_edge_na_reason"],
            "q_bayes_interval_width_high",
        )
        self.assertFalse(audit["q_bayes_variance_gate_pass"])
        self.assertAlmostEqual(audit["q_bayes_interval_width_for_entry_gate"], 0.35)

    def test_low_price_bucket_adds_effective_price_penalty(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.01,
                "exec_entry_price": 0.001,
                "spread": 0.001,
                "penalty": 0.0,
                "q_calibrated_lcb": 0.02,
                "exec_exit_price": 0.004,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "RJTT",
            }
        )

        self.assertTrue(audit["entry_usable_edge_low_price_bucket"])
        self.assertAlmostEqual(audit["entry_usable_edge_price_penalty"], 0.002)
        self.assertAlmostEqual(audit["entry_usable_edge_effective_price"], 0.003)
        self.assertTrue(audit["entry_usable_edge_low_price_exit_ok"])
        self.assertFalse(audit["entry_usable_edge_low_price_block_size"])
        self.assertAlmostEqual(audit["entry_usable_edge_low_price_edge"], 0.017)
        self.assertAlmostEqual(audit["entry_usable_edge"], 0.008)

    def test_low_price_bucket_missing_exit_bid_caps_positive_entry_edge(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.01,
                "exec_entry_price": 0.001,
                "spread": 0.001,
                "q_quote_lcb": 0.0085,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "RKSI",
            }
        )

        self.assertTrue(audit["entry_usable_edge_low_price_bucket"])
        self.assertFalse(audit["entry_usable_edge_low_price_exit_ok"])
        self.assertTrue(audit["entry_usable_edge_low_price_block_size"])
        self.assertEqual(
            audit["entry_usable_edge_low_price_na_reason"],
            "expected_exit_bid_missing",
        )
        self.assertLess(audit["entry_usable_edge"], 0.0)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_low_price_bucket_uses_ask_floor_and_blocks_unexitably_low_bid(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.0872,
                "exec_entry_price": 0.0025,
                "ask": 0.004,
                "spread": 0.003,
                "q_quote_lcb": 0.0255555555555556,
                "exec_exit_price": 0.003,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "EDDM",
            }
        )

        self.assertTrue(audit["entry_usable_edge_low_price_bucket"])
        self.assertAlmostEqual(audit["entry_usable_edge_low_price_entry_price"], 0.004)
        self.assertAlmostEqual(audit["entry_usable_edge_effective_price"], 0.006)
        self.assertAlmostEqual(
            audit["entry_usable_edge_low_price_exit_edge"],
            -0.003,
        )
        self.assertFalse(audit["entry_usable_edge_low_price_exit_ok"])
        self.assertTrue(audit["entry_usable_edge_low_price_block_size"])
        self.assertEqual(
            audit["entry_usable_edge_low_price_na_reason"],
            "expected_exit_bid_below_effective_price",
        )
        self.assertLess(audit["entry_usable_edge"], 0.0)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_cheap_bucket_above_two_cents_uses_lcb_and_exit_cost(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.0301025396456137,
                "exec_entry_price": 0.041,
                "ask": 0.041,
                "spread": 0.0,
                "q_quote_lcb": 0.0241025641025641,
                "exec_exit_price": 0.041,
                "exec_exit_cost": 0.003,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertTrue(audit["entry_usable_edge_low_price_bucket"])
        self.assertEqual(
            audit["entry_usable_edge_low_price_trigger"],
            "q_lcb_below_effective_price",
        )
        self.assertTrue(audit["entry_edge_false_edge"])
        self.assertEqual(
            audit["entry_edge_false_edge_type"],
            "q_lcb_below_effective_entry_price",
        )
        self.assertEqual(
            audit["entry_edge_boundary"],
            "q_conservative_gt_effective_entry_price_and_target_exit_covers_friction_or_hold",
        )
        self.assertEqual(audit["entry_edge_boundary_action"], "size_zero")
        self.assertAlmostEqual(audit["entry_edge_q_conservative"], 0.0241025641025641)
        self.assertAlmostEqual(audit["entry_edge_effective_entry_price"], 0.044)
        self.assertAlmostEqual(
            audit["entry_edge_entry_surplus"],
            -0.0198974358974359,
        )
        self.assertAlmostEqual(audit["entry_usable_edge_price_penalty"], 0.003)
        self.assertAlmostEqual(audit["entry_usable_edge_effective_price"], 0.044)
        self.assertAlmostEqual(
            audit["entry_usable_edge_low_price_edge"],
            -0.0198974358974359,
        )
        self.assertAlmostEqual(audit["entry_usable_edge_low_price_exit_edge"], -0.003)
        self.assertFalse(audit["entry_usable_edge_low_price_exit_ok"])
        self.assertTrue(audit["entry_usable_edge_low_price_block_size"])
        self.assertEqual(
            audit["entry_usable_edge_low_price_na_reason"],
            "q_lcb_below_effective_price",
        )
        self.assertLess(audit["entry_usable_edge"], 0.0)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_constitution_uses_most_conservative_lcb_not_mean_fallback(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.37,
                "exec_entry_price": 0.2,
                "ask": 0.21,
                "spread": 0.01,
                "q_calibrated_lcb": 0.90,
                "q_quote_lcb": 0.229166666666667,
                "q_entry": 0.903440477181746,
                "exec_exit_price": 0.19,
                "exec_exit_cost": 0.008,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertAlmostEqual(audit["entry_edge_q_conservative"], 0.229166666666667)
        self.assertEqual(audit["entry_edge_q_conservative_source"], "q_quote_lcb")
        self.assertAlmostEqual(audit["entry_edge_effective_entry_price"], 0.218)
        self.assertAlmostEqual(audit["entry_edge_entry_surplus"], 0.011166666666667)
        self.assertEqual(audit["entry_edge_false_edge_type"], "target_exit_missing")

    def test_repricing_requires_exit_path_even_when_lcb_covers_entry_cost(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.03,
                "exec_entry_price": 0.041,
                "ask": 0.041,
                "spread": 0.0,
                "q_quote_lcb": 0.06,
                "exec_exit_price": 0.041,
                "exec_exit_cost": 0.003,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertFalse(audit["entry_usable_edge_low_price_bucket"])
        self.assertTrue(audit["entry_edge_exit_path_required"])
        self.assertTrue(audit["entry_edge_false_edge"])
        self.assertEqual(audit["entry_edge_false_edge_type"], "target_exit_missing")
        self.assertAlmostEqual(audit["entry_edge_entry_surplus"], 0.016)
        self.assertAlmostEqual(audit["entry_edge_exit_surplus"], -0.003)
        self.assertAlmostEqual(audit["entry_usable_edge"], -0.003)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_repricing_target_positive_caps_size_without_requiring_current_exit(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.37,
                "exec_entry_price": 0.2,
                "ask": 0.21,
                "spread": 0.01,
                "q_quote_lcb": 0.229166666666667,
                "exec_exit_price": 0.19,
                "exec_exit_cost": 0.008,
                "weather_strategy_target_exit_price": 0.23,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertFalse(audit["entry_edge_false_edge"])
        self.assertEqual(audit["entry_edge_exit_path_status"], "current_exit_weak_target_positive")
        self.assertEqual(audit["entry_edge_boundary_action"], "cap_size_by_conservative_surplus")
        self.assertAlmostEqual(audit["entry_edge_entry_surplus"], 0.011166666666667)
        self.assertAlmostEqual(audit["entry_edge_target_exit_surplus"], 0.012)
        self.assertAlmostEqual(audit["entry_edge_repricing_friction_floor"], 0.01)
        self.assertAlmostEqual(audit["entry_edge_target_surplus_to_friction"], 0.002)
        self.assertAlmostEqual(audit["entry_edge_exit_surplus"], -0.028)
        self.assertAlmostEqual(audit["entry_usable_edge"], 0.011166666666667)
        self.assertGreater(audit["entry_usable_edge_size_scale"], 0.0)

    def test_live_bid_takes_precedence_over_stale_exec_exit_price(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.08,
                "exec_entry_price": 0.0055,
                "ask": 0.009,
                "best_bid": 0.002,
                "spread": 0.007,
                "q_quote_lcb": 0.0244615384615385,
                "exec_exit_price": 0.0175,
                "weather_strategy_target_exit_price": 0.0766,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
                "market_tick_override": True,
            }
        )

        self.assertTrue(audit["entry_usable_edge_low_price_bucket"])
        self.assertAlmostEqual(audit["entry_edge_effective_entry_price"], 0.0125)
        self.assertAlmostEqual(audit["entry_edge_current_exit_bid"], 0.002)
        self.assertEqual(audit["entry_edge_current_exit_bid_source"], "best_bid")
        self.assertAlmostEqual(audit["entry_edge_exit_surplus"], -0.0105)
        self.assertAlmostEqual(audit["entry_usable_edge_expected_exit_bid"], 0.002)
        self.assertEqual(audit["entry_usable_edge_expected_exit_bid_source"], "best_bid")
        self.assertAlmostEqual(audit["entry_usable_edge_low_price_exit_edge"], -0.0105)
        self.assertEqual(
            audit["entry_edge_exit_path_status"],
            "current_exit_weak_target_positive",
        )
        self.assertAlmostEqual(audit["entry_edge_target_exit_surplus"], 0.0641)

    def test_repricing_target_needs_execution_evidence_when_current_exit_weak(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.08,
                "exec_entry_price": 0.0055,
                "ask": 0.009,
                "best_bid": 0.002,
                "spread": 0.007,
                "q_quote_lcb": 0.0244615384615385,
                "weather_strategy_target_exit_price": 0.0766,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "edge_realizable_exec_audit_blocked": True,
                "edge_realizable_exec_audit_scale": 0.0,
                "edge_realizable_p_realize_missing": True,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertTrue(audit["entry_edge_false_edge"])
        self.assertEqual(
            audit["entry_edge_false_edge_type"],
            "target_exit_execution_evidence_missing",
        )
        self.assertEqual(
            audit["entry_edge_root_cause"],
            "repricing_target_exit_not_supported_by_execution_evidence",
        )
        self.assertAlmostEqual(audit["entry_edge_target_exit_surplus"], 0.0641)
        self.assertAlmostEqual(audit["entry_edge_target_exit_surplus_usable"], 0.0)
        self.assertAlmostEqual(audit["entry_edge_repricing_exit_evidence_scale"], 0.0)
        self.assertTrue(audit["entry_edge_repricing_exit_evidence_limited"])
        self.assertTrue(audit["entry_edge_repricing_exit_evidence_missing"])
        self.assertFalse(audit["entry_edge_repricing_exit_evidence_low_sample"])
        self.assertEqual(audit["entry_edge_repricing_exit_evidence_status"], "missing")
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_na_reason"],
            "calibration_bucket_missing",
        )
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_missing_reason"],
            "p_realize_missing",
        )
        self.assertTrue(audit["entry_usable_edge_low_price_block_size"])
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_repricing_target_default_flag_writes_evidence_status(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.08,
                "exec_entry_price": 0.0055,
                "ask": 0.009,
                "best_bid": 0.002,
                "spread": 0.007,
                "q_quote_lcb": 0.0244615384615385,
                "weather_strategy_target_exit_price": 0.0766,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "edge_realizable_p_realize": 0.51,
                "edge_realizable_p_realize_missing": True,
                "edge_realizable_p_realize_uses_default": True,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertTrue(audit["entry_edge_false_edge"])
        self.assertEqual(
            audit["entry_edge_false_edge_type"],
            "target_exit_execution_evidence_missing",
        )
        self.assertTrue(audit["entry_edge_repricing_exit_evidence_missing"])
        self.assertEqual(audit["entry_edge_repricing_exit_evidence_status"], "default_only")
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_na_reason"],
            "calibration_defaults_without_bucket_evidence",
        )
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_missing_reason"],
            "calibration_default_fallback",
        )
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_scale_source"],
            "edge_realizable_p_realize_evidence_status:default_only",
        )

    def test_blocked_exec_audit_does_not_cap_repricing_evidence_when_scale_is_one(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.08,
                "exec_entry_price": 0.0055,
                "ask": 0.009,
                "best_bid": 0.002,
                "spread": 0.007,
                "q_quote_lcb": 0.0244615384615385,
                "weather_strategy_target_exit_price": 0.0766,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "edge_realizable_exec_audit_blocked": True,
                "edge_realizable_exec_audit_failed_checks": "audit_gate",
                "edge_realizable_exec_audit_gate_failed_names": "walk_proxy_brier_cal_max",
                "edge_realizable_exec_audit_scale": 1.0,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertFalse(audit["entry_edge_false_edge"])
        self.assertAlmostEqual(audit["entry_edge_target_exit_surplus_usable"], 0.0641)
        self.assertAlmostEqual(audit["entry_edge_repricing_exit_evidence_scale"], 1.0)
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_scale_source"],
            "not_limited",
        )
        self.assertGreater(audit["entry_usable_edge_size_scale"], 0.0)

    def test_repricing_target_uses_p_realize_as_execution_evidence_scale(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.08,
                "exec_entry_price": 0.0055,
                "ask": 0.009,
                "best_bid": 0.002,
                "spread": 0.007,
                "q_quote_lcb": 0.0244615384615385,
                "weather_strategy_target_exit_price": 0.0766,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "edge_realizable_exec_audit_blocked": True,
                "edge_realizable_exec_audit_failed_checks": "audit_gate",
                "edge_realizable_exec_audit_scale": 1.0,
                "edge_realizable_p_realize": 0.05,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertTrue(audit["entry_edge_false_edge"])
        self.assertEqual(
            audit["entry_edge_false_edge_type"],
            "target_exit_execution_probability_too_low",
        )
        self.assertEqual(
            audit["entry_edge_root_cause"],
            "repricing_target_surplus_discounted_by_execution_probability_below_friction",
        )
        self.assertAlmostEqual(audit["entry_edge_target_exit_surplus"], 0.0641)
        self.assertAlmostEqual(audit["entry_edge_target_exit_surplus_usable"], 0.003205)
        self.assertAlmostEqual(audit["entry_edge_repricing_exit_evidence_scale"], 0.05)
        self.assertAlmostEqual(audit["entry_edge_repricing_execution_probability"], 0.05)
        self.assertAlmostEqual(audit["entry_edge_repricing_target_surplus_raw"], 0.0641)
        self.assertAlmostEqual(
            audit["entry_edge_repricing_target_surplus_discounted"], 0.003205
        )
        self.assertAlmostEqual(
            audit["entry_edge_repricing_target_surplus_discounted_to_friction"],
            0.003205 - 0.007,
        )
        self.assertEqual(
            audit["entry_edge_repricing_probability_chain_version"],
            "repricing_probability_chain_v1_2026_05_06",
        )
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_scale_source"],
            "edge_realizable_p_realize",
        )
        self.assertFalse(audit["entry_edge_repricing_exit_evidence_missing"])
        self.assertFalse(audit["entry_edge_repricing_exit_evidence_low_sample"])
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_repricing_target_default_only_evidence_remains_missing(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.08,
                "exec_entry_price": 0.0055,
                "ask": 0.009,
                "best_bid": 0.002,
                "spread": 0.007,
                "q_quote_lcb": 0.0244615384615385,
                "weather_strategy_target_exit_price": 0.0766,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "edge_realizable_p_realize": 0.51,
                "edge_realizable_p_realize_evidence_status": "default_only",
                "edge_realizable_p_realize_missing_tables": "p_converge_buckets",
                "edge_realizable_p_realize_default_tables": "p_fill_exit",
                "edge_realizable_p_converge_evidence_status": "missing",
                "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                "edge_realizable_p_fill_exit_evidence_status": "default_only",
                "edge_realizable_p_fill_entry_samples": 42,
                "edge_realizable_p_converge_source": "regional_calibration",
                "edge_realizable_p_fill_exit_source": "global_default",
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertTrue(audit["entry_edge_false_edge"])
        self.assertEqual(
            audit["entry_edge_false_edge_type"],
            "target_exit_execution_evidence_missing",
        )
        self.assertEqual(
            audit["entry_edge_root_cause"],
            "repricing_target_exit_not_supported_by_execution_evidence",
        )
        self.assertAlmostEqual(audit["entry_edge_target_exit_surplus_usable"], 0.0)
        self.assertAlmostEqual(audit["entry_edge_repricing_exit_evidence_scale"], 0.0)
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_scale_source"],
            "edge_realizable_p_realize_evidence_status:default_only",
        )
        self.assertTrue(audit["entry_edge_repricing_exit_evidence_missing"])
        self.assertFalse(audit["entry_edge_repricing_exit_evidence_low_sample"])
        self.assertEqual(audit["entry_edge_repricing_exit_evidence_status"], "default_only")
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_missing_reason"],
            "calibration_bucket_missing",
        )
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_probability_source"],
            "p_converge=missing:regional_calibration;p_fill_entry=bucket_evidence;p_fill_exit=default_only:global_default",
        )
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_missing_tables"],
            "p_converge_buckets",
        )
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_default_tables"],
            "p_fill_exit",
        )
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_missing_table_count"],
            1,
        )
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_default_table_count"],
            1,
        )
        self.assertAlmostEqual(
            audit["entry_edge_repricing_exit_evidence_bucket_samples_min"],
            42.0,
        )

    def test_repricing_target_low_sample_evidence_is_not_missing(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.08,
                "exec_entry_price": 0.0055,
                "ask": 0.009,
                "best_bid": 0.002,
                "spread": 0.007,
                "q_quote_lcb": 0.0244615384615385,
                "weather_strategy_target_exit_price": 0.0766,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "edge_realizable_p_realize": 0.05,
                "edge_realizable_p_realize_evidence_status": "low_sample",
                "edge_realizable_p_realize_low_sample": True,
                "edge_realizable_p_realize_low_sample_tables": "p_fill_exit_buckets",
                "edge_realizable_p_realize_samples_min": 3,
                "edge_realizable_p_converge_evidence_status": "bucket_evidence",
                "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
                "edge_realizable_p_fill_exit_evidence_status": "low_sample",
                "edge_realizable_p_converge_samples": 20,
                "edge_realizable_p_fill_entry_samples": 18,
                "edge_realizable_p_fill_exit_samples": 3,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertTrue(audit["entry_edge_false_edge"])
        self.assertEqual(
            audit["entry_edge_false_edge_type"],
            "target_exit_execution_evidence_low_sample",
        )
        self.assertEqual(
            audit["entry_edge_root_cause"],
            "repricing_target_exit_low_sample_execution_evidence",
        )
        self.assertAlmostEqual(audit["entry_edge_target_exit_surplus_usable"], 0.003205)
        self.assertAlmostEqual(audit["entry_edge_repricing_exit_evidence_scale"], 0.05)
        self.assertFalse(audit["entry_edge_repricing_exit_evidence_missing"])
        self.assertTrue(audit["entry_edge_repricing_exit_evidence_low_sample"])
        self.assertEqual(audit["entry_edge_repricing_exit_evidence_status"], "low_sample")
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_low_sample_tables"],
            "p_fill_exit_buckets",
        )
        self.assertEqual(
            audit["entry_edge_repricing_exit_evidence_low_sample_table_count"],
            1,
        )
        self.assertAlmostEqual(
            audit["entry_edge_repricing_exit_evidence_bucket_samples_min"],
            3.0,
        )

    def test_repricing_target_must_cover_spread_and_cost_floor(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.180098,
                "exec_entry_price": 0.0105,
                "ask": 0.018,
                "spread": 0.015,
                "q_quote_lcb": 0.205598,
                "exec_exit_price": 0.004,
                "exec_exit_cost": 0.0065,
                "weather_strategy_target_exit_price": 0.033,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "WIII",
            }
        )

        self.assertTrue(audit["entry_edge_false_edge"])
        self.assertEqual(
            audit["entry_edge_false_edge_type"],
            "target_exit_does_not_cover_friction",
        )
        self.assertEqual(
            audit["entry_edge_root_cause"],
            "repricing_target_surplus_below_spread_cost_floor",
        )
        self.assertAlmostEqual(audit["entry_edge_effective_entry_price"], 0.0255)
        self.assertAlmostEqual(audit["entry_edge_target_exit_surplus"], 0.0075)
        self.assertAlmostEqual(audit["entry_edge_repricing_friction_floor"], 0.015)
        self.assertAlmostEqual(audit["entry_edge_target_surplus_to_friction"], -0.0075)
        self.assertLessEqual(audit["entry_usable_edge"], 0.0)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_gamma_price_fallback_is_not_tradeable_edge(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.05,
                "exec_entry_price": 0.003,
                "ask": 0.003,
                "ask_missing_reason": "gamma_price_fallback",
                "spread": 0.0,
                "q_quote_lcb": 0.04,
                "exec_exit_price": 0.003,
                "exec_exit_cost": 0.002,
                "weather_strategy_target_exit_price": 0.06,
                "weather_strategy_exit_plan": "repricing_exit_target",
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertTrue(audit["entry_edge_false_edge"])
        self.assertTrue(audit["entry_edge_market_price_fallback_unverified"])
        self.assertEqual(
            audit["entry_edge_false_edge_type"],
            "market_price_fallback_unverified",
        )
        self.assertEqual(
            audit["entry_edge_root_cause"],
            "gamma_price_fallback_not_orderbook_quote",
        )
        self.assertLessEqual(audit["entry_usable_edge"], 0.0)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_four_cent_bucket_is_not_flagged_when_lcb_covers_effective_price(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.03,
                "exec_entry_price": 0.041,
                "ask": 0.041,
                "spread": 0.0,
                "q_quote_lcb": 0.06,
                "exec_exit_price": 0.041,
                "exec_exit_cost": 0.003,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "KDAL",
            }
        )

        self.assertFalse(audit["entry_usable_edge_low_price_bucket"])
        self.assertFalse(audit["entry_edge_false_edge"])
        self.assertEqual(audit["entry_edge_boundary_status"], "pass")
        self.assertEqual(audit["entry_edge_boundary_action"], "cap_size_by_conservative_surplus")
        self.assertAlmostEqual(audit["entry_usable_edge_price_penalty"], 0.0)
        self.assertAlmostEqual(audit["entry_usable_edge"], 0.016)

    def test_low_price_bucket_blocks_size_without_exit_or_positive_low_price_edge(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.02,
                "exec_entry_price": 0.001,
                "spread": 0.001,
                "q_calibrated_lcb": 0.002,
                "weather_strategy_rules_ok": True,
                "weather_strategy_settlement_station": "RJTT",
            }
        )

        self.assertTrue(audit["entry_usable_edge_low_price_bucket"])
        self.assertFalse(audit["entry_usable_edge_low_price_exit_ok"])
        self.assertTrue(audit["entry_usable_edge_low_price_block_size"])
        self.assertEqual(
            audit["entry_usable_edge_low_price_na_reason"],
            "q_lcb_below_effective_price",
        )
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_rules_missing_penalty_blocks_default_size_when_nonpositive(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_edge_lcb": 0.04,
                "order_price": 0.08,
                "weather_strategy_rules_ok": False,
                "weather_strategy_rules_na_reason": "station_id_missing",
            }
        )

        self.assertAlmostEqual(audit["entry_usable_edge_station_penalty"], 0.05)
        self.assertEqual(
            audit["entry_usable_edge_station_na_reason"],
            "station_id_missing",
        )
        self.assertLess(audit["entry_usable_edge"], 0.0)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)
        self.assertTrue(audit["entry_usable_edge_block_nonpositive"])

    def test_negative_usable_edge_size_floor_can_be_restored_for_audit(self):
        with patch.dict("os.environ", {"PM_ENTRY_USABLE_EDGE_BLOCK_NONPOSITIVE": "0"}):
            audit = _build_entry_usable_edge_audit(
                {
                    "edge_tradeability_edge_lcb": 0.04,
                    "order_price": 0.08,
                    "weather_strategy_rules_ok": False,
                    "weather_strategy_rules_na_reason": "station_id_missing",
                }
            )

        self.assertLess(audit["entry_usable_edge"], 0.0)
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.10)
        self.assertFalse(audit["entry_usable_edge_block_nonpositive"])
        self.assertTrue(audit["entry_usable_edge_size_floor_applied"])
        self.assertEqual(
            _entry_order_policy_block_reason(audit),
            "non_bayes_entry_candidate_disabled",
        )

    def test_order_guard_blocks_legacy_edge_candidates_even_when_edge_positive(self):
        self.assertEqual(
            _entry_order_policy_block_reason(
                {
                    "entry_usable_edge": -0.001,
                    "entry_usable_edge_size_scale": 0.0,
                    "entry_usable_edge_block_nonpositive": True,
                }
            ),
            "non_bayes_entry_candidate_disabled",
        )
        self.assertEqual(
            _entry_order_policy_block_reason(
                {
                    "entry_usable_edge": 0.02,
                    "entry_usable_edge_size_scale": 0.0,
                }
            ),
            "non_bayes_entry_candidate_disabled",
        )
        self.assertEqual(
            _entry_order_policy_block_reason(
                {
                    "entry_usable_edge": 0.02,
                    "entry_usable_edge_size_scale": 0.4,
                    "strategy": "lcb_global",
                }
            ),
            "non_bayes_entry_candidate_disabled",
        )

    def test_order_guard_blocks_trajectory_projected_bucket_as_entry_source(self):
        self.assertEqual(
            _entry_order_policy_block_reason(
                {
                    "strategy_family": "path_probability_follow",
                    "entry_strategy_family": "path_probability_follow",
                    "strategy": "path_probability_follow",
                    "temperature_trajectory_projected_bucket": "32°C",
                    "breakout_candidate_bucket": "32°C",
                    "entry_usable_edge": 0.08,
                    "entry_usable_edge_size_scale": 1.0,
                }
            ),
            "non_bayes_entry_candidate_disabled",
        )

    def test_reason_codes_add_churn_penalty(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.03,
                "ask": 0.05,
            },
            reason_codes=["entry_gate_blocked", "residual_exit_lock"],
        )

        self.assertAlmostEqual(audit["entry_usable_edge_churn_penalty"], 0.01)
        self.assertEqual(
            audit["entry_usable_edge_churn_reasons"],
            "residual_exit_lock",
        )
        self.assertAlmostEqual(audit["entry_usable_edge"], 0.02)

    def test_stale_penalty_uses_configured_threshold(self):
        with patch.dict(
            "os.environ",
            {
                "PM_ENTRY_USABLE_EDGE_STALE_SEC": "100",
                "PM_ENTRY_USABLE_EDGE_STALE_PENALTY_MAX": "0.02",
            },
        ):
            audit = _build_entry_usable_edge_audit(
                {
                    "edge_tradeability_profit_value": 0.05,
                    "exec_edge_entry": 0.20,
                    "ask": 0.1,
                    "market_tick_age_sec": 300,
                }
            )

        self.assertAlmostEqual(audit["entry_usable_edge_stale_penalty"], 0.002)
        self.assertAlmostEqual(audit["entry_usable_edge"], 0.048)

    def test_exec_edge_entry_without_tradeability_does_not_become_base(self):
        audit = _build_entry_usable_edge_audit(
            {
                "exec_edge_entry": 0.50,
                "ask": 0.1,
            }
        )

        self.assertEqual(audit["entry_usable_edge_na_reason"], "edge_tradeability_missing")
        self.assertEqual(audit["entry_usable_edge_base_source"], "edge_tradeability_missing")
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_tradeability_blocked_does_not_report_positive_usable_edge(self):
        audit = _build_entry_usable_edge_audit(
            {
                "edge_tradeability_profit_value": 0.50,
                "edge_tradeability_pass": False,
                "edge_tradeability_blocked": True,
                "edge_tradeability_reasons": "signal_lcb_below_min",
                "ask": 0.1,
            }
        )

        self.assertEqual(
            audit["entry_usable_edge_na_reason"],
            "edge_tradeability_blocked:signal_lcb_below_min",
        )
        self.assertEqual(audit["entry_usable_edge_base_source"], "edge_tradeability_missing")
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_plain_lcb_pinned_tail_no_has_specific_missing_reason(self):
        audit = _build_entry_usable_edge_audit(
            {
                "exec_edge_entry": -0.01,
                "plain_lcb_pinned_tail_no_filtered": True,
                "ask": 0.999,
            }
        )

        self.assertEqual(
            audit["entry_usable_edge_na_reason"],
            "soft_shape_filter_plain_lcb_pinned_tail_no",
        )
        self.assertEqual(audit["entry_usable_edge_base_source"], "edge_tradeability_missing")
        self.assertAlmostEqual(audit["entry_usable_edge_size_scale"], 0.0)

    def test_sort_score_prefers_entry_usable_edge(self):
        score = _candidate_sort_score_value(
            {
                "entry_usable_edge_sort_score": 1.5,
                "edge_realizable_sort_score": 9.0,
                "edge_score": 12.0,
            }
        )

        self.assertAlmostEqual(score, 1.5)

    def test_entry_block_reason_codes_are_split_into_hard_and_soft(self):
        hard, soft = _normalize_entry_block_reason_codes(
            [
                "entry_balance_insufficient_preflight",
                "plain_lcb_pinned_tail_no_filtered",
                "strategy_size_ratio_zero",
            ]
        )

        self.assertEqual(hard, ["balance_insufficient"])
        self.assertEqual(
            soft,
            ["plain_lcb_pinned_tail_no_filtered", "strategy_size_ratio_zero"],
        )

    def test_soft_only_entry_block_reason_codes_do_not_become_hard_blocks(self):
        hard, soft = _normalize_entry_block_reason_codes(
            ["edge_tradeability_blocked", "q_low", "spread_wide", "event_worker_failed"]
        )

        self.assertEqual(hard, [])
        self.assertEqual(
            soft,
            ["edge_tradeability_blocked", "q_low", "spread_wide", "event_worker_failed"],
        )

    def test_realizable_only_softens_historical_entry_gate_codes(self) -> None:
        hard, soft = _normalize_entry_block_reason_codes(
            [
                "q_range_soft",
                "ask_above_cap_soft",
                "spread_too_wide_soft",
                "depth_below_min",
                "ask_le_q_limit",
                "lag_warning",
                "volume_warning",
                "yes_overprice_blocked",
                "no_by_yes_q_blocked",
                "skip_top_no_blocked",
            ],
            entry_only_realizable=True,
        )

        self.assertEqual(hard, [])
        self.assertEqual(
            soft,
            [
                "q_range_soft",
                "ask_above_cap_soft",
                "spread_too_wide_soft",
                "depth_below_min",
                "ask_le_q_limit",
                "lag_warning",
                "volume_warning",
                "yes_overprice_blocked",
                "no_by_yes_q_blocked",
                "skip_top_no_blocked",
            ],
        )

    def test_entry_strategy_family_splits_lcb_variants(self):
        self.assertEqual(
            _entry_strategy_family_from_details({"strategy": "lcb_global"}),
            "path_probability_follow",
        )
        self.assertEqual(
            _entry_strategy_family_from_details({"entry_strategy_family": "lcb_global_main"}),
            "path_probability_follow",
        )
        self.assertEqual(
            _entry_strategy_family_from_details({"strategy": "hard_source_direct"}),
            "path_probability_follow",
        )
        self.assertEqual(
            _entry_strategy_family_from_details(
                {"strategy": "lcb_global", "strategy_variant": "consensus_yes_top"}
            ),
            "consensus_top_regional",
        )
        self.assertEqual(
            _entry_strategy_family_from_details(
                {"strategy": "lcb_global", "edge_tradeability_lcb_canary_override": True}
            ),
            "exploration_canary",
        )
        self.assertEqual(
            _entry_strategy_family_from_details(
                {"strategy": "lcb_global", "weather_strategy_exit_plan": "repricing_exit_target"}
            ),
            "repricing_bucket",
        )

    def test_candidate_decision_context_includes_bayes_q(self):
        context = _build_candidate_decision_context(
            {
                "edge_tradeability_bayes_q": 0.6123,
                "edge_tradeability_bayes_q_scale": 0.8,
                "edge_tradeability_bayes_q_evidence": 12,
                "edge_tradeability_bayes_q_source": "q_calibrated",
                "edge_tradeability_bayes_na_reason": "low_sample",
            }
        )

        self.assertEqual(context.get("edge_tradeability_bayes_q"), 0.6123)
        self.assertEqual(context.get("edge_tradeability_bayes_q_scale"), 0.8)
        self.assertEqual(context.get("edge_tradeability_bayes_q_evidence"), 12)
        self.assertEqual(context.get("edge_tradeability_bayes_q_source"), "q_calibrated")
        self.assertEqual(context.get("edge_tradeability_bayes_na_reason"), "low_sample")


if __name__ == "__main__":
    unittest.main()
