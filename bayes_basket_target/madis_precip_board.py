"""Observed rain every five minutes for the US ASOS stations, from NOAA MADIS.

User 2026-10-09「做」(MADIS 5-minute rain on the board).  Production already
reads the MADIS HF-METAR hourly objects for temperature only (madis_hfmetar);
the same public objects carry, per station every five minutes, the present
weather (``presWx``) and ``precipAccum`` -- the precipitation since the last
routine hourly report, in mm, reset just after it (measured 2026-10-09 on
Alaska rain: 0.254 through 03:45-03:50, 0.0 from 03:55).

Board only: nothing here writes the SSOT or feeds a probability.  One
background thread reads the objects of the hours a board request asks for
and keeps, per object, only the stations asked about; a finished hour is read
once, the current hour again every five minutes.  The request never waits:
what is not read yet is reported as pending.
"""

from __future__ import annotations

import gzip
import queue
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

MADIS_BASE = "https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/hfmetar/netCDF"
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
CURRENT_HOUR_TTL_SECONDS = 300.0
MISS_RETRY_SECONDS = 300.0
FILL_ABOVE = 1e30
# Present-weather tokens that are precipitation at the station (VC, in the
# vicinity, is not).
PRECIP_CODES = ("RA", "DZ", "SN", "SG", "PL", "GR", "GS", "IC", "UP", "TS")
# The routine report closes the accumulation at about :51-:56; a record from
# :53 on already counts toward the next window.
RESET_MINUTE = 53

_OBJECTS: dict[str, tuple[float, dict[str, list[tuple[datetime, str, float | None]]] | None]] = {}
_PENDING: set[str] = set()
_STATIONS: set[str] = set()
_QUEUE: "queue.Queue[str]" = queue.Queue()
_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


def _object_name(hour_utc: datetime) -> str:
    return f"{hour_utc:%Y%m%d_%H00}"


def _read_object(name: str, stations: set[str]) -> dict[str, list[tuple[datetime, str, float | None]]]:
    import ssl  # noqa: PLC0415
    from urllib.request import Request, urlopen  # noqa: PLC0415

    import certifi  # noqa: PLC0415
    from scipy.io import netcdf_file  # noqa: PLC0415

    context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(Request(f"{MADIS_BASE}/{name}.gz", headers={"User-Agent": "weather-board"}),
                 timeout=60, context=context) as response:  # noqa: S310 -- fixed https host
        packed = response.read(MAX_COMPRESSED_BYTES + 1)
    if len(packed) > MAX_COMPRESSED_BYTES:
        raise IOError("madis_object_too_large")
    out: dict[str, list[tuple[datetime, str, float | None]]] = {s: [] for s in stations}
    with tempfile.NamedTemporaryFile(suffix=".nc") as target:
        target.write(gzip.decompress(packed))
        target.flush()
        dataset = netcdf_file(target.name, "r", mmap=False)
        try:
            v = dataset.variables
            for index in range(int(v["observationTime"].shape[0])):
                station = v["stationId"][index].tobytes().decode("latin1").strip("\x00 ").upper()
                if station not in out:
                    continue
                when = datetime.fromtimestamp(float(v["observationTime"][index]), timezone.utc)
                wx = v["presWx"][index].tobytes().decode("latin1").strip("\x00 ")
                accum = float(v["precipAccum"][index])
                out[station].append((when, wx, None if accum > FILL_ABOVE or accum < 0 else round(accum, 2)))
        finally:
            dataset.close()
    for rows in out.values():
        rows.sort()
    return out


def _work() -> None:
    while True:
        name = _QUEUE.get()
        with _LOCK:
            stations = set(_STATIONS)
        try:
            body = _read_object(name, stations)
        except Exception:  # noqa: BLE001 -- asked again after MISS_RETRY_SECONDS
            body = None
        with _LOCK:
            _OBJECTS[name] = (time.monotonic(), body)
            _PENDING.discard(name)


def _wet(wx: str) -> bool:
    return any(code in token for token in wx.split() if not token.startswith("VC") for code in PRECIP_CODES)


def station_day(station: str, day: str, zone: ZoneInfo) -> dict[str, Any]:
    """Five-minute rain for the station's local day so far, as read."""

    global _WORKER
    station = station.upper()
    start = datetime.fromisoformat(day).replace(tzinfo=zone).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if start > now:
        return {"available": False, "reason": "day_not_started"}
    end = min(start + timedelta(days=1), now)
    hours = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour <= end:
        hours.append(hour)
        hour += timedelta(hours=1)
    records: list[tuple[datetime, str, float | None]] = []
    pending = 0
    clock = time.monotonic()
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_work, name="madis-precip-reader", daemon=True)
            _WORKER.start()
        _STATIONS.add(station)
        for hour in hours:
            name = _object_name(hour)
            hit = _OBJECTS.get(name)
            current = hour + timedelta(hours=1) > now
            stale = hit is None or (current and clock - hit[0] > CURRENT_HOUR_TTL_SECONDS) or (
                hit[1] is None and clock - hit[0] > MISS_RETRY_SECONDS) or (
                # read before this station was asked about: read again for it
                hit[1] is not None and station not in hit[1])
            if hit is not None and hit[1] is not None:
                records.extend(hit[1].get(station) or [])
            if stale:
                if name not in _PENDING:
                    _PENDING.add(name)
                    _QUEUE.put(name)
                if hit is None or hit[1] is None or station not in (hit[1] or {}):
                    pending += 1
    lo, hi = start, start + timedelta(days=1)
    records = sorted({r for r in records if lo <= r[0] < hi})
    if not records:
        return {"available": False, "pending_hours": pending, "reason": "reading" if pending else "no_records"}
    hourly: dict[str, dict[str, Any]] = {}
    for when, wx, accum in records:
        local = when.astimezone(zone)
        stamp = min(local.hour + 1 + (1 if local.minute >= RESET_MINUTE else 0), 23)
        cell = hourly.setdefault(f"{stamp:02d}", {"mm": None, "wet": False, "wx": []})
        if accum is not None:
            cell["mm"] = max(cell["mm"] or 0.0, accum)
        if _wet(wx):
            cell["wet"] = True
            if wx not in cell["wx"]:
                cell["wx"].append(wx)
    last = records[-1]
    return {
        "available": True, "source": "NOAA MADIS HF-METAR（5 分钟）", "pending_hours": pending,
        "latest": {"local": last[0].astimezone(zone).strftime("%H:%M"), "wx": last[1],
                   "accum_mm": last[2], "wet": _wet(last[1])},
        "hourly": hourly,
        "total_mm": round(sum(c["mm"] or 0.0 for c in hourly.values()), 2),
        "records": len(records),
    }
