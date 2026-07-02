import os
import unittest
from unittest import mock

import scan_weather_market as swm


class ProxyLocalFallbackTests(unittest.TestCase):
    def test_open_meteo_is_not_default_proxy_bypass_host(self) -> None:
        self.assertFalse(
            swm._should_bypass_proxy(
                "https://api.open-meteo.com/v1/forecast",
                requested=False,
            )
        )
        self.assertFalse(
            swm._should_bypass_proxy(
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                requested=False,
            )
        )
        self.assertTrue(
            swm._should_bypass_proxy(
                "https://api.open-meteo.com/v1/forecast",
                requested=True,
            )
        )

    def test_ensure_system_proxy_settings_uses_local_proxy_fallback(self) -> None:
        env = {
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
            "PM_PROXY_LOCAL_FALLBACK_PORTS": "7897",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            swm._SYSTEM_PROXY_LAST_CHECK = 0.0
            with mock.patch.object(swm.shutil, "which", return_value="scutil"):
                with mock.patch.object(swm.subprocess, "check_output", return_value="HTTPEnable : 0\nHTTPSEnable : 0\nSOCKSEnable : 0\n"):
                    with mock.patch.object(
                        swm,
                        "_proxy_reachable",
                        side_effect=lambda host, port, timeout=0.5: host == "127.0.0.1" and int(port or 0) == 7897,
                    ):
                        swm.ensure_system_proxy_settings(force=True)
                        self.assertEqual(os.environ.get("HTTP_PROXY"), "http://127.0.0.1:7897")
                        self.assertEqual(os.environ.get("HTTPS_PROXY"), "http://127.0.0.1:7897")
                        self.assertEqual(os.environ.get("http_proxy"), "http://127.0.0.1:7897")
                        self.assertEqual(os.environ.get("https_proxy"), "http://127.0.0.1:7897")

    def test_build_node_env_uses_local_proxy_fallback(self) -> None:
        env = {
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
            "PM_PROXY_LOCAL_FALLBACK_PORTS": "7897",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(
                swm,
                "_proxy_reachable",
                side_effect=lambda host, port, timeout=0.5: host == "127.0.0.1" and int(port or 0) == 7897,
            ):
                node_env = swm._build_node_env()
        self.assertEqual(node_env.get("HTTP_PROXY"), "http://127.0.0.1:7897")
        self.assertEqual(node_env.get("HTTPS_PROXY"), "http://127.0.0.1:7897")
        self.assertEqual(node_env.get("http_proxy"), "http://127.0.0.1:7897")
        self.assertEqual(node_env.get("https_proxy"), "http://127.0.0.1:7897")
        self.assertEqual(node_env.get("GLOBAL_AGENT_HTTP_PROXY"), "http://127.0.0.1:7897")


if __name__ == "__main__":
    unittest.main()
