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

import fcntl
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
STALE_DOWNLOAD_SECONDS = 3600
LISTING_TTL_SECONDS = 300.0
MISS_RETRY_SECONDS = 600.0
KEEP_HOURS = 96
CACHE_PATH = ROOT / "data" / "runtime" / "jaxa_ptree_swr_v1.json"
CACHE_SCHEMA = "jaxa_ptree_swr_v1"
_FILE_RE = re.compile(r"^H\d\d_(\d{8})_(\d{2})00_1H_RFL\d{3}_FLDK\.02801_02401\.nc$")
#: The 10-minute Level 2 product (user 2026-10-15 「加L2」): one scan, same
#: grid and algorithm, lands ~21 minutes after the scan begins (measured
#: 2026-10-15: the 00:40 scan at 01:01) -- the latest scan only, for 「此刻」.
L2_DIR = "/pub/himawari/L2/PAR"
_L2_RE = re.compile(r"^H\d\d_(\d{8})_(\d{4})_RFL\d{3}_FLDK\.02801_02401\.nc$")
#: The server is asked for a newer scan at most this often.
L2_CHECK_SECONDS = 300.0
_L2_TASK = "L2"
#: JAXA Himawari Cloud Property (user 2026-10-15 「接云」): Level 2, one
#: 10-minute scan, day-time only, 0.05 deg on its own grid (60N..60S,
#: 80E..160W, 2401 x 2401), ~50 minutes after the scan (the 23:50 scan at
#: 00:38).  Read as a 5 x 5 cell box (~25 km, about what an observer's sky
#: covers): the share of cells the cloud mask calls cloudy, the commonest
#: ISCCP type among them, and their median top height and optical thickness.
CLP_DIR = "/pub/himawari/L2/CLP"
_CLP_RE = re.compile(r"^NC_H\d\d_(\d{8})_(\d{4})_L2CLP\d{3}_FLDK\.02401_02401\.nc$")
CLP_UPPER_LAT, CLP_LEFT_LON, CLP_SIZE = 60.0, 80.0, 2401
CLP_PUBLISH_AFTER = timedelta(minutes=55)
CLP_HALF_BOX = 2
CLP_TYPES = {0: "晴", 1: "卷云", 2: "卷层云", 3: "深对流", 4: "高积云", 5: "高层云", 6: "雨层云",
             7: "积云", 8: "层积云", 9: "层云", 10: "未知"}
#: JAXA Himawari aerosol (user 2026-10-25「增加气溶胶的数据」): Level 3 hourly
#: combined aerosol, the CLP grid (60N..60S, 80E..160W, 0.05 deg), ~90 minutes
#: after the hour begins (the 23:00 file at 00:30).  ``AOT_Pure`` is the
#: quality-screened optical thickness (500 nm); ``AOT_Merged`` adds the
#: interpolated cells and stands in when no pure cell is in the box.  The hour's
#: ``AOT_L2_Mean`` is left out: cloud edges pass into it (ZUUU 10-25 03Z read 4.99).
ARP_DIR = "/pub/himawari/L3/ARP"
_ARP_RE = re.compile(r"^H\d\d_(\d{8})_(\d{2})00_1HARP\d{3}_FLDK\.02401_02401\.nc$")
ARP_PUBLISH_AFTER = timedelta(minutes=95)
ARP_LABEL = "JAXA Himawari 气溶胶（P-Tree 官方）"
_ARP: dict[str, dict[str, dict[str, Any]]] = {}   # hour ISO -> station -> reading
_L2_STATE: dict[str, Any] = {"pending": False, "checked": -1e9}
_CLP: dict[str, dict[str, dict[str, Any]]] = {}   # scan ISO -> station -> reading

_LOCK = threading.Lock()
_QUEUE: "queue.Queue[Any]" = queue.Queue()
_PENDING: set[str] = set()
_MISSES: dict[str, float] = {}
_LISTINGS: dict[str, tuple[float, list[str]]] = {}
_CACHE: dict[str, dict[str, float | None]] | None = None   # hour ISO -> station -> W/m2
_CACHE_STAMP: int | None = None   # the file's mtime when last folded in
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


def _version(product_dir: str = PRODUCT_DIR) -> str:
    versions = sorted(name for name in _listing(product_dir) if name.isdigit())
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


def _latest_l2(now: datetime) -> tuple[datetime, str] | None:
    """The newest 5 km scan on the server: this UTC hour's folder, else the
    hour before (a folder not made yet is simply empty)."""

    version = _version(L2_DIR)
    for back in (0, 1):
        hour = hour_start(now) - timedelta(hours=back)
        directory = f"{L2_DIR}/{version}/{hour:%Y%m}/{hour:%d}/{hour:%H}"
        try:
            names = _listing(directory)
        except RuntimeError:
            continue
        scans = sorted((m.group(1) + m.group(2), name) for name in names if (m := _L2_RE.match(name)))
        if scans:
            stamp, name = scans[-1]
            return datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc), f"{directory}/{name}"
    return None


def clp_cell(lat: float, lon: float) -> tuple[int, int] | None:
    east = lon if lon >= 0.0 else lon + 360.0
    r = round((CLP_UPPER_LAT - lat) / STEP)
    c = round((east - CLP_LEFT_LON) / STEP)
    if not (CLP_HALF_BOX <= r < CLP_SIZE - CLP_HALF_BOX and CLP_HALF_BOX <= c < CLP_SIZE - CLP_HALF_BOX):
        return None
    return r, c


def _clp_remote(scan: datetime) -> str | None:
    scan = scan.astimezone(timezone.utc)
    directory = f"{CLP_DIR}/{_version(CLP_DIR)}/{scan:%Y%m}/{scan:%d}/{scan:%H}"
    try:
        names = _listing(directory)
    except RuntimeError:
        return None
    stamp = (scan.strftime("%Y%m%d"), scan.strftime("%H%M"))
    for name in names:
        m = _CLP_RE.match(name)
        if m and (m.group(1), m.group(2)) == stamp:
            return f"{directory}/{name}"
    return None


def _download(remote: str) -> str:
    workdir = CACHE_PATH.parent / "jaxa_ptree_tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(workdir).free < MIN_FREE_BYTES:
        raise RuntimeError("free disk below the floor")
    _sweep_stale_downloads(workdir)
    handle, path = tempfile.mkstemp(suffix=".nc", dir=workdir)
    os.close(handle)
    try:
        # A transfer the server closes early (curl 18, seen 2026-10-15) is
        # tried once more; anything else fails at once.
        for _attempt in range(2):
            got = _curl("--max-filesize", str(MAX_FILE_BYTES), "-o", path, f"ftps://{HOST}{remote}",
                        timeout=CURL_TIMEOUT_SECONDS)
            if got.returncode != 18:
                break
        if got.returncode != 0:
            raise RuntimeError(f"download: curl {got.returncode} {got.stderr.decode(errors='replace')[:120]}")
    except BaseException:
        # A curl timeout raised here left its partial file behind: 749 MiB
        # of tmp*.nc by 2026-10-26.
        os.unlink(path)
        raise
    return path


def _sweep_stale_downloads(workdir: Path) -> None:
    """Drop temp files a killed reader never got to delete.

    A reader holds its file for at most two curl attempts plus the read, so
    anything older than STALE_DOWNLOAD_SECONDS has no owner left.
    """

    cutoff = time.time() - STALE_DOWNLOAD_SECONDS
    for stale in workdir.glob("tmp*.nc"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except FileNotFoundError:
            pass


def read_clp_file(remote: str, stations: dict[str, tuple[float, float]]) -> dict[str, dict[str, Any]]:
    """One cloud-property scan at every station's 5 x 5 box (file deleted after)."""

    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    path = _download(remote)
    try:
        with h5py.File(path, "r") as f:
            if f["QA"].shape != (CLP_SIZE, CLP_SIZE):
                raise RuntimeError(f"grid {f['QA'].shape}")
            lat_axis, lon_axis = f["latitude"], f["longitude"]

            def scaled(name: str, block):
                attrs = f[name].attrs
                missing = int(np.ravel(attrs.get("missing_value", [-32768]))[0])
                data = block.astype(float)
                data[block == missing] = np.nan
                return data * float(np.ravel(attrs.get("scale_factor", [1.0]))[0]) + float(
                    np.ravel(attrs.get("add_offset", [0.0]))[0])

            out: dict[str, dict[str, Any]] = {}
            for station, (lat, lon) in stations.items():
                cell = clp_cell(lat, lon)
                if cell is None:
                    continue
                r, c = cell
                if abs(float(lat_axis[r]) - lat) > STEP or abs(((float(lon_axis[c]) - lon) + 180.0) % 360.0 - 180.0) > STEP:
                    raise RuntimeError(f"grid axis disagrees at {station}")
                box = np.s_[r - CLP_HALF_BOX:r + CLP_HALF_BOX + 1, c - CLP_HALF_BOX:c + CLP_HALF_BOX + 1]
                qa = f["QA"][box].astype(int)
                retrieval = qa & 0b111
                judged = (retrieval >= 2) & (retrieval != 3)        # clear or a successful retrieval
                if not judged.any():
                    out[station] = {"state": "no_retrieval"}
                    continue
                cloudy = judged & (((qa >> 3) & 0b11) >= 2)          # probably cloudy / cloudy
                types = f["CLTYPE"][box][cloudy]
                types = types[(types >= 1) & (types <= 10)]
                top = scaled("CLTH", f["CLTH"][box])[cloudy]
                cot = scaled("CLOT", f["CLOT"][box])[cloudy]
                out[station] = {
                    "state": "ok", "cover": round(float(cloudy.sum()) / float(judged.sum()), 2),
                    "cells": int(judged.sum()),
                    "type": int(np.bincount(types).argmax()) if types.size else (0 if not cloudy.any() else None),
                    "top_km": round(float(np.nanmedian(top)), 1) if top.size and np.isfinite(top).any() else None,
                    "optical_thickness": round(float(np.nanmedian(cot)), 1) if cot.size and np.isfinite(cot).any() else None,
                }
        return out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _arp_remote(hour: datetime) -> str | None:
    hour = hour_start(hour)
    directory = f"{ARP_DIR}/{_version(ARP_DIR)}/{hour:%Y%m}/{hour:%d}"
    try:
        names = _listing(directory)
    except RuntimeError:
        return None
    stamp = (hour.strftime("%Y%m%d"), hour.strftime("%H"))
    for name in names:
        m = _ARP_RE.match(name)
        if m and (m.group(1), m.group(2)) == stamp:
            return f"{directory}/{name}"
    return None


def read_arp_file(remote: str, stations: dict[str, tuple[float, float]]) -> dict[str, dict[str, Any]]:
    """One hourly aerosol file at every station's 5 x 5 box (file deleted after)."""

    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    path = _download(remote)
    try:
        with h5py.File(path, "r") as f:
            if f["AOT_Pure"].shape != (CLP_SIZE, CLP_SIZE):
                raise RuntimeError(f"grid {f['AOT_Pure'].shape}")
            lat_axis, lon_axis = f["latitude"], f["longitude"]

            def scaled(name: str, box):
                attrs = f[name].attrs
                raw = f[name][box]
                data = raw.astype(float)
                data[raw == int(np.ravel(attrs.get("missing_value", [-32768]))[0])] = np.nan
                return data * float(np.ravel(attrs.get("scale_factor", [1.0]))[0]) + float(
                    np.ravel(attrs.get("add_offset", [0.0]))[0])

            out: dict[str, dict[str, Any]] = {}
            for station, (lat, lon) in stations.items():
                cell = clp_cell(lat, lon)
                if cell is None:
                    continue
                r, c = cell
                if abs(float(lat_axis[r]) - lat) > STEP or abs(((float(lon_axis[c]) - lon) + 180.0) % 360.0 - 180.0) > STEP:
                    raise RuntimeError(f"grid axis disagrees at {station}")
                box = np.s_[r - CLP_HALF_BOX:r + CLP_HALF_BOX + 1, c - CLP_HALF_BOX:c + CLP_HALF_BOX + 1]
                for name, kind in (("AOT_Pure", "pure"), ("AOT_Merged", "merged")):
                    values = scaled(name, box)
                    good = values[np.isfinite(values)]
                    if good.size:
                        out[station] = {"aod": round(float(good.mean()), 3), "kind": kind, "cells": int(good.size)}
                        break
                else:
                    out[station] = {"aod": None, "kind": None, "cells": 0}
        return out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


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
    return read_file(remote, stations)


def read_file(remote: str, stations: dict[str, tuple[float, float]]) -> dict[str, float | None]:
    """One full-disk SWR file's value at every station's grid cell (bounded
    download, file deleted after).  Raises on a transport or format failure."""

    import h5py  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    path = _download(remote)
    try:
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


def _absorb(doc: dict[str, Any]) -> None:
    """Fold the file's reads into this process's: an hour or scan this
    process has not read is taken from the file, the newer L2 scan wins."""

    global _CACHE
    if _CACHE is None:
        _CACHE = {}
    if doc.get("schema") != CACHE_SCHEMA:
        return
    for key, values in (doc.get("hours") or {}).items():
        _CACHE.setdefault(key, values)
    for key, values in (doc.get("clp") or {}).items():
        _CLP.setdefault(key, values)
    for key, values in (doc.get("arp") or {}).items():
        _ARP.setdefault(key, values)
    latest = doc.get("l2_latest")
    mine = _L2_STATE.get("latest") or {}
    if isinstance(latest, dict) and str(latest.get("scan_utc") or "") > str(mine.get("scan_utc") or ""):
        _L2_STATE["latest"] = latest


def _absorb_file() -> None:
    global _CACHE_STAMP
    try:
        stamp = CACHE_PATH.stat().st_mtime_ns
        if stamp == _CACHE_STAMP and _CACHE is not None:
            return
        doc = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc, stamp = {}, None
    _absorb(doc if isinstance(doc, dict) else {})
    _CACHE_STAMP = stamp


def _load() -> dict[str, dict[str, float | None]]:
    # Several processes (the board's research refresh and its page compute)
    # each run a reader on the one file: what another process read since is
    # folded in whenever the file changes, so no hour is fetched twice and
    # none is shown as missing here after another process read it.
    _absorb_file()
    assert _CACHE is not None
    return _CACHE


def _persist(cache: dict[str, dict[str, float | None]]) -> None:
    # Merge-then-replace under a file lock: writing this process's dict over
    # the file dropped every hour another process had read since this one
    # loaded (2026-10-20: the 01Z hour read, then overwritten, shown 未到).
    global _CACHE_STAMP
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH.with_suffix(".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            _absorb_file()
            keep = sorted(cache)[-KEEP_HOURS:]
            for stale in set(cache) - set(keep):
                cache.pop(stale, None)
            keep_scans = sorted(_CLP)[-KEEP_HOURS:]
            for stale in set(_CLP) - set(keep_scans):
                _CLP.pop(stale, None)
            for stale in set(_ARP) - set(sorted(_ARP)[-KEEP_HOURS:]):
                _ARP.pop(stale, None)
            fd, temporary = tempfile.mkstemp(dir=CACHE_PATH.parent, prefix=CACHE_PATH.name, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"schema": CACHE_SCHEMA, "written_at_utc": datetime.now(timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"), "hours": cache,
                           "l2_latest": _L2_STATE.get("latest"), "clp": _CLP, "arp": _ARP}, out, sort_keys=True)
            os.replace(temporary, CACHE_PATH)
            _CACHE_STAMP = CACHE_PATH.stat().st_mtime_ns
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_latest_l2() -> None:
    try:
        found = _latest_l2(datetime.now(timezone.utc))
        latest = _L2_STATE.get("latest") or {}
        values = None
        if found is not None and found[1] != latest.get("file"):
            values = read_file(found[1], _fleet())
        error = None
    except Exception as exc:  # noqa: BLE001 -- checked again after L2_CHECK_SECONDS
        found, values, error = None, None, f"{type(exc).__name__}: {str(exc)[:120]}"
    with _LOCK:
        _L2_STATE.update(pending=False, checked=time.monotonic())
        if error:
            _STATUS.update(l2_error=error)
        if values is not None and found is not None:
            _L2_STATE["latest"] = {"scan_utc": found[0].strftime("%Y-%m-%dT%H:%MZ"), "file": found[1],
                                   "read_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "values": values}
            _persist(_load())


def _clp_key(scan: datetime) -> str:
    return scan.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _read_clp(scan: datetime) -> None:
    key = "CLP:" + _clp_key(scan)
    try:
        remote = _clp_remote(scan)
        values = read_clp_file(remote, _fleet_clp()) if remote else None
        error = None
    except Exception as exc:  # noqa: BLE001 -- asked again after MISS_RETRY_SECONDS
        values, error = None, f"{type(exc).__name__}: {str(exc)[:120]}"
    with _LOCK:
        _PENDING.discard(key)
        if values is None:
            _MISSES[key] = time.monotonic()
            if error:
                _STATUS.update(clp_error=error)
            return
        _load()
        _CLP[_clp_key(scan)] = values
        _persist(_load())


def _read_arp(hour: datetime) -> None:
    key = "ARP:" + _key(hour)
    try:
        remote = _arp_remote(hour)
        values = read_arp_file(remote, _fleet_clp()) if remote else None
        error = None
    except Exception as exc:  # noqa: BLE001 -- asked again after MISS_RETRY_SECONDS
        values, error = None, f"{type(exc).__name__}: {str(exc)[:120]}"
    with _LOCK:
        _PENDING.discard(key)
        if values is None:
            _MISSES[key] = time.monotonic()
            if error:
                _STATUS.update(arp_error=error)
            return
        _load()
        _ARP[_key(hour)] = values
        _persist(_load())


def _work() -> None:
    while True:
        hour = _QUEUE.get()
        if hour == _L2_TASK:
            _read_latest_l2()
            continue
        if isinstance(hour, tuple) and hour[0] == "ARP":
            _read_arp(hour[1])
            continue
        if isinstance(hour, tuple) and hour[0] == "CLP":
            _read_clp(hour[1])
            continue
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

    out: dict[str, float | None] = {}
    now = time.monotonic()
    with _LOCK:
        _ensure_worker()
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


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is None or not _WORKER.is_alive():
        _WORKER = threading.Thread(target=_work, name="jaxa-ptree-swr", daemon=True)
        _WORKER.start()


def _fleet_clp() -> dict[str, tuple[float, float]]:
    from .station_registry import _COORDINATES  # noqa: PLC0415

    return {str(s).upper(): (float(lat), float(lon)) for s, (lat, lon) in _COORDINATES.items()
            if clp_cell(float(lat), float(lon)) is not None}


def clouds(station: str, scans: list[datetime]) -> dict[str, dict[str, Any] | None]:
    """scan (``YYYY-MM-DDTHH:MMZ``) -> the station's cloud reading for the
    scans already read; the others are queued, newest first.  Never waits."""

    out: dict[str, dict[str, Any] | None] = {}
    now = time.monotonic()
    with _LOCK:
        _ensure_worker()
        _load()
        for scan in sorted(scans, reverse=True):
            key = _clp_key(scan)
            if key in _CLP:
                out[key] = _CLP[key].get(station.upper())
                continue
            task = "CLP:" + key
            if task in _PENDING or now - _MISSES.get(task, -1e9) < MISS_RETRY_SECONDS:
                continue
            _PENDING.add(task)
            _QUEUE.put(("CLP", scan.astimezone(timezone.utc)))
    return out


def aerosol(station: str, hours: list[datetime]) -> dict[str, dict[str, Any] | None]:
    """hour start (``YYYY-MM-DDTHH:00Z``) -> the station's hourly aerosol reading
    for the hours already read; the others are queued, newest first.  Never waits."""

    out: dict[str, dict[str, Any] | None] = {}
    now = time.monotonic()
    with _LOCK:
        _ensure_worker()
        _load()
        for hour in sorted(hours, reverse=True):
            key = _key(hour)
            if key in _ARP:
                out[key] = _ARP[key].get(station.upper())
                continue
            task = "ARP:" + key
            if task in _PENDING or now - _MISSES.get(task, -1e9) < MISS_RETRY_SECONDS:
                continue
            _PENDING.add(task)
            _QUEUE.put(("ARP", hour_start(hour)))
    return out


def latest(station: str) -> dict[str, Any] | None:
    """The newest 10-minute scan read so far at the station: ``{"scan_utc",
    "sw_wm2", "read_at_utc"}``, or None; asks the background reader for a newer
    scan at most every L2_CHECK_SECONDS.  Never waits on the network."""

    with _LOCK:
        _ensure_worker()
        _load()
        if not _L2_STATE["pending"] and time.monotonic() - _L2_STATE["checked"] >= L2_CHECK_SECONDS:
            _L2_STATE["pending"] = True
            _QUEUE.put(_L2_TASK)
        got = _L2_STATE.get("latest") or {}
        values = got.get("values") or {}
        if station.upper() not in values:
            return None
        return {"scan_utc": got.get("scan_utc"), "sw_wm2": values[station.upper()],
                "read_at_utc": got.get("read_at_utc")}


def status() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATUS, pending=len(_PENDING))


__all__ = ["ARP_LABEL", "ARP_PUBLISH_AFTER", "CLP_PUBLISH_AFTER", "CLP_TYPES", "LABEL", "PUBLISH_AFTER", "aerosol",
           "clouds", "clp_cell", "covers", "read_arp_file",
           "hour_start", "latest", "measured", "pixel", "read_clp_file", "read_file", "read_hour", "status"]
