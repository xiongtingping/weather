"""best_match pulled once an hour (user 2026-10-27 「best_match每个小时都要去拉取一次」): the pull
lands on disk and a board within the hour reads it without asking; the hourly job writes one
history line per station."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bayes_basket_target import best_match_hourly as M
from bayes_basket_target import station_day_board as B


def test_board_reads_a_fresh_disk_pull_without_asking(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(B, "BEST_MATCH_DIR", tmp_path)
    monkeypatch.setattr(B, "_FORECAST_CACHE", {})
    key = ("30.578,103.947", "2026-10-27")
    fresh = {"available": True, "hourly": {"temperature_2m": [19.6]},
             "fetched_at_utc": B._iso(datetime.now(timezone.utc) - timedelta(minutes=50))}
    B._best_match_snapshot(key).write_text(json.dumps(fresh), encoding="utf-8")

    def no_network(*_a, **_k):
        raise AssertionError("asked Open-Meteo")

    monkeypatch.setattr("bayes_basket_target.open_meteo_quota.charge", no_network)
    assert B._open_meteo_hourly(30.578, 103.947, "Asia/Shanghai", "2026-10-27")["hourly"]["temperature_2m"] == [19.6]
    stale = {**fresh, "fetched_at_utc": B._iso(datetime.now(timezone.utc) - timedelta(minutes=70))}
    B._best_match_snapshot(key).write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(B, "_FORECAST_CACHE", {})
    with pytest.raises(AssertionError):
        B._open_meteo_hourly(30.578, 103.947, "Asia/Shanghai", "2026-10-27")


def test_hourly_job_writes_one_history_line_per_station(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ssot.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE calibration_station_market_timezone_binding (station_id TEXT)")
    conn.executemany("INSERT INTO calibration_station_market_timezone_binding VALUES (?)", [("ZUUU",), ("EGLC",)])
    conn.commit(); conn.close()
    monkeypatch.setattr(B, "BEST_MATCH_DIR", tmp_path / "bm")
    monkeypatch.setattr(M, "HISTORY_PATH", tmp_path / "bm" / "history.jsonl")
    monkeypatch.setattr(B, "_event_days", lambda conn, st, now: ("Asia/Shanghai", []) if st == "ZUUU" else None)
    monkeypatch.setattr(B, "_station_lat_lon", lambda conn, st: (30.578, 103.947))
    seen = []

    def fake(lat, lon, tz, day, *, force=False):
        seen.append((day, force, B.quota_lane.get()))
        return {"available": True, "fetched_at_utc": "2026-10-27T05:05:00Z",
                "hourly": {"temperature_2m": [15.0, 19.6], "temperature_2m_previous_day1": [15.2, 20.2]}}

    monkeypatch.setattr(B, "_open_meteo_hourly", fake)
    out = M.run(db=db, now=datetime(2026, 10, 27, 5, 5, tzinfo=timezone.utc))
    assert out["pulled"] == 1 and out["failed"] == [["EGLC", "no_timezone_or_coordinates"]]
    assert seen == [("2026-10-27", True, "best_match_hourly")]
    line = json.loads((tmp_path / "bm" / "history.jsonl").read_text().splitlines()[0])
    assert (line["station_id"], line["tmax_latest_c"], line["tmax_d1_c"]) == ("ZUUU", 19.6, 20.2)
