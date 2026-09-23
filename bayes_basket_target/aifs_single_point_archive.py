"""ECMWF AIFS Single (deterministic, 0.25 degree) 2 m temperature at points.

Charter 201 (2026-09-23): the deterministic-forecast family's first global AI
source.  Measured before it was built (docs/research/
station_regional_features_20260922): as a station-level P0 centre correction
at SBGR it took the centre's MAE from 1.64 to 1.17 C against the market's
1.23 and RPS -0.27 [-0.44, -0.11] over 35 event days, surviving the removal
of its two best days, both eras, every lead tier and a knowable time six
hours later -- the one station-source pair that did.

Transport: ECMWF open data on the GCS mirror the ECMWF ensemble lane already
reads (``ecmwf-open-data``), one ``.index`` (JSON lines) and one ranged read
of the ``2t`` message per step.  The stream is six-hourly (steps 0, 6, ...
360); there is no hourly field and no maximum-temperature field, so a day's
maximum is the largest of the 6-hourly values inside the local day -- the
form the research measured (``ecmwf_aifs025_single_6h``: -0.271 against the
hourly-interpolated -0.266).  Checked against the relay's series of the same
runs: official field = relay value - 0.27 C (56 points, max 0.37), a constant
the feature's own debias takes.

Cell: charter 200's rule, read off the run's own ``lsm`` (the land-sea mask
ships at step 0).  At 0.25 degrees a station's nearest cell is water at FACT,
NZWN and WSSS, whose nearest land cells lie 14-19 km away; the search reaches
1.5 grid spacings (``noaa_conus_archive.land_search_radius_km``).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import httpx

from .contracts import ContractError
from .noaa_conus_archive import nearest_land_points, nearest_point_kelvin
from .pass_deadline import PassDeadlineExceeded, deadline_spent

BASE_URL = "https://storage.googleapis.com/ecmwf-open-data"
STEP_HOURS = 6
MAX_STEP = 360
# Steps asked for: D+2's afternoon at every roster station from a run issued
# the day before D+1 is inside 96 hours.
MAX_CAPTURE_STEP = 96
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_ATTEMPTS = 3
_client_instance: httpx.Client | None = None


class AifsPassDeadlineExceeded(PassDeadlineExceeded):
    """The pass ran out of wall clock inside an AIFS fetch."""


def _client() -> httpx.Client:
    global _client_instance
    if _client_instance is None:
        _client_instance = httpx.Client(timeout=_TIMEOUT)
    return _client_instance


def close_client() -> None:
    global _client_instance
    if _client_instance is not None:
        _client_instance.close()
        _client_instance = None


def step_stem(run_init: datetime, step: int) -> str:
    stamp = run_init.astimezone(timezone.utc)
    return (
        f"{BASE_URL}/{stamp:%Y%m%d}/{stamp:%H}z/aifs-single/0p25/oper/"
        f"{stamp:%Y%m%d%H}0000-{int(step)}h-oper-fc"
    )


def run_directory_url(run_init: datetime) -> str:
    stamp = run_init.astimezone(timezone.utc)
    return f"{BASE_URL}/{stamp:%Y%m%d}/{stamp:%H}z/aifs-single/0p25/oper/"


def _get(url: str, headers: Mapping[str, str] | None = None) -> bytes | None:
    for _attempt in range(_ATTEMPTS):
        if deadline_spent():
            raise AifsPassDeadlineExceeded("aifs_single_pass_deadline_exceeded")
        try:
            response = _client().get(url, headers=dict(headers or {}))
        except httpx.HTTPError:
            continue
        if response.status_code == 404:
            return None
        if response.status_code in (200, 206):
            return response.content
    return None


def _message(run_init: datetime, step: int, param: str) -> bytes | None:
    """One parameter's GRIB message at one step, by byte range off the index."""

    stem = step_stem(run_init, step)
    raw = _get(stem + ".index")
    if raw is None:
        return None
    entry = None
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("param") == param and item.get("levtype") == "sfc":
            entry = item
            break
    if entry is None:
        return None
    start, length = int(entry["_offset"]), int(entry["_length"])
    return _get(stem + ".grib2", {"Range": f"bytes={start}-{start + length - 1}"})


def run_published(run_init: datetime, step: int = MAX_CAPTURE_STEP) -> bool:
    """One index read: is the run out to the step the lane needs?"""

    return _get(step_stem(run_init, step) + ".index") is not None


def local_day_steps(
    *, run_init: datetime, event_date: date, utc_offset_hours: int,
) -> list[int]:
    start = datetime.combine(event_date, datetime.min.time(), tzinfo=timezone.utc) - timedelta(
        hours=utc_offset_hours
    )
    return [
        step for step in range(0, MAX_CAPTURE_STEP + 1, STEP_HOURS)
        if start <= run_init + timedelta(hours=step) < start + timedelta(days=1)
    ]


def fetch_run_for_targets(
    *, run_init: datetime, targets: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Every target's 6-hourly local-day curve from one read per step.

    Streamed: each step's message is decoded for every target that wants it
    and dropped, so a pass holds one field at a time.  A station charter
    200's rule cannot place on land within reach is left out.
    """

    wanted: dict[tuple[str, str], list[int]] = {}
    for target in targets:
        key = (str(target["station_id"]), target["event_date"].isoformat())
        wanted[key] = local_day_steps(
            run_init=run_init, event_date=target["event_date"],
            utc_offset_hours=int(target["utc_offset_hours"]),
        )
    union = sorted({step for steps in wanted.values() for step in steps})
    if not union:
        return {}
    mask = _message(run_init, 0, "lsm")
    if mask is None:
        # Not published yet (the 0 h file comes first) or unreadable: the
        # run is not read at all rather than read at an unchecked cell.
        return {}
    stations: dict[str, tuple[float, float]] = {}
    for target in targets:
        stations.setdefault(
            str(target["station_id"]), (float(target["latitude"]), float(target["longitude"]))
        )
    order = sorted(stations)
    sampled = dict(zip(order, nearest_land_points(mask, [stations[s] for s in order])))
    values: dict[tuple[str, str], dict[int, float]] = {key: {} for key in wanted}
    for step in union:
        message = _message(run_init, step, "2t")
        if message is None:
            continue
        cache: dict[str, float] = {}
        for key, steps in wanted.items():
            if step not in steps:
                continue
            point = sampled.get(key[0])
            if point is None:
                continue
            if key[0] not in cache:
                kelvin, _distance = nearest_point_kelvin(
                    message, latitude=float(point["latitude"]), longitude=float(point["longitude"]),
                )
                cache[key[0]] = kelvin - 273.15
            values[key][step] = cache[key[0]]
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for key, steps in wanted.items():
        point = sampled.get(key[0])
        if point is None:
            continue
        url = run_directory_url(run_init)
        if point.get("moved"):
            url += f"#land_cell={float(point['latitude']):.5f},{float(point['longitude']):.5f}"
        results[key] = {"hourly_c": values[key], "hours_wanted": len(steps), "source_url": url}
    return results


__all__ = [
    "BASE_URL", "MAX_CAPTURE_STEP", "STEP_HOURS", "close_client",
    "fetch_run_for_targets", "local_day_steps", "run_directory_url",
    "run_published", "step_stem",
]
