"""Official DWD ICON Global EPS native member archive for one station-day.

This is the missing first link for `DWD_ICON_GLOBAL_EPS`.  Every downstream
piece already exists -- arrival index, strict OOS, daily curve, training facts,
coverage matrix -- but they had no official capture to consume, so the system
survived only as an Open-Meteo republisher with no native run cycle and could
never qualify for a `d2`/`18Z` cross-ensemble candidate slot.

Three properties make ICON different from the GEFS/ECMWF collectors:

* One object holds all 40 members as separate GRIB messages keyed by
  ``perturbationNumber``.  GEFS publishes one object per member, so its
  per-member URL loop does not apply here; splitting messages in-process is
  also what keeps this from fabricating forty raw objects that DWD never
  published.
* The grid is ``unstructured_grid`` (icosahedral), so eccodes' nearest-point
  search over a regular lat/lon topology is unavailable.  Station indices are
  resolved once against the time-invariant ``clat``/``clon`` fields and cached.
* Objects are bzip2-compressed and carry no ``.idx`` sidecar, so a byte-range
  fetch of a single field is impossible: each lead is a whole ~36 MiB download.
"""

from __future__ import annotations

import bz2
import ctypes
import hashlib
import math
import sqlite3
import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from time import sleep as _sleep
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import httpx

from .contracts import ContractError, content_sha256
from .future_temperature_curve import (
    MIN_CURVE_POINTS,
    MIN_SAME_DAY_CURVE_POINTS,
    build_native_family_temperature_curve,
)
from .native_archive_lead import (
    capture_lead_and_run,
    capture_lead_bucket,
    run_cycle_label,
)
from .native_ensemble_store import _insert_content_addressed
from .resource_bounds import assert_process_memory_budget
from .ssot import native_ensemble_foundation_schema_status
from .p0_producer import bucket_intervals_from_registry_rows
from .station_registry import station_coordinates
from official_nwp_https_transport import official_nwp_proxy_url


SCHEMA = "dwd_icon_eps_official_historical_station_members_v1"
MULTISTATION_SCHEMA = (
    "dwd_icon_eps_official_historical_multistation_members_v1"
)
SYSTEM_ID = "DWD_ICON_GLOBAL_EPS"
INDEPENDENCE_FAMILY_ID = "DWD_ICON_EPS"
# 12Z, not the 18Z that ECMWF/GEFS use.  DWD publishes ``t_2m`` hourly out to
# +48h on its primary assimilation cycles (00Z/12Z) but only 6-hourly on the
# secondary ones (06Z/18Z) -- the same split ECCC makes by publishing no 06Z or
# 18Z at all.  Sitting on 18Z therefore bought six hours of freshness at the
# price of the only sampling grid coarse enough to matter: 6-hourly sampling
# injects 0.32C of within-station noise that no bias correction removes,
# against 0.15C at 3-hourly, while the three established families differ from
# each other by only 0.07-0.15C once per-station bias is out.  Re-capturing
# ECCC at 12Z against its 00Z archive showed those six hours are worth about
# 0.04C at a d2 lead, and even that favoured the *earlier* run.  Trading them
# for the primary cycle's hourly grid is the clear side of that bargain.
RUN_CYCLE = "12Z"
# P0 follows the freshest run, so this collector is asked for a shorter lead as
# the event day approaches.  The default keeps existing d2 callers unchanged.
SUPPORTED_LEAD_DAYS = (0, 1, 2)
DEFAULT_LEAD_DAYS = 2
# The cycle is not free to choose: it must leave enough forecast steps to cover
# the whole target local day, and that day starts at a different UTC hour for
# every station.  12Z works at d2 because the run precedes the day, but at d1
# it starts after the local day has already begun in the far east -- Auckland's
# day opens at 12Z the previous day -- so the grid comes up short and the
# capture is rejected outright.  Shorter leads take 00Z, which is DWD's other
# primary assimilation cycle and therefore the only other one carrying t_2m
# hourly rather than 6-hourly.
RUN_CYCLE_HOUR_BY_LEAD = {2: 12, 1: 0, 0: 0}
ARCHIVE_LEAD_BUCKET = f"d{DEFAULT_LEAD_DAYS}"
# Charter 192 (2026-09-21, user ruling "全球模型的 run 全采，按起报时刻滚动用，
# 谁新用谁"): every cycle DWD publishes is captured at d1 and d0, on top of --
# never instead of -- the lead's own cycle above.  The mapping above stays the
# run each lead is captured and served from by default; the other cycles are
# extra captures under their own extraction identity, trained in their own
# (hour bucket x cycle) scope, and served only once that scope has its own
# certified fit (``bayes_basket_target.follow_run``).  d2 is unchanged.
SUPPORTED_RUN_CYCLE_HOURS = (0, 6, 12, 18)
ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD = {2: (12,), 1: (0, 6, 12, 18), 0: (0, 6, 12, 18)}
# DWD's secondary cycles carry ``t_2m`` 6-hourly only (verified on the open-data
# listing 2026-09-21: 06Z and 18Z list steps 000, 006, ... 120 and nothing
# between).  They are read on that grid rather than not at all, under an
# extraction identity of their own: the 6-hour grid's peak miss (0.70C mean,
# 0.32C within-station spread, measured on the 00Z/12Z runs) is a different
# measurement convention from the 3-hour one, and the OOS scope key keeps the
# two from ever training together.
SECONDARY_CYCLE_STEP_HOURS = 6
SECONDARY_CYCLE_HOURS = (6, 18)
# The primary cycles' grid; ``LEAD_STEP_HOURS`` below says why it is 3h.
PRIMARY_CYCLE_STEP_HOURS = 3


def lead_step_hours(cycle_hour: int) -> int:
    """The sampling grid a cycle can be read on."""

    if int(cycle_hour) not in SUPPORTED_RUN_CYCLE_HOURS:
        raise ContractError("icon_eps_archive_run_cycle_unsupported")
    return (
        SECONDARY_CYCLE_STEP_HOURS
        if int(cycle_hour) in SECONDARY_CYCLE_HOURS
        else PRIMARY_CYCLE_STEP_HOURS
    )


def extraction_version_for_lead(
    lead_days: int, cycle_hour: int | None = None
) -> str:
    """Extraction identity is specific to the lead *and* to its cycle.

    A d1 row read the same way as a d2 row is still different evidence, drawn
    from a different run with different skill.  A shared version string would
    let the OOS scope check accept it against d2's contract and train the two
    together.

    The cycle is spelled out rather than fixed at ``12z`` because it moves with
    the lead.  A capture drawn from 00Z but labelled ``12z`` would be
    undetectable downstream: the label and the contract would agree with each
    other while both disagreed with the object that was downloaded.

    ``cycle_hour`` omitted is the lead's own cycle, and every string that
    existed before charter 192 is reproduced byte for byte -- the 00Z d1/d0
    and 12Z d2 histories keep their identity and nothing already trained is
    orphaned.  A secondary cycle names its 6-hour grid.
    """

    lead = int(lead_days)
    cycle = (
        RUN_CYCLE_HOUR_BY_LEAD[lead] if cycle_hour is None else int(cycle_hour)
    )
    if lead_step_hours(cycle) == SECONDARY_CYCLE_STEP_HOURS:
        return f"dwd_icon_global_eps_{cycle:02d}z_d{lead}_station_tmax_6h_v1"
    return (
        f"dwd_icon_global_eps_{cycle:02d}z_"
        f"d{lead}_station_tmax_3h_v3"
    )


EXTRACTION_VERSION = extraction_version_for_lead(DEFAULT_LEAD_DAYS)
BASE_URL = "https://opendata.dwd.de/weather/nwp/icon-eps/grib"
ECCODES_LIBRARY = Path("/opt/homebrew/lib/libeccodes.dylib")
EXPECTED_MEMBER_COUNT = 40
# ICON EPS publishes ``t_2m`` hourly out to +48h -- an earlier note here claimed
# a 6-hour grid, which was simply wrong, and the collector sampled every six
# hours on the strength of it.  Reading the same run at both steps showed what
# that cost: the 6-hour grid misses 0.70C of the daily peak on average and
# 2.07C at the worst station.  The mean miss is harmless -- it is a per-station
# constant and calibration absorbs it, which is why ECMWF/GEFS/ECCC can sample
# instantaneously at 3h without penalty.  What is not harmless is the spread of
# the miss *within* a station: 0.32C at 6h against 0.15C at 3h, and no bias term
# reaches that.  For scale, the three established families differ from each
# other by only 0.07-0.15C once per-station bias is removed, so the 6-hour grid
# was injecting more irreducible noise than the entire real skill gap between
# systems.
#
# 3h rather than 1h: it puts ICON on the same footing as the other three, while
# 1h would triple the download to ~1.5GiB and push a resident capture against
# its deadline for a further 0.15C.
LEAD_STEP_HOURS = PRIMARY_CYCLE_STEP_HOURS
# A 24-hour local day lands exactly 8 points on a 3-hour grid; a DST transition
# day is 23 or 25 hours long and yields 7 or 9.
MIN_LOCAL_DAY_HOURS = 7
MAX_LOCAL_DAY_HOURS = 9
GRID_POINT_COUNT = 737280
# One compressed lead is ~36 MiB.  A single station's local day needs up to
# nine, but the 45-station union spans enough timezones to reach ~15 leads at
# the 3-hour step, so the total budget is sized from the union rather than from
# one station's day.
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 1024 * 1024 * 1024
MAX_STATION_COUNT = 64
# Whole-object downloads, so concurrency is bounded by memory rather than by
# request latency: each in-flight object holds its compressed and decompressed
# form at once.
# Measured against opendata.dwd.de with 512 KiB range requests: 3 workers
# sustain 0.06 MB/s, 16 sustain 0.59, and 24 starts returning ConnectError --
# a connection ceiling rather than a rate limit, and the throughput does not
# improve past 16 anyway.
#
# The old value of three was the binding constraint on this collector: a single
# capture pulls ~543 MiB (15 leads at ~36 MiB), which needs two and a half
# hours at 0.06 MB/s and so could never finish inside the 780s daily deadline.
MAX_DOWNLOAD_WORKERS = 16
DEFAULT_GRID_CACHE = Path("data/runtime/official_nwp_sources/icon_eps/grid")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _eccodes() -> ctypes.CDLL:
    if not ECCODES_LIBRARY.is_file():
        raise ContractError("icon_eps_archive_eccodes_library_missing")
    library = ctypes.CDLL(str(ECCODES_LIBRARY))
    for name, argtypes, restype in (
        (
            "codes_handle_new_from_message",
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t],
            ctypes.c_void_p,
        ),
        (
            "codes_get_size",
            [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t)],
            ctypes.c_int,
        ),
        (
            "codes_get_double_array",
            [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_double),
                ctypes.POINTER(ctypes.c_size_t),
            ],
            ctypes.c_int,
        ),
        (
            "codes_get_long",
            [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_long)],
            ctypes.c_int,
        ),
        ("codes_handle_delete", [ctypes.c_void_p], ctypes.c_int),
    ):
        function = getattr(library, name)
        function.argtypes = argtypes
        function.restype = restype
    return library


def _split_grib_messages(raw: bytes) -> list[tuple[int, int]]:
    """Return (offset, length) for every GRIB2 message in one object."""

    messages: list[tuple[int, int]] = []
    offset = 0
    while True:
        start = raw.find(b"GRIB", offset)
        if start < 0:
            break
        if start + 16 > len(raw):
            raise ContractError("icon_eps_archive_grib_message_truncated")
        total = struct.unpack(">Q", raw[start + 8 : start + 16])[0]
        if total <= 0 or start + total > len(raw):
            raise ContractError("icon_eps_archive_grib_message_truncated")
        messages.append((start, total))
        offset = start + total
    if not messages:
        raise ContractError("icon_eps_archive_grib_message_missing")
    return messages


def _message_values(
    library: ctypes.CDLL, raw: bytes
) -> tuple[int, list[float]]:
    handle = library.codes_handle_new_from_message(None, raw, len(raw))
    if not handle:
        raise ContractError("icon_eps_archive_grib_handle_invalid")
    try:
        perturbation = ctypes.c_long()
        if library.codes_get_long(
            handle, b"perturbationNumber", ctypes.byref(perturbation)
        ) != 0:
            raise ContractError("icon_eps_archive_perturbation_number_missing")
        size = ctypes.c_size_t()
        library.codes_get_size(handle, b"values", ctypes.byref(size))
        if int(size.value) != GRID_POINT_COUNT:
            raise ContractError("icon_eps_archive_grid_point_count_invalid")
        values = (ctypes.c_double * size.value)()
        length = ctypes.c_size_t(size.value)
        if library.codes_get_double_array(
            handle, b"values", values, ctypes.byref(length)
        ) != 0:
            raise ContractError("icon_eps_archive_values_unavailable")
        return int(perturbation.value), list(values)
    finally:
        library.codes_handle_delete(handle)


def _single_field(library: ctypes.CDLL, raw: bytes) -> list[float]:
    messages = _split_grib_messages(raw)
    if len(messages) != 1:
        raise ContractError("icon_eps_archive_invariant_field_not_single")
    offset, length = messages[0]
    handle = library.codes_handle_new_from_message(
        None, raw[offset : offset + length], length
    )
    if not handle:
        raise ContractError("icon_eps_archive_grib_handle_invalid")
    try:
        size = ctypes.c_size_t()
        library.codes_get_size(handle, b"values", ctypes.byref(size))
        values = (ctypes.c_double * size.value)()
        got = ctypes.c_size_t(size.value)
        library.codes_get_double_array(
            handle, b"values", values, ctypes.byref(got)
        )
        return list(values)
    finally:
        library.codes_handle_delete(handle)


def _fetch_once(client: httpx.Client, url: str) -> bytes:
    with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise ContractError("icon_eps_archive_object_unavailable")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_OBJECT_BYTES:
                raise ContractError("icon_eps_archive_object_too_large")
            chunks.append(chunk)
    return b"".join(chunks)


# DWD's open-data server drops transfers mid-body often enough to lose whole
# runs.  2026-09-18: the 00Z run for the next day failed from its publication
# (~03:30Z) past 10:53Z with "peer closed connection without sending complete
# message body (received 21364407 bytes, expected 36253836)".  One capture
# pulls fifteen ~36 MB objects in parallel and a single cut transfer failed the
# whole family, so every P0 for that day went out on ICON's run of the day
# before; the per-date retry thirty minutes later re-downloaded all fifteen and
# met the same odds.  The objects are immutable, so a transport failure is
# retried on the same object in place.  A status the server did answer (404:
# not published yet) is not a transport failure and still fails at once.
FETCH_TRANSPORT_ATTEMPTS = 3
FETCH_TRANSPORT_BACKOFF_SEC = (2.0, 5.0)


def _fetch(
    client: httpx.Client,
    url: str,
    *,
    attempts: int = FETCH_TRANSPORT_ATTEMPTS,
    sleep: Any = _sleep,
) -> bytes:
    for attempt in range(max(1, int(attempts))):
        try:
            return _fetch_once(client, url)
        except httpx.TransportError:
            if attempt + 1 >= max(1, int(attempts)):
                raise
            sleep(FETCH_TRANSPORT_BACKOFF_SEC[
                min(attempt, len(FETCH_TRANSPORT_BACKOFF_SEC) - 1)
            ])
    raise AssertionError("unreachable")


def _grid_index(
    client: httpx.Client,
    library: ctypes.CDLL,
    *,
    run_day: str,
    cycle: str,
    station_ids: Sequence[str],
    cache_dir: Path,
) -> dict[str, int]:
    """Map each station to its nearest icosahedral grid point.

    The grid is time-invariant, so this is resolved once and cached: refetching
    1.5 MiB of coordinates for every capture would be pure waste, and the answer
    cannot change between runs.
    """

    cache_dir.mkdir(parents=True, exist_ok=True)
    coordinates: dict[str, list[float]] = {}
    for field in ("clat", "clon"):
        cached = cache_dir / f"{field}.f64"
        if cached.is_file():
            payload = cached.read_bytes()
            if len(payload) != GRID_POINT_COUNT * 8:
                raise ContractError("icon_eps_archive_grid_cache_invalid")
            coordinates[field] = list(
                struct.unpack(f">{GRID_POINT_COUNT}d", payload)
            )
            continue
        url = (
            f"{BASE_URL}/{cycle}/{field}/"
            f"icon-eps_global_icosahedral_time-invariant_"
            f"{run_day}{cycle}_{field}.grib2.bz2"
        )
        values = _single_field(library, bz2.decompress(_fetch(client, url)))
        if len(values) != GRID_POINT_COUNT:
            raise ContractError("icon_eps_archive_grid_point_count_invalid")
        cached.write_bytes(struct.pack(f">{GRID_POINT_COUNT}d", *values))
        coordinates[field] = values

    latitudes = coordinates["clat"]
    longitudes = coordinates["clon"]
    index: dict[str, int] = {}
    for station_id in station_ids:
        scope = station_coordinates(station_id)
        latitude = math.radians(float(scope["latitude"]))
        longitude = math.radians(float(scope["longitude"]))
        cosine = math.cos(latitude)
        best_distance = None
        best_point = -1
        for point in range(GRID_POINT_COUNT):
            delta_lat = math.radians(latitudes[point]) - latitude
            delta_lon = math.radians(longitudes[point]) - longitude
            if delta_lon > math.pi:
                delta_lon -= 2.0 * math.pi
            elif delta_lon < -math.pi:
                delta_lon += 2.0 * math.pi
            distance = delta_lat * delta_lat + (delta_lon * cosine) ** 2
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_point = point
        if best_point < 0:
            raise ContractError("icon_eps_archive_station_grid_point_missing")
        index[station_id] = best_point
    return index


def _forecast_hours(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init_utc: datetime,
) -> list[int]:
    target = date.fromisoformat(event_date)
    zone = ZoneInfo(settlement_timezone)
    hours = [
        forecast_hour
        for forecast_hour in range(0, 121, LEAD_STEP_HOURS)
        if (
            run_init_utc + timedelta(hours=forecast_hour)
        ).astimezone(zone).date()
        == target
    ]
    # A full local day is eight periods, and that is the right bar for d2 and
    # d1: their run precedes the day, so a short grid means it is broken.
    #
    # It is the wrong bar for d0.  That run is issued after the local day has
    # begun and cannot describe hours that already happened -- nor does it need
    # to, since those are settled observation by then.  Its value is the fresher
    # initial state, not sampling density: a same-day run has assimilated that
    # morning's observations and beats a two-day-old d2 even with few points.
    run_day = run_init_utc.astimezone(timezone.utc).date()
    minimum = (
        1 if (target - run_day).days == 0 else MIN_LOCAL_DAY_HOURS
    )
    if not minimum <= len(hours) <= MAX_LOCAL_DAY_HOURS:
        raise ContractError("icon_eps_archive_local_day_grid_incomplete")
    return hours


def _same_local_day_run(
    *, event_date: str, settlement_timezone: str, run_init_utc: datetime
) -> bool:
    """A run issued on the event's UTC date or after its local day began.

    The same test the capture's coverability filter applies: such a run cannot
    describe the hours before it, so it is held to the same-day floor.
    """

    target = date.fromisoformat(event_date)
    run_start = run_init_utc.astimezone(timezone.utc)
    day_start = datetime.combine(
        target, time(0), tzinfo=ZoneInfo(settlement_timezone)
    ).astimezone(timezone.utc)
    return run_start.date() == target or run_start >= day_start


def follow_forecast_hours(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init_utc: datetime,
) -> list[int] | None:
    """Charter 192: the steps a followed run seals for one station, or None.

    Non-raising, so a follow capture can leave out the stations its run cannot
    cover instead of failing whole -- the capture is all-or-nothing across its
    stations, and a same-day 12Z or 18Z run structurally reaches only the
    western part of the network.  None means "keep the run this station
    already has", which is what the station would be serving anyway.

    The primary cycles keep ``_forecast_hours``'s bar exactly.  The secondary
    cycles are read on their 6-hour grid: a run that precedes the local day
    must seal the whole of it (four points; a DST day may give three or five
    and the curve seal wants four), and one issued inside the day seals what
    is left of it with the same-day curve floor.
    """

    run_start = run_init_utc.astimezone(timezone.utc)
    step = lead_step_hours(run_start.hour)
    target = date.fromisoformat(event_date)
    zone = ZoneInfo(settlement_timezone)
    hours = [
        forecast_hour
        for forecast_hour in range(0, 121, step)
        if (run_start + timedelta(hours=forecast_hour)).astimezone(
            zone
        ).date() == target
    ]
    same_day = _same_local_day_run(
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        run_init_utc=run_start,
    )
    if step == LEAD_STEP_HOURS:
        utc_same_day = run_start.date() == target
        minimum = (
            MIN_SAME_DAY_CURVE_POINTS if utc_same_day else MIN_LOCAL_DAY_HOURS
        )
        maximum = MAX_LOCAL_DAY_HOURS
    else:
        minimum = MIN_SAME_DAY_CURVE_POINTS if same_day else MIN_CURVE_POINTS
        maximum = 24 // step + 1
    if not minimum <= len(hours) <= maximum:
        return None
    return hours


def capture_icon_eps_station_days(
    *,
    event_date: str,
    station_scopes: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    timeout_seconds: float = 45.0,
    grid_cache_dir: Path | None = None,
    lead_days: int = DEFAULT_LEAD_DAYS,
    run_cycle_hour: int | None = None,
    download_workers: int | None = None,
) -> dict[str, Any]:
    """Capture one official ICON EPS cohort at the requested lead.

    ``run_cycle_hour`` omitted (or equal to the lead's own cycle) is the
    capture as it always was.  Any other published cycle is a charter 192
    follow capture: its own extraction identity, its own sampling grid, and a
    station set the caller has already restricted to what the run can cover.
    """

    if lead_days not in SUPPORTED_LEAD_DAYS:
        raise ContractError("icon_eps_archive_lead_days_invalid")
    cycle_hour = (
        RUN_CYCLE_HOUR_BY_LEAD[lead_days]
        if run_cycle_hour is None
        else int(run_cycle_hour)
    )
    if cycle_hour not in ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD[lead_days]:
        raise ContractError("icon_eps_archive_run_cycle_unsupported")
    follow = cycle_hour != RUN_CYCLE_HOUR_BY_LEAD[lead_days]

    target = date.fromisoformat(event_date)
    run_init = datetime.combine(
        target - timedelta(days=lead_days),
        time(hour=cycle_hour),
        tzinfo=timezone.utc,
    )
    query_at = datetime.fromisoformat(
        archive_query_at_utc.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if query_at <= run_init:
        raise ContractError("icon_eps_archive_scope_invalid")
    if not 1 <= len(station_scopes) <= MAX_STATION_COUNT:
        raise ContractError("icon_eps_archive_scope_invalid")

    scopes: dict[str, dict[str, Any]] = {}
    for raw_scope in station_scopes:
        station_id = str(raw_scope.get("station_id") or "").strip().upper()
        timezone_name = str(
            raw_scope.get("settlement_timezone") or ""
        ).strip()
        if not station_id or not timezone_name or station_id in scopes:
            raise ContractError("icon_eps_archive_scope_invalid")
        registry_rows = raw_scope.get("registry_rows")
        if not isinstance(registry_rows, list) or not registry_rows:
            raise ContractError("icon_eps_archive_registry_rows_required")
        if follow:
            hours = follow_forecast_hours(
                event_date=event_date,
                settlement_timezone=timezone_name,
                run_init_utc=run_init,
            )
            if hours is None:
                # The caller filters with the same function; a station that
                # reaches here uncovered is a caller defect, not a short run.
                raise ContractError(
                    "icon_eps_archive_local_day_grid_incomplete"
                )
        else:
            hours = _forecast_hours(
                event_date=event_date,
                settlement_timezone=timezone_name,
                run_init_utc=run_init,
            )
        scopes[station_id] = {
            "station_id": station_id,
            "settlement_timezone": timezone_name,
            "registry_rows": registry_rows,
            "hours": hours,
        }

    run_day = run_init.strftime("%Y%m%d")
    cycle = run_init.strftime("%H")
    union_hours = sorted({
        hour for scope in scopes.values() for hour in scope["hours"]
    })
    library = _eccodes()
    proxy = official_nwp_proxy_url()
    total_bytes = 0
    values_by_station: dict[str, dict[str, dict[int, float]]] = {
        station_id: {} for station_id in scopes
    }
    # One receipt per published DWD object, not per member: forty members
    # arrive inside a single object and inventing forty receipts would claim
    # provenance DWD never issued.
    object_receipts: list[dict[str, Any]] = []

    with httpx.Client(
        timeout=httpx.Timeout(timeout_seconds),
        proxy=proxy or None,
        follow_redirects=False,
        headers={"User-Agent": "weather-icon-eps-history/1"},
    ) as client:
        grid = _grid_index(
            client,
            library,
            run_day=run_day,
            cycle=cycle,
            station_ids=sorted(scopes),
            cache_dir=Path(grid_cache_dir or DEFAULT_GRID_CACHE),
        )

        def load(forecast_hour: int) -> tuple[int, bytes]:
            url = (
                f"{BASE_URL}/{cycle}/t_2m/"
                f"icon-eps_global_icosahedral_single-level_"
                f"{run_day}{cycle}_{forecast_hour:03d}_t_2m.grib2.bz2"
            )
            return forecast_hour, _fetch(client, url)

        with ThreadPoolExecutor(
            # Charter 192: a follow capture may ask with fewer connections,
            # so it can share DWD's ceiling with a pass's own ICON fetch.
            max_workers=(
                MAX_DOWNLOAD_WORKERS
                if download_workers is None
                else max(1, min(int(download_workers), MAX_DOWNLOAD_WORKERS))
            ),
            thread_name_prefix="icon-eps-download",
        ) as executor:
            for forecast_hour, compressed in executor.map(load, union_hours):
                total_bytes += len(compressed)
                if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                    raise ContractError("icon_eps_archive_response_budget_exceeded")
                object_receipts.append({
                    "forecast_hour": forecast_hour,
                    "object_sha256": hashlib.sha256(compressed).hexdigest(),
                    "compressed_bytes": len(compressed),
                })
                raw = bz2.decompress(compressed)
                del compressed
                seen: set[int] = set()
                for offset, length in _split_grib_messages(raw):
                    perturbation, values = _message_values(
                        library, raw[offset : offset + length]
                    )
                    if (
                        not 1 <= perturbation <= EXPECTED_MEMBER_COUNT
                        or perturbation in seen
                    ):
                        raise ContractError(
                            "icon_eps_archive_perturbation_number_invalid"
                        )
                    seen.add(perturbation)
                    member_id = f"member_{perturbation:02d}"
                    for station_id, point in grid.items():
                        if forecast_hour not in scopes[station_id]["hours"]:
                            continue
                        kelvin = values[point]
                        if not math.isfinite(kelvin):
                            raise ContractError(
                                "icon_eps_archive_temperature_invalid"
                            )
                        values_by_station[station_id].setdefault(
                            member_id, {}
                        )[forecast_hour] = kelvin - 273.15
                    del values
                if len(seen) != EXPECTED_MEMBER_COUNT:
                    raise ContractError("icon_eps_archive_member_cohort_incomplete")
                del raw
                assert_process_memory_budget()

    object_receipts.sort(key=lambda row: int(row["forecast_hour"]))
    object_receipt_sha = content_sha256(object_receipts)
    captures: list[dict[str, Any]] = []
    for station_id in sorted(scopes):
        scope = scopes[station_id]
        by_member = values_by_station[station_id]
        if len(by_member) != EXPECTED_MEMBER_COUNT:
            raise ContractError("icon_eps_archive_member_cohort_incomplete")
        members = []
        for member_id in sorted(by_member):
            hours = by_member[member_id]
            if set(hours) != set(scope["hours"]):
                raise ContractError("icon_eps_archive_member_curve_incomplete")
            members.append({
                "member_id": member_id,
                "member_role": "PERTURBED",
                "raw_tmax_c": max(hours.values()),
            })
        intervals = bucket_intervals_from_registry_rows(
            scope["registry_rows"]
        )
        frozen_intervals = {
            bucket: [
                None if lower is None else float(lower),
                None if upper is None else float(upper),
            ]
            for bucket, (lower, upper) in intervals.items()
        }
        # The same field decode the members came from, sealed as the family
        # mean curve the P0 root needs before its conditional future-path
        # evidence can run.  ICON was the only family omitting this, and the
        # root-curve builder is all-or-nothing across seated systems, so every
        # station-day ICON sat on served P1 without a soft likelihood layer.
        family_curve = build_native_family_temperature_curve(
            # A same-day run cannot describe hours that have already
            # happened, so its curve is legitimately shorter than a run that
            # precedes the day.
            min_points=(
                MIN_SAME_DAY_CURVE_POINTS
                if (
                    target - run_init.astimezone(timezone.utc).date()
                ).days == 0
                # Charter 192: a 6-hour follow read inside the local day
                # seals the remainder at the same-day floor, the bar
                # ``follow_forecast_hours`` admitted it on.
                or (
                    follow
                    and lead_step_hours(cycle_hour) != LEAD_STEP_HOURS
                    and _same_local_day_run(
                        event_date=event_date,
                        settlement_timezone=str(
                            scope["settlement_timezone"]
                        ),
                        run_init_utc=run_init,
                    )
                )
                else MIN_CURVE_POINTS
            ),
            payload={
                "station_id": station_id,
                "event_date": event_date,
                "settlement_timezone": scope["settlement_timezone"],
                "ensemble_system_id": SYSTEM_ID,
                "independence_family_id": INDEPENDENCE_FAMILY_ID,
                "available_at_utc": archive_query_at_utc,
                "source_field_receipt_set_sha256": object_receipt_sha,
                "points": [
                    {
                        "valid_time_utc": (
                            run_init + timedelta(hours=forecast_hour)
                        ).isoformat().replace("+00:00", "Z"),
                        "temperature_c": sum(
                            by_member[member_id][forecast_hour]
                            for member_id in by_member
                        ) / len(by_member),
                    }
                    for forecast_hour in scope["hours"]
                ],
            },
        )
        identity = {
            "schema": SCHEMA,
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": scope["settlement_timezone"],
            "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
            "run_cycle": run_cycle_label(run_init),
            # Hours ahead of this station's own local day, not the calendar
            # ``d`` label the collector was asked for.  The two disagree: one
            # run reaches a UTC+8 and a UTC-4 station half a day apart, and the
            # calendar label called both the same lead.
            "archive_lead_bucket": capture_lead_bucket(
                event_date=event_date,
                settlement_timezone=str(scope["settlement_timezone"]),
                run_init=run_init,
            ),
            "forecast_hours": scope["hours"],
            "ensemble_system_id": SYSTEM_ID,
            "independence_family_id": INDEPENDENCE_FAMILY_ID,
            "member_count": len(members),
            "member_availability_contract": (
                "OFFICIAL_ICON_EPS_PERTURBED_1_TO_40"
            ),
            "members": members,
            "member_set_sha256": content_sha256([
                {
                    "member_id": row["member_id"],
                    "member_role": row["member_role"],
                    "raw_tmax_c": row["raw_tmax_c"],
                }
                for row in members
            ]),
            "family_temperature_curve": family_curve,
            # The ingest path stores a member run *and* the bucket projection
            # it implies, so the capture has to freeze the market's bucket
            # geometry alongside the raw members.  Deriving it later from a
            # newer registry would silently re-bucket a frozen fact.
            "bucket_intervals": frozen_intervals,
            "bucket_contract_sha256": content_sha256(frozen_intervals),
            "raw_bucket_probabilities": {
                bucket: sum(
                    (
                        bounds[0] is None
                        or row["raw_tmax_c"] >= float(bounds[0])
                    )
                    and (
                        bounds[1] is None
                        or row["raw_tmax_c"] < float(bounds[1])
                    )
                    for row in members
                )
                / len(members)
                for bucket, bounds in frozen_intervals.items()
            },
            "source_class": "OFFICIAL_NATIVE_ARCHIVE",
            "training_only": True,
            "holdout_eligible": False,
            "field_receipt_set_sha256": object_receipt_sha,
            "archive_query_at_utc": archive_query_at_utc,
            "source_prefix": f"{BASE_URL}/{cycle}/t_2m/",
            "extraction_version": extraction_version_for_lead(
                lead_days, cycle_hour
            ),
            "grid_point_index": grid[station_id],
        }
        captures.append({
            **identity,
            "content_sha256": content_sha256(identity),
        })

    identity = {
        "schema": MULTISTATION_SCHEMA,
        "event_date": event_date,
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "run_cycle": run_cycle_label(run_init),
        "archive_lead_bucket": f"d{lead_days}",
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "member_count": EXPECTED_MEMBER_COUNT,
        "member_availability_contract": (
            "OFFICIAL_ICON_EPS_PERTURBED_1_TO_40"
        ),
        "station_count": len(captures),
        # The retained-archive reuse check matches on the exact station set, so
        # an archive that does not name its stations can never satisfy it: this
        # family was the only one of the four omitting the field, and it was
        # therefore re-downloaded in full on every capture while the other
        # three were served from the retained copy.  ~517MiB a pass, for bytes
        # already on disk.
        "station_ids": [
            str(capture["station_id"]) for capture in captures
        ],
        "union_forecast_hours": union_hours,
        "archive_query_at_utc": archive_query_at_utc,
        "object_receipt_count": len(object_receipts),
        "object_receipt_set_sha256": object_receipt_sha,
        "training_only": True,
        "holdout_eligible": False,
        "single_download_per_global_field": True,
        "extraction_version": extraction_version_for_lead(
            lead_days, cycle_hour
        ),
        "source_prefix": f"{BASE_URL}/{cycle}/t_2m/",
        "source_class": "OFFICIAL_NATIVE_ARCHIVE",
        "captures": captures,
    }
    # Every field except the digest itself belongs to the identity.  The ingest
    # side recomputes the hash over "everything but content_sha256", so a field
    # emitted outside `identity` here would make the archive fail its own
    # content-address check the moment it is read back.
    identity = {
        **identity,
        "total_response_bytes": total_bytes,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def ingest_icon_eps_station_day(
    conn: sqlite3.Connection,
    capture: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one frozen station-day member cohort, or replay it unchanged."""

    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    # Lead, cycle and run come from the capture, never from the module
    # constants: reading the constants would relabel a shorter-lead capture as
    # d2 at the moment it lands, where the mislabelling becomes permanent.
    capture_lead_days, capture_lead_bucket, _run_init, capture_run_cycle = (
        capture_lead_and_run(
            capture,
            supported_lead_days=SUPPORTED_LEAD_DAYS,
            default_lead_days=DEFAULT_LEAD_DAYS,
            error_code="icon_eps_archive_lead_days_invalid",
        )
    )
    # Charter 192: the cycle the capture came from, too -- a 12Z d1 follow
    # stamped with the lead's 00Z identity would train inside the 00Z scope.
    capture_extraction_version = extraction_version_for_lead(
        capture_lead_days, int(capture_run_cycle[:2])
    )
    identity = {
        key: value
        for key, value in capture.items()
        if key != "content_sha256"
    }
    members = capture.get("members")
    if (
        capture.get("schema") != SCHEMA
        or capture.get("source_class") != "OFFICIAL_NATIVE_ARCHIVE"
        or capture.get("training_only") is not True
        or capture.get("holdout_eligible") is not False
        or capture.get("content_sha256") != content_sha256(identity)
        or not isinstance(members, list)
        or len(members) != EXPECTED_MEMBER_COUNT
    ):
        raise ContractError("icon_eps_archive_capture_invalid")
    system = conn.execute(
        "SELECT provider_id,independence_family_id,expected_member_count "
        "FROM calibration_native_ensemble_system "
        "WHERE ensemble_system_id=?",
        (SYSTEM_ID,),
    ).fetchone()
    if (
        system is None
        or str(system[0]) != "DWD"
        or str(system[1]) != INDEPENDENCE_FAMILY_ID
        or int(system[2]) != EXPECTED_MEMBER_COUNT
    ):
        raise ContractError("icon_eps_archive_system_registration_invalid")
    station = str(capture["station_id"])
    event_date = str(capture["event_date"])
    existing = conn.execute(
        "SELECT member_id,member_role,raw_tmax_c "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE ensemble_system_id=? AND station_id=? "
        "AND target_local_date=? AND archive_lead_bucket=? "
        "AND run_cycle=? AND extraction_version=? "
        "ORDER BY member_id",
        (
            SYSTEM_ID,
            station,
            event_date,
            capture_lead_bucket,
            capture_run_cycle,
            capture_extraction_version,
        ),
    ).fetchall()
    expected = sorted(
        (str(row["member_id"]), str(row["member_role"]), float(row["raw_tmax_c"]))
        for row in members
    )
    if existing:
        # A frozen fact may be replayed but never rewritten: a differing value
        # for the same scope means two incompatible truths, not an update.
        if len(existing) != EXPECTED_MEMBER_COUNT or any(
            str(actual[0]) != wanted[0]
            or str(actual[1]) != wanted[1]
            or abs(float(actual[2]) - wanted[2]) > 1e-9
            for actual, wanted in zip(existing, expected)
        ):
            raise ContractError("icon_eps_archive_frozen_fact_conflict")
        return {
            "state": "REPLAY",
            "member_run_wrote_count": 0,
        }
    member_writes = 0
    for row in members:
        member_identity = {
            "ensemble_system_id": SYSTEM_ID,
            "station_id": station,
            "target_local_date": event_date,
            "archive_lead_bucket": capture_lead_bucket,
            "run_cycle": capture_run_cycle,
            "member_id": str(row["member_id"]),
            "member_role": str(row["member_role"]),
            "raw_tmax_c": float(row["raw_tmax_c"]),
            "settlement_definition": "official_daily_tmax_c",
            "extraction_version": capture_extraction_version,
            "provider_id": str(system[0]),
            "transport_provider_id": "DWD_OPENDATA_ICON_EPS",
            "native_run_init_utc": str(capture["run_init_utc"]),
            "offline_oos_eligible": 1,
            "archive_query_at_utc": str(capture["archive_query_at_utc"]),
            "source_url": str(capture["source_prefix"]),
            "raw_object_ref": (
                "dwd_icon_eps_object_receipt_set_sha256:"
                + str(capture["field_receipt_set_sha256"])
            ),
            "source_object_sha256": str(capture["field_receipt_set_sha256"]),
            "qc_status": "ACCEPTED",
            "authority_scope": "CALIBRATION_TRAIN_ONLY",
            "holdout_eligible": 0,
        }
        row_sha = content_sha256(member_identity)
        member_writes += _insert_content_addressed(
            conn,
            table="calibration_native_ensemble_historical_member_run",
            primary_key="historical_member_run_id",
            row={
                "historical_member_run_id": (
                    "dwd-icon-eps-history-member-" + row_sha[:24]
                ),
                **member_identity,
                "content_sha256": row_sha,
            },
        )
    return {
        "state": "COMMITTED",
        "member_run_wrote_count": member_writes,
    }


def ingest_icon_eps_station_days(
    conn: sqlite3.Connection,
    archive: Mapping[str, Any],
) -> dict[str, Any]:
    """Ingest every station-day cohort from one multistation archive."""

    identity = {
        key: value
        for key, value in archive.items()
        if key != "content_sha256"
    }
    captures = archive.get("captures")
    if (
        archive.get("schema") != MULTISTATION_SCHEMA
        or archive.get("training_only") is not True
        or archive.get("holdout_eligible") is not False
        or archive.get("single_download_per_global_field") is not True
        or archive.get("content_sha256") != content_sha256(identity)
        or not isinstance(captures, list)
        or len(captures) != int(archive.get("station_count") or 0)
    ):
        raise ContractError("icon_eps_archive_multistation_invalid")
    results = [
        {
            "station_id": str(capture["station_id"]),
            **ingest_icon_eps_station_day(conn, capture),
        }
        for capture in captures
    ]
    return {
        "state": (
            "REPLAY"
            if all(row["state"] == "REPLAY" for row in results)
            else "COMMITTED"
        ),
        "station_count": len(results),
        "member_run_wrote_count": sum(
            int(row["member_run_wrote_count"]) for row in results
        ),
        "station_results": results,
    }


__all__ = [
    "ARCHIVE_LEAD_BUCKET",
    "ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD",
    "EXPECTED_MEMBER_COUNT",
    "EXTRACTION_VERSION",
    "INDEPENDENCE_FAMILY_ID",
    "MULTISTATION_SCHEMA",
    "RUN_CYCLE",
    "SCHEMA",
    "SYSTEM_ID",
    "SUPPORTED_RUN_CYCLE_HOURS",
    "capture_icon_eps_station_days",
    "follow_forecast_hours",
    "ingest_icon_eps_station_day",
    "ingest_icon_eps_station_days",
    "lead_step_hours",
]
