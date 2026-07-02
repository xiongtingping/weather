import unittest

from posterior_settlement_runtime import (
    LANE_NO_REMOTE,
    LANE_OBSERVE,
    LANE_YES_TARGET,
    apply_posterior_observed_constraints_to_distribution,
    classify_posterior_settlement_candidate,
    event_target_context,
    infer_trajectory_support_state,
)


class PosteriorSettlementRuntimeTests(unittest.TestCase):
    def test_yes_target_allowed_when_bayes_target_and_reliability_pass(self) -> None:
        profile = classify_posterior_settlement_candidate(
            {
                "side": "YES",
                "outcome": "33°C",
                "target_bucket": "33°C",
                "target_posterior": 0.42,
                "target_realization_reliability": 0.74,
            }
        )

        self.assertEqual(profile["strategy_family"], "posterior_settlement")
        self.assertEqual(profile["strategy_lane"], LANE_YES_TARGET)
        self.assertTrue(profile["posterior_settlement_entry_allowed"])
        self.assertFalse(profile["take_profit_enabled"])
        self.assertFalse(profile["stop_loss_enabled"])
        self.assertTrue(profile["settlement_exit_only"])

    def test_yes_target_observe_when_reliability_below_min(self) -> None:
        profile = classify_posterior_settlement_candidate(
            {
                "side": "YES",
                "outcome": "33°C",
                "target_bucket": "33°C",
                "target_posterior": 0.42,
                "target_realization_reliability": 0.59,
            }
        )

        self.assertEqual(profile["strategy_lane"], LANE_OBSERVE)
        self.assertFalse(profile["posterior_settlement_entry_allowed"])
        self.assertEqual(
            profile["posterior_settlement_entry_block_reason"],
            "target_realization_reliability_below_min",
        )

    def test_ssot_second_target_yes_add_is_allowed(self) -> None:
        profile = classify_posterior_settlement_candidate(
            {
                "side": "YES",
                "outcome": "28°C",
                "target_bucket": "27°C or below",
                "target_posterior": 0.39,
                "second_target_bucket": "28°C",
                "second_target_posterior": 0.16,
                "target_realization_reliability": 0.87,
                "target_realization_plan_action": "add",
                "target_realization_leg_role": "second_target_yes",
            }
        )

        self.assertEqual(profile["strategy_lane"], LANE_YES_TARGET)
        self.assertTrue(profile["posterior_settlement_entry_allowed"])
        self.assertEqual(profile["posterior_settlement_entry_block_reason"], "")

    def test_remote_no_allowed_only_when_far_and_low_realization_prob(self) -> None:
        profile = classify_posterior_settlement_candidate(
            {
                "side": "NO",
                "outcome": "35°C",
                "target_bucket": "33°C",
                "target_posterior": 0.44,
                "candidate_bucket_realization_prob": 0.08,
            }
        )

        self.assertEqual(profile["strategy_lane"], LANE_NO_REMOTE)
        self.assertTrue(profile["posterior_settlement_entry_allowed"])
        self.assertEqual(profile["remote_no_bucket"], "35°C")
        self.assertEqual(
            profile["remote_no_reason"],
            "far_from_target_and_low_realization_prob",
        )

    def test_remote_no_rejects_bucket_below_target(self) -> None:
        profile = classify_posterior_settlement_candidate(
            {
                "side": "NO",
                "outcome": "31°C",
                "target_bucket": "33°C",
                "target_posterior": 0.44,
                "candidate_bucket_realization_prob": 0.08,
            }
        )

        self.assertEqual(profile["strategy_lane"], LANE_OBSERVE)
        self.assertFalse(profile["posterior_settlement_entry_allowed"])
        self.assertEqual(
            profile["posterior_settlement_entry_block_reason"],
            "remote_no_must_be_above_target_bucket",
        )

    def test_remote_no_rejects_bucket_above_plus_three(self) -> None:
        profile = classify_posterior_settlement_candidate(
            {
                "side": "NO",
                "outcome": "35°C",
                "target_bucket": "31°C",
                "target_posterior": 0.44,
                "candidate_bucket_realization_prob": 0.01,
            }
        )

        self.assertEqual(profile["strategy_lane"], LANE_OBSERVE)
        self.assertFalse(profile["posterior_settlement_entry_allowed"])
        self.assertEqual(
            profile["posterior_settlement_entry_block_reason"],
            "remote_no_bucket_too_far",
        )

    def test_remote_no_rejects_target_same_bucket(self) -> None:
        profile = classify_posterior_settlement_candidate(
            {
                "side": "NO",
                "outcome": "33°C",
                "target_bucket": "33°C",
                "target_posterior": 0.44,
                "candidate_bucket_realization_prob": 0.08,
            }
        )

        self.assertEqual(profile["strategy_lane"], LANE_OBSERVE)
        self.assertFalse(profile["posterior_settlement_entry_allowed"])
        self.assertEqual(
            profile["posterior_settlement_entry_block_reason"],
            "remote_no_cannot_target_same_bucket",
        )

    def test_event_target_context_uses_bayes_only_not_q_calibrated_fallback(self) -> None:
        context = event_target_context(
            [
                {"outcome": "31°C or below", "q_calibrated": 0.90},
                {"outcome": "32°C", "q_bayes_posterior": 0.35},
                {"outcome": "33°C", "q_temperature_bucket_posterior": 0.40},
            ]
        )

        self.assertEqual(context["target_bucket"], "33°C")
        self.assertEqual(context["second_target_bucket"], "32°C")
        self.assertNotIn("31°c or below", context["posterior_settlement_bucket_prob_by_label"])

    def test_trajectory_support_uses_observed_max_floor_when_current_source_lags(self) -> None:
        state = infer_trajectory_support_state(
            {
                "target_bucket": "33°C",
                "observed_temp_c": 31.11,
                "observed_today_max_c": 33.0,
                "temperature_momentum_effective_c_per_hour": -0.20,
            },
            target_bucket="33°C",
        )

        self.assertEqual(state, "supported")

    def test_event_target_context_zeroes_physical_hard_zero_before_target_sort(self) -> None:
        context = event_target_context(
            [
                {
                    "outcome": "24°C or below",
                    "q_bayes_posterior": 0.48,
                    "q_bayes_physical_likelihood_hard_zero": True,
                    "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
                },
                {"outcome": "25°C", "q_temperature_bucket_posterior": 0.42},
                {"outcome": "26°C", "q_temperature_bucket_posterior": 0.10},
            ]
        )

        self.assertEqual(context["target_bucket"], "25°C")
        self.assertEqual(context["second_target_bucket"], "26°C")
        hard_zero_meta = context["posterior_settlement_bucket_prob_by_label"]["24°c or below"]
        self.assertEqual(hard_zero_meta["posterior_settlement_candidate_bucket_prob"], 0.0)
        self.assertEqual(hard_zero_meta["posterior_settlement_candidate_bucket_prob_raw"], 0.48)
        self.assertTrue(hard_zero_meta["posterior_settlement_candidate_bucket_hard_zero"])
        self.assertIn(
            "observed_today_max_above_bucket",
            hard_zero_meta["posterior_settlement_candidate_bucket_hard_zero_reasons"],
        )

    def test_event_target_context_reweights_by_observed_temperature_feasibility(self) -> None:
        context = event_target_context(
            [
                {
                    "outcome": "24°C or below",
                    "q_temperature_bucket_posterior": 0.50,
                    "q_bayes_physical_likelihood_observed_prob": 0.001,
                },
                {
                    "outcome": "25°C",
                    "q_temperature_bucket_posterior": 0.30,
                    "q_bayes_physical_likelihood_observed_prob": 0.98,
                },
                {
                    "outcome": "26°C",
                    "q_temperature_bucket_posterior": 0.20,
                    "q_bayes_physical_likelihood_observed_prob": 0.10,
                },
            ]
        )

        self.assertEqual(context["target_bucket"], "25°C")
        self.assertGreater(context["target_posterior"], 0.90)
        low_meta = context["posterior_settlement_bucket_prob_by_label"]["24°c or below"]
        self.assertLess(low_meta["posterior_settlement_candidate_bucket_prob"], 0.01)
        self.assertEqual(
            low_meta["posterior_settlement_candidate_bucket_prob_pre_observed_feasibility"],
            0.50,
        )
        self.assertEqual(low_meta["posterior_settlement_candidate_observed_feasibility_factor"], 0.001)

    def test_apply_observed_constraints_zeroes_broken_24_bucket_and_reweights_top(self) -> None:
        rows = [
            {
                "outcome": "24°C or below",
                "q_temperature_bucket_posterior": 0.12944318649698958,
                "q_bayes_posterior": 0.21306231723841437,
                "q_bayes_posterior_no": 0.7869376827615856,
                "q_bayes_physical_likelihood_hard_zero": True,
                "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
                "q_bayes_physical_likelihood_observed_prob": 2.9555807870642647e-12,
            },
            {
                "outcome": "25°C",
                "q_temperature_bucket_posterior": 0.1543376965824814,
                "q_bayes_posterior": 0.1463556543535873,
                "q_bayes_posterior_no": 0.8536443456464127,
                "q_bayes_physical_likelihood_hard_zero": False,
                "q_bayes_physical_likelihood_observed_prob": 0.9848099373879884,
            },
            {
                "outcome": "26°C",
                "q_temperature_bucket_posterior": 0.08965027592551895,
                "q_bayes_posterior": 0.06539899715681338,
                "q_bayes_posterior_no": 0.9346010028431866,
                "q_bayes_physical_likelihood_hard_zero": False,
                "q_bayes_physical_likelihood_observed_prob": 0.01519006146580636,
            },
        ]

        changed = apply_posterior_observed_constraints_to_distribution(rows)

        self.assertTrue(changed)
        by_label = {row["outcome"]: row for row in rows}
        low = by_label["24°C or below"]
        self.assertEqual(low["q_temperature_bucket_posterior"], 0.0)
        self.assertEqual(low["q_bayes_posterior"], 0.0)
        self.assertEqual(low["q_temperature_bucket_posterior_pre_observed_constraints"], 0.12944318649698958)
        self.assertEqual(low["q_bayes_posterior_pre_observed_constraints"], 0.21306231723841437)
        self.assertTrue(low["q_posterior_observed_constraints_hard_zero"])
        self.assertIn(
            "observed_today_max_above_bucket",
            low["q_posterior_observed_constraints_reason_codes"],
        )
        self.assertGreater(by_label["25°C"]["q_temperature_bucket_posterior"], 0.99)
        self.assertGreater(by_label["25°C"]["q_bayes_posterior"], 0.99)
        self.assertLess(by_label["26°C"]["q_temperature_bucket_posterior"], 0.01)

    def test_future_physical_hard_zero_does_not_apply_observed_constraint(self) -> None:
        rows = [
            {
                "outcome": "22°C",
                "q_temperature_bucket_posterior": 0.20,
                "q_bayes_posterior": 0.20,
                "market_closed": False,
                "event_date": "2026-06-30",
                "zuuu_metar_raw_awc_target_event_date": "2026-06-30",
                "zuuu_metar_raw_awc_observation_local_date": "2026-06-28",
                "q_bayes_physical_d0_explanation": {"d0": False},
                "q_bayes_physical_likelihood_hard_zero": True,
                "q_bayes_physical_likelihood_hard_reasons": ["dewpoint_above_bucket"],
            },
            {
                "outcome": "30°C or higher",
                "q_temperature_bucket_posterior": 0.80,
                "q_bayes_posterior": 0.80,
                "market_closed": False,
                "event_date": "2026-06-30",
                "zuuu_metar_raw_awc_target_event_date": "2026-06-30",
                "zuuu_metar_raw_awc_observation_local_date": "2026-06-28",
                "q_bayes_physical_d0_explanation": {"d0": False},
            },
        ]

        changed = apply_posterior_observed_constraints_to_distribution(rows)

        self.assertFalse(changed)
        self.assertEqual(rows[0]["q_bayes_posterior"], 0.20)
        self.assertNotIn("q_posterior_observed_constraints_applied", rows[0])
        self.assertNotIn("posterior_settlement_candidate_bucket_hard_zero", rows[0])

    def test_future_stale_observed_constraint_meta_is_cleared(self) -> None:
        rows = [
            {
                "outcome": "22°C",
                "q_temperature_bucket_posterior": 0.0,
                "q_temperature_bucket_posterior_pre_observed_constraints": 0.20,
                "q_bayes_posterior": 0.0,
                "q_bayes_posterior_pre_observed_constraints": 0.20,
                "q_bayes_posterior_no": 1.0,
                "q_bayes_posterior_no_pre_observed_constraints": 0.80,
                "q_posterior_observed_constraints_applied": True,
                "q_posterior_observed_constraints_source": "posterior_settlement_event_target_context",
                "q_posterior_observed_constraints_hard_zero": True,
                "q_posterior_observed_constraints_reason_codes": ["dewpoint_above_bucket"],
                "market_closed": False,
                "event_date": "2026-06-30",
                "zuuu_metar_raw_awc_target_event_date": "2026-06-30",
                "zuuu_metar_raw_awc_observation_local_date": "2026-06-28",
                "q_bayes_physical_d0_explanation": {"d0": False},
                "q_bayes_physical_likelihood_hard_zero": True,
                "q_bayes_physical_likelihood_hard_reasons": ["dewpoint_above_bucket"],
            },
            {
                "outcome": "30°C or higher",
                "q_temperature_bucket_posterior": 1.0,
                "q_temperature_bucket_posterior_pre_observed_constraints": 0.80,
                "q_bayes_posterior": 1.0,
                "q_bayes_posterior_pre_observed_constraints": 0.80,
                "q_bayes_posterior_no": 0.0,
                "q_bayes_posterior_no_pre_observed_constraints": 0.20,
                "q_posterior_observed_constraints_applied": True,
                "q_posterior_observed_constraints_source": "posterior_settlement_event_target_context",
                "q_posterior_observed_constraints_hard_zero": False,
                "market_closed": False,
                "event_date": "2026-06-30",
                "zuuu_metar_raw_awc_target_event_date": "2026-06-30",
                "zuuu_metar_raw_awc_observation_local_date": "2026-06-28",
                "q_bayes_physical_d0_explanation": {"d0": False},
            },
        ]

        changed = apply_posterior_observed_constraints_to_distribution(rows)

        self.assertTrue(changed)
        self.assertEqual(rows[0]["q_bayes_posterior"], 0.20)
        self.assertEqual(rows[0]["q_temperature_bucket_posterior"], 0.20)
        self.assertEqual(rows[0]["q_bayes_posterior_no"], 0.80)
        self.assertFalse(rows[0]["q_posterior_observed_constraints_applied"])
        self.assertFalse(rows[0]["q_posterior_observed_constraints_hard_zero"])
        self.assertEqual(
            rows[0]["q_posterior_observed_constraints_skipped_reason"],
            "observed_constraints_not_applicable",
        )
        self.assertNotIn("q_posterior_observed_constraints_source", rows[0])
        self.assertFalse(rows[1]["q_posterior_observed_constraints_applied"])

    def test_hard_source_final_lock_rows_are_not_rewritten(self) -> None:
        rows = [
            {
                "outcome": "29°C",
                "q_temperature_bucket_posterior": 0.0,
                "q_temperature_bucket_posterior_pre_observed_constraints": 0.40,
                "q_bayes_posterior": 0.0,
                "q_bayes_posterior_pre_observed_constraints": 0.40,
                "hard_source_posterior_override_applied": True,
                "hard_source_posterior_override_final_bucket_lock": True,
                "q_posterior_observed_constraints_applied": True,
                "q_posterior_observed_constraints_source": "hard_source_posterior_override",
                "q_posterior_observed_constraints_hard_zero": True,
            },
            {
                "outcome": "30°C",
                "q_temperature_bucket_posterior": 1.0,
                "q_temperature_bucket_posterior_pre_observed_constraints": 0.60,
                "q_bayes_posterior": 1.0,
                "q_bayes_posterior_pre_observed_constraints": 0.60,
                "hard_source_posterior_override_applied": True,
                "hard_source_posterior_override_final_bucket_lock": True,
                "q_posterior_observed_constraints_applied": True,
                "q_posterior_observed_constraints_source": "hard_source_posterior_override",
                "q_posterior_observed_constraints_hard_zero": False,
            },
        ]

        changed = apply_posterior_observed_constraints_to_distribution(rows)

        self.assertFalse(changed)
        self.assertEqual(rows[0]["q_bayes_posterior"], 0.0)
        self.assertEqual(rows[1]["q_bayes_posterior"], 1.0)
        self.assertTrue(rows[0]["q_posterior_observed_constraints_applied"])
        self.assertEqual(rows[0]["q_posterior_observed_constraints_source"], "hard_source_posterior_override")


if __name__ == "__main__":
    unittest.main()
