"""The board reads the observation door and the cycle journal (2026-09-20).

User: 「检查前端是否还有旧的内容，以及哪些新内容没有展示的」 -> 「做」.

Charter 162 gave the observation lane a second door (NOAA TGFTP) for when
every AWC channel fails at once, which is what happened when
aviationweather.gov's certificate expired at 2026-09-19T23:59:59Z.  Charters
153 and 154 put TAF and observations at the front of the production cycle to
cut the wait between a report landing and the probability moving.  None of
the three had anything on the page: the only sign of the outage was a
freshness count inside the station-day table, and the cycle's own journal --
per-stage milliseconds for every cycle -- had no reader at all.
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

import calibration_dashboard as C  # noqa: E402

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from weather_basket_yes_schema import apply_weather_basket_yes_schema  # noqa: E402


def _archive(tmp_path: Path, rows: list[tuple]) -> None:
    """Production DDL, not a hand-written subset of it.

    The hand-written version carried six of the fifteen columns, so the day
    the board started reading ``first_seen_at`` (charter 167) these tests went
    red on a missing column rather than on anything they were testing.  A row
    may name its own arrival stamp; without one it arrived when it committed,
    which is what the older cases assume.
    """

    db = sqlite3.connect(tmp_path / "archive.db")
    apply_weather_basket_yes_schema(db)
    for row in rows:
        oid, station, valid, committed, origin = row[:5]
        first_seen = row[5] if len(row) > 5 else committed
        db.execute(
            "INSERT INTO reported_trajectory_observations(observation_id,source_lane,"
            "station_id,report_type,valid_at,reported_temperature_c,raw_sha256,raw_text,"
            "first_seen_at,parsed_at,canonical_committed_at,fetch_started_at,"
            "fetch_completed_at,source_poll_id,correction_suspected)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, "awc", station, "METAR", valid, 20, oid.ljust(64, "0"),
             f"METAR {station}", first_seen, committed, committed, valid, first_seen,
             f"poll-{oid}", 0),
        )
        db.execute(
            "INSERT INTO reported_trajectory_observation_origins(observation_id,"
            "source_lane,station_id,origin_source_id,origin_country_code,"
            "origin_authority,origin_url,origin_transport,direct_country_official,"
            "fallback_used,fallback_reason,preferred_origin_source_id,"
            "preferred_origin_authority,official_source_registry_sha256,origin_json,"
            "committed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, "awc", station, origin, "US", "NOAA", "https://example.invalid",
             "t", 0, 0, "", origin, "NOAA", "0" * 64, "{}", committed),
        )
    db.commit()
    db.close()


def _ssot(tmp_path: Path, stations: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.execute(
        "CREATE TABLE basket_probability_envelope (station_id TEXT, event_date TEXT,"
        " stage TEXT, state TEXT, decision_at_utc TEXT, lineage_json TEXT)"
    )
    for station in stations:
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?)",
            (station, "2026-09-20", "P1", "COMMITTED", "2026-09-20T03:00:00Z", "{}"),
        )
    conn.commit()
    return conn


def test_the_lamp_reads_the_door_not_the_lane(tmp_path, monkeypatch) -> None:
    """Charter 162 keeps ``source_lane='awc'`` when TGFTP serves -- the lane
    is the station's roster binding, not the transport -- so a lamp that read
    the lane would show nothing at all during the outage."""

    monkeypatch.setattr(C, "ROOT", tmp_path)
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 20, 3, 30, tzinfo=C.timezone.utc))
    (tmp_path / "data").mkdir()
    _archive(tmp_path / "data", [
        ("o1", "ZUUU", "2026-09-20T03:00:00Z", "2026-09-20T03:03:00Z", "global_noaa_awc_relay"),
        ("o2", "KLAX", "2026-09-20T03:00:00Z", "2026-09-20T03:02:00Z", "global_noaa_tgftp_relay"),
    ])
    out = C.collect_observation_intake(_ssot(tmp_path, ["ZUUU", "KLAX"]))
    assert out["available"] is True
    assert out["doors"] == {"AWC": 1, "NOAA TGFTP": 1}
    assert out["fallback"] == 1
    assert out["health"] == "amber", "a station on the second door is not a green fleet"
    assert out["oldest_minutes"] == pytest.approx(30.0)
    assert out["median_landing_minutes"] == pytest.approx(2.5)


def test_a_fleet_that_stopped_reporting_goes_red(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(C, "ROOT", tmp_path)
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 20, 8, 0, tzinfo=C.timezone.utc))
    (tmp_path / "data").mkdir()
    _archive(tmp_path / "data", [
        ("o1", "ZUUU", "2026-09-20T03:00:00Z", "2026-09-20T03:03:00Z", "global_noaa_awc_relay"),
        ("o2", "KLAX", "2026-09-20T03:00:00Z", "2026-09-20T03:02:00Z", "global_noaa_awc_relay"),
    ])
    out = C.collect_observation_intake(_ssot(tmp_path, ["ZUUU", "KLAX"]))
    assert out["stale"] == 2 and out["health"] == "red", out


def _latency_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.execute(
        "CREATE TABLE basket_probability_envelope (station_id TEXT, event_date TEXT,"
        " stage TEXT, state TEXT, decision_at_utc TEXT, created_at TEXT,"
        " lineage_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_forward_taf_capture (station_id TEXT, event_date TEXT,"
        " received_at_utc TEXT, tx_available INTEGER)"
    )
    return conn


def test_the_panel_and_the_lamp_are_wired() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert '"observation_intake": observation_intake,' in source
    # The cycle panel left the head on 2026-10-05 (health cards only); the
    # lamp stays.
    assert 'push("upstream", OB.health || "muted",' in source
