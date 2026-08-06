from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from asos_one_minute_ingress import AsosOneMinuteIngress, icao_to_iem_station
from weather_basket_yes_schema import apply_weather_basket_yes_schema


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_asos_complete_coverage_outranks_awc_without_mixing_lineage() -> None:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """INSERT INTO reported_trajectory_station_day_bounds(
             source_lane,station_id,event_date,settlement_timezone,reported_max_c,
             max_observation_id,max_valid_at,bound_generation,updated_at
           ) VALUES('awc','KJFK','2025-08-05','UTC',40,'awc-1',?,1,?)""",
        ("2025-08-05T00:05:00.000Z", "2025-08-05T00:11:00.000Z"),
    )
    start = datetime(2025, 8, 5, tzinfo=timezone.utc)
    reports = []
    for minute in range(11):
        value_f = 77.0 + minute / 10
        reports.append({
            "station_id": "KJFK",
            "provider_station_id": "JFK",
            "valid_at": _stamp(start + timedelta(minutes=minute)),
            "temperature_c": (value_f - 32) * 5 / 9,
            "provider_temperature_f": value_f,
            "raw_record_sha256": hashlib.sha256(str(minute).encode()).hexdigest(),
        })
    poll = {
        "source_lane": "asos_1min",
        "provider_id": "IEM_ASOS1MIN",
        "source_class": "aggregator_republisher",
        "upstream_authority": "NOAA/NCEI ASOS",
        "origin_url": "https://example.invalid/asos1min",
        "source_receipt_sha256": "a" * 64,
        "fetch_started_at": "2025-08-05T00:10:30.000Z",
        "fetch_completed_at": "2025-08-05T00:11:00.000Z",
        "reports": reports,
    }
    ingress = AsosOneMinuteIngress(conn)
    assert ingress.ingest(poll)["new_observation_count"] == 11
    selected = ingress.refresh_station_day(
        station_id="KJFK",
        event_date="2025-08-05",
        settlement_timezone="UTC",
        as_of_utc="2025-08-05T00:10:00.000Z",
        source_receipt_sha256="a" * 64,
    )
    assert selected["selected_source_lane"] == "asos_1min"
    row = conn.execute(
        """SELECT selected_source_lane,source_priority,selection_reason
             FROM reported_trajectory_canonical_station_day_bounds
            WHERE station_id='KJFK' AND event_date='2025-08-05'"""
    ).fetchone()
    assert row == ("asos_1min", 220, "ASOS_COMPLETE_OUTRANKS_AWC")
    stale = ingress.select_canonical_bound(
        station_id="KJFK",
        event_date="2025-08-05",
        as_of_utc="2025-08-05T01:00:00.000Z",
    )
    assert stale["selected_source_lane"] == "awc"
    assert stale["selection_reason"] == "ASOS_UNAVAILABLE_AWC_FALLBACK"
    assert conn.execute(
        "SELECT COUNT(*) FROM reported_trajectory_observations WHERE source_lane='awc'"
    ).fetchone()[0] == 0


def test_incomplete_asos_coverage_falls_back_to_awc() -> None:
    conn = sqlite3.connect(":memory:")
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """INSERT INTO reported_trajectory_station_day_bounds(
             source_lane,station_id,event_date,settlement_timezone,reported_max_c,
             max_observation_id,max_valid_at,bound_generation,updated_at
           ) VALUES('awc','KJFK','2025-08-05','UTC',30,'awc-1',?,1,?)""",
        ("2025-08-05T00:05:00.000Z", "2025-08-05T00:11:00.000Z"),
    )
    ingress = AsosOneMinuteIngress(conn)
    selected = ingress.refresh_station_day(
        station_id="KJFK",
        event_date="2025-08-05",
        settlement_timezone="UTC",
        as_of_utc="2025-08-05T00:10:00.000Z",
        source_receipt_sha256="b" * 64,
    )
    assert selected == {
        "state": "selected",
        "selected_source_lane": "awc",
        "observed_max_c": 30.0,
        "selection_reason": "ASOS_UNAVAILABLE_AWC_FALLBACK",
    }


def test_only_unambiguous_k_prefix_stations_are_mapped() -> None:
    assert icao_to_iem_station("KJFK") == "JFK"
    assert icao_to_iem_station("CYYZ") is None
    assert icao_to_iem_station("ZBAA") is None
