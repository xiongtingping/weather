"""Byte-range reads of the NOAA CONUS archives: HRRR and the NBM core.

Same admission as the JMA MSM lane and a simpler transport: both products
publish a ``.idx`` sidecar naming every field's byte offset, so one field is
one small ranged GET of a complete GRIB2 message -- no section walk, no
message reassembly.  What is NOT simpler is the grid: both run on Lambert
conformal projections, so the regular-lat-lon row/column arithmetic the MSM
module uses would land on the wrong cell.  The nearest cell comes from
eccodes' own nearest-neighbour search instead, which knows the projection.

Ruled in on 2026-08-28 with the 46-pair sweep: HRRR and the NBM cleared the
three admission gates at more roster stations than every other regional
source combined, and their previous-day runs named the settled bucket at up
to 74% against a served morning read in the 30s.  NBM is itself a
calibrated blend of other systems; it still gets its own independence
family (the weight machinery aggregates within a family before capping
across), and the blend's provenance is recorded here so the family-overlap
question is asked with open eyes when a seat race ever puts it beside the
globals it blends.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import httpx

from .contracts import ContractError
from .dwd_icon_eps_archive import _eccodes
from .jma_msm_archive import local_day_hours

TRANSPORTS = {
    "HRRR": "NOAA_HRRR_PDS_S3",
    "NBM": "NOAA_NBM_PDS_S3",
}
SYSTEM_IDS = {
    "HRRR": "NCEP_HRRR_POINT",
    "NBM": "NCEP_NBM_POINT",
}
FAMILY_IDS = {
    "HRRR": "NCEP_HRRR",
    "NBM": "NCEP_NBM",
}
PROVIDER_IDS = {
    "HRRR": "NCEP_NOAA",
    "NBM": "NCEP_NOAA",
}
_BUCKETS = {
    "HRRR": "https://noaa-hrrr-bdp-pds.s3.amazonaws.com",
    "NBM": "https://noaa-nbm-grib2-pds.s3.amazonaws.com",
}
# The idx line that names the 2m temperature field.  NBM carries a second
# TMP 2m line for the ensemble standard deviation; the trailing colon on
# the match keeps the mean and drops ":ens std dev".
_FIELD_MARK = ":TMP:2 m above ground:"
_TIMEOUT = httpx.Timeout(45.0, connect=15.0)
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


def run_directory_url(product: str, run_init: datetime) -> str:
    """The run's directory -- the citation root a stored row carries.

    A day-maximum row is built from up to twenty-four files, so no single
    file URL is the evidence; the directory plus the idx naming convention
    is what makes every byte re-fetchable.
    """

    stamp = run_init.astimezone(timezone.utc)
    if product == "HRRR":
        return f"{_BUCKETS['HRRR']}/hrrr.{stamp:%Y%m%d}/conus/"
    if product == "NBM":
        return f"{_BUCKETS['NBM']}/blend.{stamp:%Y%m%d}/{stamp:%H}/core/"
    raise ContractError("noaa_conus_product_unknown")


def file_url(product: str, run_init: datetime, forecast_hour: int) -> str:
    stamp = run_init.astimezone(timezone.utc)
    if product == "HRRR":
        return (
            f"{_BUCKETS['HRRR']}/hrrr.{stamp:%Y%m%d}/conus/"
            f"hrrr.t{stamp:%H}z.wrfsfcf{forecast_hour:02d}.grib2"
        )
    if product == "NBM":
        return (
            f"{_BUCKETS['NBM']}/blend.{stamp:%Y%m%d}/{stamp:%H}/core/"
            f"blend.t{stamp:%H}z.core.f{forecast_hour:03d}.co.grib2"
        )
    raise ContractError("noaa_conus_product_unknown")


def product_hours(product: str, run_init: datetime) -> list[int]:
    """Forecast hours this product actually publishes for this run.

    HRRR's synoptic cycles (00/06/12/18Z) run hourly to 48; its other
    cycles stop at 18 and are not used here.  The NBM core is hourly to 36
    and three-hourly beyond, which matters for a daily maximum: an
    afternoon peak falling in the three-hourly stretch is why the capture
    prefers the run whose hourly window covers the target day's afternoon.
    """

    if product == "HRRR":
        return list(range(0, 49))
    if product == "NBM":
        return list(range(1, 37)) + list(range(39, 49, 3))
    raise ContractError("noaa_conus_product_unknown")


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


def _range_get(url: str, start: int, end: int | None) -> bytes | None:
    header = f"bytes={start}-" if end is None else f"bytes={start}-{end - 1}"
    for attempt in range(_ATTEMPTS):
        try:
            response = _client().get(url, headers={"Range": header})
        except httpx.HTTPError:
            continue
        if response.status_code in (200, 206):
            return response.content
    return None


def _field_range(url: str) -> tuple[int, int | None] | None:
    """(start, end) byte range of the 2m temperature message, from the idx."""

    raw = _get(url + ".idx")
    if raw is None:
        return None
    lines = raw.decode("utf-8", "replace").splitlines()
    offsets = []
    hit = None
    for index, line in enumerate(lines):
        parts = line.split(":")
        if len(parts) < 3:
            continue
        try:
            offsets.append(int(parts[1]))
        except ValueError:
            offsets.append(-1)
        if _FIELD_MARK in line and not line.rstrip(":").endswith(
            "ens std dev"
        ):
            if hit is None:
                hit = index
    if hit is None:
        return None
    start = offsets[hit]
    end = None
    for later in offsets[hit + 1:]:
        if later > start:
            end = later
            break
    return (start, end)


@dataclass(frozen=True)
class ConusField:
    run_init_utc: str
    forecast_hour: int
    value_c: float
    distance_km: float
    source_url: str
    bytes_downloaded: int


def _bind_nearest(library: ctypes.CDLL) -> None:
    for name, argtypes, restype in (
        ("codes_grib_nearest_new",
         [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)], ctypes.c_void_p),
        ("codes_grib_nearest_find",
         [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_double, ctypes.c_double,
          ctypes.c_ulong, ctypes.POINTER(ctypes.c_double),
          ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
          ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_int),
          ctypes.POINTER(ctypes.c_size_t)], ctypes.c_int),
        ("codes_grib_nearest_delete",
         [ctypes.c_void_p], ctypes.c_int),
    ):
        fn = getattr(library, name)
        fn.argtypes = argtypes
        fn.restype = restype


def nearest_point_kelvin(
    message: bytes, *, latitude: float, longitude: float,
) -> tuple[float, float]:
    """(kelvin, distance_km) of the grid cell nearest the point.

    Grid-agnostic on purpose: eccodes' nearest search knows the
    projection, so the same call serves the CONUS Lambert grids and the
    ICON-EU regular grid alike.  Shared here rather than copied into each
    lane -- a decode bug fixed in one lane must not survive in another.
    """

    library = _eccodes()
    _bind_nearest(library)
    buffer = ctypes.create_string_buffer(message, len(message))
    handle = library.codes_handle_new_from_message(
        None, buffer, ctypes.c_size_t(len(message))
    )
    if not handle:
        raise ContractError("noaa_conus_message_unreadable")
    try:
        err = ctypes.c_int()
        nearest = library.codes_grib_nearest_new(handle, ctypes.byref(err))
        if not nearest or err.value != 0:
            raise ContractError("noaa_conus_nearest_unavailable")
        try:
            lats = (ctypes.c_double * 4)()
            lons = (ctypes.c_double * 4)()
            values = (ctypes.c_double * 4)()
            distances = (ctypes.c_double * 4)()
            indexes = (ctypes.c_int * 4)()
            length = ctypes.c_size_t(4)
            # Longitudes in these grids run 0..360.
            lon = longitude % 360.0
            if library.codes_grib_nearest_find(
                nearest, handle, ctypes.c_double(latitude),
                ctypes.c_double(lon), 0,
                lats, lons, values, distances, indexes,
                ctypes.byref(length),
            ) != 0:
                raise ContractError("noaa_conus_nearest_find_failed")
            best = min(range(int(length.value)), key=lambda i: distances[i])
            kelvin = float(values[best])
            distance = float(distances[best])
        finally:
            library.codes_grib_nearest_delete(nearest)
        if not 180.0 <= kelvin <= 340.0:
            raise ContractError("noaa_conus_value_out_of_range")
    finally:
        library.codes_handle_delete(handle)
    return kelvin, distance


def decode_field(
    message: bytes, *, latitude: float, longitude: float,
    run_init: datetime, forecast_hour: int, url: str,
) -> ConusField:
    kelvin, distance = nearest_point_kelvin(
        message, latitude=latitude, longitude=longitude
    )
    return ConusField(
        run_init_utc=run_init.astimezone(timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        forecast_hour=forecast_hour,
        value_c=kelvin - 273.15,
        distance_km=distance,
        source_url=url,
        bytes_downloaded=len(message),
    )


def _peak_hours(
    *, run_init: datetime, event_date: date, utc_offset_hours: int,
) -> list[int]:
    """Forecast hours covering local 11:00-18:59, where the maximum lives.

    The intersection of the local day with what a run publishes can be
    real but useless: a previous-day 00Z HRRR run reaches an Eastern
    station's morning and dies at f48 before its afternoon.  Every hour it
    does publish would then be fetched and the "daily maximum" would be a
    breakfast temperature.  Completeness is therefore judged against the
    afternoon window, not against whatever happened to be publishable.
    """

    hours = local_day_hours(
        run_init=run_init, event_date=event_date,
        utc_offset_hours=utc_offset_hours,
    )
    # local_day_hours orders by local hour from midnight, so index = local
    # hour whenever the run starts before the day does -- which every
    # caller here guarantees (a run issued after its day began is not a
    # forecast of it).
    return hours[11:19]


def run_covers_peak(
    *, product: str, run_init: datetime, event_date: date,
    utc_offset_hours: int,
) -> bool:
    """Would this run's published hours cover the afternoon?  Pure
    arithmetic, no network -- the capture loop uses it to skip runs that
    could only ever come back incomplete (NBM's three-hourly stretch,
    HRRR's f48 ceiling) instead of burning a fetch to learn it."""

    peak = _peak_hours(
        run_init=run_init, event_date=event_date,
        utc_offset_hours=utc_offset_hours,
    )
    published = set(product_hours(product, run_init))
    return len(peak) == 8 and all(hour in published for hour in peak)


def fetch_day_maximum(
    *, product: str, run_init: datetime, event_date: date,
    utc_offset_hours: int, latitude: float, longitude: float,
) -> dict[str, Any]:
    """The forecast daily maximum for one station's local day.

    One idx read and one ranged message per hour the local day wants and
    the product publishes.  A missing file is a gap the caller judges, not
    an error: NBM's three-hourly stretch and a not-yet-published tail both
    land here.
    """

    wanted_local = set(local_day_hours(
        run_init=run_init, event_date=event_date,
        utc_offset_hours=utc_offset_hours,
    ))
    published = set(product_hours(product, run_init))
    wanted = sorted(wanted_local & published)
    peak = _peak_hours(
        run_init=run_init, event_date=event_date,
        utc_offset_hours=utc_offset_hours,
    )
    values: dict[int, float] = {}
    downloaded = 0
    urls: list[str] = []
    for hour in wanted:
        url = file_url(product, run_init, hour)
        span = _field_range(url)
        if span is None:
            continue
        message = _range_get(url, span[0], span[1])
        if message is None:
            continue
        field = decode_field(
            message, latitude=latitude, longitude=longitude,
            run_init=run_init, forecast_hour=hour, url=url,
        )
        values[hour] = field.value_c
        downloaded += field.bytes_downloaded
        urls.append(url)
    complete = (
        bool(wanted)
        and len(values) == len(wanted)
        and len(peak) == 8
        and all(hour in values for hour in peak)
    )
    return {
        "complete": complete,
        "hours_wanted": wanted,
        "hours_covered": sorted(values),
        "forecast_tmax_c": max(values.values()) if values else None,
        "bytes_downloaded": downloaded,
        "source_urls": urls,
        # The Lambert grid has no row/column arithmetic worth citing; the
        # provenance that matters is which product and cycle.
        "grid_geometry": f"{product} lambert nearest-cell",
    }
