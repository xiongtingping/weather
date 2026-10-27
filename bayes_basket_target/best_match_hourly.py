"""Every station's Open-Meteo best_match pulled once an hour (user 2026-10-27 「best_match每个
小时都要去拉取一次」).

For each station with a timezone binding, today's local day is asked of Open-Meteo whatever the
board's caches hold (``station_day_board._open_meteo_hourly(force=True)``, the same request the
board makes), on the ``best_match_hourly`` quota lane.  The pull lands in the board's disk
snapshot (``data/runtime/best_match_hourly/``), so a board opened within the hour reads it, and
one line per station goes to ``history.jsonl``: the latest run's day maximum and the maximum
of the run one day earlier, so the board can show how best_match moved hour by hour.

The SSOT is read only (coordinates, timezone); nothing enters any layer.  launchd
``com.weather.best_match_hourly`` runs it every hour; one pass is ~49 requests, about a minute.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import station_day_board as B

QUOTA_LANE = "best_match_hourly"
HISTORY_PATH = B.BEST_MATCH_DIR / "history.jsonl"
KEEP_SNAPSHOT_DAYS = 3


def _day_max(hourly: dict[str, Any], name: str) -> float | None:
    vals = [v for v in hourly.get(name) or [] if v is not None]
    return round(max(vals), 1) if vals else None


def _stations(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT station_id FROM calibration_station_market_timezone_binding"
        " WHERE station_id IS NOT NULL AND station_id<>'' ORDER BY station_id")]


def run(*, db: Path = B.DEFAULT_DB, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    token = B.quota_lane.set(QUOTA_LANE)
    lines, failed = [], []
    try:
        conn.execute("PRAGMA query_only=1")
        for station in _stations(conn):
            resolved = B._event_days(conn, station, now)
            where = B._station_lat_lon(conn, station)
            if resolved is None or where is None:
                failed.append([station, "no_timezone_or_coordinates"])
                continue
            tz = resolved[0]
            day = now.astimezone(B.ZoneInfo(tz)).date().isoformat()
            out = B._open_meteo_hourly(where[0], where[1], tz, day, force=True)
            if not out.get("available"):
                failed.append([station, out.get("reason")])
                if "额度" in str(out.get("reason")):
                    break  # the lane or the address is spent: the rest would be refused too
                continue
            hourly = out.get("hourly") or {}
            lines.append({"station_id": station, "event_date": day, "fetched_at_utc": out.get("fetched_at_utc"),
                          "tmax_latest_c": _day_max(hourly, "temperature_2m"),
                          "tmax_d1_c": _day_max(hourly, "temperature_2m_previous_day1")})
    finally:
        B.quota_lane.reset(token)
        conn.close()
    B.BEST_MATCH_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    cutoff = time.time() - KEEP_SNAPSHOT_DAYS * 86400
    for f in B.BEST_MATCH_DIR.glob("*_*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
    return {"pulled": len(lines), "failed": failed, "at_utc": B._iso(now)}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
