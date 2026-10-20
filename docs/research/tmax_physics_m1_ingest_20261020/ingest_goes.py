"""GOES ABI L2 DSR history for the 16 Americas stations, on the board's own
basis (``bayes_basket_target.goes_surface_radiation``): local hour h reads
the scan nearest h-0:30, a 5x5-pixel mean, HDF5 chunks range-read from the
public NOAA buckets.  User 2026-10-20 「抽 GOES」, then 「GOES 在后台分批抽齐后
再替换，判定只用 GOES 版」 -- run in budgeted foreground batches (standing
rule: no background processes), resumable via ``fetch_log``.

Satellites by date: GOES-East = GOES-16 up to 2025-04-07 (its DSRF archive
ends on day 097), GOES-19 after; GOES-West = GOES-18 (the board's split:
west of 110W).  One unit = one bucket x one UTC scan instant; the file is
opened once for every station that hour belongs to.  Rows land in
``sw_measured_hourly`` keyed by the board's stamp (local h:00 in UTC),
``source='GOES_DSRF'``, ``proxy=0``.

    .venv/bin/python docs/research/tmax_physics_m1_ingest_20261020/ingest_goes.py --budget 540
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store  # noqa: E402

from bayes_basket_target import goes_surface_radiation as goes  # noqa: E402
from bayes_basket_target.station_day_board import _clear_sky_ghi  # noqa: E402
from bayes_basket_target.station_registry import _COORDINATES  # noqa: E402

TASK = "goes_dsrf"
EAST_SWITCH = datetime(2025, 4, 7, tzinfo=timezone.utc)
FIRST_LOCAL_HOUR, LAST_LOCAL_HOUR = 6, 17   # the hours the model reads (05..H, H<=17; 05 is ~dark); 18-20 cut 10-20 for the tiled files' slow metadata
goes.MAX_FETCH_BYTES = 96 * 1024 * 1024   # one file serves up to 13 stations here
goes.BLOCK = 64 * 1024                    # one DSR chunk row-band is ~tens of KB compressed; 512 KB blocks inflated the transfer ~10x


def americas() -> list[str]:
    """The stations the board reads from GOES: no Open-Meteo satellite product
    west of 60W (station_day_board._satellite_product); SBGR and SAEZ, east of
    it, read EUMETSAT MSG there and are not GOES stations."""

    return sorted(s for s, (lat, lon) in _COORDINATES.items() if -150.0 < lon < -60.0)


def buckets_for(lon: float, instant: datetime) -> list[str]:
    if lon < -110.0:
        return ["noaa-goes18"]
    if instant < EAST_SWITCH:
        return ["noaa-goes16"]
    if instant < EAST_SWITCH + timedelta(days=1):
        return ["noaa-goes19", "noaa-goes16"]
    return ["noaa-goes19"]


def units(tz: dict[str, str], start: date, end: date) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """(bucket key, scan instant ISO) -> [(station, stamp ISO)]."""

    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for st in americas():
        lat, lon = _COORDINATES[st]
        zone = ZoneInfo(tz[st])
        d = start
        while d <= end:
            for h in range(FIRST_LOCAL_HOUR, LAST_LOCAL_HOUR + 1):
                stamp = datetime(d.year, d.month, d.day, h, tzinfo=zone).astimezone(timezone.utc)
                middle = stamp - timedelta(minutes=30)
                if _clear_sky_ghi(lat, lon, middle) <= 0.0:
                    continue
                key = ("west" if lon < -110.0 else "east", middle.strftime("%Y-%m-%dT%H:%M:%SZ"))
                out.setdefault(key, []).append((st, stamp.strftime("%Y-%m-%dT%H:%M:%SZ")))
            d += timedelta(days=1)
    return out


def _box_from_chunks(url: str, chunks: dict[tuple[int, int], tuple[int, int]], shape: tuple[int, int],
                     line: int, col: int, hb: int, fill, scale: float, offset: float):
    """The station's 5x5 box decoded outside h5py.  DSR is stored shuffle +
    deflate in chunks of ``shape``: a 24-row band of the full width in the
    current files, 226 x 226 tiles in older ones (seen 2026-10-20); a box may
    straddle up to 2 x 2 chunks."""

    import zlib  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    cr, cc = shape
    box = np.empty((2 * hb + 1, 2 * hb + 1), dtype=float)
    for i, r in enumerate(range(line - hb, line + hb + 1)):
        for j, c in enumerate(range(col - hb, col + hb + 1)):
            r0, c0 = r - r % cr, c - c % cc
            if (url, r0, c0) not in _CHUNK_CACHE:
                off, size, mask = chunks[(r0, c0)]
                if off is None:   # never written: the whole chunk is fill
                    _CHUNK_CACHE[(url, r0, c0)] = np.full((cr, cc), fill[0] if fill.size else 0, dtype="<u2")
                    box[i, j] = _CHUNK_CACHE[(url, r0, c0)][r - r0, c - c0]
                    continue
                body = goes._get(url, {"Range": f"bytes={off}-{off + size - 1}"}).content
                if len(body) != size:
                    raise IOError("goes_chunk_short_read")
                # Filter pipeline [shuffle, deflate]; a set mask bit means
                # that filter was skipped for this chunk.
                data = body if mask & 2 else zlib.decompress(body)
                raw = np.frombuffer(data, dtype=np.uint8)
                if mask & 1:
                    plain = raw
                else:
                    n = raw.size // 2
                    plain = np.empty(raw.size, dtype=np.uint8)
                    plain[0::2], plain[1::2] = raw[:n], raw[n:]
                _CHUNK_CACHE[(url, r0, c0)] = plain.view("<u2").reshape(cr, cc)
            box[i, j] = _CHUNK_CACHE[(url, r0, c0)][r - r0, c - c0]
    if fill.size:
        box[box == fill[0]] = np.nan
    good = (box * scale + offset)[np.isfinite(box)]
    return None if not good.size else round(float(good.mean()), 1)


_CHUNK_CACHE: dict = {}
LATLON_SOURCE = "GOES_DSRF_V01_0P5DEG"


def _read_latlon(url: str, members: list[tuple[str, str]], scan_at: str) -> list[tuple]:
    import io  # noqa: PLC0415

    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    body = goes._get(url).content
    rows = []
    with h5py.File(io.BytesIO(body), "r") as handle:
        if "goes_lat_lon_projection" not in handle:
            raise IOError("goes_small_file_not_latlon")
        unpack = lambda v: v[:].astype(float) * float(np.ravel(v.attrs["scale_factor"])[0]) + float(np.ravel(v.attrs["add_offset"])[0])  # noqa: E731
        lats, lons, ds = unpack(handle["lat"]), unpack(handle["lon"]), handle["DSR"]
        fill = np.asarray(ds.attrs.get("_FillValue")).ravel()
        scale = float(np.asarray(ds.attrs["scale_factor"]).ravel()[0])
        offset = float(np.asarray(ds.attrs.get("add_offset", [0.0])).ravel()[0])
        grid = ds[:]
        for st, stamp in members:
            lat, lon = _COORDINATES[st]
            i, j = int(np.abs(lats - lat).argmin()), int(np.abs(lons - lon).argmin())
            raw = grid[i, j]
            value = None if (fill.size and raw == fill[0]) else round(float(raw) * scale + offset, 1)
            if abs(float(lats[i]) - lat) > 0.5 or abs(float(lons[j]) - lon) > 0.5:
                value = None
            rows.append((st, stamp, value, LATLON_SOURCE, 0, scan_at))
    return rows


def _box_by_h5py(url: str, size: int, line: int, col: int, hb: int):
    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    with h5py.File(goes._RangeFile(url, size), "r") as handle:
        ds = handle["DSR"]
        raw = ds[line - hb:line + hb + 1, col - hb:col + hb + 1].astype(float)
        fill = np.asarray(ds.attrs.get("_FillValue")).ravel()
        if fill.size:
            raw[raw == fill[0]] = np.nan
        values = raw * float(np.asarray(ds.attrs["scale_factor"]).ravel()[0]) + float(np.asarray(ds.attrs.get("add_offset", [0.0])).ravel()[0])
        good = values[np.isfinite(values)]
        return None if not good.size else round(float(good.mean()), 1)


def read_unit(key: tuple[str, str], members: list[tuple[str, str]]) -> list[tuple]:
    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    instant = datetime.fromisoformat(key[1].replace("Z", "+00:00"))
    lon0 = _COORDINATES[members[0][0]][1]
    scan = bucket = None
    for bucket in buckets_for(lon0, instant):
        scan = goes._scan_near(bucket, instant)
        if scan is not None:
            break
    if scan is None:
        return [(st, stamp, None, "GOES_DSRF", 0, None) for st, stamp in members]
    name, size = scan
    found = __import__("re").search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})", name)
    y, doy, hh, mm = (int(x) for x in found.groups())
    scan_at = (datetime(y, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1, hours=hh, minutes=mm)).strftime("%Y-%m-%dT%H:%MZ")
    url = f"https://{bucket}.s3.amazonaws.com/{name}"
    if size < 2 * 1024 * 1024:
        # GOES-16 DSRF v01r00 (to mid-April 2024): a 0.5-degree lat/lon grid,
        # ~230 KB a file, no fixed-grid projection.  Its nearest cell is
        # read and kept under its own source name: a different resolution
        # from the 2 km product the board reads.
        return _read_latlon(url, members, scan_at)
    rf = goes._RangeFile(url, size)
    # The superblock and root group sit in the first two blocks: fetched here,
    # outside h5py's global lock, so threads do not queue on the network.
    head = goes._get(url, {"Range": f"bytes=0-{2 * goes.BLOCK - 1}"}).content
    for i in range(2):
        rf.blocks[i] = head[i * goes.BLOCK:(i + 1) * goes.BLOCK]
    hb = goes.HALF_BOX
    plan = []
    with h5py.File(rf, "r") as handle:
        proj, xs, ys, ds = handle["goes_imager_projection"], handle["x"], handle["y"], handle["DSR"]
        sx, ox = float(xs.attrs["scale_factor"][0]), float(xs.attrs["add_offset"][0])
        sy, oy = float(ys.attrs["scale_factor"][0]), float(ys.attrs["add_offset"][0])
        fill = np.asarray(ds.attrs.get("_FillValue")).ravel()
        scale = float(np.asarray(ds.attrs["scale_factor"]).ravel()[0])
        offset = float(np.asarray(ds.attrs.get("add_offset", [0.0])).ravel()[0])
        n_y, n_x = ds.shape
        chunk_shape = ds.chunks
        pl = ds.id.get_create_plist()
        filters = [pl.get_filter(i)[0] for i in range(pl.get_nfilters())]
        if chunk_shape is None or filters != [2, 1] or ds.dtype.str != "<u2":
            raise IOError(f"goes_layout_unexpected:{ds.chunks}:{filters}:{ds.dtype.str}")
        for st, stamp in members:
            lat, lon = _COORDINATES[st]
            grid = goes._fixed_grid(lat, lon, proj)
            if grid is None:
                plan.append((st, stamp, None))
                continue
            col, line = round((grid[0] - ox) / sx), round((grid[1] - oy) / sy)
            if not (hb <= col < n_x - hb and hb <= line < n_y - hb):
                plan.append((st, stamp, None))
                continue
            chunks = {}
            for r in range(line - hb, line + hb + 1):
                for c in range(col - hb, col + hb + 1):
                    origin = (r - r % chunk_shape[0], c - c % chunk_shape[1])
                    if origin not in chunks:
                        info = ds.id.get_chunk_info_by_coord(origin)
                        chunks[origin] = (info.byte_offset, info.size, info.filter_mask)
            plan.append((st, stamp, (chunks, line, col)))
    rows = []
    try:
        for st, stamp, where in plan:
            value = None
            if where is not None:
                chunks, line, col = where
                try:
                    value = _box_from_chunks(url, chunks, chunk_shape, line, col, hb, fill, scale, offset)
                except (OSError, ValueError, __import__("zlib").error):
                    try:   # rare odd chunk: the board's own path
                        value = _box_by_h5py(url, size, line, col, hb)
                    except OSError:   # the file's chunk is corrupt for h5py too: missing, as on the board
                        value = None
            rows.append((st, stamp, value, "GOES_DSRF", 0, scan_at))
    finally:
        for k in [k for k in list(_CHUNK_CACHE) if k[0] == url]:
            _CHUNK_CACHE.pop(k, None)
    return rows


MAX_PROCESSES = 4   # user 2026-10-20: four foreground worker processes, this script only, gone when it ends


def run_batch(batch: list[tuple[tuple[str, str], list[tuple[str, str]]]], threads: int) -> list[tuple]:
    """One worker process: its own h5py (and so its own global lock), a few
    threads inside it.  Returns (key, rows | None, error | None) per unit."""

    def one(item):
        key, members = item
        try:
            return key, read_unit(key, members), None
        except Exception as exc:  # noqa: BLE001 -- retried next batch
            return key, None, f"{type(exc).__name__}: {str(exc)[:200]}"

    with ThreadPoolExecutor(threads) as pool:
        return list(pool.map(one, batch))


def main() -> int:
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait  # noqa: PLC0415

    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=480.0)
    ap.add_argument("--processes", type=int, default=MAX_PROCESSES)
    ap.add_argument("--threads", type=int, default=4, help="threads inside each process")
    ap.add_argument("--batch", type=int, default=12, help="units handed to a process at a time")
    args = ap.parse_args()
    processes = max(1, min(MAX_PROCESSES, args.processes))
    t0 = time.time()
    tz = store.timezones()
    conn = store.connect()
    have = store.done(conn, TASK)
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    plan = units(tz, date.fromisoformat(store.START), end)
    todo = sorted((k for k in plan if f"{k[0]}|{k[1]}" not in have), key=lambda k: k[1], reverse=True)
    batches = [[(k, plan[k]) for k in todo[i:i + args.batch]] for i in range(0, len(todo), args.batch)]
    n_ok = n_err = 0
    with ProcessPoolExecutor(processes) as pool:
        it = iter(batches)
        running = set()
        for _ in range(processes * 2):
            nxt = next(it, None)
            if nxt is not None:
                running.add(pool.submit(run_batch, nxt, args.threads))
        while running:
            finished, running = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished:
                for key, rows, err in fut.result():
                    if rows is not None:
                        conn.executemany("INSERT OR REPLACE INTO sw_measured_hourly VALUES (?,?,?,?,?,?)", rows)
                        store.mark(conn, TASK, f"{key[0]}|{key[1]}", "ok", f"{sum(r[2] is not None for r in rows)}/{len(rows)}")
                        n_ok += 1
                    else:
                        store.mark(conn, TASK, f"{key[0]}|{key[1]}", "error", err or "")
                        n_err += 1
                conn.commit()
                if time.time() - t0 < args.budget and store.disk_ok():
                    nxt = next(it, None)
                    if nxt is not None:
                        running.add(pool.submit(run_batch, nxt, args.threads))
    left = len(todo) - n_ok
    print(f"goes: {n_ok} units ok, {n_err} errors, {left} of {len(plan)} left, {time.time() - t0:.0f}s, {processes} processes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
