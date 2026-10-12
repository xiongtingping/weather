"""Round 54c: which native cell rule reproduces Open-Meteo's station value (GFS)?

Open-Meteo's documented default: ``cell_selection=land`` -- "finds a suitable
grid-cell on land with similar elevation to the requested coordinates using a
90-meter digital elevation model", and temperature is downscaled to that
elevation.  Ours: the nearest land cell, no height adjustment.  For six GFS
listing runs (D-2 00Z, event days spread over the period), the 2 m field at
every 3-hourly step covering the stations' local day, decoded once, gives for
each roster station the local-day maximum under each candidate rule:
  nearest        the nearest cell (land or not)
  nearest-land   our rule (nearest cell if land >= 0.5 else nearest land cell
                 within max(10 km, 1.5 spacings)); checked against the archive
  bilinear       the four surrounding cells, inverse-distance in lat/lon
  similar-elev   among land cells within +-2 cells, the one whose GFS terrain
                 height is closest to the station's elevation (AWC)
each also with the lapse-rate downscaling 0.0065 x (cell height - station).
Delta_rule = rule value - nearest-land value, per station x run x day, against
Delta_OM = Open-Meteo gfs_global (bench first_runs.json) - archive native value
for the same run and day: correlation across station-days and of per-station
means; slope.  Terrain: GFS HGT:surface at f000 of the first run; land: LAND.
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
RUN_DAYS = [date(2026, 9, 25), date(2026, 9, 29), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 8), date(2026, 10, 10)]
STEPS = list(range(36, 79, 3))


def decode(message):
    lib = _eccodes()
    buf = ctypes.create_string_buffer(message, len(message))
    h = lib.codes_handle_new_from_message(None, buf, ctypes.c_size_t(len(message)))
    if not h:
        raise RuntimeError("unreadable")
    try:
        R = N.RegularLatLonReader
        ni, nj = GridPointReader._long(lib, h, b"Ni"), GridPointReader._long(lib, h, b"Nj")
        lat0 = R._double(lib, h, b"latitudeOfFirstGridPointInDegrees")
        lon0 = R._double(lib, h, b"longitudeOfFirstGridPointInDegrees")
        di, dj = R._double(lib, h, b"iDirectionIncrementInDegrees"), R._double(lib, h, b"jDirectionIncrementInDegrees")
        j_pos = GridPointReader._long(lib, h, b"jScansPositively")
        vals = np.array(GridPointReader._array(lib, h, b"values"), dtype=float).reshape(int(nj), int(ni))
        return {"v": vals, "lat0": lat0, "lon0": lon0, "di": di, "dj": dj, "ni": int(ni), "nj": int(nj), "j_pos": bool(j_pos)}
    finally:
        lib.codes_handle_delete(h)


def cell_latlon(g, j, i):
    lat = g["lat0"] + (j * g["dj"] if g["j_pos"] else -j * g["dj"])
    lon = (g["lon0"] + i * g["di"]) % 360.0
    return lat, lon


def km(a, b, c, d):
    p = math.pi / 180
    h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def frac_index(g, lat, lon):
    fi = ((lon - g["lon0"]) % 360.0) / g["di"]
    fj = ((lat - g["lat0"]) if g["j_pos"] else (g["lat0"] - lat)) / g["dj"]
    return fj, fi


def cells_for(g_land, g_hgt, lat, lon, elev):
    """Indexes under each rule (j, i) and the bilinear weights."""
    fj, fi = frac_index(g_land, lat, lon)
    j0, i0 = int(round(fj)), int(round(fi)) % g_land["ni"]
    out = {"nearest": (j0, i0)}
    # nearest-land: our rule
    if g_land["v"][j0, i0] >= 0.5:
        out["nearest-land"] = (j0, i0)
    else:
        spacing = km(*cell_latlon(g_land, j0, i0), *cell_latlon(g_land, j0, (i0 + 1) % g_land["ni"]))
        reach = max(10.0, 1.5 * spacing)
        best = None
        for dj_ in range(-3, 4):
            for di_ in range(-3, 4):
                j, i = j0 + dj_, (i0 + di_) % g_land["ni"]
                if not 0 <= j < g_land["nj"] or g_land["v"][j, i] < 0.5:
                    continue
                d = km(lat, lon, *cell_latlon(g_land, j, i))
                if d <= reach and (best is None or d < best[0]):
                    best = (d, (j, i))
        out["nearest-land"] = best[1] if best else None
    # similar elevation among land cells within +-2 cells
    best = None
    for dj_ in range(-2, 3):
        for di_ in range(-2, 3):
            j, i = j0 + dj_, (i0 + di_) % g_land["ni"]
            if not 0 <= j < g_land["nj"] or g_land["v"][j, i] < 0.5:
                continue
            dh = abs(g_hgt["v"][j, i] - elev)
            if best is None or dh < best[0]:
                best = (dh, (j, i))
    out["similar-elev"] = best[1] if best else None
    # bilinear corners
    jf, if_ = int(math.floor(fj)), int(math.floor(fi))
    tj, ti = fj - jf, fi - if_
    corners = [((jf, if_ % g_land["ni"]), (1 - tj) * (1 - ti)), ((jf, (if_ + 1) % g_land["ni"]), (1 - tj) * ti),
               ((jf + 1, if_ % g_land["ni"]), tj * (1 - ti)), ((jf + 1, (if_ + 1) % g_land["ni"]), tj * ti)]
    out["bilinear"] = [(c, w) for c, w in corners if 0 <= c[0] < g_land["nj"]]
    return out


# masks and terrain from the first run
run0 = datetime(RUN_DAYS[0].year, RUN_DAYS[0].month, RUN_DAYS[0].day, tzinfo=timezone.utc) - timedelta(days=2)
land = decode(N.read_message("GFS", run0, 0, field="LAND:surface"))
hgt = decode(N.read_message("GFS", run0, 0, field="HGT:surface"))
print(f"GFS grid {land['ni']}x{land['nj']}, land/terrain from {run0.isoformat()}", file=sys.stderr)
stations = []
for st, lat, lon, zone in ROSTER:
    t = TERRAIN.get(st)
    if not t or t.get("station_elevation_m") is None:
        continue
    cells = cells_for(land, hgt, lat, lon, float(t["station_elevation_m"]))
    stations.append({"st": st, "lat": lat, "lon": lon, "zone": zone, "elev": float(t["station_elevation_m"]), "cells": cells})
print(f"{len(stations)} stations with an elevation; nearest != nearest-land on {sum(s['cells']['nearest'] != s['cells']['nearest-land'] for s in stations)},"
      f" similar-elev != nearest-land on {sum(s['cells']['similar-elev'] != s['cells']['nearest-land'] for s in stations)}", file=sys.stderr)

RULES = ("nearest", "nearest-land", "bilinear", "similar-elev")


def value(g, s, rule, lapse):
    c = s["cells"][rule]
    if c is None:
        return None
    if rule == "bilinear":
        v = sum(g["v"][j, i] * w for (j, i), w in c)
        h = sum(hgt["v"][j, i] * w for (j, i), w in c)
    else:
        v, h = g["v"][c], hgt["v"][c]
    return v - 273.15 + (LAPSE * (h - s["elev"]) if lapse else 0.0)


records = []  # (st, run_iso, day, rule, lapse, value)
for D in RUN_DAYS:
    run = datetime(D.year, D.month, D.day, tzinfo=timezone.utc) - timedelta(days=2)
    fields = {}
    for step in STEPS:
        msg = N.read_message("GFS", run, step)
        if msg is None:
            print(f"  {run.isoformat()} step {step}: missing", file=sys.stderr)
            continue
        fields[step] = decode(msg)
    print(f"  {run.isoformat()}: {len(fields)} steps decoded", file=sys.stderr)
    for s in stations:
        tz = ZoneInfo(s["zone"])
        for rule in RULES:
            for lapse in (False, True):
                by_day = {}
                for step, g in fields.items():
                    t = (run + timedelta(hours=step)).astimezone(tz)
                    v = value(g, s, rule, lapse)
                    if v is not None:
                        by_day.setdefault(t.date(), []).append((t.hour, v))
                for day, pts in by_day.items():
                    hours = [h for h, _v in pts]
                    if min(hours) <= 9 and max(hours) >= 15 and day in (D, D - timedelta(days=1), D + timedelta(days=1)):
                        records.append((s["st"], run.strftime("%Y-%m-%dT%H:%M"), day.isoformat(), rule, lapse, max(v for _h, v in pts)))

# archive native value and Open-Meteo value for the same run and day
rows = {}
for st, run_key, day, rule, lapse, v in records:
    rows.setdefault((st, run_key, day), {})[(rule, lapse)] = v
native_hit = om_hit = 0
pairs = []
for (st, run_key, day), vals in rows.items():
    nat = conn.execute("SELECT forecast_tmax_c FROM calibration_regional_point_run WHERE ensemble_system_id='NOAA_GFS_025_NATIVE_POINT'"
                       " AND station_id=? AND target_local_date=? AND run_init_utc=? ORDER BY archive_query_at_utc DESC LIMIT 1",
                       (st, day, run_key + ":00Z")).fetchone()
    om = OM.get(f"{st}|{run_key}", {}).get("tmax", {}).get("gfs_global", {}).get(day)
    if nat is None or om is None:
        continue
    native_hit += abs(float(nat[0]) - vals[("nearest-land", False)]) < 0.05
    pairs.append({"st": st, "day": day, "native": float(nat[0]), "om": float(om), **{f"{r}|{int(l)}": v for (r, l), v in vals.items()}})
print(f"\n{len(pairs)} station x run x day with an archive native value and an Open-Meteo value; my nearest-land replica matches the archive within 0.05 C on {native_hit}/{len(pairs)}")
d_om = np.array([p["om"] - p["native"] for p in pairs])
print(f"Delta_OM = OM - native: mean {d_om.mean():+.2f}, sd {d_om.std():.2f}")
print(f"{'rule':22s} {'mean Delta_rule':>15s} {'sd':>6s} {'corr(Delta_rule, Delta_OM)':>28s} {'per-station corr':>17s} {'slope':>6s} {'sd(OM - rule)':>14s}")
for rule in RULES:
    for lapse in (False, True):
        key = f"{rule}|{int(lapse)}"
        if any(key not in p for p in pairs):
            continue
        d_rule = np.array([p[key] - p["native"] for p in pairs])
        ok = np.isfinite(d_rule)
        per = {}
        for p in pairs:
            per.setdefault(p["st"], []).append((p[key] - p["native"], p["om"] - p["native"]))
        ps = np.array([(statistics.mean(a for a, _b in v), statistics.mean(b for _a, b in v)) for v in per.values() if len(v) >= 3])
        corr = np.corrcoef(d_rule[ok], d_om[ok])[0, 1] if d_rule[ok].std() > 1e-9 else float("nan")
        pcorr = np.corrcoef(ps[:, 0], ps[:, 1])[0, 1] if len(ps) > 3 and ps[:, 0].std() > 1e-9 else float("nan")
        slope = np.polyfit(d_rule[ok], d_om[ok], 1)[0] if d_rule[ok].std() > 1e-9 else float("nan")
        resid = np.array([p["om"] - p[key] for p in pairs])
        print(f"{rule + (' + lapse' if lapse else ''):22s} {d_rule.mean():+15.2f} {d_rule.std():6.2f} {corr:28.2f} {pcorr:17.2f} {slope:6.2f} {resid.std():14.2f}")
json.dump(pairs, open(B / "gfs_cell_selection_r54c.json", "w"))
