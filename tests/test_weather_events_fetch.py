import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scan_weather_market as swm


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")

    def json(self):
        return self._payload


class TestWeatherEventsCache(unittest.TestCase):
    def test_fetch_weather_events_ignores_tiny_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            cache_path = tmp_dir / "cache.json"
            good_path = tmp_dir / "good.json"

            tiny_payload = {
                "updated_at": swm.utc_now_iso(),
                "events": [
                    {"slug": "a", "title": "Highest temperature in X on February 10?", "endDate": "2026-02-10T00:00:00Z"},
                    {"slug": "b", "title": "Highest temperature in Y on February 10?", "endDate": "2026-02-10T00:00:00Z"},
                ],
            }
            cache_path.write_text(json.dumps(tiny_payload), encoding="utf-8")

            with mock.patch.object(swm, "DATA_DIR", tmp_dir), mock.patch.object(
                swm, "WEATHER_EVENTS_CACHE_PATH", cache_path
            ), mock.patch.object(swm, "WEATHER_EVENTS_CACHE_GOOD_PATH", good_path), mock.patch.object(
                swm, "fetch_weather_events_from_page", return_value=[]
            ), mock.patch.object(
                swm,
                "fetch_weather_events_from_gamma",
                return_value=(
                    [
                        {
                            "slug": "c",
                            "title": "Highest temperature in Z on February 10?",
                            "endDate": "2026-02-10T00:00:00Z",
                            "tags": [{"slug": "weather", "label": "Weather"}],
                        }
                    ],
                    [],
                ),
            ):
                events, warnings = swm.fetch_weather_events("https://example.invalid/weather")
                self.assertEqual(warnings, [])
                self.assertEqual([e.get("slug") for e in events], ["c"])


class TestWeatherEventsGamma(unittest.TestCase):
    def test_gamma_uses_tag_slug_by_default(self) -> None:
        def _requests_get(url, *, params=None, **_kwargs):
            self.assertIn("tag_slug", params or {})
            self.assertEqual((params or {}).get("tag_slug"), "weather")
            return _FakeResponse(
                [
                    {
                        "slug": "highest-temperature-in-nyc-on-february-10-2026",
                        "title": "Highest temperature in NYC on February 10?",
                        "endDate": "2026-02-10T00:00:00Z",
                        "tags": [{"slug": "weather", "label": "Weather"}],
                    }
                ]
            )

        with mock.patch.object(swm, "GAMMA_MAX_PAGES", 1), mock.patch.object(
            swm, "requests_get", side_effect=_requests_get
        ):
            events, warnings = swm.fetch_weather_events_from_gamma()
            self.assertEqual(warnings, [])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].get("slug"), "highest-temperature-in-nyc-on-february-10-2026")

    def test_gamma_falls_back_when_tag_yields_none(self) -> None:
        calls = []

        def _requests_get(url, *, params=None, **_kwargs):
            calls.append(dict(params or {}))
            if len(calls) == 1:
                # Tagged fetch returns only non-temperature events (filtered out)
                return _FakeResponse(
                    [
                        {
                            "slug": "will-a-hurricane-form-by-may-31",
                            "title": "Will a hurricane form by May 31?",
                            "endDate": "2026-05-31T00:00:00Z",
                            "tags": [{"slug": "weather", "label": "Weather"}],
                        }
                    ]
                )
            return _FakeResponse(
                [
                    {
                        "slug": "highest-temperature-in-seoul-on-february-10-2026",
                        "title": "Highest temperature in Seoul on February 10?",
                        "endDate": "2026-02-10T00:00:00Z",
                        "tags": [{"slug": "weather", "label": "Weather"}],
                    }
                ]
            )

        with mock.patch.object(swm, "GAMMA_MAX_PAGES", 1), mock.patch.object(
            swm, "requests_get", side_effect=_requests_get
        ):
            events, warnings = swm.fetch_weather_events_from_gamma()
            self.assertTrue(any("tag_slug=weather" in w for w in warnings))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].get("slug"), "highest-temperature-in-seoul-on-february-10-2026")
            self.assertEqual(calls[0].get("tag_slug"), "weather")
            self.assertNotIn("tag_slug", calls[1])


class TestWeatherLocationResolution(unittest.TestCase):
    def test_zuuu_resolution_uses_chengdu_station_timezone(self) -> None:
        event = {
            "description": "",
            "resolutionSource": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        }
        with mock.patch.object(swm, "fetch_meteostat_station_coords", return_value=None), mock.patch.object(
            swm, "fetch_ourairports_station_coords", return_value=(30.558257, 103.945966)
        ), mock.patch.object(
            swm, "fetch_ourairports_station_city", return_value="Chengdu (Shuangliu)"
        ), mock.patch.object(
            swm, "geocode_city", side_effect=AssertionError("known station timezone should not geocode")
        ):
            loc = swm.resolve_location_for_event(
                event,
                "highest-temperature-in-chengdu-on-june-19-2026",
                "Highest temperature in Chengdu on June 19?",
                {},
            )

        self.assertEqual(loc, (30.558257, 103.945966, "Asia/Shanghai", "ZUUU"))

    def test_chengdu_city_geocode_uses_local_settlement_coords(self) -> None:
        self.assertEqual(
            swm.geocode_city("Chengdu", {}),
            (30.558257, 103.945966, "Asia/Shanghai"),
        )


if __name__ == "__main__":
    unittest.main()
