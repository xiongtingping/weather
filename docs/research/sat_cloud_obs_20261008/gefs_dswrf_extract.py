"""Official forecast shortwave: NOAA GEFS ensemble-mean DSWRF at the 48 stations.

熊先生 2026-10-08「做」: the radiation increment held out of sample against
Open-Meteo's day-ahead shortwave, but Open-Meteo is a republisher and may not
enter P1.  This replaces the forecast side with the official native product
of a family already in the active registry (NOAA_GEFS_05): ``geavg``
pgrb2a 0.5 deg ``DSWRF:surface``, every cycle 00/06/12/18Z, steps f003..f018.

Each record is fetched by its .idx byte range from the public NOAA bucket and
kept with the object's Last-Modified (the instant it was publicly readable)
and the message's sha256.  GEFS writes DSWRF as means since the last 6-hour
boundary (f003 0-3h, f006 0-6h, f009 6-9h, ...); ``blocks`` turns them into
3-hour block means.  Read-only; nothing here touches the SSOT.

Usage:  .venv-sat/bin/python gefs_dswrf_extract.py [--start 2026-08-30] [--end 2026-10-06] [--max-minutes 8]
"""
from __future__ import annotations

import argparse
import ctypes
import gzip
import hashlib
import json
import math
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from rangefile import fetch  # noqa: E402

BUCKET = "https://noaa-gefs-pds.s3.amazonaws.com"
import os
# Two step sets: the freshest run (f003-f018) and a day ahead (f021-f042),
# the latter to match what a forecast issued the day before said.
STEP_SET = os.environ.get("GEFS_STEP_SET", "near")
OUT = HERE / ("gefs_dswrf" if STEP_SET == "near" else "gefs_dswrf_d1")
STEPS = (3, 6, 9, 12, 15, 18) if STEP_SET == "near" else (21, 24, 27, 30, 33, 36, 39, 42)
ECCODES = Path("/opt/homebrew/lib/libeccodes.dylib")
MAX_MESSAGE = 1024 * 1024


def stations() -> list[tuple[str, float, float]]:
    import sqlite3
    root = HERE.parents[2]
    con = sqlite3.connect(f"file:{root / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=30)
    rows = con.execute(
        "SELECT station_id, latitude, longitude FROM calibration_station_forecast_physics_version v"
        " WHERE latitude IS NOT NULL AND captured_at_utc=(SELECT MAX(captured_at_utc) FROM calibration_station_forecast_physics_version w"
        " WHERE w.station_id=v.station_id AND w.latitude IS NOT NULL) GROUP BY station_id ORDER BY station_id").fetchall()
    return [(s, float(a), float(b)) for s, a, b in rows]


def nearest_values(message: bytes, points: list[tuple[str, float, float]]) -> dict[str, float]:
    """ecCodes nearest grid point for many stations from one GRIB2 message."""
    if len(message) <= 16 or len(message) > MAX_MESSAGE:
        raise ValueError("dswrf_message_size")
    lib = ctypes.CDLL(str(ECCODES))
    lib.codes_handle_new_from_message.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    lib.codes_handle_new_from_message.restype = ctypes.c_void_p
    lib.codes_grib_nearest_find_multiple.argtypes = [ctypes.c_void_p, ctypes.c_int] + [ctypes.POINTER(ctypes.c_double)] * 2 + [ctypes.c_long] + [ctypes.POINTER(ctypes.c_double)] * 4 + [ctypes.POINTER(ctypes.c_int)]
    lib.codes_grib_nearest_find_multiple.restype = ctypes.c_int
    lib.codes_handle_delete.argtypes = [ctypes.c_void_p]
    raw = ctypes.create_string_buffer(message)
    handle = lib.codes_handle_new_from_message(None, ctypes.cast(raw, ctypes.c_void_p), len(message))
    if not handle:
        raise ValueError("dswrf_handle_failed")
    n = len(points)
    D = ctypes.c_double * n
    inlat, inlon = D(*(p[1] for p in points)), D(*(p[2] % 360.0 for p in points))
    olat, olon, val, dist = D(), D(), D(), D()
    idx = (ctypes.c_int * n)()
    try:
        err = lib.codes_grib_nearest_find_multiple(handle, 0, inlat, inlon, n, olat, olon, val, dist, idx)
    finally:
        lib.codes_handle_delete(handle)
    if err != 0:
        raise ValueError("dswrf_nearest_failed")
    out = {}
    for i, (st, _, _) in enumerate(points):
        v = float(val[i])
        if not math.isfinite(v) or not -1.0 <= v <= 1500.0 or float(dist[i]) > 60.0:
            raise ValueError(f"dswrf_value_invalid:{st}:{v}:{dist[i]}")
        out[st] = round(max(0.0, v), 2)
    return out


def one_step(run: datetime, step: int, points) -> dict:
    hh = f"{run:%H}"
    path = f"gefs.{run:%Y%m%d}/{hh}/atmos/pgrb2ap5/geavg.t{hh}z.pgrb2a.0p50.f{step:03d}"
    idx = fetch(f"{BUCKET}/{path}.idx", timeout=60).text
    lines = idx.splitlines()
    hit = [i for i, l in enumerate(lines) if ":DSWRF:surface:" in l]
    if len(hit) != 1:
        raise ValueError("dswrf_idx_not_unique")
    i = hit[0]
    start = int(lines[i].split(":")[1])
    end = int(lines[i + 1].split(":")[1]) - 1 if i + 1 < len(lines) else None
    if end is None or not 0 < end - start < MAX_MESSAGE:
        raise ValueError("dswrf_range_invalid")
    window = re.search(r":(\d+)-(\d+) hour ave fcst:", lines[i])
    if not window:
        raise ValueError("dswrf_not_an_average:" + lines[i])
    r = fetch(f"{BUCKET}/{path}", {"Range": f"bytes={start}-{end}"}, timeout=60)
    body = r.content
    if len(body) != end - start + 1 or body[:4] != b"GRIB":
        raise ValueError("dswrf_message_short")
    return dict(run=run.isoformat().replace("+00:00", "Z"), step=step, avg_from=int(window.group(1)), avg_to=int(window.group(2)),
                key=path, last_modified=r.headers.get("Last-Modified"), message_sha256=hashlib.sha256(body).hexdigest(),
                values=nearest_values(body, points))


def _job(args) -> dict:
    run, points = args
    rows, errors = [], []
    for step in STEPS:
        try:
            rows.append(one_step(run, step, points))
        except Exception as exc:  # noqa: BLE001 -- recorded; the run is retried next batch
            errors.append(dict(step=step, error=repr(exc)[:200]))
    if errors:
        return dict(run=run.isoformat(), errors=errors)
    target = OUT / f"{run:%Y-%m-%d}" / f"{run:%H}Z.json.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt") as out:
        json.dump(dict(schema="gefs_geavg_dswrf_station_points_v1", run=run.isoformat().replace("+00:00", "Z"), steps=rows), out)
    tmp.rename(target)
    return dict(run=run.isoformat(), ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-30")
    ap.add_argument("--end", default="2026-10-06")
    ap.add_argument("--max-minutes", type=float, default=8.0)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    points = stations()
    first = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc) - (timedelta(days=1) if STEP_SET != "near" else timedelta(0))
    last = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) + timedelta(hours=18)
    runs = []
    t = first
    while t <= last:
        if not (OUT / f"{t:%Y-%m-%d}" / f"{t:%H}Z.json.gz").exists():
            runs.append(t)
        t += timedelta(hours=6)
    runs.sort(key=lambda r: (r < datetime(2026, 9, 20, tzinfo=timezone.utc), -r.timestamp()))
    deadline = time.time() + 60 * args.max_minutes
    import multiprocessing as mp
    done = failed = 0
    with mp.get_context("spawn").Pool(args.workers) as pool:
        pending = iter(runs)
        for res in pool.imap_unordered(_job, ((r, points) for r in runs if time.time() < deadline)):
            if res.get("ok"):
                done += 1
            else:
                failed += 1
                print(json.dumps(res)[:300], flush=True)
            if time.time() > deadline:
                break
    print(json.dumps(dict(stations=len(points), runs_written=done, failed=failed, pending_left=len(runs) - done)), flush=True)


if __name__ == "__main__":
    main()
