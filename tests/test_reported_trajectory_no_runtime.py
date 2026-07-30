from __future__ import annotations

import unittest

from reported_trajectory_no_runtime import (
    conservative_market_temperature,
    normalize_market_buckets,
    parse_metar_temperature,
    select_nearest_impossible_lower_bucket,
    unified_capital_plan,
)


class ReportedTrajectoryNoRuntimeTests(unittest.TestCase):
    def test_parser_reads_only_main_integer_temperature(self) -> None:
        parsed = parse_metar_temperature(
            "METAR ZBAA 171030Z 18004MPS CAVOK 30/20 Q1000 NOSIG RMK T03010201",
            expected_station="ZBAA",
        )
        self.assertEqual(parsed.report_type, "METAR")
        self.assertEqual(parsed.reported_temperature_c, 30)

    def test_parser_accepts_speci_and_negative_temperature(self) -> None:
        parsed = parse_metar_temperature(
            "SPECI CYYZ 171100Z 33010KT 10SM CLR M03/M07 A3010",
            expected_station="CYYZ",
        )
        self.assertEqual(parsed.report_type, "SPECI")
        self.assertEqual(parsed.reported_temperature_c, -3)

    def test_parser_rejects_taf_and_station_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "header"):
            parse_metar_temperature("TAF ZBAA 171100Z 1712/1812 CAVOK TX35/1708Z")
        with self.assertRaisesRegex(ValueError, "station_mismatch"):
            parse_metar_temperature("METAR ZBAA 171030Z CAVOK 30/20 Q1000", expected_station="ZUUU")

    def test_conservative_unit_conversion_is_one_sided(self) -> None:
        self.assertEqual(conservative_market_temperature(36, "C"), 36)
        self.assertEqual(conservative_market_temperature(23, "F"), 73)
        self.assertLessEqual(conservative_market_temperature(23, "F"), 23 * 9 / 5 + 32)

    def test_nearest_impossible_bucket_is_previous_bucket(self) -> None:
        rows = [
            {"outcome": "34°C or below", "no_token": "n34"},
            {"outcome": "35°C", "no_token": "n35"},
            {"outcome": "36°C", "no_token": "n36"},
            {"outcome": "37°C or higher", "no_token": "n37"},
        ]
        chosen = select_nearest_impossible_lower_bucket(rows, hard_lower_bound=36)
        self.assertEqual(chosen["outcome"], "35°C")
        self.assertEqual(chosen["no_token"], "n35")

    def test_ladder_gaps_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "not_contiguous"):
            normalize_market_buckets(
                [{"outcome": "30°C or below"}, {"outcome": "32°C"}, {"outcome": "33°C or higher"}]
            )

    def test_fahrenheit_two_degree_ranges_select_previous_range(self) -> None:
        chosen = select_nearest_impossible_lower_bucket(
            [
                {"outcome": "79°F or below", "no_token": "n79"},
                {"outcome": "80-81°F", "no_token": "n81"},
                {"outcome": "82-83°F", "no_token": "n83"},
                {"outcome": "84°F or higher", "no_token": "n84"},
            ],
            hard_lower_bound=84,
        )
        self.assertEqual(chosen["outcome"], "82-83°F")

    def test_strict_bound_is_exclusive_and_rejects_nonfinite_values(self) -> None:
        rows = [
            {"outcome": "29°C or below"},
            {"outcome": "30°C"},
            {"outcome": "31°C"},
            {"outcome": "32°C or higher"},
        ]
        self.assertEqual(
            select_nearest_impossible_lower_bucket(rows, hard_lower_bound=31)["outcome"],
            "30°C",
        )
        self.assertEqual(
            select_nearest_impossible_lower_bucket(rows, hard_lower_bound=30)["outcome"],
            "29°C or below",
        )
        self.assertIsNone(select_nearest_impossible_lower_bucket(rows, hard_lower_bound=29))
        with self.assertRaisesRegex(ValueError, "hard_lower_bound_non_finite"):
            select_nearest_impossible_lower_bucket(rows, hard_lower_bound=float("nan"))

    def test_only_metar_main_temperature_controls_hard_bound(self) -> None:
        reports = [
            "METAR ZUUU 230000Z CAVOK 31/20 Q1000",
            "METAR ZUUU 230000Z CAVOK 31/20 Q1000 RMK TAF=35 NWP=99 REGIME=UP",
            "SPECI ZUUU 230000Z 00000KT 31/20 Q1000 RMK PROB40 40C",
        ]
        bounds = [
            conservative_market_temperature(
                parse_metar_temperature(report, expected_station="ZUUU").reported_temperature_c,
                "C",
            )
            for report in reports
        ]
        self.assertEqual(bounds, [31, 31, 31])

    def test_capital_uses_one_shared_pool_but_caps_each_order_at_50(self) -> None:
        plan = unified_capital_plan(
            account_free_usdc=100,
            pending_reserved_usdc=10,
            executable_depth_usdc=200,
            max_price=0.99,
        )
        self.assertEqual(plan["shared_cash_available_usdc"], 90)
        self.assertEqual(plan["frozen_order_notional_usdc"], 50)
        self.assertTrue(plan["unified_pool"])
        self.assertFalse(plan["deploy_all_available"])
        self.assertEqual(plan["maximum_order_notional_usdc"], 50)
        self.assertFalse(plan["source_quota_applied"])

    def test_thin_visible_depth_does_not_shrink_fixed_50_usdc_fak(self) -> None:
        plan = unified_capital_plan(
            account_free_usdc=100,
            pending_reserved_usdc=0,
            executable_depth_usdc=15.5924,
            max_price=0.99,
        )
        self.assertEqual(plan["target_order_notional_usdc"], 50)
        self.assertEqual(plan["frozen_order_notional_usdc"], 50)
        self.assertFalse(plan["visible_depth_caps_requested_notional"])
        self.assertTrue(plan["fak_partial_fill_allowed"])
        self.assertEqual(plan["position_priority"], "NO")
        self.assertTrue(plan["existing_yes_positions_ignored"])

    def test_fixed_50_usdc_fails_closed_when_free_cash_is_insufficient(self) -> None:
        plan = unified_capital_plan(
            account_free_usdc=49.99,
            pending_reserved_usdc=0,
            executable_depth_usdc=100,
            max_price=0.99,
        )
        self.assertEqual(plan["frozen_order_notional_usdc"], 0)
        self.assertEqual(plan["capital_block_reason"], "capital_below_fixed_per_bucket_notional")


if __name__ == "__main__":
    unittest.main()
