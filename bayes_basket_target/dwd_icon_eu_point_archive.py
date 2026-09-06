"""The ICON-EU deterministic 2m temperature, from DWD open data, run by run.

Ruled in on 2026-08-28 with the 46-pair sweep: LTAC and EPWA cleared all
three admission gates against the served morning posterior (LTAC named the
settled bucket 52% against 24%), and five more stations cleared CRPS and
Brier while missing NLL -- captured anyway, on the MSM precedent that the
corpus is what future gates re-ask.

Two properties shape everything here.  The fields are WHOLE-DOMAIN: one
~1 MB bz2 file holds all of Europe for one forecast hour, so a run is
downloaded once and every station reads its own cell from the same bytes
-- this lane is run-scoped, not station-scoped, and adding a station costs
nothing.  And the archive DOES NOT KEEP HISTORY: each cycle directory
holds only the newest run of that cycle, overwritten when the next day's
arrives, so the capture window is about 24 hours and a run missed is a run
permanently gone -- the same fragility the ICON global family already
taught this fleet.

The grid is regular lat-lon, but the cell is still found by eccodes'
nearest search shared with the CONUS lane: one decode path, one place for
a decode bug to be fixed.
"""

from __future__ import annotations

import bz2
from datetime import datetime, timezone
from typing import Any

import httpx

from .contracts import ContractError
from .jma_msm_archive import local_day_hours
from .noaa_conus_archive import nearest_point_kelvin

TRANSPORT_PROVIDER_ID = "DWD_OPENDATA_ICON_EU"
SYSTEM_ID = "DWD_ICON_EU_POINT"
# The same independence family as the ICON EPS systems, deliberately: this
# is the same ICON physics from the same centre in a deterministic
# configuration, and giving it its own family would let one model's
# physics spend two families' worth of cap.  MSM and the NBM got their own
# families because no seated family shares their physics; this one has
# three seated siblings.
FAMILY_ID = "DWD_ICON_EPS"
PROVIDER_ID = "DWD"
_BASE = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
# The roster this lane is ruled in at.  LTAC and EPWA cleared all three
# gates; the next five cleared CRPS and Brier and missed NLL, captured on
# the MSM precedent and free at the margin (the fields are whole-domain,
# so a seventh station costs one more nearest-cell decode).  EGLC and
# LIMC measured reversed and are absent.  It lives here, not in the
# capture script, because the board reports the ruling too -- and a board
# that infers the roster from which rows exist yet would call a station
# whose turn has not come "measured reversed".
CAPTURE_STATIONS = (
    ("LTAC", 40.128, 32.995, "Europe/Istanbul"),
    ("EPWA", 52.163, 20.961, "Europe/Warsaw"),
    ("EDDM", 48.348, 11.813, "Europe/Berlin"),
    ("EHAM", 52.315, 4.79, "Europe/Amsterdam"),
    ("EFHK", 60.327, 24.957, "Europe/Helsinki"),
    ("LFPB", 48.967, 2.428, "Europe/Paris"),
    ("LEMD", 40.466, -3.555, "Europe/Madrid"),
)
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_ATTEMPTS = 3

_client_instance: httpx.Client | None = None


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


def file_url(run_init: datetime, forecast_hour: int) -> str:
    stamp = run_init.astimezone(timezone.utc)
    return (
        f"{_BASE}/{stamp:%H}/t_2m/"
        f"icon-eu_europe_regular-lat-lon_single-level_"
        f"{stamp:%Y%m%d%H}_{forecast_hour:03d}_T_2M.grib2.bz2"
    )


def run_directory_url(run_init: datetime) -> str:
    stamp = run_init.astimezone(timezone.utc)
    return f"{_BASE}/{stamp:%H}/t_2m/"


def product_hours(run_init: datetime) -> list[int]:
    """Hourly to f78, three-hourly to f120."""

    return list(range(0, 79)) + list(range(81, 121, 3))


def _get(url: str) -> bytes | None:
    for attempt in range(_ATTEMPTS):
        try:
            response = _client().get(url)
        except httpx.HTTPError:
            continue
        if response.status_code == 404:
            return None
        if response.status_code == 200:
            return response.content
    return None


def fetch_run_day_maxima(
    *, run_init: datetime, targets: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Every target's forecast day-maximum from ONE download of the run.

    ``targets`` rows carry ``station_id``, ``latitude``, ``longitude``,
    ``event_date`` (a ``date``) and ``utc_offset_hours``; the same station
    may appear for two event days, since a run's hourly window reaches
    both tomorrow and the day after.  The union of every target's wanted
    hours is fetched once; each field is decoded once per target that
    wants it.  Result keyed by (station_id, event_date iso), each value
    the same shape as the per-station lanes' ``fetch_day_maximum``.
    """

    published = set(product_hours(run_init))
    wanted_by_target: dict[tuple[str, str], list[int]] = {}
    for target in targets:
        hours = local_day_hours(
            run_init=run_init, event_date=target["event_date"],
            utc_offset_hours=target["utc_offset_hours"],
        )
        key = (target["station_id"], target["event_date"].isoformat())
        wanted_by_target[key] = [
            hour for hour in hours if hour in published
        ]
    union = sorted({
        hour for hours in wanted_by_target.values() for hour in hours
    })
    fields: dict[int, bytes] = {}
    downloaded = 0
    for hour in union:
        raw = _get(file_url(run_init, hour))
        if raw is None:
            continue
        try:
            fields[hour] = bz2.decompress(raw)
        except OSError as exc:
            raise ContractError("icon_eu_bz2_undecodable") from exc
        downloaded += len(raw)
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for target in targets:
        key = (target["station_id"], target["event_date"].isoformat())
        wanted = wanted_by_target[key]
        hours_all = local_day_hours(
            run_init=run_init, event_date=target["event_date"],
            utc_offset_hours=target["utc_offset_hours"],
        )
        peak = hours_all[11:19]
        values: dict[int, float] = {}
        for hour in wanted:
            message = fields.get(hour)
            if message is None:
                continue
            kelvin, _distance = nearest_point_kelvin(
                message,
                latitude=target["latitude"],
                longitude=target["longitude"],
            )
            values[hour] = kelvin - 273.15
        complete = (
            bool(wanted)
            and len(values) == len(wanted)
            and len(peak) == 8
            and all(hour in values for hour in peak)
        )
        results[key] = {
            "complete": complete,
            "hours_wanted": wanted,
            "hours_covered": sorted(values),
            "forecast_tmax_c": max(values.values()) if values else None,
            # Charter 95 (P1 lane): the curve itself, forecast hour -> °C.
            "hourly_c": dict(sorted(values.items())),
            # The run's download is shared; attributing the full cost to
            # each station would overcount it sevenfold in any audit that
            # sums rows.  Attributed to the first station, zero after.
            "bytes_downloaded": (
                downloaded if target is targets[0] else 0
            ),
            "grid_geometry": "ICON-EU regular-lat-lon nearest-cell",
        }
    return results
