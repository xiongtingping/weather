import tempfile
import unittest
from pathlib import Path

from scan_weather_market import _derive_no_quote_from_yes, _select_rotating_window


class MarketRefreshPriorityRotationTests(unittest.TestCase):
    def test_select_rotating_window_advances_offset(self) -> None:
        items = ["a", "b", "c"]
        with tempfile.TemporaryDirectory() as tmpdir:
            offset_path = Path(tmpdir) / "fixed.offset"
            first = _select_rotating_window(items, 2, offset_path)
            second = _select_rotating_window(items, 2, offset_path)
        self.assertEqual(first, ["a", "b"])
        self.assertEqual(second, ["c", "a"])

    def test_derive_no_quote_from_yes_uses_complement(self) -> None:
        best_bid_no, best_ask_no, spread_no, bid_depth_no, ask_depth_no = _derive_no_quote_from_yes(
            0.40,
            0.41,
            120.0,
            35.0,
        )
        self.assertAlmostEqual(best_bid_no, 0.59)
        self.assertAlmostEqual(best_ask_no, 0.60)
        self.assertAlmostEqual(spread_no, 0.01)
        self.assertAlmostEqual(bid_depth_no, 35.0)
        self.assertAlmostEqual(ask_depth_no, 120.0)


if __name__ == "__main__":
    unittest.main()
