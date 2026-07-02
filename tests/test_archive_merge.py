import unittest

from scan_weather_market import _merge_rows_preserve_edge_scores


class TestArchiveMerge(unittest.TestCase):
    def test_preserve_missing_edge_from_previous_snapshot(self) -> None:
        current_rows = [
            {
                "outcome": "20-21 C",
                "best_ask": None,
                "ask_missing_reason_yes": "market_depth_empty",
                "edge_score_yes": None,
                "entry_price_yes": None,
                "signal_edge_round": None,
                "edge_min_yes": None,
                "best_ask_no": None,
                "ask_missing_reason_no": "market_depth_empty",
                "edge_score_no": None,
            }
        ]
        previous_rows = [
            {
                "outcome": "20-21 C",
                "best_ask": 0.44,
                "ask_missing_reason_yes": None,
                "edge_score_yes": 9.8,
                "entry_price_yes": 0.44,
                "signal_edge_round": 0.05,
                "edge_min_yes": 0.04,
                "best_ask_no": 0.58,
                "ask_missing_reason_no": None,
                "edge_score_no": 0.0,
            }
        ]

        merged = _merge_rows_preserve_edge_scores(current_rows, previous_rows)

        self.assertEqual(len(merged), 1)
        row = merged[0]
        self.assertEqual(row.get("best_ask"), 0.44)
        self.assertEqual(row.get("edge_score_yes"), 9.8)
        self.assertEqual(row.get("entry_price_yes"), 0.44)
        self.assertEqual(row.get("signal_edge_round"), 0.05)
        self.assertEqual(row.get("edge_min_yes"), 0.04)
        self.assertIsNone(row.get("ask_missing_reason_yes"))
        self.assertEqual(row.get("best_ask_no"), 0.58)
        self.assertEqual(row.get("edge_score_no"), 0.0)
        self.assertIsNone(row.get("ask_missing_reason_no"))
        self.assertIn("snapshot_fallback_fields", row)

    def test_keep_current_values_when_present(self) -> None:
        current_rows = [
            {
                "outcome": "22 C or higher",
                "best_ask": 0.31,
                "ask_missing_reason_yes": None,
                "edge_score_yes": 7.1,
                "best_ask_no": 0.72,
                "ask_missing_reason_no": None,
                "edge_score_no": 1.2,
            }
        ]
        previous_rows = [
            {
                "outcome": "22 C or higher",
                "best_ask": 0.55,
                "ask_missing_reason_yes": "orderbook_fetch_disabled",
                "edge_score_yes": 2.0,
                "best_ask_no": 0.45,
                "ask_missing_reason_no": "orderbook_fetch_disabled",
                "edge_score_no": 9.9,
            }
        ]

        merged = _merge_rows_preserve_edge_scores(current_rows, previous_rows)

        self.assertEqual(len(merged), 1)
        row = merged[0]
        self.assertEqual(row.get("best_ask"), 0.31)
        self.assertEqual(row.get("edge_score_yes"), 7.1)
        self.assertEqual(row.get("best_ask_no"), 0.72)
        self.assertEqual(row.get("edge_score_no"), 1.2)
        self.assertNotIn("snapshot_fallback_fields", row)


if __name__ == "__main__":
    unittest.main()
