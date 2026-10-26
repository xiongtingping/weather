"""Satellite rainfall from JAXA GSMaP_NOW, for the station board (user
2026-10-15 「云雨数据呢」 → 「可以」 over plain FTP).

GSMaP_NOW is JAXA's real-time global rainfall: the hourly rain rate (mm/h)
on a 0.1-degree grid (rain filled 60N..60S), in one netCDF per half hour
(``gsmap_now_rain.YYYYMMDD.HHNN.nc`` = the hour HH:NN..+59 min, UTC), public
about a minute after that hour ends; ``hourlyPrecipRateGC`` is the same rate
calibrated against rain gauges.  Every fleet station between 60N and 60S
falls on it -- the Americas and Europe included.

Access: ``hokusai.eorc.jaxa.jp``, the shared GSMaP account the registration
mail gives (``~/.netrc`` entry for that host, never in this tree), plain FTP
only -- the server offers no TLS and the SFTP door refuses this account; the
password is the one JAXA mails every registered user (ruling 「可以」).

One background thread reads a half-hour file once (4-5 MB, size-capped,
deleted after) for every covered fleet station: the station's cell and the
3 x 3 cells around it (~30 km); readings are kept in a small runtime file.
The board never waits on the network.  Board only -- nothing here feeds a
probability or an order.
"""

from __future__ import annotations

import ftplib
import json
import netrc
import os
import queue
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HOST = "hokusai.eorc.jaxa.jp"
LABEL = "JAXA GSMaP_NOW（官方）"
#: The file's grid is global, south row first (89.95S..89.95N) and
#: 179.95W..179.95E; the rain itself is filled only 60N..60S.
BOTTOM_LAT, LEFT_LON, STEP, ROWS, COLS = -89.95, -179.95, 0.1, 1800, 3600
DATA_LAT_LIMIT = 60.0
PUBLISH_AFTER = timedelta(minutes=62)
MAX_FILE_BYTES = 16 * 1024 * 1024
MIN_FREE_BYTES = 2 * 1024 ** 3
STALE_DOWNLOAD_SECONDS = 3600
FTP_TIMEOUT_SECONDS = 60
MISS_RETRY_SECONDS = 600.0
KEEP_SLOTS = 200
CACHE_PATH = ROOT / "data" / "runtime" / "jaxa_gsmap_v1.json"
CACHE_SCHEMA = "jaxa_gsmap_v1"

_LOCK = threading.Lock()
_QUEUE: "queue.Queue[datetime]" = queue.Queue()
_PENDING: set[str] = set()
_MISSES: dict[str, float] = {}
_CACHE: dict[str, dict[str, dict[str, float | None]]] | None = None
_WORKER: threading.Thread | None = None
_STATUS: dict[str, Any] = {}


def cell(lat: float, lon: float) -> tuple[int, int] | None:
    if abs(lat) > DATA_LAT_LIMIT:
        return None
    wrapped = (lon + 180.0) % 360.0 - 180.0
    return round((lat - BOTTOM_LAT) / STEP), round((wrapped - LEFT_LON) / STEP) % COLS


def slot_key(start: datetime) -> str:
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _account() -> tuple[str, str]:
    auth = netrc.netrc(os.path.expanduser("~/.netrc")).authenticators(HOST)
    if not auth or not auth[0] or not auth[2]:
        raise RuntimeError(f"no ~/.netrc entry for {HOST}")
    return auth[0], auth[2]


def _remote(start: datetime) -> tuple[str, str]:
    s = start.astimezone(timezone.utc)
    name = f"gsmap_now_rain.{s:%Y%m%d}.{s:%H%M}.nc"
    return f"/now/netcdf/{s:%Y}/{s:%m}/{s:%d}/{name}", f"/now/latest/{name}"


def _fleet() -> dict[str, tuple[float, float]]:
    from .station_registry import _COORDINATES  # noqa: PLC0415

    return {str(s).upper(): (float(lat), float(lon)) for s, (lat, lon) in _COORDINATES.items()
            if cell(float(lat), float(lon)) is not None}


def read_slot(start: datetime, stations: dict[str, tuple[float, float]]) -> dict[str, dict[str, float | None]] | None:
    """One half-hour file's rain at every station (cell and 3 x 3 mean, raw
    and gauge-calibrated); None when the file is not on the server (yet)."""

    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    workdir = CACHE_PATH.parent / "jaxa_gsmap_tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(workdir).free < MIN_FREE_BYTES:
        raise RuntimeError("free disk below the floor")
    user, password = _account()
    # A reader killed mid-read never reaches its finally; nothing still
    # running holds a file this old.
    cutoff = time.time() - STALE_DOWNLOAD_SECONDS
    for stale in workdir.glob("tmp*.nc"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except FileNotFoundError:
            pass
    handle, path = tempfile.mkstemp(suffix=".nc", dir=workdir)
    os.close(handle)
    try:
        ftp = ftplib.FTP(HOST, timeout=FTP_TIMEOUT_SECONDS)
        try:
            ftp.login(user, password)
            got = False
            for remote in _remote(start):
                written = [0]
                try:
                    with open(path, "wb") as fh:
                        def sink(block: bytes) -> None:
                            written[0] += len(block)
                            if written[0] > MAX_FILE_BYTES:
                                raise RuntimeError("file over the size cap")
                            fh.write(block)
                        ftp.retrbinary(f"RETR {remote}", sink)
                    got = True
                    break
                except ftplib.error_perm:
                    continue
        finally:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                pass
        if not got:
            return None
        with h5py.File(path, "r") as f:
            raw, gauge = f["hourlyPrecipRate"], f["hourlyPrecipRateGC"]
            if raw.shape[1:] != (ROWS, COLS):
                raise RuntimeError(f"grid {raw.shape}")
            missing = float(np.ravel(raw.attrs.get("missing_value", [-9999.9]))[0])
            lat_axis, lon_axis = f["Latitude"], f["Longitude"]
            out: dict[str, dict[str, float | None]] = {}
            for station, (lat, lon) in stations.items():
                rc = cell(lat, lon)
                if rc is None:
                    continue
                r, c = rc
                if abs(float(lat_axis[r]) - lat) > STEP or abs(((float(lon_axis[c]) - lon) + 180.0) % 360.0 - 180.0) > STEP:
                    raise RuntimeError(f"grid axis disagrees at {station}")
                cols = [(c + d) % COLS for d in (-1, 0, 1)]
                reading: dict[str, float | None] = {}
                for name, data in (("mm", raw), ("mm_gc", gauge)):
                    box = np.array([[float(data[0, rr, cc]) for cc in cols] for rr in (r - 1, r, r + 1)])
                    box[box <= missing + 1.0] = np.nan
                    centre = box[1, 1]
                    reading[name] = None if np.isnan(centre) else round(max(0.0, float(centre)), 2)
                    reading[name + "_area"] = None if np.isnan(box).all() else round(max(0.0, float(np.nanmean(box))), 2)
                out[station] = reading
        return out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _load() -> dict[str, dict[str, dict[str, float | None]]]:
    global _CACHE
    if _CACHE is None:
        try:
            doc = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            _CACHE = dict(doc.get("slots") or {}) if doc.get("schema") == CACHE_SCHEMA else {}
        except (OSError, ValueError):
            _CACHE = {}
    return _CACHE


def _persist(cache: dict[str, Any]) -> None:
    for stale in sorted(cache)[:-KEEP_SLOTS]:
        cache.pop(stale, None)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"schema": CACHE_SCHEMA, "slots": cache}, sort_keys=True), encoding="utf-8")
    os.replace(temporary, CACHE_PATH)


def _work() -> None:
    while True:
        start = _QUEUE.get()
        key = slot_key(start)
        try:
            values, error = read_slot(start, _fleet()), None
        except Exception as exc:  # noqa: BLE001 -- asked again after MISS_RETRY_SECONDS
            values, error = None, f"{type(exc).__name__}: {str(exc)[:120]}"
        with _LOCK:
            _PENDING.discard(key)
            if values is None:
                _MISSES[key] = time.monotonic()
                if error:
                    _STATUS.update(last_error=error)
                continue
            cache = _load()
            cache[key] = values
            _persist(cache)
            _STATUS.update(last_read_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def rain(station: str, starts: list[datetime]) -> dict[str, dict[str, float | None] | None]:
    """slot start -> the station's reading for slots already read; the others
    are queued, newest first.  Never waits on the network."""

    global _WORKER
    out: dict[str, dict[str, float | None] | None] = {}
    now = time.monotonic()
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_work, name="jaxa-gsmap", daemon=True)
            _WORKER.start()
        cache = _load()
        for start in sorted(starts, reverse=True):
            key = slot_key(start)
            if key in cache:
                out[key] = cache[key].get(station.upper())
                continue
            if key in _PENDING or now - _MISSES.get(key, -1e9) < MISS_RETRY_SECONDS:
                continue
            _PENDING.add(key)
            _QUEUE.put(start.astimezone(timezone.utc))
    return out


def status() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATUS, pending=len(_PENDING))


__all__ = ["LABEL", "PUBLISH_AFTER", "cell", "rain", "read_slot", "slot_key", "status"]
