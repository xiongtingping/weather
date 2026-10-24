"""Measured surface shortwave for the Americas stations, from NOAA GOES-R.

User 2026-10-09「全部都用卫星实测日照」.  The board's hourly sunshine column
takes a geostationary measurement for every station; west of 60W no
republisher carries one (Open-Meteo's satellite archive has no GOES product,
and its default there is forecast-filled), so this reads the official one:
NOAA's ABI L2 Downward Shortwave Radiation (``ABI-L2-DSRF``, 2 km, every 10
minutes) from the public open-data buckets, no account.

Board only -- nothing here feeds a probability or an order.  Each file is
hundreds of MB; only the HDF5 chunk around the station is range-read.  A
local hour's figure is the scan nearest the middle of that hour (the forecast
column is the mean of the hour ending at the stamp), a 5x5-pixel (~10 km)
mean.  Reads run on one background thread and land in a cache keyed by
(station, UTC instant), so the board's request never waits on the network:
an hour not read yet is simply absent.

The same reader takes NOAA's ABI L2 Land Surface (skin) Temperature
(``ABI-L2-LST2KMF``, 2 km, hourly at HH:00, ~15 minutes after the scan) for
the station board's soil card (user 2026-10-24「先接地表皮温」): clear-sky
only, a 3x3-pixel (~6 km) mean of high/medium-quality pixels, in °C.
"""

from __future__ import annotations

import io
import math
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

BUCKET_EAST = "noaa-goes19"
BUCKET_WEST = "noaa-goes18"
PRODUCT = "ABI-L2-DSRF"
LABEL = "NOAA GOES-19/18 DSR"
LST_PRODUCT = "ABI-L2-LST2KMF"
LST_LABEL = "NOAA GOES-19/18 LST"
LST_HALF_BOX = 1
#: DQF 0 high, 1 medium quality; 2 low (cloud edges read cold) and 3 none are dropped.
LST_MAX_DQF = 1
BLOCK = 16 * 1024
MAX_FETCH_BYTES = 24 * 1024 * 1024
HALF_BOX = 2
REQUEST_TIMEOUT_SECONDS = 30.0
MISS_RETRY_SECONDS = 600.0
MAX_CACHE = 20000

_CACHE: dict[tuple[str, str, str], tuple[float, float | None]] = {}
_PENDING: set[tuple[str, str, str]] = set()
_QUEUE: "queue.Queue[tuple[str, str, float, float, datetime]]" = queue.Queue()
_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_LOCAL = threading.local()


def bucket_for(lon: float) -> str:
    """GOES-West for the Pacific coast, GOES-East for everything else."""

    return BUCKET_WEST if lon < -110.0 else BUCKET_EAST


def _client(route: str):
    import certifi  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    key = "client_" + route
    client = getattr(_LOCAL, key, None)
    if client is None:
        # Direct to the public bucket first (TLS ~0.5 s); the proxy route
        # when the far end resets a direct connection.
        client = httpx.Client(verify=certifi.where(), trust_env=route == "proxy",
                              timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": "weather-board"})
        setattr(_LOCAL, key, client)
    return client


def _get(url: str, headers: dict[str, str] | None = None):
    last: Exception | None = None
    for attempt, route in enumerate(("direct", "direct", "proxy", "proxy")):
        try:
            response = _client(route).get(url, headers=headers or {})
            if response.status_code in (200, 206):
                return response
            last = IOError(f"http_status_{response.status_code}")
        except Exception as exc:  # noqa: BLE001 -- retried on the other route
            last = exc
            client = getattr(_LOCAL, "client_" + route, None)
            if client is not None:
                client.close()
                setattr(_LOCAL, "client_" + route, None)
        time.sleep(1.0 * (attempt + 1))
    raise IOError(f"goes_fetch_failed:{last!r}"[:200])


class _RangeFile(io.RawIOBase):
    """A read-only file over HTTP Range requests, block-cached, byte-capped."""

    def __init__(self, url: str, size: int) -> None:
        self.url, self.size, self.pos, self.fetched = url, size, 0, 0
        self.blocks: dict[int, bytes] = {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def readinto(self, buffer) -> int:
        n = min(len(buffer), self.size - self.pos)
        if n <= 0:
            return 0
        first, last = self.pos // BLOCK, (self.pos + n - 1) // BLOCK
        missing = [i for i in range(first, last + 1) if i not in self.blocks]
        if missing:
            start, end = missing[0] * BLOCK, min(self.size, (missing[-1] + 1) * BLOCK) - 1
            body = _get(self.url, {"Range": f"bytes={start}-{end}"}).content
            if len(body) != end - start + 1:
                raise IOError("goes_range_short_read")
            self.fetched += len(body)
            if self.fetched > MAX_FETCH_BYTES:
                raise IOError("goes_range_cap_exceeded")
            for i in range(missing[0], missing[-1] + 1):
                self.blocks[i] = body[(i - missing[0]) * BLOCK:(i - missing[0] + 1) * BLOCK]
        data = b"".join(self.blocks[i] for i in range(first, last + 1))
        offset = self.pos - first * BLOCK
        buffer[:n] = data[offset:offset + n]
        self.pos += n
        return n


def _scan_near(bucket: str, instant: datetime, product: str = PRODUCT) -> tuple[str, int] | None:
    """The ``product`` object whose scan starts nearest ``instant`` (same UTC hour)."""

    prefix = f"{product}/{instant:%Y}/{instant.timetuple().tm_yday:03d}/{instant:%H}/"
    body = _get(f"https://{bucket}.s3.amazonaws.com/?list-type=2&prefix={prefix}&max-keys=20").text
    best: tuple[float, str, int] | None = None
    for key, size in zip(re.findall(r"<Key>([^<]+)</Key>", body), re.findall(r"<Size>(\d+)</Size>", body)):
        found = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", key)
        if not found:
            continue
        y, doy, hh, mm, ss = (int(x) for x in found.groups())
        start = datetime(y, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1, hours=hh, minutes=mm, seconds=ss)
        gap = abs((start - instant).total_seconds())
        if best is None or gap < best[0]:
            best = (gap, key, int(size))
    return None if best is None else (best[1], best[2])


def _fixed_grid(lat: float, lon: float, proj) -> tuple[float, float] | None:
    """GOES-R PUG lat/lon -> fixed-grid scan angles (rad); None if not visible."""

    req = float(proj.attrs["semi_major_axis"][0])
    rpol = float(proj.attrs["semi_minor_axis"][0])
    height = float(proj.attrs["perspective_point_height"][0]) + req
    lon0 = math.radians(float(proj.attrs["longitude_of_projection_origin"][0]))
    phi, lam = math.radians(lat), math.radians(lon)
    phic = math.atan(rpol ** 2 / req ** 2 * math.tan(phi))
    rc = rpol / math.sqrt(1 - (1 - rpol ** 2 / req ** 2) * math.cos(phic) ** 2)
    sx = height - rc * math.cos(phic) * math.cos(lam - lon0)
    sy = -rc * math.cos(phic) * math.sin(lam - lon0)
    sz = rc * math.sin(phic)
    if height * (height - sx) < sy ** 2 + req ** 2 / rpol ** 2 * sz ** 2:
        return None
    return math.asin(-sy / math.sqrt(sx * sx + sy * sy + sz * sz)), math.atan(sz / sx)


def _read_box(product: str, var: str, half: int, max_dqf: int | None,
              lat: float, lon: float, instant: datetime) -> float | None:
    """The mean of ``var`` over a (2*half+1)^2 pixel box around the station at
    the scan nearest ``instant``; None when no scan or no valid pixel."""

    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    bucket = bucket_for(lon)
    scan = _scan_near(bucket, instant, product)
    if scan is None:
        return None
    key, size = scan
    with h5py.File(_RangeFile(f"https://{bucket}.s3.amazonaws.com/{key}", size), "r") as handle:
        grid = _fixed_grid(lat, lon, handle["goes_imager_projection"])
        if grid is None:
            return None
        xs, ys, ds = handle["x"], handle["y"], handle[var]
        col = round((grid[0] - float(xs.attrs["add_offset"][0])) / float(xs.attrs["scale_factor"][0]))
        line = round((grid[1] - float(ys.attrs["add_offset"][0])) / float(ys.attrs["scale_factor"][0]))
        n_y, n_x = ds.shape
        if not (half <= col < n_x - half and half <= line < n_y - half):
            return None
        box = (slice(line - half, line + half + 1), slice(col - half, col + half + 1))
        raw = ds[box].astype(float)
        fill = np.asarray(ds.attrs.get("_FillValue")).ravel()
        if fill.size:
            raw[raw == fill[0]] = np.nan
        if max_dqf is not None:
            raw[handle["DQF"][box] > max_dqf] = np.nan
        values = raw * float(np.asarray(ds.attrs["scale_factor"]).ravel()[0]) + float(np.asarray(ds.attrs.get("add_offset", [0.0])).ravel()[0])
        good = values[np.isfinite(values)]
        return None if not good.size else float(good.mean())


def read_dsr(lat: float, lon: float, instant: datetime) -> float | None:
    """Surface downward shortwave (W/m2) around the station at the scan
    nearest ``instant``; None when no scan or no valid pixel."""

    value = _read_box(PRODUCT, "DSR", HALF_BOX, None, lat, lon, instant)
    return None if value is None else round(value, 1)


def read_lst(lat: float, lon: float, instant: datetime) -> float | None:
    """Land surface skin temperature (°C) around the station at the hourly
    scan nearest ``instant``; None when cloudy, no scan or no good pixel."""

    value = _read_box(LST_PRODUCT, "LST", LST_HALF_BOX, LST_MAX_DQF, lat, lon, instant)
    return None if value is None else round(value - 273.15, 1)


_READERS = {"dsr": read_dsr, "lst": read_lst}


def _work() -> None:
    while True:
        kind, station, lat, lon, instant = _QUEUE.get()
        key = (kind, station, instant.isoformat())
        try:
            value = _READERS[kind](lat, lon, instant)
        except Exception:  # noqa: BLE001 -- asked again after MISS_RETRY_SECONDS
            value = None
        with _LOCK:
            if len(_CACHE) > MAX_CACHE:
                _CACHE.clear()
            _CACHE[key] = (time.monotonic(), value)
            _PENDING.discard(key)


def measured(station: str, lat: float, lon: float, instants: list[datetime],
             kind: str = "dsr") -> dict[str, float]:
    """instant (ISO, UTC) -> W/m2 (``kind="lst"``: skin °C) for the instants already read; the others
    are queued for the background reader (an empty answer is re-asked after
    a while, so a scan published late still arrives)."""

    global _WORKER
    out: dict[str, float] = {}
    now = time.monotonic()
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_work, name="goes-dsr-reader", daemon=True)
            _WORKER.start()
        for instant in instants:
            key = (kind, station, instant.isoformat())
            hit = _CACHE.get(key)
            if hit is not None and hit[1] is not None:
                out[key[2]] = hit[1]
                continue
            if key in _PENDING or (hit is not None and now - hit[0] < MISS_RETRY_SECONDS):
                continue
            _PENDING.add(key)
            _QUEUE.put((kind, station, lat, lon, instant))
    return out
