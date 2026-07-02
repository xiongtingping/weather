import unittest
from pathlib import Path

from posterior_settlement_runtime import (
    ACTION_EXIT_CLEAR,
    ACTION_HOLD,
    ACTION_OBSERVE_REPRICE,
    ACTION_SWITCH_BUY,
    evaluate_realtime_target_adjustment,
    event_target_context,
)
from weather_strategy_runtime import classify_weather_strategy_candidate


class AutoTradeTargetRealizationSwitchingTests(unittest.TestCase):
    def _target_yes_candidate(self, **overrides):
        candidate = {
            "side": "YES",
            "outcome": "33°C",
            "bucket_label": "33°C",
            "target_bucket": "33°C",
            "target_posterior": 0.42,
            "target_realization_reliability": 0.72,
            "q_temperature_bucket_posterior": 0.42,
            "price": 0.20,
            "days_to_event": 1.0,
        }
        candidate.update(overrides)
        return candidate

    def test_scan_buffer_canonicalizes_target_realization_fields_before_insert(self) -> None:
        text = Path("scan_weather_market.py").read_text(encoding="utf-8")
        helper_start = text.index("def _canonicalize_target_realization_ssot_fields(")
        helper_end = text.index("# 复用模块级 _compact_decision_details", helper_start)
        helper_block = text[helper_start:helper_end]
        self.assertIn('out["target_bucket"] = plan_target_bucket', helper_block)
        self.assertIn('out["ssot_target_bucket"] = plan_target_bucket', helper_block)
        self.assertIn('out["new_target_bucket"] = plan_target_bucket', helper_block)
        self.assertIn('current_top_bucket = str(out.get("current_top_bucket")', helper_block)
        self.assertIn('canonical_source = "target_adjustment_current_top_bucket"', helper_block)
        self.assertIn('out.setdefault("switch_reference_bucket", out_label)', helper_block)

        buffer_start = text.index("def _buffer_decision(")
        buffer_end = text.index("def _target_switch_expected_key(", buffer_start)
        buffer_block = text[buffer_start:buffer_end]
        compact_at = buffer_block.index("merged_details = _compact_decision_details(")
        canonical_at = buffer_block.rindex(
            "_canonicalize_target_realization_ssot_fields(",
            0,
            compact_at,
        )
        self.assertLess(canonical_at, compact_at)

    def test_strategy_allows_initial_target_entry_without_active_yes_position(self) -> None:
        profile = classify_weather_strategy_candidate(
            self._target_yes_candidate(
                has_active_yes_position=False,
                target_adjustment_action=ACTION_HOLD,
            )
        )

        self.assertTrue(profile["weather_strategy_entry_allowed"])
        self.assertEqual(profile["strategy_lane"], "yes_realized_target")

    def test_strategy_blocks_repeated_same_target_yes_when_hold(self) -> None:
        profile = classify_weather_strategy_candidate(
            self._target_yes_candidate(
                has_active_yes_position=True,
                held_target_bucket="33°C",
                target_adjustment_action=ACTION_HOLD,
            )
        )

        self.assertFalse(profile["weather_strategy_entry_allowed"])
        self.assertEqual(
            profile["weather_strategy_entry_block_reason"],
            "target_adjustment_hold_no_rebalance",
        )

    def test_strategy_allows_new_target_yes_only_when_switch_buy_allowed(self) -> None:
        profile = classify_weather_strategy_candidate(
            self._target_yes_candidate(
                has_active_yes_position=True,
                held_target_bucket="32°C",
                target_adjustment_action=ACTION_SWITCH_BUY,
                switch_buy_allowed=True,
            )
        )

        self.assertTrue(profile["weather_strategy_entry_allowed"])
        self.assertEqual(profile["strategy_lane"], "yes_realized_target")

    def test_strategy_allows_old_yes_bucket_reverse_no_when_switching_target(self) -> None:
        profile = classify_weather_strategy_candidate(
            {
                "side": "NO",
                "outcome": "28°C",
                "bucket_label": "28°C",
                "target_bucket": "29°C",
                "target_posterior": 0.42,
                "q_temperature_bucket_posterior": 0.18,
                "candidate_bucket_realization_prob": 0.18,
                "target_realization_leg_role": "old_bucket_reverse",
                "target_adjustment_action": ACTION_SWITCH_BUY,
                "held_target_bucket": "28°C",
                "held_target_side": "YES",
                "has_active_position": True,
                "has_active_yes_position": True,
                "price": 0.82,
            }
        )

        self.assertTrue(profile["weather_strategy_entry_allowed"])
        self.assertEqual(profile["strategy_lane"], "no_remote_bucket")
        self.assertEqual(profile["target_realization_leg_role"], "old_bucket_reverse")
        self.assertEqual(profile["remote_no_reason"], "old_bucket_reverse_after_target_switch")

    def test_strategy_allows_old_no_bucket_reverse_yes_when_switching_target(self) -> None:
        profile = classify_weather_strategy_candidate(
            {
                "side": "YES",
                "outcome": "28°C",
                "bucket_label": "28°C",
                "target_bucket": "29°C",
                "target_posterior": 0.42,
                "q_temperature_bucket_posterior": 0.18,
                "candidate_bucket_realization_prob": 0.18,
                "target_realization_leg_role": "old_bucket_reverse",
                "target_adjustment_action": ACTION_SWITCH_BUY,
                "held_target_bucket": "28°C",
                "held_target_side": "NO",
                "has_active_position": True,
                "has_active_yes_position": False,
                "price": 0.18,
            }
        )

        self.assertTrue(profile["weather_strategy_entry_allowed"])
        self.assertEqual(profile["strategy_lane"], "yes_realized_target")
        self.assertEqual(profile["target_realization_leg_role"], "old_bucket_reverse")

    def test_top_unchanged_and_target_supported_holds(self) -> None:
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "33°C",
                "target_realization_reliability": 0.72,
                "trajectory_support_state": "supported",
            },
            {
                "previous_top_bucket": "33°C",
                "current_top_bucket": "33°C",
                "target_posterior": 0.41,
                "snapshot_id": "snap-1",
            },
        )

        self.assertEqual(decision["action"], ACTION_HOLD)
        self.assertFalse(decision["top_bucket_changed"])
        self.assertFalse(decision["unrealizable_triggered"])
        self.assertEqual(decision["trigger_type"], "none")

    def test_top_changed_and_new_bucket_reliable_switches(self) -> None:
        context = {
            "previous_top_bucket": "32°C",
            "current_top_bucket": "33°C",
            "current_top_posterior": 0.47,
            "snapshot_id": "snap-2",
            "posterior_settlement_bucket_prob_by_label": {
                "32°c": {"posterior_settlement_candidate_bucket_prob": 0.35},
                "33°c": {"posterior_settlement_candidate_bucket_prob": 0.47},
            },
        }
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "32°C",
                "target_realization_reliability": 0.70,
                "new_target_realization_reliability": 0.71,
                "trajectory_support_state": "supported",
                "new_target_trajectory_support_state": "supported",
            },
            context,
        )

        self.assertEqual(decision["action"], ACTION_SWITCH_BUY)
        self.assertTrue(decision["top_bucket_changed"])
        self.assertTrue(decision["switch_buy_allowed"])
        self.assertEqual(decision["new_buy_allowed_this_snapshot"], 1)
        self.assertEqual(decision["target_bucket"], "33°C")
        self.assertEqual(decision["held_target_bucket"], "32°C")
        self.assertEqual(decision["target_adjustment_decision_budget_sec"], 3.0)

    def test_d0_hard_zero_held_lower_bucket_switches_by_posterior_and_trajectory_only(self) -> None:
        context = event_target_context(
            [
                {
                    "outcome": "24°C or below",
                    "q_bayes_posterior": 0.48,
                    "q_bayes_physical_likelihood_hard_zero": True,
                    "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
                },
                {"outcome": "25°C", "q_temperature_bucket_posterior": 0.98},
                {"outcome": "26°C", "q_temperature_bucket_posterior": 0.02},
            ]
        )
        context.update(
            {
                "event_date": "2026-06-17",
                "weather_likelihood_refreshed_at": "2026-06-17T07:01:04+00:00",
                "observed_today_max_c": 25.0,
                "wu_twc_observed_intraday_today_max_c": 25.0,
                "temperature_momentum_effective_c_per_hour": 0.40,
                "snapshot_id": "chengdu-d0-hard-zero",
            }
        )

        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "24°C or below",
                "target_unrealizable_consecutive_snapshots": 2,
            },
            context,
        )

        self.assertEqual(decision["action"], ACTION_SWITCH_BUY)
        self.assertEqual(decision["held_target_bucket"], "24°C or below")
        self.assertEqual(decision["target_bucket"], "25°C")
        self.assertEqual(decision["new_target_bucket"], "25°C")
        self.assertEqual(decision["switch_reference_posterior"], 0.0)
        self.assertTrue(decision["d0_posterior_trajectory_switch_allowed"])
        self.assertEqual(
            decision["d0_posterior_trajectory_switch_context"]["decision_inputs"],
            "posterior_probability_and_temperature_trajectory_only",
        )

    def test_d0_observed_feasibility_factor_can_confirm_switch_without_market_price(self) -> None:
        context = event_target_context(
            [
                {
                    "outcome": "24°C or below",
                    "q_temperature_bucket_posterior": 0.2474,
                    "q_bayes_physical_likelihood_hard_zero": True,
                    "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
                    "q_bayes_physical_likelihood_observed_prob": 0.0,
                },
                {
                    "outcome": "25°C",
                    "q_temperature_bucket_posterior": 0.2169,
                    "q_bayes_physical_likelihood_observed_prob": 0.985,
                },
                {
                    "outcome": "26°C",
                    "q_temperature_bucket_posterior": 0.1458,
                    "q_bayes_physical_likelihood_observed_prob": 0.015,
                },
            ]
        )
        context.update(
            {
                "event_date": "2026-06-17",
                "weather_likelihood_refreshed_at": "2026-06-17T07:01:04+00:00",
                "snapshot_id": "chengdu-observed-feasibility",
            }
        )

        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "24°C or below",
                "target_unrealizable_consecutive_snapshots": 2,
            },
            context,
        )

        self.assertEqual(decision["action"], ACTION_SWITCH_BUY)
        self.assertEqual(decision["target_bucket"], "25°C")
        self.assertGreater(decision["new_target_posterior"], 0.98)
        switch_context = decision["d0_posterior_trajectory_switch_context"]
        self.assertTrue(switch_context["posterior_confirmed"])
        self.assertTrue(switch_context["observed_feasibility_supports_candidate"])
        self.assertEqual(switch_context["decision_inputs"], "posterior_probability_and_temperature_trajectory_only")

    def test_d0_hard_zero_switches_after_regular_intraday_window(self) -> None:
        context = event_target_context(
            [
                {
                    "outcome": "24°C or below",
                    "q_temperature_bucket_posterior": 0.2474,
                    "q_bayes_physical_likelihood_hard_zero": True,
                    "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
                    "q_bayes_physical_likelihood_observed_prob": 0.0,
                },
                {
                    "outcome": "25°C",
                    "q_temperature_bucket_posterior": 0.2169,
                    "q_bayes_physical_likelihood_observed_prob": 0.985,
                },
                {
                    "outcome": "26°C",
                    "q_temperature_bucket_posterior": 0.1458,
                    "q_bayes_physical_likelihood_observed_prob": 0.015,
                },
            ]
        )
        context.update(
            {
                "event_date": "2026-06-17",
                "weather_likelihood_refreshed_at": "2026-06-17T08:45:59+00:00",
                "snapshot_id": "chengdu-observed-feasibility-post-window",
            }
        )

        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "24°C or below",
                "target_unrealizable_consecutive_snapshots": 2,
            },
            context,
        )

        self.assertEqual(decision["action"], ACTION_SWITCH_BUY)
        switch_context = decision["d0_posterior_trajectory_switch_context"]
        self.assertEqual(
            switch_context["intraday_window"]["reason"],
            "outside_intraday_switch_window",
        )
        self.assertTrue(switch_context["held_bucket_hard_zero"])
        self.assertTrue(switch_context["hard_zero_window_override"])
        self.assertEqual(switch_context["decision_inputs"], "posterior_probability_and_temperature_trajectory_only")

    def test_decision_outputs_required_ssot_fields(self) -> None:
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "32°C",
                "target_realization_reliability": 0.70,
                "new_target_realization_reliability": 0.71,
                "trajectory_support_state": "supported",
                "new_target_trajectory_support_state": "supported",
            },
            {
                "previous_top_bucket": "32°C",
                "current_top_bucket": "33°C",
                "posterior_settlement_bucket_prob_by_label": {
                    "32°c": {"posterior_settlement_candidate_bucket_prob": 0.35},
                    "33°c": {"posterior_settlement_candidate_bucket_prob": 0.47},
                },
            },
        )
        required = {
            "previous_top_bucket",
            "current_top_bucket",
            "top_bucket_changed",
            "target_bucket",
            "second_target_bucket",
            "target_realization_reliability",
            "trajectory_support_state",
            "unrealizable_triggered",
            "trigger_type",
            "action",
            "reason_codes",
        }

        self.assertTrue(required.issubset(decision.keys()))
        self.assertTrue(
            all(f"posterior_settlement_{key}" in decision for key in required)
        )

    def test_top_changed_but_new_bucket_reliability_low_observes(self) -> None:
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "32°C",
                "target_realization_reliability": 0.70,
                "new_target_realization_reliability": 0.61,
                "trajectory_support_state": "neutral",
                "new_target_trajectory_support_state": "supported",
            },
            {
                "previous_top_bucket": "32°C",
                "current_top_bucket": "33°C",
                "posterior_settlement_bucket_prob_by_label": {
                    "32°c": {"posterior_settlement_candidate_bucket_prob": 0.34},
                    "33°c": {"posterior_settlement_candidate_bucket_prob": 0.45},
                },
            },
        )

        self.assertEqual(decision["action"], ACTION_OBSERVE_REPRICE)
        self.assertFalse(decision["switch_buy_allowed"])
        self.assertIn("new_target_reliability_below_min", decision["reason_codes"])

    def test_top_changed_but_posterior_lead_below_min_observes(self) -> None:
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "32°C",
                "target_realization_reliability": 0.70,
                "new_target_realization_reliability": 0.72,
                "trajectory_support_state": "supported",
                "new_target_trajectory_support_state": "supported",
            },
            {
                "previous_top_bucket": "32°C",
                "current_top_bucket": "33°C",
                "posterior_settlement_bucket_prob_by_label": {
                    "32°c": {"posterior_settlement_candidate_bucket_prob": 0.40},
                    "33°c": {"posterior_settlement_candidate_bucket_prob": 0.43},
                },
            },
        )

        self.assertEqual(decision["action"], ACTION_OBSERVE_REPRICE)
        self.assertFalse(decision["switch_buy_allowed"])
        self.assertIn("new_target_posterior_lead_below_min", decision["reason_codes"])

    def test_top_changed_but_new_target_trajectory_unsupported_observes(self) -> None:
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "32°C",
                "target_realization_reliability": 0.70,
                "new_target_realization_reliability": 0.72,
                "trajectory_support_state": "supported",
                "new_target_trajectory_support_state": "unsupported",
            },
            {
                "previous_top_bucket": "32°C",
                "current_top_bucket": "33°C",
                "posterior_settlement_bucket_prob_by_label": {
                    "32°c": {"posterior_settlement_candidate_bucket_prob": 0.34},
                    "33°c": {"posterior_settlement_candidate_bucket_prob": 0.45},
                },
            },
        )

        self.assertEqual(decision["action"], ACTION_OBSERVE_REPRICE)
        self.assertFalse(decision["switch_buy_allowed"])
        self.assertIn("new_target_trajectory_not_supported", decision["reason_codes"])

    def test_top_unchanged_but_target_unrealizable_exits(self) -> None:
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "33°C",
                "target_realization_reliability": 0.48,
                "trajectory_support_state": "unsupported",
                "target_unrealizable_consecutive_snapshots": 2,
            },
            {
                "previous_top_bucket": "33°C",
                "current_top_bucket": "33°C",
                "target_posterior": 0.38,
            },
        )

        self.assertEqual(decision["action"], ACTION_EXIT_CLEAR)
        self.assertTrue(decision["unrealizable_triggered"])
        self.assertEqual(decision["trigger_type"], "target_unrealizable")

    def test_previous_unsupported_snapshot_count_increments_before_exit(self) -> None:
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "33°C",
                "target_realization_reliability": 0.48,
                "trajectory_support_state": "unsupported",
                "target_unrealizable_previous_consecutive_snapshots": 1,
            },
            {
                "previous_top_bucket": "33°C",
                "current_top_bucket": "33°C",
                "target_posterior": 0.38,
            },
        )

        self.assertEqual(decision["action"], ACTION_EXIT_CLEAR)
        self.assertEqual(decision["target_unrealizable_consecutive_snapshots"], 2)
        self.assertTrue(decision["unrealizable_triggered"])

    def test_dual_trigger_generates_single_switch_decision(self) -> None:
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "32°C",
                "target_realization_reliability": 0.43,
                "trajectory_support_state": "unsupported",
                "target_unrealizable_consecutive_snapshots": 2,
                "new_target_realization_reliability": 0.72,
                "new_target_trajectory_support_state": "supported",
            },
            {
                "previous_top_bucket": "32°C",
                "current_top_bucket": "33°C",
                "posterior_settlement_bucket_prob_by_label": {
                    "32°c": {"posterior_settlement_candidate_bucket_prob": 0.31},
                    "33°c": {"posterior_settlement_candidate_bucket_prob": 0.44},
                },
            },
        )

        self.assertEqual(decision["action"], ACTION_SWITCH_BUY)
        self.assertEqual(decision["trigger_type"], "top_bucket_changed+target_unrealizable")
        self.assertEqual(decision["new_buy_allowed_this_snapshot"], 1)

    def test_same_snapshot_has_stable_dedupe_key_and_one_buy_budget(self) -> None:
        candidate = {
            "held_target_bucket": "31°C or below",
            "new_target_realization_reliability": 0.75,
            "new_target_trajectory_support_state": "supported",
        }
        context = {
            "slug": "highest-temperature-in-chengdu-on-june-17-2026",
            "event_date": "2026-06-17",
            "snapshot_id": "snapshot-42",
            "previous_top_bucket": "31°C or below",
            "current_top_bucket": "32°C",
            "posterior_settlement_bucket_prob_by_label": {
                "31°c or below": {"posterior_settlement_candidate_bucket_prob": 0.32},
                "32°c": {"posterior_settlement_candidate_bucket_prob": 0.41},
            },
            "trajectory_support_state": "supported",
        }

        first = evaluate_realtime_target_adjustment(candidate, context)
        second = evaluate_realtime_target_adjustment(candidate, context)

        self.assertEqual(first["action"], ACTION_SWITCH_BUY)
        self.assertEqual(first["target_adjustment_dedupe_key"], second["target_adjustment_dedupe_key"])
        self.assertEqual(first["new_buy_allowed_this_snapshot"], 1)
        self.assertEqual(second["new_buy_allowed_this_snapshot"], 1)

    def test_d0_low_boundary_switch_uses_posterior_and_temperature_trajectory_only(self) -> None:
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "24°C or below",
                "trajectory_support_state": "neutral",
                "observed_today_max_c": 23.0,
                "temperature_momentum_effective_c_per_hour": 0.8,
                "best_bid": 0.02,
                "best_ask": 0.04,
            },
            {
                "slug": "highest-temperature-in-chengdu-on-june-17-2026",
                "event_date": "2026-06-17",
                "weather_likelihood_refreshed_at": "2026-06-17T02:25:42+00:00",
                "expected_peak_hour_local": 15.0,
                "previous_top_bucket": "24°C or below",
                "current_top_bucket": "24°C or below",
                "d0_switch_candidate_bucket": "25°C",
                "market_top_bucket": "25°C",
                "market_top_probability": 0.95,
                "posterior_settlement_bucket_prob_by_label": {
                    "24°c or below": {"posterior_settlement_candidate_bucket_prob": 0.35},
                    "25°c": {"posterior_settlement_candidate_bucket_prob": 0.30},
                },
            },
        )

        self.assertEqual(decision["action"], ACTION_SWITCH_BUY)
        self.assertEqual(decision["target_bucket"], "25°C")
        self.assertEqual(decision["trigger_type"], "d0_posterior_trajectory_switch")
        self.assertIn("d0_posterior_trajectory_switch", decision["reason_codes"])
        self.assertEqual(
            decision["d0_posterior_trajectory_switch_context"]["decision_inputs"],
            "posterior_probability_and_temperature_trajectory_only",
        )
        self.assertNotIn("market_confirmed", decision["d0_posterior_trajectory_switch_context"])
        self.assertNotIn("held_target_bid", decision["d0_posterior_trajectory_switch_context"])

    def test_d0_market_price_without_trajectory_support_does_not_switch(self) -> None:
        decision = evaluate_realtime_target_adjustment(
            {
                "held_target_bucket": "24°C or below",
                "trajectory_support_state": "supported",
                "observed_today_max_c": 22.0,
                "temperature_momentum_effective_c_per_hour": 0.0,
                "best_bid": 0.01,
                "best_ask": 0.02,
            },
            {
                "event_date": "2026-06-17",
                "weather_likelihood_refreshed_at": "2026-06-17T02:25:42+00:00",
                "expected_peak_hour_local": 15.0,
                "previous_top_bucket": "24°C or below",
                "current_top_bucket": "24°C or below",
                "d0_switch_candidate_bucket": "25°C",
                "market_top_bucket": "25°C",
                "market_top_probability": 0.99,
                "posterior_settlement_bucket_prob_by_label": {
                    "24°c or below": {"posterior_settlement_candidate_bucket_prob": 0.35},
                    "25°c": {"posterior_settlement_candidate_bucket_prob": 0.30},
                },
            },
        )

        self.assertEqual(decision["action"], ACTION_HOLD)
        self.assertFalse(decision["d0_posterior_trajectory_switch_allowed"])


if __name__ == "__main__":
    unittest.main()
