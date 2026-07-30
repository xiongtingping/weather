from __future__ import annotations

import json

from copy_follow_optional_forecasts import fetch_open_meteo_daily_peak


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_open_meteo_daily_peak_is_local_date_bound_and_display_only() -> None:
    calls = []

    def opener(request, *, timeout):
        calls.append((request.full_url, timeout))
        return _Response(
            {
                "daily": {
                    "time": ["2026-07-20"],
                    "temperature_2m_max": [29.2],
                }
            }
        )

    result = fetch_open_meteo_daily_peak(
        latitude=40.08,
        longitude=116.58,
        event_date="2026-07-20",
        timezone_name="Asia/Shanghai",
        opener=opener,
    )

    assert result["state"] == "available"
    assert result["peak_c"] == 29.2
    assert result["display_only"] is True
    assert "temperature_2m_max" in calls[0][0]
    assert "timezone=Asia%2FShanghai" in calls[0][0]


def test_open_meteo_rejects_non_official_api_host_without_network() -> None:
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("unallowlisted host must not be requested")

    result = fetch_open_meteo_daily_peak(
        latitude=40.08,
        longitude=116.58,
        event_date="2026-07-20",
        timezone_name="Asia/Shanghai",
        api_url="https://example.com/v1/forecast",
        opener=opener,
    )

    assert result["state"] == "unavailable"
    assert result["reason"] == "api_host_not_allowlisted"
    assert called is False
