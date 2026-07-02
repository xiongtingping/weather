import unittest

import scan_weather_market as swm


class EdgeTradeabilityReasonCodesTests(unittest.TestCase):
    def test_empty_reasons_keeps_base_block_code(self) -> None:
        self.assertEqual(
            swm._edge_tradeability_reason_codes(""),
            ["edge_tradeability_blocked"],
        )

    def test_comma_separated_reasons_expand_into_reason_codes(self) -> None:
        self.assertEqual(
            swm._edge_tradeability_reason_codes("signal_lcb_below_min,edge_lcb_below_min"),
            ["edge_tradeability_blocked", "signal_lcb_below_min", "edge_lcb_below_min"],
        )

    def test_duplicate_reason_codes_are_deduplicated(self) -> None:
        self.assertEqual(
            swm._edge_tradeability_reason_codes(
                "edge_lcb_below_min, edge_lcb_below_min , signal_lcb_below_min"
            ),
            ["edge_tradeability_blocked", "edge_lcb_below_min", "signal_lcb_below_min"],
        )

    def test_merge_reason_codes_preserves_sort_missing_and_deduplicates(self) -> None:
        self.assertEqual(
            swm._merge_reason_codes(
                ["edge_realizable_sort_missing"],
                swm._edge_tradeability_reason_codes("signal_lcb_below_min"),
                ["edge_realizable_sort_missing"],
            ),
            ["edge_realizable_sort_missing", "edge_tradeability_blocked", "signal_lcb_below_min"],
        )

    def test_market_tick_stale_preserves_tradeability_block_reason(self) -> None:
        self.assertEqual(
            swm._market_tick_stale_reason_codes(
                {
                    "edge_tradeability_blocked": True,
                    "edge_tradeability_reasons": "signal_lcb_below_min",
                    "edge_tradeability_profit_value": 0.004,
                }
            ),
            ["market_tick_stale", "edge_tradeability_blocked", "signal_lcb_below_min"],
        )

    def test_market_tick_stale_with_blocked_flag_adds_base_block_code(self) -> None:
        self.assertEqual(
            swm._market_tick_stale_reason_codes({"edge_tradeability_blocked": True}),
            ["market_tick_stale", "edge_tradeability_blocked"],
        )


if __name__ == "__main__":
    unittest.main()
