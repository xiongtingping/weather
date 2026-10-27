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
# best_match's first forecast of a day for 首次预报 vs 实测 (user 2026-10-27 「这里也要增加：首次预报 vs
# 实测」): the run two days before it (``temperature_2m_previous_day2``), the one out when a market
# lists; once a UTC day per station, the last 31 days to two days ahead in one request.
FIRST_PATH = B.BEST_MATCH_DIR / "first_d2.json"
FIRST_BACK_DAYS, FIRST_AHEAD_DAYS = 31, 2


def _day_max(hourly: dict[str, Any], name: str) -> float | None:
    vals = [v for v in hourly.get(name) or [] if v is not None]
    return round(max(vals), 1) if vals else None


def _first_d2(lat: float, lon: float, tz: str, today: str) -> dict[str, float] | None:
    """Local day -> the day-two-earlier run's maximum; None on any failure (quota included)."""

    import ssl  # noqa: PLC0415
    from datetime import date, timedelta  # noqa: PLC0415
    from urllib.parse import urlencode  # noqa: PLC0415
    from urllib.request import Request, urlopen  # noqa: PLC0415

    from . import open_meteo_quota as quota  # noqa: PLC0415

    d = date.fromisoformat(today)
    query = urlencode({"latitude": lat, "longitude": lon, "timezone": tz, "hourly": "temperature_2m_previous_day2",
                       "start_date": (d - timedelta(days=FIRST_BACK_DAYS)).isoformat(),
                       "end_date": (d + timedelta(days=FIRST_AHEAD_DAYS)).isoformat()})
    try:
        quota.charge(QUOTA_LANE, 1)
        try:
            import certifi  # noqa: PLC0415

            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        with urlopen(Request(f"{B.OPEN_METEO_PREVIOUS_RUNS}?{query}", headers={"User-Agent": "weather-best-match-hourly"}),
                     timeout=30, context=context) as resp:  # noqa: S310 -- fixed https host
            hourly = (json.loads(resp.read().decode("utf-8")) or {}).get("hourly") or {}
    except Exception:  # noqa: BLE001 -- tried again next hour
        return None
    days: dict[str, list[float]] = {}
    for t, v in zip(hourly.get("time") or [], hourly.get("temperature_2m_previous_day2") or []):
        if v is not None:
            days.setdefault(str(t)[:10], []).append(float(v))
    return {k: round(max(v), 1) for k, v in days.items() if len(v) == 24}


def load_first() -> dict[str, Any]:
    try:
        doc = json.loads(FIRST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _stations(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT station_id FROM calibration_station_market_timezone_binding"
        " WHERE station_id IS NOT NULL AND station_id<>'' ORDER BY station_id")]


def run(*, db: Path = B.DEFAULT_DB, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    token = B.quota_lane.set(QUOTA_LANE)
    lines, failed = [], []
    first = load_first()
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
            if (first.get(station) or {}).get("pulled_on") != now.date().isoformat():
                got = _first_d2(where[0], where[1], tz, day)
                if got:
                    first[station] = {"pulled_on": now.date().isoformat(), "days": got}
            lines.append({"station_id": station, "event_date": day, "fetched_at_utc": out.get("fetched_at_utc"),
                          "tmax_latest_c": _day_max(hourly, "temperature_2m"),
                          "tmax_d1_c": _day_max(hourly, "temperature_2m_previous_day1")})
    finally:
        B.quota_lane.reset(token)
        conn.close()
    B.BEST_MATCH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FIRST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(first, sort_keys=True), encoding="utf-8")
    tmp.replace(FIRST_PATH)
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
