import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import check_runtime_env_health as health


class RuntimeEnvHealthTests(unittest.TestCase):
    def test_default_labels_exclude_legacy_trade_sync_loop(self) -> None:
        self.assertIn("com.weather.trade_engine", health.DEFAULT_LABELS)
        self.assertNotIn("com.weather.trade_sync_loop", health.DEFAULT_LABELS)
        self.assertIn("com.weather.trade_sync_loop", health.LEGACY_LABELS)

    def test_parse_env_accepts_bare_private_key_literal_without_value_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            private_key = "0x" + "a" * 64
            path.write_text(
                "\n".join(
                    [
                        "PM_AUTO_TRADE_ENABLED=1",
                        private_key,
                        "bad line without equals",
                        "POLY_API_KEY=test-key",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            public, values = health._parse_env_file(path)

        self.assertEqual(values.get("PRIVATE_KEY"), private_key)
        self.assertEqual(public["private_key_literal_lines"], [2])
        self.assertEqual(public["invalid_lines"], [{"line": 3, "reason": "not_key_value"}])
        self.assertNotIn(private_key, str(public))
        self.assertNotIn("test-key", str(public))

    def test_proxy_status_marks_replacement_without_secret_leak(self) -> None:
        values = {
            "HTTPS_PROXY": "http://user:pass@127.0.0.1:7897",
            "PM_PROXY_PORTS": "7897,7890",
        }
        with mock.patch.object(
            health,
            "_tcp_reachable",
            side_effect=lambda host, port, timeout=0.4: host == "127.0.0.1" and int(port or 0) == 7890,
        ), mock.patch.object(health, "_scutil_proxy_ports", return_value=[]):
            payload = health._proxy_status(values)

        self.assertFalse(payload["current_reachable"])
        self.assertEqual(payload["replacement_available"]["port"], 7890)
        self.assertEqual(payload["current"]["safe_url"], "http://127.0.0.1:7897")
        self.assertNotIn("user", str(payload))
        self.assertNotIn("pass", str(payload))


if __name__ == "__main__":
    unittest.main()
