import unittest

import scan_weather_market as swm


class ComposeMarketBootstrapTests(unittest.TestCase):
    def test_should_bootstrap_market_for_compose_error(self) -> None:
        self.assertTrue(swm._should_bootstrap_market_for_compose_error("missing_snapshot"))
        self.assertTrue(swm._should_bootstrap_market_for_compose_error("market_snapshot_expired"))
        self.assertTrue(swm._should_bootstrap_market_for_compose_error("snapshot_time_mismatch"))
        self.assertTrue(swm._should_bootstrap_market_for_compose_error("model_snapshot_expired"))
        self.assertFalse(swm._should_bootstrap_market_for_compose_error(None))
