import unittest

from scan_weather_market import _resolve_exit_liquidity_price


class ExitPriceFallbackTests(unittest.TestCase):
    def test_uses_ask_fallback_when_bid_is_zero(self) -> None:
        price, used_ask = _resolve_exit_liquidity_price(
            None,
            0.0,
            0.002,
            allow_ask_fallback=True,
        )
        self.assertAlmostEqual(float(price), 0.002, places=9)
        self.assertTrue(used_ask)

    def test_keeps_positive_preferred_price(self) -> None:
        price, used_ask = _resolve_exit_liquidity_price(
            0.69,
            0.0,
            0.002,
            allow_ask_fallback=True,
        )
        self.assertAlmostEqual(float(price), 0.69, places=9)
        self.assertFalse(used_ask)

    def test_prefers_positive_bid_for_forced_exit(self) -> None:
        price, used_ask = _resolve_exit_liquidity_price(
            None,
            0.68,
            0.7,
            allow_ask_fallback=True,
        )
        self.assertAlmostEqual(float(price), 0.68, places=9)
        self.assertFalse(used_ask)

    def test_returns_none_without_ask_fallback(self) -> None:
        price, used_ask = _resolve_exit_liquidity_price(
            None,
            0.0,
            0.002,
            allow_ask_fallback=False,
        )
        self.assertIsNone(price)
        self.assertFalse(used_ask)


if __name__ == "__main__":
    unittest.main()
