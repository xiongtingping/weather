"""Measured surface shortwave from JAXA's own Himawari product, through the
P-Tree FTP service (user 2026-10-15 「接入：http://hokusai.eorc.jaxa.jp/」 --
hokusai.eorc.jaxa.jp and ftp.ptree.jaxa.jp are names on one JAXA server's
certificate).

The board's hourly sunshine column read this product republished by
Open-Meteo (``jma_jaxa_himawari``); the same hour reads the same number at
the source (ZUUU 2026-10-14 05-06Z: 412.7 W/m2 here, 411.8 there), so this is
the official copy of what the column already showed, not a new quantity.

Product: JAXA Himawari Short Wave Radiation, Level 3 hourly, version 2.1,
0.05-degree equal latitude-longitude grid (60N..60S, 70E..150W, 2401 x
2801), ``SWR`` int16 W/m2 with its own scale factor and missing value; a
file named ``..._YYYYMMDD_HH00_1H_RFL...`` is the mean of HH:00-HH:59 UTC
and lands about 15 minutes after that hour ends.  Every file is the whole
disk, so an hour is downloaded once and read for every fleet station in it,
the file deleted at once (bounded: size cap, a free-disk floor, one reader
thread); the readings are kept in a small runtime file so a restart does not
fetch again.  Access is the account in ``~/.netrc`` (never in this tree)
over implicit FTPS against the pinned SECOM TLS RSA Root CA 2024
(``config/certs``; macOS and certifi do not carry it yet).

Board only -- nothing here feeds a probability or an order.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HOST = "ftp.ptree.jaxa.jp"
PRODUCT_DIR = "/pub/himawari/L3/PAR"
CA_FILE = ROOT / "config" / "certs" / "secom_tls_rsa_root_ca_2024.pem"
LABEL = "JAXA Himawari（P-Tree 官方）"
#: Grid of the 5 km full-disk file.
UPPER_LAT, LEFT_LON, STEP = 60.0, 70.0, 0.05
ROWS, COLS = 2401, 2801
#: An hour's file is public this long after the hour began (measured
#: 2026-10-14: the 22:00 file at 23:15).
PUBLISH_AFTER = timedelta(minutes=75)
MAX_FILE_BYTES = 80 * 1024 * 1024
MIN_FREE_BYTES = 2 * 1024 ** 3
CURL_TIMEOUT_SECONDS = 300
LISTING_TTL_SECONDS = 300.0
MISS_RETRY_SECONDS = 600.0
KEEP_HOURS = 96
CACHE_PATH = ROOT / "data" / "runtime" / "jaxa_ptree_swr_v1.json"
CACHE_SCHEMA = "jaxa_ptree_swr_v1"
_FILE_RE = re.compile(r"^H\d\d_(\d{8})_(\d{2})00_1H_RFL\d{3}_FLDK\.02801_02401\.nc$")

_LOCK = threading.Lock()
_QUEUE: "queue.Queue[datetime]" = queue.Queue()
_PENDING: set[str] = set()
_MISSES: dict[str, float] = {}
_LISTINGS: dict[str, tuple[float, list[str]]] = {}
_CACHE: dict[str, dict[str, float | None]] | None = None   # hour ISO -> station -> W/m2
_WORKER: threading.Thread | None = None
_STATUS: dict[str, Any] = {}


def covers(lat: float, lon: float) -> bool:
    east = lon if lon >= 0.0 else lon + 360.0
    return -60.0 <= lat <= 60.0 and LEFT_LON <= east <= LEFT_LON + STEP * (COLS - 1)


def pixel(lat: float, lon: float) -> tuple[int, int] | None:
    """The grid cell nearest the point, or None outside the disk."""

    if not covers(lat, lon):
        return None
    east = lon if lon >= 0.0 else lon + 360.0
    return round((UPPER_LAT - lat) / STEP), round((east - LEFT_LON) / STEP)


def hour_start(instant: datetime) -> datetime:
    return instant.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _key(hour: datetime) -> str:
    return hour_start(hour).strftime("%Y-%m-%dT%H:00Z")


def _curl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["curl", "--netrc", "--cacert", str(CA_FILE), "-sS", "-m", str(timeout), *args],
        capture_output=True, timeout=timeout + 15, check=False,
    )


def _listing(directory: str) -> list[str]:
    hit = _LISTINGS.get(directory)
    if hit and time.monotonic() - hit[0] < LISTING_TTL_SECONDS:
        return hit[1]
    out = _curl("--list-only", f"ftps://{HOST}{directory}/")
    if out.returncode != 0:
        raise RuntimeError(f"listing {directory}: curl {out.returncode} {out.stderr.decode(errors='replace')[:120]}")
    names = [line.strip() for line in out.stdout.decode(errors="replace").splitlines() if line.strip()]
    _LISTINGS[directory] = (time.monotonic(), names)
    return names


def _version() -> str:
    versions = sorted(name for name in _listing(PRODUCT_DIR) if name.isdigit())
    if not versions:
        raise RuntimeError("no product version directory")
    return versions[-1]


def _remote_file(hour: datetime) -> str | None:
    hour = hour_start(hour)
    directory = f"{PRODUCT_DIR}/{_version()}/{hour:%Y%m}/{hour:%d}"
    stamp = (hour.strftime("%Y%m%d"), hour.strftime("%H"))
    for name in _listing(directory):
        m = _FILE_RE.match(name)
        if m and (m.group(1), m.group(2)) == stamp:
            return f"{directory}/{name}"
    return None


def _fleet() -> dict[str, tuple[float, float]]:
    from .station_registry import _COORDINATES  # noqa: PLC0415

    return {str(s).upper(): (float(lat), float(lon)) for s, (lat, lon) in _COORDINATES.items() if covers(lat, lon)}


def read_hour(hour: datetime, stations: dict[str, tuple[float, float]]) -> dict[str, float | None] | None:
    """One hour's SWR at every station's grid cell; None when the hour's file
    is not on the server (yet).  Raises on a transport or format failure."""

    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    remote = _remote_file(hour)
    if remote is None:
        return None
    workdir = CACHE_PATH.parent / "jaxa_ptree_tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(workdir).free < MIN_FREE_BYTES:
        raise RuntimeError("free disk below the floor")
    handle, path = tempfile.mkstemp(suffix=".nc", dir=workdir)
    os.close(handle)
    try:
        got = _curl("--max-filesize", str(MAX_FILE_BYTES), "-o", path, f"ftps://{HOST}{remote}",
                    timeout=CURL_TIMEOUT_SECONDS)
        if got.returncode != 0:
            raise RuntimeError(f"download: curl {got.returncode} {got.stderr.decode(errors='replace')[:120]}")
        with h5py.File(path, "r") as f:
            swr = f["SWR"]
            if swr.shape != (ROWS, COLS):
                raise RuntimeError(f"grid {swr.shape}")
            scale = float(np.ravel(swr.attrs.get("scale_factor", [1.0]))[0])
            missing = int(np.ravel(swr.attrs.get("missing_value", [-32768]))[0])
            lat_axis, lon_axis = f["latitude"], f["longitude"]
            out: dict[str, float | None] = {}
            for station, (lat, lon) in stations.items():
                cell = pixel(lat, lon)
                if cell is None:
                    continue
                r, c = cell
                if abs(float(lat_axis[r]) - lat) > STEP or abs(((float(lon_axis[c]) - lon) + 180.0) % 360.0 - 180.0) > STEP:
                    raise RuntimeError(f"grid axis disagrees at {station}")
                raw = int(swr[r, c])
                out[station] = None if raw == missing else round(raw * scale, 1)
        return out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _load() -> dict[str, dict[str, float | None]]:
    global _CACHE
    if _CACHE is None:
        try:
            doc = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            _CACHE = dict(doc.get("hours") or {}) if doc.get("schema") == CACHE_SCHEMA else {}
        except (OSError, ValueError):
            _CACHE = {}
    return _CACHE


def _persist(cache: dict[str, dict[str, float | None]]) -> None:
    keep = sorted(cache)[-KEEP_HOURS:]
    for stale in set(cache) - set(keep):
        cache.pop(stale, None)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"schema": CACHE_SCHEMA, "written_at_utc": datetime.now(timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%SZ"), "hours": cache}, sort_keys=True),
                         encoding="utf-8")
    os.replace(temporary, CACHE_PATH)


def _work() -> None:
    while True:
        hour = _QUEUE.get()
        key = _key(hour)
        try:
            values = read_hour(hour, _fleet())
            error = None
        except Exception as exc:  # noqa: BLE001 -- asked again after MISS_RETRY_SECONDS
            values, error = None, f"{type(exc).__name__}: {str(exc)[:120]}"
        with _LOCK:
            _PENDING.discard(key)
            if values is None:
                _MISSES[key] = time.monotonic()
                if error:
                    _STATUS.update(last_error=error, last_error_hour=key)
                continue
            cache = _load()
            cache[key] = values
            _persist(cache)
            _STATUS.update(last_read_hour=key, last_read_at_utc=datetime.now(timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"))


def measured(station: str, hours: list[datetime]) -> dict[str, float | None]:
    """hour start (``YYYY-MM-DDTHH:00Z``) -> W/m2 for the hours already read
    (None where the file has no value at the station); the others are queued
    for the background reader, newest first.  Never waits on the network."""

    global _WORKER
    out: dict[str, float | None] = {}
    now = time.monotonic()
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_work, name="jaxa-ptree-swr", daemon=True)
            _WORKER.start()
        cache = _load()
        for hour in sorted(hours, reverse=True):
            key = _key(hour)
            if key in cache:
                if station.upper() in cache[key]:
                    out[key] = cache[key][station.upper()]
                continue
            if key in _PENDING or now - _MISSES.get(key, -1e9) < MISS_RETRY_SECONDS:
                continue
            _PENDING.add(key)
            _QUEUE.put(hour_start(hour))
    return out


def status() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATUS, pending=len(_PENDING))


__all__ = ["LABEL", "PUBLISH_AFTER", "covers", "hour_start", "measured", "pixel", "read_hour", "status"]
