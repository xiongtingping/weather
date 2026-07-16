from __future__ import annotations

import types

import scan_weather_market as swm


def test_parse_station_from_wunderground_hourly_url() -> None:
    url = "https://www.wunderground.com/hourly/cn/chengdu/ZUUU"

    assert swm.parse_station_from_resolution_source(url) == "ZUUU"

    profile = swm.weather_resolution_source_profile(url, station_id="ZUUU")
    assert profile["source_kind"] == "wunderground_aux"
    assert profile["source_station_id"] == "ZUUU"
    assert profile["source_station_match"] is True


def test_fetch_wunderground_hourly_temperatures_prefers_native_decimal_values(
    monkeypatch,
) -> None:
    html = (
        "https://api.weather.com/v3/wx/forecast/hourly/15day"
        "?apiKey=test&geocode=30.573%2C103.958&units=m&language=en-US&format=json"
    )
    calls: list[str] = []

    def fake_requests_get(url, **kwargs):
        calls.append(url)
        if "api.weather.com" not in url:
            return types.SimpleNamespace(status_code=200, text=html)
        if "units=e" in url:
            temps = [76.4, 74.2]
        else:
            temps = [24, 23]
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {
                "temperature": temps,
                "validTimeLocal": ["2026-06-08T20:00:00+0800", "2026-06-08T21:00:00+0800"],
                "validTimeUtc": [1780920000, 1780923600],
            },
        )

    monkeypatch.setattr(swm, "requests_get", fake_requests_get)

    rows = swm.fetch_wunderground_hourly_temperatures(
        "https://www.wunderground.com/hourly/cn/chengdu/ZUUU",
        market_unit="C",
    )

    assert "units=m" in calls[-2]
    assert "units=e" in calls[-1]
    assert rows[0]["temperature"] == 76.4
    assert rows[0]["source_temperature"] == 76.4
    assert rows[0]["source_unit"] == "F"
    assert rows[0]["derived_from"] == ""
    assert rows[0]["native_decimal"] is True


def test_fetch_wunderground_hourly_temperatures_falls_back_to_inline_page_curve_when_api_blocked(
    monkeypatch,
) -> None:
    api_url = (
        "https://api.weather.com/v3/wx/forecast/hourly/15day"
        "?apiKey=test&geocode=30.573%2C103.958&units=e&language=en-US&format=json"
    )
    html = (
        f'<script>const endpoint="{api_url}";</script>'
        '{"temperature":[82,90,88],"validTimeLocal":['
        '"2026-07-12T12:00:00+0800","2026-07-12T13:00:00+0800","2026-07-12T14:00:00+0800"]}'
    )

    def fake_requests_get(url, **kwargs):
        if "api.weather.com" not in url:
            return types.SimpleNamespace(status_code=200, text=html)
        return types.SimpleNamespace(status_code=403, text="Bad Request - Blocked")

    monkeypatch.setattr(swm, "requests_get", fake_requests_get)

    rows = swm.fetch_wunderground_hourly_temperatures(
        "https://www.wunderground.com/hourly/cn/chengdu/ZUUU",
        market_unit="C",
    )

    assert [row["temperature"] for row in rows] == [82.0, 90.0, 88.0]
    assert rows[0]["unit"] == "F"
    assert rows[0]["derived_from"] == "wunderground_hourly_page_inline_payload"


def test_weather_com_hourly_peak_metrics_emit_plateau_distribution(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        url = "https://api.weather.com/v3/wx/forecast/hourly/15day?apiKey=secret"

        def json(self):
            return {
                "temperature": [29.0, 31.0, 30.8, 30.7, 30.4],
                "validTimeLocal": [
                    "2999-06-12T14:00:00+0800",
                    "2999-06-12T15:00:00+0800",
                    "2999-06-12T16:00:00+0800",
                    "2999-06-12T17:00:00+0800",
                    "2999-06-12T18:00:00+0800",
                ],
            }

    monkeypatch.setenv("PM_WEATHER_COM_API_KEY", "secret")
    monkeypatch.setattr(swm, "requests_get", lambda *args, **kwargs: FakeResponse())

    metrics = swm.fetch_weather_com_hourly_peak_metrics(
        30.558257,
        103.945966,
        "2999-06-12",
        "temperature_2m_max",
        "C",
        station_id="ZUUU",
        resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
    )

    assert metrics["ok"] is True
    assert metrics["expected_peak_window_start_hour_local"] == 15.0
    assert metrics["expected_peak_window_end_hour_local"] == 17.0
    assert [row["hour_local"] for row in metrics["expected_peak_hour_distribution"]] == [15.0, 16.0, 17.0]
    assert metrics["peak_at"].startswith("2999-06-12T16:00:00")
    assert len(metrics["hourly_curve"]) == 5
    assert metrics["hourly_curve"][1]["temperature_c"] == 31.0


def test_wunderground_forecast_peak_metrics_emit_hourly_plateau(monkeypatch) -> None:
    seen_sources = []
    monkeypatch.setattr(
        swm,
        "fetch_wunderground_daily_temp",
        lambda resolution_source, event_date, kind, market_unit, **kwargs: 31.0,
    )

    def fake_hourly(source, *args, **kwargs):
        seen_sources.append(source)
        return [
            {"valid_time_local": "2999-06-12T14:00:00+0800", "temperature": 29.0, "unit": "C"},
            {"valid_time_local": "2999-06-12T15:00:00+0800", "temperature": 31.0, "unit": "C"},
            {"valid_time_local": "2999-06-12T16:00:00+0800", "temperature": 30.8, "unit": "C"},
            {"valid_time_local": "2999-06-12T17:00:00+0800", "temperature": 30.7, "unit": "C"},
        ]

    monkeypatch.setattr(
        swm,
        "fetch_wunderground_hourly_temperatures",
        fake_hourly,
    )

    metrics = swm.fetch_wunderground_forecast_peak_metrics(
        "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "2999-06-12",
        "temperature_2m_max",
        "C",
    )

    assert metrics["ok"] is True
    assert metrics["peak_time_source"] == "wunderground_hourly_forecast_plateau"
    assert seen_sources == ["https://www.wunderground.com/hourly/cn/chengdu/ZUUU"]
    assert metrics["expected_peak_window_start_hour_local"] == 15.0
    assert metrics["expected_peak_window_end_hour_local"] == 17.0
    assert [row["hour_local"] for row in metrics["expected_peak_hour_distribution"]] == [15.0, 16.0, 17.0]
    assert len(metrics["hourly_curve"]) == 4
    assert metrics["hourly_curve"][1]["temperature_c"] == 31.0


def test_wunderground_forecast_rejects_hourly_plateau_far_below_daily_peak(monkeypatch) -> None:
    monkeypatch.setattr(
        swm,
        "fetch_wunderground_daily_temp",
        lambda resolution_source, event_date, kind, market_unit, **kwargs: 34.0,
    )
    monkeypatch.setattr(
        swm,
        "fetch_wunderground_hourly_temperatures",
        lambda *args, **kwargs: [
            {"valid_time_local": "2999-06-12T21:00:00+0800", "temperature": 26.0, "unit": "C"},
            {"valid_time_local": "2999-06-12T22:00:00+0800", "temperature": 26.0, "unit": "C"},
        ],
    )

    metrics = swm.fetch_wunderground_forecast_peak_metrics(
        "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "2999-06-12",
        "temperature_2m_max",
        "C",
    )

    assert metrics["ok"] is True
    assert metrics["peak_at"] == ""
    assert metrics["expected_peak_hour_distribution"] == []
    assert metrics["expected_peak_hour_distribution_na_reason"] == "hourly_plateau_max_far_from_daily_peak"
