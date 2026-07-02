import os
import unittest

import scan_weather_market as swm


class TestAutoTradeOpenOrdersCache(unittest.TestCase):
    def setUp(self) -> None:
        self._env = dict(os.environ)
        os.environ["PM_AUTO_TRADE_OPEN_ORDERS_CACHE_SEC"] = "60"
        os.environ["PM_AUTO_TRADE_OPEN_ORDERS_FAIL_COOLDOWN_SEC"] = "60"
        swm._AUTO_TRADE_OPEN_ORDERS_CACHE = {"ts": 0.0, "by_token": {}}
        swm._AUTO_TRADE_OPEN_ORDERS_LAST_FAIL_AT = 0.0
        swm._AUTO_TRADE_OPEN_ORDERS_LAST_FAIL_MSG = ""

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_cache_hits_without_refetch(self) -> None:
        calls = {"count": 0}

        def fake_open_orders():
            calls["count"] += 1
            return True, {
                "open_orders": [
                    {
                        "tokenID": "t1",
                        "side": "BUY",
                        "orderID": "o1",
                        "createdAt": "2026-02-10T00:00:00Z",
                    }
                ]
            }

        orig_fetch = swm._run_node_open_orders
        orig_time = swm.time.time
        now = {"ts": 1000.0}
        swm._run_node_open_orders = fake_open_orders
        swm.time.time = lambda: now["ts"]
        try:
            first = swm._auto_trade_cached_open_orders_by_token(allow_fetch=True)
            second = swm._auto_trade_cached_open_orders_by_token(allow_fetch=True)
            self.assertEqual(calls["count"], 1)
            self.assertIn("t1", first)
            self.assertIn("t1", second)

            now["ts"] = 1061.0
            third = swm._auto_trade_cached_open_orders_by_token(allow_fetch=True)
            self.assertEqual(calls["count"], 2)
            self.assertIn("t1", third)
        finally:
            swm._run_node_open_orders = orig_fetch
            swm.time.time = orig_time

    def test_fail_cooldown_skips_refetch(self) -> None:
        calls = {"count": 0}

        def fake_open_orders_fail():
            calls["count"] += 1
            return False, {
                "error": "exec_nonzero",
                "stderr": '{"headers":{"POLY_API_KEY":"abc","PRIVATE_KEY":"0xdead"}}',
            }

        orig_fetch = swm._run_node_open_orders
        orig_time = swm.time.time
        now = {"ts": 2000.0}
        swm._run_node_open_orders = fake_open_orders_fail
        swm.time.time = lambda: now["ts"]
        try:
            first = swm._auto_trade_cached_open_orders_by_token(allow_fetch=True)
            self.assertEqual(calls["count"], 1)
            self.assertEqual(first, {})

            now["ts"] = 2010.0
            second = swm._auto_trade_cached_open_orders_by_token(allow_fetch=True)
            self.assertEqual(calls["count"], 1)
            self.assertEqual(second, {})
        finally:
            swm._run_node_open_orders = orig_fetch
            swm.time.time = orig_time

    def test_redact_sensitive_text(self) -> None:
        raw = '{"POLY_API_KEY":"abc","PRIVATE_KEY":"0xdead","POLY_SIGNATURE":"sig"} PRIVATE_KEY=0xdead'
        redacted = swm._redact_sensitive_text(raw)
        self.assertNotIn("abc", redacted)
        self.assertNotIn("0xdead", redacted)
        self.assertIn("***", redacted)


if __name__ == "__main__":
    unittest.main()

