"""Hourly precipitation rides the physics capture (user, 2026-09-23: 「采集降水」).

The same Open-Meteo request now carries ``hourly=precipitation``; every
distinct hourly series per station-day becomes one append-only version with
the aggregates a served term reads (total, before/after the 15:00 peak hour,
first wet hour), and a decision resolves the version it could have read.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import capture_station_forecast_physics as CAP  # noqa: E402
from bayes_basket_target.forecast_precip_asof import resolve_precip_hourly_as_of  # noqa: E402


def _payload(days: dict[str, list[float | None]], daily_days: list[str] | None = None) -> dict:
    stamps, values = [], []
    for day, series in days.items():
        for hour, value in enumerate(series):
            stamps.append(f"{day}T{hour:02d}:00")
            values.append(value)
    daily_days = daily_days or list(days)
    return {
        "hourly": {"time": stamps, "precipitation": values},
        "daily": {
            "time": daily_days,
            "shortwave_radiation_sum": [10.0] * len(daily_days),
            "windspeed_10m_max": [5.0] * len(daily_days),
            "cloudcover_mean": [50.0] * len(daily_days),
            "precipitation_sum": [round(sum(v or 0.0 for v in days[d]), 2) for d in daily_days],
        },
    }


def test_the_series_is_grouped_by_local_day_and_a_missing_hour_voids_the_day() -> None:
    wet = [0.0] * 24
    wet[9], wet[14], wet[20] = 2.0, 3.5, 6.0
    broken = [0.0] * 24
    broken[3] = None
    days = CAP.hourly_precipitation_days(_payload({"2026-09-24": wet, "2026-09-25": broken}))
    assert set(days) == {"2026-09-24"}
    agg = CAP.hourly_precipitation_aggregates(days["2026-09-24"])
    assert agg["precip_sum_mm"] == pytest.approx(11.5)
    # The 15:00 stamp covers 14:00–15:00, so hours 0..15 are before the peak.
    assert agg["precip_before_peak_mm"] == pytest.approx(5.5)
    assert agg["precip_after_peak_mm"] == pytest.approx(6.0)
    assert agg["first_wet_hour"] == 9 and agg["wet_hours"] == 3 and agg["peak_hour"] == 15


def test_a_repeated_local_hour_adds_up_and_a_short_day_still_counts() -> None:
    # The autumn clock change repeats 01:00; the spring one has 23 hours.
    payload = {"hourly": {"time": ["2026-11-01T00:00", "2026-11-01T01:00", "2026-11-01T01:00"]
                          + [f"2026-11-01T{h:02d}:00" for h in range(2, 24)],
                          "precipitation": [0.0, 1.0, 1.5] + [0.0] * 22}}
    days = CAP.hourly_precipitation_days(payload)
    assert days["2026-11-01"]["01"] == pytest.approx(2.5)
    spring = {"hourly": {"time": [f"2026-03-29T{h:02d}:00" for h in range(24) if h != 2],
                         "precipitation": [0.2] * 23}}
    assert "2026-03-29" in CAP.hourly_precipitation_days(spring)
    dry_trace = CAP.hourly_precipitation_aggregates({"05": 0.05})
    assert dry_trace["first_wet_hour"] is None and dry_trace["wet_hours"] == 0


def test_only_a_changed_series_becomes_a_new_version_and_the_reader_resolves_as_of(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    CAP.ensure_schema(conn)
    monkeypatch.setattr(CAP, "station_coordinates", lambda s: {"latitude": 30.0, "longitude": 104.0})
    first = [0.0] * 24
    first[10] = 4.0
    second = list(first)
    second[10] = 0.0
    second[19] = 4.0                      # same total, moved after the peak
    calls = iter([
        _payload({"2026-09-24": first}), _payload({"2026-09-24": first}), _payload({"2026-09-24": second}),
    ])
    monkeypatch.setattr(CAP, "_fetch_json", lambda url, timeout_seconds: next(calls))
    stamps = iter(["2026-09-22T06:00:00Z", "2026-09-22T12:00:00Z", "2026-09-23T06:00:00Z"])

    class Clock:
        @staticmethod
        def now(tz=None):
            from datetime import datetime  # noqa: PLC0415
            return datetime.fromisoformat(next(stamps).replace("Z", "+00:00"))

    monkeypatch.setattr(CAP, "datetime", Clock)
    receipts = [
        CAP.capture_station(conn, station_id="ZUUU", start_date="x", end_date="y",
                            settlement_timezone="Asia/Shanghai", mode="forward", timeout_seconds=5)
        for _ in range(3)
    ]
    assert [r["hourly_versions"] for r in receipts] == [1, 0, 1], receipts
    rows = conn.execute(
        f"SELECT captured_at_utc, precip_before_peak_mm, precip_after_peak_mm, hourly_precip_mm_json"
        f" FROM {CAP.PRECIP_HOURLY_TABLE} ORDER BY captured_at_utc"
    ).fetchall()
    assert len(rows) == 2
    assert (rows[0][1], rows[0][2]) == (4.0, 0.0) and (rows[1][1], rows[1][2]) == (0.0, 4.0)
    assert json.loads(rows[0][3])["10"] == 4.0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {CAP.PRECIP_HOURLY_TABLE}")
    # As-of: a decision at 2026-09-22T18:00Z sees the first version; one at
    # 2026-09-23T09:00Z sees the second; one before any capture sees nothing.
    agg, receipt = resolve_precip_hourly_as_of(conn, station_id="ZUUU", local_date="2026-09-24",
                                               cutoff_utc="2026-09-22T18:00:00Z")
    assert receipt["state"] == "RESOLVED" and agg["precip_before_peak_mm"] == 4.0
    assert receipt["version_age_hours"] == pytest.approx(12.0)
    agg, receipt = resolve_precip_hourly_as_of(conn, station_id="ZUUU", local_date="2026-09-24",
                                               cutoff_utc="2026-09-23T09:00:00Z")
    assert agg["precip_after_peak_mm"] == 4.0 and receipt["version_captured_at_utc"].startswith("2026-09-23T06")
    agg, receipt = resolve_precip_hourly_as_of(conn, station_id="ZUUU", local_date="2026-09-24",
                                               cutoff_utc="2026-09-21T00:00:00Z")
    assert agg is None and receipt["state"] == "NOT_KNOWN_AT_CUTOFF"
    agg, receipt = resolve_precip_hourly_as_of(conn, station_id="ZUUU", local_date="2026-09-25",
                                               cutoff_utc="2026-09-23T09:00:00Z")
    assert agg is None and receipt["state"] == "NO_VERSIONS"
    # The daily table is untouched by the hourly lane: still one row, one version.
    assert conn.execute(f"SELECT COUNT(*) FROM {CAP.TABLE}").fetchone()[0] == 1


def test_historical_rows_are_not_as_of_values(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    CAP.ensure_schema(conn)
    monkeypatch.setattr(CAP, "station_coordinates", lambda s: {"latitude": 30.0, "longitude": 104.0})
    series = [0.0] * 24
    series[8] = 9.0
    monkeypatch.setattr(CAP, "_fetch_json", lambda url, timeout_seconds: _payload({"2026-08-01": series}))
    CAP.capture_station(conn, station_id="ZUUU", start_date="2026-08-01", end_date="2026-08-01",
                        settlement_timezone="Asia/Shanghai", mode="historical", timeout_seconds=5)
    agg, receipt = resolve_precip_hourly_as_of(conn, station_id="ZUUU", local_date="2026-08-01",
                                               cutoff_utc="2026-09-30T00:00:00Z")
    assert agg is None and receipt["state"] == "NO_VERSIONS"
    agg, receipt = resolve_precip_hourly_as_of(conn, station_id="ZUUU", local_date="2026-08-01",
                                               cutoff_utc="2026-09-30T00:00:00Z", modes=("forward", "historical"))
    assert agg["precip_before_peak_mm"] == 9.0 and receipt["capture_mode"] == "historical"
