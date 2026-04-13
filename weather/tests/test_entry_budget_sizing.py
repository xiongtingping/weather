import os
import unittest
from unittest.mock import patch

from scan_weather_market import _apply_kelly_size_in_place, _resolve_order_size


class EntryBudgetSizingTests(unittest.TestCase):
    def test_resolve_order_size_scales_notional_by_budget_ratio(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PM_ORDER_USDC": "5",
                "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "0.70",
                "PM_AUTO_TRADE_ENTRY_BUDGET_MIN_USDC": "1",
                "PM_MAX_ORDER_USDC": "",
                "PM_MAX_ORDER_SIZE": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_ENFORCE_EXCHANGE_MIN_ORDER_SIZE": "0",
            },
            clear=False,
        ):
            size = _resolve_order_size(0.5, None, None)
        self.assertAlmostEqual(float(size), 7.0, places=4)

    def test_resolve_order_size_honors_1u_budget_floor(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PM_ORDER_USDC": "1",
                "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "0.70",
                "PM_AUTO_TRADE_ENTRY_BUDGET_MIN_USDC": "1",
                "PM_MAX_ORDER_USDC": "",
                "PM_MAX_ORDER_SIZE": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_ENFORCE_EXCHANGE_MIN_ORDER_SIZE": "0",
            },
            clear=False,
        ):
            size = _resolve_order_size(0.25, None, None)
        self.assertAlmostEqual(float(size), 4.0, places=4)

    def test_kelly_bankroll_path_scales_budget_before_final_size(self) -> None:
        candidate = {
            "price": 0.5,
            "q_calibrated": 0.8,
        }
        with patch.dict(
            os.environ,
            {
                "PM_KELLY_ENABLED": "1",
                "PM_BANKROLL_USDC": "100",
                "PM_ORDER_USDC": "1",
                "PM_KELLY_FRACTION": "0.25",
                "PM_KELLY_MIN_NOTIONAL_USDC": "1",
                "PM_KELLY_MAX_NOTIONAL_USDC": "",
                "PM_MAX_ORDER_USDC": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_MAX_ORDER_SIZE": "",
                "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "0.70",
                "PM_AUTO_TRADE_ENTRY_BUDGET_MIN_USDC": "1",
            },
            clear=False,
        ):
            _apply_kelly_size_in_place(candidate, allow_apply=True)
        self.assertTrue(bool(candidate.get("kelly_applied")))
        self.assertAlmostEqual(float(candidate.get("kelly_target_notional")), 10.5, places=4)
        self.assertAlmostEqual(float(candidate.get("size")), 21.0, places=4)


if __name__ == "__main__":
    unittest.main()
