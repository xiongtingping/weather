"""M1 training store of the same-day Tmax increment model (ARCHITECTURE.md in
docs/research/tmax_physics_model_design_20261020; coverage in
docs/research/tmax_physics_m0_coverage_20261020/COVERAGE.md).

A research store of its own, ``data/tmax_physics/train.db`` -- never the
SSOT (charter 267 addendum 8 alike).  Every row keeps its source and whether
it is a proxy; nothing here feeds P0/P0-M/P1, a lane or an order.

Window: 2024-01-01 onward, the day-ahead basis's archive (Open-Meteo
``_previous_day1``) being the binding limit.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "tmax_physics" / "train.db"
START = "2024-01-01"
MIN_FREE_BYTES = 3 * 1024 ** 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS metar_hourly (
  station TEXT NOT NULL, valid_utc TEXT NOT NULL,
  tmp_c REAL, dew_c REAL, rh_pct REAL, wind_dir_deg REAL, wind_kt REAL, vis_mi REAL,
  sky_json TEXT, wx TEXT, source TEXT NOT NULL,
  PRIMARY KEY (station, valid_utc));
CREATE TABLE IF NOT EXISTS tmax_daily (
  station TEXT NOT NULL, local_date TEXT NOT NULL, tmax_c REAL NOT NULL, n_reports INTEGER NOT NULL,
  timezone TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (station, local_date));
CREATE TABLE IF NOT EXISTS sw_measured_hourly (
  station TEXT NOT NULL, hour_utc TEXT NOT NULL, sw_wm2 REAL, source TEXT NOT NULL, proxy INTEGER NOT NULL,
  scan_utc TEXT,
  PRIMARY KEY (station, hour_utc, source));
CREATE TABLE IF NOT EXISTS forecast_d1_hourly (
  station TEXT NOT NULL, valid_utc TEXT NOT NULL, variable TEXT NOT NULL, value REAL, source TEXT NOT NULL,
  PRIMARY KEY (station, valid_utc, variable, source));
CREATE TABLE IF NOT EXISTS fetch_log (
  task TEXT NOT NULL, unit TEXT NOT NULL, state TEXT NOT NULL, detail TEXT, at_utc TEXT NOT NULL,
  PRIMARY KEY (task, unit));
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def disk_ok() -> bool:
    import shutil  # noqa: PLC0415

    return shutil.disk_usage(DB.parent if DB.parent.exists() else ROOT).free >= MIN_FREE_BYTES


def timezones() -> dict[str, str]:
    """Station -> settlement timezone, from the one binding (AGENTS: never the
    registry's seed timezone)."""

    ssot = ROOT / "data" / "bayes_basket_target_ssot.db"
    conn = sqlite3.connect(f"file:{ssot}?mode=ro", uri=True, timeout=60.0)
    try:
        return dict(conn.execute(
            "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"))
    finally:
        conn.close()


def mark(conn: sqlite3.Connection, task: str, unit: str, state: str, detail: str = "") -> None:
    from datetime import datetime, timezone  # noqa: PLC0415

    conn.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
                 (task, unit, state, detail[:500], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))


def done(conn: sqlite3.Connection, task: str) -> set[str]:
    return {u for (u,) in conn.execute("SELECT unit FROM fetch_log WHERE task=? AND state='ok'", (task,))}
