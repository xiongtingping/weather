from __future__ import annotations

import unittest

from weather_basket_yes_runtime import (
    conservative_market_temperature,
    normalize_market_buckets,
    parse_metar_temperature,
)


class WeatherBasketYesRuntimeTests(unittest.TestCase):
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

    def test_ladder_gaps_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "not_contiguous"):
            normalize_market_buckets(
                [{"outcome": "30°C or below"}, {"outcome": "32°C"}, {"outcome": "33°C or higher"}]
            )

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

if __name__ == "__main__":
    unittest.main()
