"""Satellite sky per station-hour, and its join to the strict-replay anchors.

Reads the raw boxes written by ``goes_extract.py`` / ``himawari_extract.py``
and reduces each scan to a few fractions of the ~10 km box around the
airport:

* ``cloudy``   any cloud seen (Himawari mask probably-cloudy/cloudy; GOES TCF)
* ``low_<H>``  cloud whose *top* is at or below H ft above the station
               (Himawari, H = 5000 / 6500).  A satellite sees tops: a deck
               based at 900 m with tops at 3.6 km is not "low" here, and a
               low deck under a higher one is invisible.
* ``low_5000`` (GOES) CCL layer 1: cloud between the surface and 5000 ft
               above *sea level* -- void where the station itself is above
               ~2500 ft (KBKF, MMMX) and only ~2500 ft deep at SBGR.

Availability: each scan is admitted at an anchor only after its object's
S3 LastModified (``available_at``) and only while its scan began within
two hours before the anchor -- the same age rule the METAR cloud uses.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ELEV = json.loads((HERE / "station_elevation.json").read_text())["elevation_m"]
MAX_AGE = timedelta(hours=2)
FT = 0.3048


def utc(s: str) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def himawari_record(r: dict) -> dict | None:
    mask = [m for m in r["mask"] if m is not None]
    if len(mask) < 13:
        return None
    elev = ELEV[r["station"]]
    pairs = [(m, t) for m, t in zip(r["mask"], r["top_m"]) if m is not None]
    n = len(pairs)
    cloudy = [t for m, t in pairs if m >= 2]
    out = dict(source="himawari9_cmsk_chgt", station=r["station"], scan_start=r["scan_start"], available_at=r["available_at"],
               n=n, cloudy=len(cloudy) / n)
    if cloudy and any(t is None for t in cloudy):
        # Cloud seen but no top retrieved for some of it: low fraction unknown.
        known = [t for t in cloudy if t is not None]
        if len(known) < 0.8 * len(cloudy):
            return dict(out, low_5000=None, low_6500=None, high=None)
        cloudy = known
    for h in (5000, 6500):
        out[f"low_{h}"] = sum(1 for t in cloudy if t - elev <= h * FT) / n
    out["high"] = sum(1 for t in cloudy if t - elev > 6000) / n
    return out


def goes_record(r: dict) -> dict | None:
    if r.get("state") != "OK" or r["product"] != "ABI-L2-CCLF":
        return None
    elev_ft = ELEV[r["station"]] / FT
    cf1, tcf = r.get("CF1_box_mean"), r.get("TCF_box_mean")
    if tcf is None:
        return None
    low = None if cf1 is None or elev_ft > 2500 else cf1 / 100.0
    return dict(source="goes_ccl", station=r["station"], scan_start=r["scan_start"], available_at=r["s3_last_modified"],
                n=r.get("CF1_box_n", 0), cloudy=float(tcf), low_5000=low, low_6500=None, high=None)


def load() -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for path in sorted((HERE / "himawari").glob("*/*.jsonl.gz")):
        for line in gzip.open(path, "rt"):
            r = json.loads(line)
            if r.get("state") == "OK":
                x = himawari_record(r)
                if x:
                    by.setdefault(x["station"], []).append(x)
    for path in sorted((HERE / "goes").glob("*/ABI-L2-CCLF/*.jsonl.gz")):
        for line in gzip.open(path, "rt"):
            r = json.loads(line)
            x = goes_record(r)
            if x:
                by.setdefault(x["station"], []).append(x)
    for v in by.values():
        v.sort(key=lambda x: x["scan_start"])
    return by


def at(records: list[dict], as_of: str) -> dict | None:
    """Newest scan publicly available at ``as_of`` and begun within 2 h of it."""
    t = utc(as_of)
    best = None
    for x in records:
        s = utc(x["scan_start"])
        if s > t:
            break
        if utc(x["available_at"]) <= t and t - s <= MAX_AGE:
            best = x
    return best


def increment(sat: float | None, interval) -> float | None:
    """Observed minus forecast, zero while the satellite value sits inside
    the TAF's coverage interval (the TAF says a range, not a number)."""
    if sat is None or interval is None:
        return None
    lo, hi = float(interval[0]), float(interval[1])
    return sat - min(max(sat, lo), hi)


_FC: dict[str, dict] = {}


def forecast_total(station: str, scan_start: str) -> float | None:
    """Open-Meteo ``cloud_cover_previous_day1`` (total cloud, %, every value
    issued >= 24 h before its hour; an aggregator_republisher, research only)
    at the local hour nearest the scan, as a fraction."""
    if station not in _FC:
        path = HERE.parent / "obs_increment_p1_20261007" / "cache" / f"{station}.json.gz"
        if not path.exists():
            _FC[station] = {}
        else:
            from zoneinfo import ZoneInfo
            d = json.load(gzip.open(path, "rt"))
            zone = ZoneInfo(d["timezone"])
            h = d["hourly"]
            _FC[station] = {datetime.fromisoformat(t).replace(tzinfo=zone).astimezone(timezone.utc): v
                            for t, v in zip(h["time"], h["cloud_cover_previous_day1"]) if v is not None}
    table = _FC[station]
    t = utc(scan_start)
    hour = (t + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    v = table.get(hour)
    return None if v is None else float(v) / 100.0
