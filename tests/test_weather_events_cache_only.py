import os
import unittest
from unittest import mock

import scan_weather_market as swm


def _make_events(prefix: str, count: int = 5):
    out = []
    for idx in range(count):
        city = f"{prefix}-{idx}"
        out.append(
            {
                "slug": f"highest-temperature-in-{city}-on-april-17-2026",
                "title": f"Highest temperature in {city} on April 17?",
                "endDate": "2026-04-17T23:59:59Z",
            }
        )
    return out


class WeatherEventsCacheOnlyTests(unittest.TestCase):
    def test_cache_only_uses_stale_regular_cache_before_network(self) -> None:
        sample = _make_events("cached")

        def _load_cache(*, allow_stale: bool = False):
            return list(sample) if allow_stale else None

        with mock.patch.dict(os.environ, {"PM_MARKET_REFRESH_CACHE_ONLY": "1"}, clear=False):
            with mock.patch.object(swm, "_load_weather_events_cache", side_effect=_load_cache):
                with mock.patch.object(swm, "_load_weather_events_good_cache", return_value=None):
                    with mock.patch.object(swm, "fetch_weather_events_from_page") as page_mock:
                        with mock.patch.object(swm, "fetch_weather_events_from_gamma") as gamma_mock:
                            events, warnings = swm.fetch_weather_events("https://example.com")
        self.assertEqual(events, sample)
        self.assertEqual(warnings, [])
        page_mock.assert_not_called()
        gamma_mock.assert_not_called()

    def test_cache_only_uses_last_good_cache_before_network(self) -> None:
        last_good = _make_events("good")

        with mock.patch.dict(os.environ, {"PM_MARKET_REFRESH_CACHE_ONLY": "1"}, clear=False):
            with mock.patch.object(swm, "_load_weather_events_cache", return_value=None):
                with mock.patch.object(swm, "_load_weather_events_good_cache", return_value=list(last_good)):
                    with mock.patch.object(swm, "_save_weather_events_cache") as save_mock:
                        with mock.patch.object(swm, "fetch_weather_events_from_page") as page_mock:
                            with mock.patch.object(swm, "fetch_weather_events_from_gamma") as gamma_mock:
                                events, warnings = swm.fetch_weather_events("https://example.com")
        self.assertEqual(events, last_good)
        self.assertEqual(
            warnings,
            [f"weather events cache-only: using last_good (events={len(last_good)})"],
        )
        save_mock.assert_called_once_with(last_good)
        page_mock.assert_not_called()
        gamma_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
