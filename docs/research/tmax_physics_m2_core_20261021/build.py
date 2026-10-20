"""M2 station-day table for the same-day Tmax model's physics core
(ARCHITECTURE.md, docs/research/tmax_physics_model_design_20261020).

Pipeline validation on the data in the M1 store so far (user 2026-10-21
「先用已有数据进行下一步」): every number here is refitted once M1 is whole
and none is a reading (charter 255: a partial store is not the current
segment of anything).

Per station x local day, local hours 06..17 (the board's stamps: the hour
ending at h:00):
  * ``sw_fc``  -- day-ahead forecast shortwave (Open-Meteo ``_previous_day1``),
  * ``sw_obs`` -- measured shortwave on the board's product (``proxy=0`` rows
    only; ERA5 proxies never enter),
  * ``t_obs``  -- IEM METAR temperature nearest each stamp (within 30 min),
  * ``tmax``   -- IEM local-day maximum (MAE 0.022 C to formal truth), days
    before the run date only,
  * ``tmax_fc`` -- day-ahead forecast maximum over the local day's 24 hours.
A station-day enters only with all twelve forecast and measured hours.
Writes ``data/tmax_physics/m2_station_days.json``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from bisect import bisect_left
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "docs" / "research" / "tmax_physics_m1_ingest_20261020"))
import store  # noqa: E402

from bayes_basket_target.station_day_board import _clear_sky_ghi  # noqa: E402
from bayes_basket_target.station_registry import _COORDINATES  # noqa: E402

HOURS = list(range(6, 18))
OUT = ROOT / "data" / "tmax_physics" / "m2_station_days.json"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main() -> int:
    tz = store.timezones()
    conn = sqlite3.connect(f"file:{store.DB}?mode=ro", uri=True, timeout=60.0)
    today = datetime.now(timezone.utc).date()
    out = []
    for st in sorted(tz):
        zone = ZoneInfo(tz[st])
        fc_sw = {_ts(t): v for t, v in conn.execute(
            "SELECT valid_utc, value FROM forecast_d1_hourly WHERE station=? AND variable='shortwave_radiation'", (st,))}
        fc_t = {_ts(t): v for t, v in conn.execute(
            "SELECT valid_utc, value FROM forecast_d1_hourly WHERE station=? AND variable='temperature_2m'", (st,))}
        obs_sw = {_ts(t): v for t, v in conn.execute(
            "SELECT hour_utc, sw_wm2 FROM sw_measured_hourly WHERE station=? AND proxy=0 AND sw_wm2 IS NOT NULL", (st,))}
        if not fc_sw or not obs_sw:
            continue
        metar = sorted((_ts(t), v) for t, v in conn.execute(
            "SELECT valid_utc, tmp_c FROM metar_hourly WHERE station=? AND tmp_c IS NOT NULL", (st,)))
        mt = [m[0] for m in metar]
        tmax = dict(conn.execute("SELECT local_date, tmax_c FROM tmax_daily WHERE station=?", (st,)))
        days = sorted({t.astimezone(zone).date() for t in obs_sw})
        for d in days:
            if d >= today or d.isoformat() not in tmax:
                continue
            stamps = [datetime(d.year, d.month, d.day, h, tzinfo=zone).astimezone(timezone.utc) for h in HOURS]
            sw_fc = [fc_sw.get(s) for s in stamps]
            # An hour whose middle is before sunrise is never read (no sun): 0.
            lat, lon = _COORDINATES[st]
            sw_obs = [obs_sw.get(s, 0.0 if _clear_sky_ghi(lat, lon, s - timedelta(minutes=30)) <= 0.0 else None)
                      for s in stamps]
            if any(v is None for v in sw_fc + sw_obs):
                continue
            t_obs = []
            for s in stamps:
                i = bisect_left(mt, s)
                best = min((metar[j] for j in (i - 1, i) if 0 <= j < len(metar)),
                           key=lambda m: abs((m[0] - s).total_seconds()), default=None)
                t_obs.append(best[1] if best and abs((best[0] - s).total_seconds()) <= 1800 else None)
            day_fc = [fc_t.get(datetime(d.year, d.month, d.day, h, tzinfo=zone).astimezone(timezone.utc))
                      for h in range(24)]
            if any(v is None for v in day_fc) or t_obs[0] is None:
                continue
            out.append({"station": st, "date": d.isoformat(), "sw_fc": sw_fc, "sw_obs": sw_obs, "t_obs": t_obs,
                        "tmax": tmax[d.isoformat()], "tmax_fc": max(day_fc)})
    conn.close()
    OUT.write_text(json.dumps(out))
    by = {}
    for r in out:
        by[r["station"]] = by.get(r["station"], 0) + 1
    print(len(out), "station-days", dict(sorted(by.items())))
    print("dates", min(r["date"] for r in out) if out else "-", "..", max(r["date"] for r in out) if out else "-")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
