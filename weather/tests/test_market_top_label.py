import os
import unittest


class TestMarketTopLabel(unittest.TestCase):
    def setUp(self) -> None:
        # Avoid leaking env across tests.
        self._old_env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_market_top_label_ignores_illiquid_high_ask(self) -> None:
        # Import here to keep module import side effects localized to this test.
        from ui.server import market_top_label

        dist = [
            {
                "outcome": "12°C or higher",
                "best_bid": 0.08,
                "best_ask": 0.91,
                "spread": 0.83,
                "yes_price": 0.905,  # can be misleading if derived from ask in sparse books
            },
            {
                "outcome": "10°C",
                "best_bid": 0.41,
                "best_ask": 0.45,
                "spread": 0.04,
                "yes_price": 0.41,
            },
        ]

        self.assertEqual(market_top_label(dist), "10°C")

    def test_market_top_label_mid_when_spread_is_tight(self) -> None:
        from ui.server import market_implied_price_for_row

        os.environ["PM_MARKET_TOP_MAX_SPREAD"] = "0.10"
        row = {"best_bid": 0.40, "best_ask": 0.46, "spread": 0.06}
        price, src = market_implied_price_for_row(row)
        self.assertAlmostEqual(price or 0.0, 0.43, places=6)
        self.assertEqual(src, "mid")

    def test_market_top_label_ask_only_is_capped(self) -> None:
        from ui.server import market_implied_price_for_row

        # Ask-only > cap should be ignored.
        os.environ["PM_MARKET_TOP_MAX_ASK_ONLY"] = "0.60"
        price, src = market_implied_price_for_row({"best_ask": 0.91})
        self.assertIsNone(price)
        self.assertEqual(src, "none")


if __name__ == "__main__":
    unittest.main()

