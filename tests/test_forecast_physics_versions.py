"""The radiation capture keeps every version it saw, append-only.

2026-09-21: the physics table holds one row per station-day and every
forward capture replaces it, so a day's row is rewritten until the evening
of the day itself (1,616 of 1,745 past forward rows).  A holdout reading it
scores a same-day radiation against a P0 that was served a forecast one to
two days ahead.  The version table is what lets a sample resolve the value
that existed at its own cutoff.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_station_forecast_physics.py"


def _module():
    spec = importlib.util.spec_from_file_location("capture_physics_versions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(module, conn, monkeypatch, radiation, stamp):
    payload = {"daily": {
        "time": ["2026-09-22", "2026-09-23"],
        "shortwave_radiation_sum": radiation,
        "windspeed_10m_max": [3.0, 4.0],
        "cloudcover_mean": [40.0, 50.0],
        "precipitation_sum": [0.0, 1.0],
    }}
    monkeypatch.setattr(module, "_fetch_json", lambda url, timeout_seconds: payload)
    monkeypatch.setattr(module, "station_coordinates", lambda station: {"latitude": 30.6, "longitude": 104.0})

    class Clock:
        @staticmethod
        def now(tz=None):
            from datetime import datetime, timezone
            return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)

    monkeypatch.setattr(module, "datetime", Clock)
    receipt = module.capture_station(
        conn, station_id="ZUUU", start_date="", end_date="",
        settlement_timezone="Asia/Shanghai", mode="forward", timeout_seconds=5,
    )
    conn.commit()
    return receipt


def _versions(module, conn):
    return conn.execute(
        f"SELECT local_date,captured_at_utc,shortwave_radiation_sum "
        f"FROM {module.VERSION_TABLE} ORDER BY local_date,captured_at_utc"
    ).fetchall()


def test_a_capture_that_changes_nothing_adds_no_version_and_a_change_adds_one(monkeypatch, tmp_path):
    module = _module()
    conn = sqlite3.connect(tmp_path / "ssot.db")
    assert module.ensure_schema(conn) == 0

    assert _capture(module, conn, monkeypatch, [18.0, 12.0], "2026-09-21T00:00:00")["versions"] == 2
    assert _capture(module, conn, monkeypatch, [18.0, 12.0], "2026-09-21T06:00:00")["versions"] == 0
    assert _capture(module, conn, monkeypatch, [15.0, 12.0], "2026-09-21T12:00:00")["versions"] == 1
    # Back to the first value is a third version, not a duplicate of the first.
    assert _capture(module, conn, monkeypatch, [18.0, 12.0], "2026-09-21T18:00:00")["versions"] == 1

    assert [row for row in _versions(module, conn) if row[0] == "2026-09-22"] == [
        ("2026-09-22", "2026-09-21T00:00:00Z", 18.0),
        ("2026-09-22", "2026-09-21T12:00:00Z", 15.0),
        ("2026-09-22", "2026-09-21T18:00:00Z", 18.0),
    ]
    # Serving still reads the one current row, unchanged in shape.
    assert conn.execute(
        f"SELECT shortwave_radiation_sum,captured_at_utc FROM {module.TABLE} "
        "WHERE local_date='2026-09-22'"
    ).fetchall() == [(18.0, "2026-09-21T18:00:00Z")]


def test_versions_cannot_be_rewritten_or_deleted(monkeypatch, tmp_path):
    module = _module()
    conn = sqlite3.connect(tmp_path / "ssot.db")
    module.ensure_schema(conn)
    _capture(module, conn, monkeypatch, [18.0, 12.0], "2026-09-21T00:00:00")
    with pytest.raises(sqlite3.DatabaseError, match="append_only"):
        conn.execute(f"UPDATE {module.VERSION_TABLE} SET shortwave_radiation_sum=1")
    with pytest.raises(sqlite3.DatabaseError, match="append_only"):
        conn.execute(f"DELETE FROM {module.VERSION_TABLE}")


def test_rows_written_before_the_version_table_existed_are_seeded_once(monkeypatch, tmp_path):
    module = _module()
    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.execute(module.SCHEMA)
    conn.execute(
        f"INSERT INTO {module.TABLE}({module.COLUMNS}) VALUES "
        "('ZUUU','2026-09-20','OPEN_METEO_MODEL_FORECAST',17.0,3.0,40.0,0.0,"
        "30.6,104.0,'Asia/Shanghai','forward','2026-09-20T12:00:00Z',1,'sha-a')"
    )
    conn.commit()
    assert module.ensure_schema(conn) == 1
    assert module.ensure_schema(conn) == 0
    assert _versions(module, conn) == [("2026-09-20", "2026-09-20T12:00:00Z", 17.0)]
