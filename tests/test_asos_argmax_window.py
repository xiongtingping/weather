from __future__ import annotations

import sqlite3

import pytest

from asos_argmax_window import (
    ARGMAX_SELECTION_RULE,
    commit_argmax_windows,
    derive_argmax_window,
    evaluate_observed_high_entry,
    materialize_missing_argmax_windows,
    fetch_station_argmax_windows,
    select_latest_contiguous_argmax_periods,
)
from weather_basket_yes_schema import apply_weather_basket_yes_schema


def _forecast() -> dict:
    return {
        "properties": {
            "generatedAt": "2026-08-05T12:00:00Z",
            "periods": [
                {
                    "startTime": "2026-08-06T14:00:00-04:00",
                    "endTime": "2026-08-06T15:00:00-04:00",
                    "temperature": 88,
                    "temperatureUnit": "F",
                },
                {
                    "startTime": "2026-08-06T15:00:00-04:00",
                    "endTime": "2026-08-06T16:00:00-04:00",
                    "temperature": 89,
                    "temperatureUnit": "F",
                },
                {
                    "startTime": "2026-08-06T16:00:00-04:00",
                    "endTime": "2026-08-06T17:00:00-04:00",
                    "temperature": 89,
                    "temperatureUnit": "F",
                },
                {
                    "startTime": "2026-08-06T17:00:00-04:00",
                    "endTime": "2026-08-06T18:00:00-04:00",
                    "temperature": 87,
                    "temperatureUnit": "F",
                },
            ],
        }
    }


def _window() -> dict:
    return derive_argmax_window(
        _forecast(), station_id="KATL", event_date="2026-08-06",
        settlement_timezone="America/New_York",
        fetched_at_utc="2026-08-05T13:00:00Z",
        points_receipt_sha256="1" * 64,
        forecast_receipt_sha256="2" * 64,
        raw_object_ref="/bounded/raw.json",
        coordinate_registry_sha256="3" * 64,
    )


def test_exact_forecast_argmax_is_timing_only_and_frozen() -> None:
    window = _window()
    assert window["argmax_window_start_utc"] == "2026-08-06T19:00:00.000Z"
    assert window["argmax_window_end_utc"] == "2026-08-06T21:00:00.000Z"
    assert len(window["argmax_periods"]) == 2
    assert window["timing_only"] is True
    assert window["provides_temperature_bucket"] is False
    assert window["provides_probability"] is False
    assert window["enters_p0"] is False
    assert window["enters_p0m"] is False


def test_forecast_arriving_after_argmax_start_fails_closed() -> None:
    with pytest.raises(ValueError, match="argmax_window_not_frozen_before_start"):
        derive_argmax_window(
            _forecast(), station_id="KATL", event_date="2026-08-06",
            settlement_timezone="America/New_York",
            fetched_at_utc="2026-08-06T19:00:00Z",
            points_receipt_sha256="1" * 64,
            forecast_receipt_sha256="2" * 64,
            raw_object_ref="/bounded/raw.json",
            coordinate_registry_sha256="3" * 64,
        )


def test_window_is_append_only_and_one_per_station_day() -> None:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    window = _window()
    assert commit_argmax_windows(conn, [window]) == 1
    assert commit_argmax_windows(conn, [window]) == 0
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "UPDATE reported_trajectory_asos_argmax_windows SET state='FROZEN' WHERE window_id=?",
            (window["window_id"],),
        )


def test_entry_requires_received_new_high_during_exact_argmax_period() -> None:
    window = {**_window(), "state": "FROZEN"}
    eligible = evaluate_observed_high_entry(
        window,
        observation_valid_at_utc="2026-08-06T19:05:00Z",
        observation_received_at_utc="2026-08-06T19:05:05Z",
        forms_new_station_day_high=True,
    )
    assert eligible["eligible"] is True
    assert eligible["reason"] == "ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD"

    before_period = evaluate_observed_high_entry(
        window,
        observation_valid_at_utc="2026-08-06T18:58:00Z",
        observation_received_at_utc="2026-08-06T18:58:05Z",
        forms_new_station_day_high=True,
    )
    assert before_period == {
        "eligible": False, "reason": "ARGMAX_PERIOD_NOT_ACTIVE"
    }

    repeated = evaluate_observed_high_entry(
        window,
        observation_valid_at_utc="2026-08-06T19:05:00Z",
        observation_received_at_utc="2026-08-06T19:05:05Z",
        forms_new_station_day_high=False,
    )
    assert repeated == {
        "eligible": False, "reason": "ASOS_NOT_NEW_STATION_DAY_HIGH"
    }


def test_entry_rejects_gap_between_disjoint_argmax_periods() -> None:
    window = {**_window(), "state": "FROZEN", "argmax_periods": [
        {"start_utc": "2026-08-06T19:00:00Z", "end_utc": "2026-08-06T20:00:00Z"},
        {"start_utc": "2026-08-06T21:00:00Z", "end_utc": "2026-08-06T22:00:00Z"},
    ]}
    assert evaluate_observed_high_entry(
        window,
        observation_valid_at_utc="2026-08-06T20:30:00Z",
        observation_received_at_utc="2026-08-06T20:30:05Z",
        forms_new_station_day_high=True,
    ) == {"eligible": False, "reason": "ARGMAX_PERIOD_NOT_ACTIVE"}


def test_disjoint_tied_argmax_keeps_only_latest_contiguous_cluster() -> None:
    forecast = _forecast()
    forecast["properties"]["periods"] = [
        {
            "startTime": "2026-08-06T14:00:00-04:00",
            "endTime": "2026-08-06T15:00:00-04:00",
            "temperature": 89,
            "temperatureUnit": "F",
        },
        {
            "startTime": "2026-08-06T15:00:00-04:00",
            "endTime": "2026-08-06T16:00:00-04:00",
            "temperature": 87,
            "temperatureUnit": "F",
        },
        {
            "startTime": "2026-08-06T16:00:00-04:00",
            "endTime": "2026-08-06T17:00:00-04:00",
            "temperature": 89,
            "temperatureUnit": "F",
        },
    ]
    window = derive_argmax_window(
        forecast, station_id="KATL", event_date="2026-08-06",
        settlement_timezone="America/New_York",
        fetched_at_utc="2026-08-05T13:00:00Z",
        points_receipt_sha256="1" * 64,
        forecast_receipt_sha256="2" * 64,
        raw_object_ref="/bounded/raw.json",
        coordinate_registry_sha256="3" * 64,
    )
    assert window["argmax_window_start_utc"] == "2026-08-06T20:00:00.000Z"
    assert window["argmax_window_end_utc"] == "2026-08-06T21:00:00.000Z"
    assert len(window["argmax_periods"]) == 1
    assert window["argmax_periods"][0]["selection_rule"] == ARGMAX_SELECTION_RULE
    assert window["argmax_periods"][0]["candidate_argmax_period_count"] == 2


def test_legacy_multi_cluster_window_is_narrowed_at_entry_time() -> None:
    legacy = [
        {"start_utc": "2026-08-06T18:00:00Z", "end_utc": "2026-08-06T19:00:00Z"},
        {"start_utc": "2026-08-06T20:00:00Z", "end_utc": "2026-08-06T21:00:00Z"},
    ]
    assert select_latest_contiguous_argmax_periods(legacy) == [legacy[1]]
    assert evaluate_observed_high_entry(
        {"state": "FROZEN", "argmax_periods": legacy},
        observation_valid_at_utc="2026-08-06T18:05:00Z",
        observation_received_at_utc="2026-08-06T18:05:05Z",
        forms_new_station_day_high=True,
    ) == {"eligible": False, "reason": "ARGMAX_PERIOD_NOT_ACTIVE"}
    assert evaluate_observed_high_entry(
        {"state": "FROZEN", "argmax_periods": legacy},
        observation_valid_at_utc="2026-08-06T20:05:00Z",
        observation_received_at_utc="2026-08-06T20:05:05Z",
        forms_new_station_day_high=True,
    )["eligible"] is True


def test_materializer_ignores_stale_active_registry_dates_and_uses_et(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    registry = {
        "slug": "highest-temperature-in-atlanta-on-august-6-2026",
        "outcome": "88-89F",
    }
    common = (
        "atlanta", "KATL", "America/Chicago", "F", "awc", 1, None,
        "https://example.invalid", "a" * 64, "yes", "no", 1, 1,
    )
    for event_date in ("2026-07-21", "2026-08-06"):
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry(
                 slug,event_date,outcome,city_slug,station_id,settlement_timezone,
                 settlement_unit,source_lane,source_eligible,source_block_reason,
                 resolution_url,rules_sha256,yes_token,no_token,market_active,
                 market_accepting_orders,registry_json,refreshed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                registry["slug"], event_date, registry["outcome"], *common,
                "{}", "2026-08-05T12:00:00Z",
            ),
        )

    calls = []

    class Response:
        def __init__(self, payload):
            import json
            self.content = json.dumps(payload).encode()

        def raise_for_status(self):
            return None

    def fake_get(url, **_kwargs):
        calls.append(url)
        if "/points/" in url:
            return Response({"properties": {"forecastHourly": "https://api.weather.gov/hourly"}})
        return Response(_forecast())

    result = materialize_missing_argmax_windows(
        conn, ["KATL"], now_utc=__import__("datetime").datetime(
            2026, 8, 5, 13, tzinfo=__import__("datetime").timezone.utc
        ), raw_root=tmp_path, get=fake_get,
    )
    assert result["ok"] is True
    rows = conn.execute(
        "SELECT event_date,settlement_timezone FROM reported_trajectory_asos_argmax_windows"
    ).fetchall()
    assert rows == [("2026-08-06", "America/New_York")]
    assert len(calls) == 2


def test_nws_hourly_503_uses_same_official_grid_temperature_fallback(tmp_path) -> None:
    import json

    class Response:
        def __init__(self, payload, *, error=False):
            self.content = json.dumps(payload).encode()
            self.error = error

        def raise_for_status(self):
            if self.error:
                raise RuntimeError("hourly_503")

    def fake_get(url, **_kwargs):
        if "/points/" in url:
            return Response({"properties": {
                "forecastHourly": "https://api.weather.gov/hourly",
                "forecastGridData": "https://api.weather.gov/grid",
            }})
        if url.endswith("/hourly"):
            return Response({}, error=True)
        return Response({"properties": {
            "updateTime": "2026-08-05T12:00:00Z",
            "temperature": {"values": [
                {"validTime": "2026-08-06T18:00:00Z/PT1H", "value": 30.0},
                {"validTime": "2026-08-06T19:00:00Z/PT2H", "value": 32.0},
                {"validTime": "2026-08-06T21:00:00Z/PT1H", "value": 31.0},
            ]},
        }})

    row = fetch_station_argmax_windows(
        "KLAX", [("2026-08-06", "America/New_York")],
        fetched_at_utc="2026-08-05T13:00:00Z", raw_root=tmp_path, get=fake_get,
    )[0]
    assert row["forecast_product_variant"] == "grid_temperature_values"
    assert row["argmax_window_start_utc"] == "2026-08-06T19:00:00.000Z"
    assert row["argmax_window_end_utc"] == "2026-08-06T21:00:00.000Z"
    assert row["timing_only"] is True
