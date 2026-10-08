"""Satellite low cloud and surface sunshine at the Americas stations (GOES).

熊先生 2026-10-08「补数据」: the observed-minus-forecast cloud/sun study needs
an observed sky that is better than the METAR's categories and that exists
for the 14 Americas stations the Open-Meteo satellite republisher does not
cover.  NOAA's open-data buckets carry the GOES-R L2 products publicly, no
account:

* ABI-L2-CCLF  Cloud Cover Layers, hourly, 10 km.  CF1 is the cloud fraction
  between the surface and 5000 ft above sea level -- the satellite's own low
  cloud, independent of the airport's eye.  TCF is total cloud fraction.
* ABI-L2-DSRF  Downward Shortwave Radiation at the surface, 10-minutely, 2 km
  (the first scan of each hour is taken).

Each value carries its object's S3 ``LastModified`` -- the instant it became
publicly readable -- so a later strict as-of consumer can admit it only
after that instant.  The scan time is the physical time.  Nothing here
writes the SSOT; reads are byte ranges, capped per object.

Usage:  .venv-sat/bin/python goes_extract.py [--start 2026-08-31] [--end 2026-10-08]
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
import h5py
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from rangefile import RangeFile, fetch  # noqa: E402

CTX = ssl.create_default_context(cafile=certifi.where())
OUT = HERE / "goes"
STATIONS = {
    "CYYZ": (43.679, -79.629), "KATL": (33.62972, -84.44223), "KAUS": (30.1831, -97.68063),
    "KBKF": (39.713, -104.758), "KDAL": (32.83836, -96.83584), "KHOU": (29.64582, -95.28214),
    "KLAX": (33.93817, -118.3866), "KLGA": (40.77945, -73.88027), "KMIA": (25.78806, -80.31692),
    "KORD": (41.96017, -87.93161), "KSEA": (47.44467, -122.31442), "KSFO": (37.61961, -122.36561),
    "MMMX": (19.436, -99.072), "MPMG": (8.967, -79.555), "SAEZ": (-34.822, -58.536),
    "SBGR": (-23.432, -46.469),
}
# GOES-West for the Pacific coast; GOES-East for everything else (smaller view angle).
SATELLITE = {s: ("noaa-goes18" if lon < -110 else "noaa-goes19") for s, (_, lon) in STATIONS.items()}
PRODUCTS = {"ABI-L2-CCLF": ("CF1", "TCF", "DQF"), "ABI-L2-DSRF": ("DSR", "DQF")}
BOX = {"ABI-L2-CCLF": 1, "ABI-L2-DSRF": 2}  # half-width in pixels: 3x3 of 10 km, 5x5 of 2 km


def listing(bucket: str, prefix: str) -> list[dict]:
    url = f"https://{bucket}.s3.amazonaws.com/?list-type=2&prefix={prefix}&max-keys=50"
    body = fetch(url, timeout=60).text[:2 * 1024 ** 2]
    keys = re.findall(r"<Key>([^<]+)</Key>", body)
    sizes = re.findall(r"<Size>([^<]+)</Size>", body)
    mods = re.findall(r"<LastModified>([^<]+)</LastModified>", body)
    etags = re.findall(r"<ETag>([^<]+)</ETag>", body)
    return [dict(key=k, size=int(s), last_modified=m, etag=e.replace("&quot;", "").strip('"'))
            for k, s, m, e in zip(keys, sizes, mods, etags)]


def scan_times(key: str) -> tuple[str, str, str]:
    def t(tag):
        v = re.search(tag + r"(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)", key).groups()
        d = datetime(int(v[0]), 1, 1, tzinfo=timezone.utc) + timedelta(days=int(v[1]) - 1, hours=int(v[2]), minutes=int(v[3]), seconds=int(v[4]), milliseconds=100 * int(v[5]))
        return d.isoformat().replace("+00:00", "Z")
    return t("_s"), t("_e"), t("_c")


def fixed_grid(lat: float, lon: float, proj) -> tuple[float, float] | None:
    """GOES-R PUG lat/lon -> fixed-grid scan angles (rad); None if not visible."""
    req, rpol = float(proj.attrs["semi_major_axis"][0]), float(proj.attrs["semi_minor_axis"][0])
    h = float(proj.attrs["perspective_point_height"][0]) + req
    lon0 = math.radians(float(proj.attrs["longitude_of_projection_origin"][0]))
    phi, lam = math.radians(lat), math.radians(lon)
    e2 = 1 - rpol ** 2 / req ** 2
    phic = math.atan(rpol ** 2 / req ** 2 * math.tan(phi))
    rc = rpol / math.sqrt(1 - e2 * math.cos(phic) ** 2)
    sx = h - rc * math.cos(phic) * math.cos(lam - lon0)
    sy = -rc * math.cos(phic) * math.sin(lam - lon0)
    sz = rc * math.sin(phic)
    if h * (h - sx) < sy ** 2 + req ** 2 / rpol ** 2 * sz ** 2:
        return None
    return math.asin(-sy / math.sqrt(sx * sx + sy * sy + sz * sz)), math.atan(sz / sx)


def decode(ds, raw: np.ndarray) -> np.ndarray:
    fill = ds.attrs.get("_FillValue")
    out = raw.astype(float)
    if fill is not None:
        out[raw == np.asarray(fill).ravel()[0]] = np.nan
    scale, offset = ds.attrs.get("scale_factor"), ds.attrs.get("add_offset")
    if scale is not None:
        out = out * float(np.asarray(scale).ravel()[0]) + float(np.asarray(offset).ravel()[0] if offset is not None else 0.0)
    return out


def extract(bucket: str, product: str, obj: dict, stations: list[str]) -> list[dict]:
    url = f"https://{bucket}.s3.amazonaws.com/{obj['key']}"
    rf = RangeFile(url, obj["size"], cap_bytes=24 * 1024 ** 2)
    rows = []
    with h5py.File(rf, "r") as h:
        proj = h["goes_imager_projection"]
        xs, ys = h["x"], h["y"]
        xsc, xoff = float(xs.attrs["scale_factor"][0]), float(xs.attrs["add_offset"][0])
        ysc, yoff = float(ys.attrs["scale_factor"][0]), float(ys.attrs["add_offset"][0])
        n_y, n_x = h[PRODUCTS[product][0]].shape
        start, end, created = scan_times(obj["key"])
        k = BOX[product]
        for st in stations:
            lat, lon = STATIONS[st]
            grid = fixed_grid(lat, lon, proj)
            row = dict(station=st, product=product, bucket=bucket, key=obj["key"], etag=obj["etag"],
                       s3_last_modified=obj["last_modified"], scan_start=start, scan_end=end, product_created=created)
            if grid is None:
                rows.append(dict(row, state="NOT_VISIBLE"))
                continue
            col, line = round((grid[0] - xoff) / xsc), round((grid[1] - yoff) / ysc)
            if not (k <= col < n_x - k and k <= line < n_y - k):
                rows.append(dict(row, state="OUTSIDE_IMAGE"))
                continue
            row.update(state="OK", line=line, col=col)
            for var in PRODUCTS[product]:
                ds = h[var]
                box = decode(ds, ds[line - k:line + k + 1, col - k:col + k + 1])
                row[var] = None if np.isnan(box[k, k]) else round(float(box[k, k]), 5)
                good = box[~np.isnan(box)]
                row[var + "_box_mean"] = None if not good.size else round(float(good.mean()), 5)
                row[var + "_box_n"] = int(good.size)
            rows.append(row)
    return [dict(r, bytes_fetched=rf.fetched) for r in rows]


def daylight_hours(day: datetime, stations: list[str]) -> list[datetime]:
    hours = []
    for h in range(24):
        t = day + timedelta(hours=h)
        # Approximate local solar hour; keep 07..18 for any station of this satellite.
        if any(7 <= (h + STATIONS[s][1] / 15.0) % 24 <= 18 for s in stations):
            hours.append(t)
    return hours


def run_day(bucket: str, stations: list[str], day: datetime, pool, products: list[str]) -> dict:
    target = OUT / bucket / "+".join(products) / f"{day:%Y-%m-%d}.jsonl.gz"
    if target.exists():
        return dict(day=f"{day:%Y-%m-%d}", bucket=bucket, state="REUSED")
    jobs, missing = [], []
    for t in daylight_hours(day, stations):
        for product in products:
            objs = listing(bucket, f"{product}/{t:%Y}/{t.timetuple().tm_yday:03d}/{t:%H}/")
            if not objs:
                missing.append(f"{product}/{t:%Y-%m-%dT%H}")
                continue
            jobs.append((product, objs[0]))  # first scan of the hour
    results, errors = [], []
    futures = {pool.submit(extract, bucket, p, o, stations): (p, o["key"]) for p, o in jobs}
    for f, (p, key) in futures.items():
        try:
            results.extend(f.result())
        except Exception as exc:  # noqa: BLE001
            errors.append(dict(product=p, key=key, error=repr(exc)[:300]))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp.gz")
    with gzip.open(tmp, "wt") as out:
        for r in sorted(results, key=lambda r: (r["station"], r["product"], r["scan_start"])):
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
        for e in errors:
            out.write(json.dumps(dict(state="EXTRACT_ERROR", **e)) + "\n")
        for m in missing:
            out.write(json.dumps(dict(state="OBJECT_MISSING", prefix=m)) + "\n")
    tmp.rename(target)
    return dict(day=f"{day:%Y-%m-%d}", bucket=bucket, objects=len(jobs), rows=len(results), errors=len(errors), missing=len(missing),
                fetched_mb=round(sum({r['key']: r['bytes_fetched'] for r in results}.values()) / 1024 ** 2, 1))


def _init_rate() -> None:
    import rangefile
    rangefile.RATE_BYTES_PER_S = 80 * 1024  # per process; shared uplink with production


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-31")
    ap.add_argument("--end", default="2026-10-08")
    ap.add_argument("--max-days", type=int, default=0, help="stop after this many new days (bounded runs)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--products", nargs="*", default=["ABI-L2-CCLF"], choices=sorted(PRODUCTS))
    ap.add_argument("--max-minutes", type=float, default=9.0)
    args = ap.parse_args()
    deadline = time.monotonic() + 60 * args.max_minutes
    by_bucket: dict[str, list[str]] = {}
    for s, b in SATELLITE.items():
        by_bucket.setdefault(b, []).append(s)
    first, last, done = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc), datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc), 0
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    # The P1 test window first (newest first), then the training history.
    days.sort(key=lambda d: (d < datetime(2026, 9, 21, tzinfo=timezone.utc), -d.timestamp()))
    # Processes, not threads: h5py serialises every call (and so every range
    # read) behind one global lock.
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(args.workers, mp_context=mp.get_context("spawn"), initializer=_init_rate) as pool:
        for day in days:
            for bucket, stations in sorted(by_bucket.items()):
                if time.monotonic() > deadline:
                    return
                began = time.monotonic()
                receipt = run_day(bucket, stations, day, pool, args.products)
                if receipt.get("state") != "REUSED":
                    done += 1
                    print(json.dumps(dict(receipt, seconds=round(time.monotonic() - began, 1))), flush=True)
            if args.max_days and done >= args.max_days:
                break


if __name__ == "__main__":
    main()
