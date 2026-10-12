"""Round 54d: GFS on its native Gaussian flux grid (~0.117 deg, hourly TMAX).

Open-Meteo's gfs_global reads NCEP's sflux files, not the 0.25 degree pgrb2
we read (round 54c: no 0.25 degree resampling reproduces its station value).
For three listing runs (D-2 00Z), the sflux ``TMAX:2 m above ground`` field
(the running maximum since the last 6-hour boundary) at steps 36..78 every
3 h gives each station's local-day maximum of HOURLY temperature on the native
grid under: nearest cell; nearest land cell; the land cell within +-2 cells
whose terrain height is closest to the station's elevation (Open-Meteo's
documented default); each with and without the 0.0065 C/m lapse-rate
downscaling to the station's elevation.  Delta_rule = rule value - our
archived 0.25 degree nearest-land value, against Delta_OM = Open-Meteo value -
archived value, same run and day.  Grid fields: LAND and HGT at f000 of the
first run.  Reads ~45 messages of ~6 MB by byte range from the public S3
bucket (a research read; nothing is written to the SSOT).
"""
import ctypes
import json
import math
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import native_deterministic_point_archive as N  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402
from bayes_basket_target.dwd_icon_eps_archive import _eccodes  # noqa: E402
from bayes_basket_target.noaa_conus_archive import GridPointReader  # noqa: E402

B = Path(sys.argv[1])
HERE = Path(__file__).parent.parent
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
ROSTER = global_roster(conn)
TERRAIN = json.load(open(HERE / "terrain_offsets_r39h.json"))["systems"]["NOAA_GFS_025_NATIVE_POINT"]
OM = json.load(open(B / "first_runs.json"))
LAPSE = 0.0065
RUN_DAYS = [date(2026, 9, 29), date(2026, 10, 5), date(2026, 10, 8)]
STEPS = list(range(36, 79, 3))
SFLUX_CAP = 40 * 1024 * 1024


def sflux_url(run, step):
    return f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{run:%Y%m%d}/{run:%H}/atmos/gfs.t{run:%H}z.sfluxgrbf{step:03d}.grib2"


def read_sflux(run, step, field):
    idx = N._get(sflux_url(run, step) + ".idx", cap=N.MAX_INDEX_BYTES)
    if idx is None:
        return None
    span = N.parse_noaa_idx(idx.decode("utf-8", "replace"), field=field)
    if span is None:
        return None
    start, length = span
    rng = f"bytes={start}-" if length is None else f"bytes={start}-{start + length - 1}"
    return N._get(sflux_url(run, step), {"Range": rng}, cap=SFLUX_CAP)


def decode(message):
    lib = _eccodes()
    buf = ctypes.create_string_buffer(message, len(message))
    h = lib.codes_handle_new_from_message(None, buf, ctypes.c_size_t(len(message)))
    if not h:
        raise RuntimeError("unreadable")
    try:
        grid = N.RegularLatLonReader._string(lib, h, b"gridType")
        vals = np.array(GridPointReader._array(lib, h, b"values"), dtype=float)
        lats = np.array(GridPointReader._array(lib, h, b"latitudes"), dtype=float)
        lons = np.array(GridPointReader._array(lib, h, b"longitudes"), dtype=float) % 360.0
        return {"grid": grid, "v": vals, "lat": lats, "lon": lons}
    finally:
        lib.codes_handle_delete(h)


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    h = np.sin((lat2 - lat1) * p / 2) ** 2 + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * np.arcsin(np.sqrt(h))


run0 = datetime(RUN_DAYS[0].year, RUN_DAYS[0].month, RUN_DAYS[0].day, tzinfo=timezone.utc) - timedelta(days=2)
land = decode(read_sflux(run0, 0, "LAND:surface"))
hgt = decode(read_sflux(run0, 0, "HGT:surface"))
print(f"sflux grid {land['grid']}, {land['v'].size} points; land fraction >= 0.5 on {np.mean(land['v'] >= 0.5):.0%}", file=sys.stderr)
# candidate cells per station: everything within 0.35 deg (about +-3 cells)
stations = []
for st, lat, lon, zone in ROSTER:
    t = TERRAIN.get(st)
    if not t or t.get("station_elevation_m") is None:
        continue
    lon360 = lon % 360.0
    near = np.flatnonzero((np.abs(land["lat"] - lat) <= 0.35) & (np.minimum(np.abs(land["lon"] - lon360), 360 - np.abs(land["lon"] - lon360)) <= 0.35))
    d = km(lat, lon360, land["lat"][near], land["lon"][near])
    order = near[np.argsort(d)]
    dsort = np.sort(d)
    nearest = int(order[0])
    spacing = float(dsort[1]) if len(dsort) > 1 else 13.0
    is_land = land["v"][order] >= 0.5
    reach = max(10.0, 1.5 * spacing)
    nl = int(order[0]) if is_land[0] else next((int(i) for i, dd, l in zip(order, dsort, is_land) if l and dd <= reach), None)
    elev = float(t["station_elevation_m"])
    within2 = [int(i) for i, dd in zip(order, dsort) if dd <= 2.2 * spacing and land["v"][i] >= 0.5]
    sim = min(within2, key=lambda i: abs(hgt["v"][i] - elev)) if within2 else None
    stations.append({"st": st, "zone": zone, "elev": elev, "cells": {"nearest": nearest, "nearest-land": nl, "similar-elev": sim}})
print(f"{len(stations)} stations; similar-elev != nearest-land on {sum(s['cells']['similar-elev'] != s['cells']['nearest-land'] for s in stations)}", file=sys.stderr)

RULES = ("nearest", "nearest-land", "similar-elev")
rows = {}
for D in RUN_DAYS:
    run = datetime(D.year, D.month, D.day, tzinfo=timezone.utc) - timedelta(days=2)
    fields = {}
    for step in STEPS:
        msg = read_sflux(run, step, "TMAX:2 m above ground")
        if msg is None:
            print(f"  {run.isoformat()} f{step:03d}: missing", file=sys.stderr)
            continue
        fields[step] = decode(msg)["v"]
    print(f"  {run.isoformat()}: {len(fields)} TMAX steps", file=sys.stderr)
    for s in stations:
        tz = ZoneInfo(s["zone"])
        for rule in RULES:
            c = s["cells"][rule]
            if c is None:
                continue
            for lapse in (False, True):
                by_day = {}
                for step, v in fields.items():
                    # TMAX over the 3 h ending at the step: assign to the hour of the step's valid time
                    t = (run + timedelta(hours=step)).astimezone(tz)
                    val = float(v[c]) - 273.15 + (LAPSE * (float(hgt["v"][c]) - s["elev"]) if lapse else 0.0)
                    by_day.setdefault(t.date(), []).append((t.hour, val))
                for day, pts in by_day.items():
                    hours = [h for h, _v in pts]
                    if min(hours) <= 9 and max(hours) >= 15:
                        rows.setdefault((s["st"], run.strftime("%Y-%m-%dT%H:%M"), day.isoformat()), {})[(rule, lapse)] = max(v for _h, v in pts)

pairs = []
for (st, run_key, day), vals in rows.items():
    nat = conn.execute("SELECT forecast_tmax_c FROM calibration_regional_point_run WHERE ensemble_system_id='NOAA_GFS_025_NATIVE_POINT'"
                       " AND station_id=? AND target_local_date=? AND run_init_utc=? ORDER BY archive_query_at_utc DESC LIMIT 1",
                       (st, day, run_key + ":00Z")).fetchone()
    om = OM.get(f"{st}|{run_key}", {}).get("tmax", {}).get("gfs_global", {}).get(day)
    if nat is None or om is None:
        continue
    pairs.append({"st": st, "day": day, "native": float(nat[0]), "om": float(om), **{f"{r}|{int(l)}": v for (r, l), v in vals.items()}})
d_om = np.array([p["om"] - p["native"] for p in pairs])
print(f"\n{len(pairs)} station x run x day paired; Delta_OM = OM - archived 0.25 nearest-land: mean {d_om.mean():+.2f}, sd {d_om.std():.2f}")
print(f"{'sflux rule':24s} {'mean Delta':>10s} {'sd':>6s} {'corr':>6s} {'per-station corr':>17s} {'slope':>6s} {'sd(OM - rule)':>14s} {'|OM - rule| < 0.5 C':>20s}")
for rule in RULES:
    for lapse in (False, True):
        key = f"{rule}|{int(lapse)}"
        sub = [p for p in pairs if key in p]
        if len(sub) < 20:
            continue
        d_rule = np.array([p[key] - p["native"] for p in sub])
        d_o = np.array([p["om"] - p["native"] for p in sub])
        per = {}
        for p in sub:
            per.setdefault(p["st"], []).append((p[key] - p["native"], p["om"] - p["native"]))
        ps = np.array([(statistics.mean(a for a, _b in v), statistics.mean(b for _a, b in v)) for v in per.values() if len(v) >= 2])
        resid = np.array([p["om"] - p[key] for p in sub])
        print(f"{rule + (' + lapse' if lapse else ''):24s} {d_rule.mean():+10.2f} {d_rule.std():6.2f} {np.corrcoef(d_rule, d_o)[0, 1]:6.2f}"
              f" {np.corrcoef(ps[:, 0], ps[:, 1])[0, 1]:17.2f} {np.polyfit(d_rule, d_o, 1)[0]:6.2f} {resid.std():14.2f} {np.mean(np.abs(resid) < 0.5):20.0%}")
json.dump(pairs, open(B / "gfs_sflux_grid_r54d.json", "w"))
