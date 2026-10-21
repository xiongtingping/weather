"""M3 station-day table (PROTOCOL.md section 1-2): the M2 table plus the
hourly report and day-ahead forecast of temperature, dew point, humidity,
wind and weather, local stamps 06..17 (the board's: the hour ending h:00).

  * forecast: Open-Meteo ``_previous_day1`` (wind fetched in km/h -> m/s;
    the board asks m/s);
  * report: IEM METAR nearest each stamp within 30 min; humidity from the
    report's temperature and dew point by the board's own Magnus
    (``station_day_board.relative_humidity``); wind knots -> m/s;
  * measured shortwave: ``proxy=0`` rows only, hours before sunrise 0.

Writes ``data/tmax_physics/m3_station_days.json``.  A station-day enters with
the 06..17 shortwave (both), the 06:00 report and the whole day-ahead day.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs" / "research" / "tmax_physics_m1_ingest_20261020"))
import store  # noqa: E402

from bayes_basket_target.station_day_board import _clear_sky_ghi, relative_humidity  # noqa: E402
from bayes_basket_target.station_registry import _COORDINATES  # noqa: E402

HOURS = list(range(6, 18))
OUT = ROOT / "data" / "tmax_physics" / "m3_station_days.json"
FC_VARS = ("temperature_2m", "shortwave_radiation", "dew_point_2m", "relative_humidity_2m", "wind_speed_10m")
SOURCE_RANK = {"GOES_DSRF": 0, "jma_jaxa_himawari": 0, "dwd_sis_europe_africa_v4": 0, "eumetsat_lsa_saf_msg": 0,
               "eumetsat_lsa_saf_iodc": 0, "eumetsat_sarah3": 1, "GOES_DSRF_V01_0P5DEG": 1}
WET = ("RA", "DZ", "SN", "SH", "TS", "GR", "PL", "SG", "UP")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _wet(wx: str | None) -> bool:
    if not wx:
        return False
    return any(any(w in tok for w in WET) and not tok.startswith("VC") for tok in wx.split())


def main() -> int:
    tz = store.timezones()
    conn = sqlite3.connect(f"file:{store.DB}?mode=ro", uri=True, timeout=60.0)
    today = datetime.now(timezone.utc).date()
    out = []
    for st in sorted(tz):
        zone = ZoneInfo(tz[st])
        lat, lon = _COORDINATES[st]
        fc = {v: {} for v in FC_VARS}
        for t, var, val in conn.execute(
                "SELECT valid_utc, variable, value FROM forecast_d1_hourly WHERE station=? AND value IS NOT NULL", (st,)):
            if var in fc:
                fc[var][_ts(t)] = val
        # Protocol addendum (2026-10-21, before any reading): the board's own
        # product first, then SARAH3 (DWD SIS / MSG history), then GOES v01
        # 0.5-degree; the day records which carried its hours.
        obs_sw: dict = {}
        src_of: dict = {}
        for t, v, src in conn.execute(
                "SELECT hour_utc, sw_wm2, source FROM sw_measured_hourly WHERE station=? AND proxy=0 AND sw_wm2 IS NOT NULL",
                (st,)):
            k = _ts(t)
            if k not in obs_sw or SOURCE_RANK.get(src, 9) < SOURCE_RANK.get(src_of[k], 9):
                obs_sw[k], src_of[k] = v, src
        if not fc["shortwave_radiation"] or not obs_sw:
            continue
        metar = sorted((_ts(t), tc, dc, kt, wx) for t, tc, dc, kt, wx in conn.execute(
            "SELECT valid_utc, tmp_c, dew_c, wind_kt, wx FROM metar_hourly WHERE station=? AND tmp_c IS NOT NULL", (st,)))
        mt = [m[0] for m in metar]
        tmax = dict(conn.execute("SELECT local_date, tmax_c FROM tmax_daily WHERE station=?", (st,)))
        for d in sorted({t.astimezone(zone).date() for t in obs_sw}):
            if d >= today or d.isoformat() not in tmax:
                continue
            stamps = [datetime(d.year, d.month, d.day, h, tzinfo=zone).astimezone(timezone.utc) for h in HOURS]
            sw_fc = [fc["shortwave_radiation"].get(s) for s in stamps]
            sw_obs = [obs_sw.get(s, 0.0 if _clear_sky_ghi(lat, lon, s - timedelta(minutes=30)) <= 0.0 else None)
                      for s in stamps]
            if any(v is None for v in sw_fc + sw_obs):
                continue
            day_t = [fc["temperature_2m"].get(datetime(d.year, d.month, d.day, h, tzinfo=zone).astimezone(timezone.utc))
                     for h in range(24)]
            if any(v is None for v in day_t):
                continue
            rep = []
            for s in stamps:
                i = bisect_left(mt, s)
                best = min((metar[j] for j in (i - 1, i) if 0 <= j < len(metar)),
                           key=lambda m: abs((m[0] - s).total_seconds()), default=None)
                rep.append(best if best and abs((best[0] - s).total_seconds()) <= 1800 else None)
            if rep[0] is None:
                continue
            g = lambda var: [fc[var].get(s) for s in stamps]  # noqa: E731
            wind_fc = [None if v is None else v / 3.6 for v in g("wind_speed_10m")]
            out.append({
                "station": st, "date": d.isoformat(), "month": d.month,
                "sw_sources": sorted({src_of[s] for s in stamps if s in src_of}),
                "tmax": tmax[d.isoformat()], "tmax_fc": max(day_t),
                "sw_fc": sw_fc, "sw_obs": sw_obs,
                "t_fc": g("temperature_2m"), "dew_fc": g("dew_point_2m"), "rh_fc": g("relative_humidity_2m"),
                "wind_fc": wind_fc,
                "t_obs": [None if r is None else r[1] for r in rep],
                "dew_obs": [None if r is None else r[2] for r in rep],
                "rh_obs": [None if r is None else relative_humidity(r[1], r[2]) for r in rep],
                "wind_obs": [None if r is None or r[3] is None else r[3] * 0.514444 for r in rep],
                "wet_obs": [None if r is None else _wet(r[4]) for r in rep],
            })
    conn.close()
    OUT.write_text(json.dumps(out))
    by: dict[str, int] = {}
    for r in out:
        by[r["station"]] = by.get(r["station"], 0) + 1
    print(len(out), "station-days,", len(by), "stations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
