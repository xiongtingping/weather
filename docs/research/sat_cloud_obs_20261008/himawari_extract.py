"""Satellite low cloud at the Himawari-9 stations (NOAA open data, no account).

Companion of ``goes_extract.py`` for the Asia-Pacific stations.  NOAA's
``noaa-himawari9`` bucket carries the NESDIS enterprise cloud products for
AHI full disk every 10 minutes, 2 km:

* AHI-CMSK  CloudMask: 0 clear, 1 probably clear, 2 probably cloudy, 3 cloudy
* AHI-CHGT  CldTopHght: cloud-top height (m)

A satellite sees cloud tops.  "Low cloud" is therefore read later as cloudy
pixels whose top is low; a low deck under a higher deck is invisible here
(the METAR sees it from below).  The raw 5x5 box (~10 km) is stored so the
threshold is chosen in the analysis, not baked in.  The first scan of each
UTC hour is used for local 08-17 at any station.  Files are 350-750 MB; only
the 200x200 chunks around each station are range-read, under the shared
rate budget in ``rangefile``.  Every row carries the object's S3
LastModified (public-availability instant) and the scan times.

Usage:  .venv-sat/bin/python himawari_extract.py [--start 2026-08-31] [--end 2026-10-08] [--max-minutes 9]
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h5py
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from rangefile import RangeFile  # noqa: E402
from goes_extract import listing  # noqa: E402

BUCKET = "noaa-himawari9"
OUT = HERE / "himawari"
PIXELS = HERE / "himawari_pixels.json"
STATIONS = {
    "NZWN": (-41.331, 174.806), "RCSS": (25.069, 121.552), "RJTT": (35.553, 139.781),
    "RKPK": (35.179, 128.938), "RKSI": (37.469, 126.451), "RPLL": (14.507, 121.004),
    "WMKK": (2.747, 101.714), "WSSS": (1.368, 103.982), "ZBAA": (40.082, 116.603),
    "ZGGG": (23.392, 113.307), "ZGSZ": (22.639, 113.803), "ZHHH": (30.783, 114.205),
    "ZSPD": (31.146, 121.8), "ZSQD": (36.362, 120.087), "ZUCK": (29.718, 106.639),
    "ZUUU": (30.576, 103.95),
}
SUB_LON, N, STEP = 140.7, 5500, 5.6e-5  # AHI full disk, 2 km: 56 microradians
HALF = 2  # 5x5 box


def guess_pixel(lat: float, lon: float) -> tuple[int, int]:
    req, rpol, h = 6378137.0, 6356752.31414, 35785863.0 + 6378137.0
    phi, lam = math.radians(lat), math.radians(lon - SUB_LON)
    phic = math.atan(rpol ** 2 / req ** 2 * math.tan(phi))
    rc = rpol / math.sqrt(1 - (1 - rpol ** 2 / req ** 2) * math.cos(phic) ** 2)
    sx = h - rc * math.cos(phic) * math.cos(lam)
    sy = -rc * math.cos(phic) * math.sin(lam)
    sz = rc * math.sin(phic)
    x = math.asin(-sy / math.sqrt(sx * sx + sy * sy + sz * sz))
    y = math.atan(sz / sx)
    return round(N / 2 - y / STEP - 0.5), round(N / 2 + x / STEP - 0.5)


def pixel_map(obj: dict) -> dict:
    """Nearest pixel per station, refined on the file's own Latitude/Longitude."""
    if PIXELS.exists():
        return json.loads(PIXELS.read_text())
    url = f"https://{BUCKET}.s3.amazonaws.com/{obj['key']}"
    out = {}
    with h5py.File(RangeFile(url, obj["size"], cap_bytes=48 * 1024 ** 2), "r") as h:
        for st, (lat, lon) in STATIONS.items():
            r0, c0 = guess_pixel(lat, lon)
            w = 40
            la = h["Latitude"][r0 - w:r0 + w, c0 - w:c0 + w].astype(float)
            lo = h["Longitude"][r0 - w:r0 + w, c0 - w:c0 + w].astype(float)
            d = (la - lat) ** 2 + ((lo - lon + 180) % 360 - 180) ** 2 * math.cos(math.radians(lat)) ** 2
            i, j = np.unravel_index(np.nanargmin(d), d.shape)
            out[st] = dict(row=int(r0 - w + i), col=int(c0 - w + j), pixel_lat=float(la[i, j]), pixel_lon=float(lo[i, j]),
                           distance_km=round(math.sqrt(float(d[i, j])) * 111.2, 2), guess=[r0, c0], source_key=obj["key"])
    PIXELS.write_text(json.dumps(out, indent=1))
    return out


def scan_times(key: str) -> tuple[str, str, str]:
    def t(tag):
        v = re.search(tag + r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d)", key).groups()
        return datetime(*map(int, v[:6]), 100000 * int(v[6]), tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return t("_s"), t("_e"), t("_c")


def read_box(obj: dict, var: str, pixels: dict, stations: list[str]) -> tuple[dict, int]:
    url = f"https://{BUCKET}.s3.amazonaws.com/{obj['key']}"
    rf = RangeFile(url, obj["size"], cap_bytes=40 * 1024 ** 2)
    out = {}
    with h5py.File(rf, "r") as h:
        ds = h[var]
        for st in stations:
            p = pixels[st]
            box = ds[p["row"] - HALF:p["row"] + HALF + 1, p["col"] - HALF:p["col"] + HALF + 1]
            out[st] = box.astype(float)
    return out, rf.fetched


def hours(day: datetime) -> list[datetime]:
    keep = []
    for h in range(24):
        if any(8 <= (h + lon / 15.0) % 24 <= 17 for _, lon in STATIONS.values()):
            keep.append(day + timedelta(hours=h))
    return keep


def one_hour(t: datetime, pixels: dict) -> list[dict]:
    objs = listing(BUCKET, f"AHI-L2-FLDK-Clouds/{t:%Y/%m/%d/%H}00/")
    by = {}
    for o in objs:
        m = re.search(r"AHI-(CMSK|CHGT)_", o["key"])
        if m:
            by[m.group(1)] = o
    if set(by) != {"CMSK", "CHGT"}:
        return [dict(state="OBJECT_MISSING", hour=t.isoformat(), present=sorted(by))]
    stations = [s for s, (_, lon) in STATIONS.items() if 7 <= (t.hour + lon / 15.0) % 24 <= 18]
    mask, b1 = read_box(by["CMSK"], "CloudMask", pixels, stations)
    # Cloud tops are only meaningful where the mask sees cloud; a clear box
    # skips the (float32, ~70 KB/chunk) height read entirely.
    cloudy = [s for s in stations if np.nanmax(np.where(mask[s] >= 0, mask[s], -1)) >= 2]
    top, b2 = read_box(by["CHGT"], "CldTopHght", pixels, cloudy) if cloudy else ({}, 0)
    rows = []
    for i, st in enumerate(stations):
        start, end, created = scan_times(by["CMSK"]["key"])
        m = mask[st]
        tp = top.get(st, np.full_like(m, np.nan))
        rows.append(dict(
            state="OK", station=st, scan_start=start, scan_end=end, product_created=created,
            cmsk_key=by["CMSK"]["key"], cmsk_etag=by["CMSK"]["etag"], cmsk_last_modified=by["CMSK"]["last_modified"],
            chgt_key=by["CHGT"]["key"], chgt_etag=by["CHGT"]["etag"], chgt_last_modified=by["CHGT"]["last_modified"],
            available_at=max(by["CMSK"]["last_modified"], by["CHGT"]["last_modified"]),
            mask=[None if not np.isfinite(v) or v < 0 else int(v) for v in m.ravel()],
            top_m=[None if not np.isfinite(v) or v < -100 or v > 30000 else round(float(v), 0) for v in tp.ravel()],
            top_read=st in top,
            bytes_fetched=(b1 + b2) if i == 0 else 0))
    return rows


_STATE: dict = {}


def _init(pixels: dict, deadline_wall: float) -> None:
    import rangefile
    rangefile.RATE_BYTES_PER_S = 100 * 1024  # per process; 6 processes share the uplink with production
    _STATE.update(pixels=pixels, deadline=deadline_wall)


def _job(t: datetime) -> int:
    if time.time() > _STATE["deadline"]:
        return 0
    try:
        rows = one_hour(t, _STATE["pixels"])
    except Exception as exc:  # noqa: BLE001 -- recorded, retried next run
        print(json.dumps(dict(hour=t.isoformat(), error=repr(exc)[:200])), flush=True)
        return 0
    target = OUT / f"{t:%Y-%m-%d}" / f"{t:%H}.jsonl.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt") as out:
        for r in rows:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.rename(target)
    return max(1, sum(r.get("bytes_fetched", 0) for r in rows))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-31")
    ap.add_argument("--end", default="2026-10-08")
    ap.add_argument("--max-minutes", type=float, default=9.0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    deadline = time.time() + 60 * args.max_minutes
    first = listing(BUCKET, "AHI-L2-FLDK-Clouds/2026/10/07/0300/")
    pixels = pixel_map(next(o for o in first if "AHI-CMSK_" in o["key"]))
    todo = []
    day = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    last = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    while day <= last:
        todo += [t for t in hours(day) if not (OUT / f"{t:%Y-%m-%d}" / f"{t:%H}.jsonl.gz").exists()]
        day += timedelta(days=1)
    # The P1 test window first (newest first), then the training history.
    todo.sort(key=lambda t: (t < datetime(2026, 9, 21, tzinfo=timezone.utc), -t.timestamp()))
    print(json.dumps(dict(pending_hours=len(todo))), flush=True)
    done = 0
    began = time.monotonic()

    # h5py serialises every HDF5 call behind one global lock, and the HTTP
    # range reads happen inside those calls -- threads fetched one at a time
    # (587 requests x 0.93 s = the whole 8.8 min, 2026-10-08).  Separate
    # processes each hold their own lock; the pool is joined before exit.
    import multiprocessing as mp
    with mp.get_context("spawn").Pool(args.workers, initializer=_init, initargs=(pixels, deadline)) as pool:
        fetched = 0
        for nbytes in pool.imap_unordered(_job, todo):
            done += nbytes > 0
            fetched += nbytes
    print(json.dumps(dict(hours_written=done, pending_left=len(todo) - done, mb=round(fetched / 1024 ** 2, 1),
                          minutes=round((time.monotonic() - began) / 60, 1))), flush=True)


if __name__ == "__main__":
    main()
