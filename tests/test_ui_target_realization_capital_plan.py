import unittest
from unittest.mock import patch

from ui.server import _build_target_realization_capital_plan


class UiTargetRealizationCapitalPlanTest(unittest.TestCase):
    def test_capital_plan_uses_posterior_top1_before_snapshot_target_selection(self):
        item = {
            "run_date": "2026-06-18",
            "event_date": "2026-06-18",
            "target_bucket_selection": {
                "target_bucket": "30°C",
                "watch_buckets": [
                    {"outcome": "30°C"},
                    {"outcome": "31°C"},
                    {"outcome": "29°C"},
                ],
            },
            "distribution": [
                {"outcome": "28°C", "q_temperature_bucket_posterior": 0.90, "best_ask": 0.05},
                {"outcome": "30°C", "q_temperature_bucket_posterior": 0.68, "best_ask": 0.55},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.30, "best_ask": 0.35},
                {"outcome": "29°C", "q_temperature_bucket_posterior": 0.02, "best_ask": 0.15},
            ],
        }
        with patch.dict(
            "os.environ",
            {
                "PM_TARGET_REALIZATION_BANKROLL_USDC": "900",
                "PM_TARGET_REALIZATION_TODAY_RATIO": "1",
                "PM_TARGET_REALIZATION_TOMORROW_RATIO": "0",
                "PM_TARGET_REALIZATION_DAY_AFTER_RATIO": "0",
            },
            clear=False,
        ):
            plan = _build_target_realization_capital_plan(item)

        self.assertTrue(plan["ok"])
        self.assertEqual(plan["target_bucket"], "28°C")
        self.assertEqual(plan["second_target_bucket"], "30°C")
        self.assertEqual(plan["target_bucket_selection_source"], "posterior_distribution_top1")
        self.assertEqual(plan["legs"][0]["bucket"], "28°C")
        self.assertEqual(plan["legs"][1]["bucket"], "30°C")

    def test_remote_no_uses_bucket_distance_window_above_target(self):
        item = {
            "run_date": "2026-07-01",
            "event_date": "2026-07-01",
            "target_bucket_selection": {
                "target_bucket": "31°C",
                "watch_buckets": [
                    {"outcome": "31°C"},
                    {"outcome": "32°C"},
                ],
            },
            "distribution": [
                {
                    "outcome": "31°C",
                    "q_temperature_bucket_posterior": 0.40,
                    "best_ask": 0.42,
                    "best_ask_no": 0.60,
                },
                {
                    "outcome": "32°C",
                    "q_temperature_bucket_posterior": 0.22,
                    "best_ask": 0.24,
                    "best_ask_no": 0.80,
                },
                {
                    "outcome": "33°C",
                    "q_temperature_bucket_posterior": 0.14,
                    "best_ask": 0.16,
                    "best_ask_no": 0.86,
                },
                {
                    "outcome": "34°C",
                    "q_temperature_bucket_posterior": 0.12,
                    "best_ask": 0.14,
                    "best_ask_no": 0.88,
                },
                {
                    "outcome": "35°C or higher",
                    "q_temperature_bucket_posterior": 0.10,
                    "best_ask": 0.12,
                    "best_ask_no": 0.90,
                },
            ],
        }
        with patch.dict(
            "os.environ",
            {
                "PM_TARGET_REALIZATION_BANKROLL_USDC": "900",
                "PM_TARGET_REALIZATION_TODAY_RATIO": "1",
                "PM_TARGET_REALIZATION_TOMORROW_RATIO": "0",
                "PM_TARGET_REALIZATION_DAY_AFTER_RATIO": "0",
                "PM_TARGET_REALIZATION_REMOTE_NO_MIN_BUCKET_DISTANCE": "2",
                "PM_TARGET_REALIZATION_REMOTE_NO_MAX_BUCKET_DISTANCE": "3",
            },
            clear=False,
        ):
            plan = _build_target_realization_capital_plan(item)

        remote_no_legs = [
            leg
            for leg in plan["legs"]
            if leg["role"] == "remote_no" and leg["side"] == "NO"
        ]
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["target_bucket"], "31°C")
        self.assertEqual(plan["second_target_bucket"], "32°C")
        self.assertEqual([leg["bucket"] for leg in remote_no_legs], ["33°C", "34°C"])
        self.assertEqual([leg["remote_no_bucket_distance"] for leg in remote_no_legs], [2, 3])
        self.assertEqual([leg["remote_no_bucket_direction"] for leg in remote_no_legs], ["above_target", "above_target"])
        self.assertNotIn("35°C or higher", [leg["bucket"] for leg in remote_no_legs])
        self.assertEqual(plan["remote_no_min_bucket_distance"], 2)
        self.assertEqual(plan["remote_no_max_bucket_distance"], 3)
        self.assertEqual(plan["remote_no_bucket_direction"], "below_and_above_target")

    def test_remote_no_uses_lower_ring_when_target_is_top_bucket(self):
        item = {
            "run_date": "2026-07-01",
            "event_date": "2026-07-01",
            "target_bucket_selection": {
                "target_bucket": "35°C or higher",
                "watch_buckets": [
                    {"outcome": "35°C or higher"},
                    {"outcome": "34°C"},
                ],
            },
            "distribution": [
                {"outcome": "35°C or higher", "q_temperature_bucket_posterior": 0.40, "best_ask": 0.42, "best_ask_no": 0.60},
                {"outcome": "34°C", "q_temperature_bucket_posterior": 0.22, "best_ask": 0.24, "best_ask_no": 0.80},
                {"outcome": "33°C", "q_temperature_bucket_posterior": 0.14, "best_ask": 0.16, "best_ask_no": 0.86},
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.12, "best_ask": 0.14, "best_ask_no": 0.88},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.10, "best_ask": 0.12, "best_ask_no": 0.90},
            ],
        }
        with patch.dict(
            "os.environ",
            {
                "PM_TARGET_REALIZATION_BANKROLL_USDC": "900",
                "PM_TARGET_REALIZATION_TODAY_RATIO": "1",
                "PM_TARGET_REALIZATION_TOMORROW_RATIO": "0",
                "PM_TARGET_REALIZATION_DAY_AFTER_RATIO": "0",
                "PM_TARGET_REALIZATION_REMOTE_NO_MIN_BUCKET_DISTANCE": "2",
                "PM_TARGET_REALIZATION_REMOTE_NO_MAX_BUCKET_DISTANCE": "3",
            },
            clear=False,
        ):
            plan = _build_target_realization_capital_plan(item)

        remote_no_legs = [
            leg
            for leg in plan["legs"]
            if leg["role"] == "remote_no" and leg["side"] == "NO"
        ]
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["target_bucket"], "35°C or higher")
        self.assertEqual([leg["bucket"] for leg in remote_no_legs], ["33°C", "32°C"])
        self.assertEqual([leg["remote_no_bucket_direction"] for leg in remote_no_legs], ["below_target", "below_target"])


if __name__ == "__main__":
    unittest.main()
