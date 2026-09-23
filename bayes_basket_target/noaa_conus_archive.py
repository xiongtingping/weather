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
from .pass_deadline import PassDeadlineExceeded, deadline_spent
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
_US_STATIONS = (
    ("KATL", 33.62972, -84.44223, "America/New_York"),
    ("KAUS", 30.1831, -97.68063, "America/Chicago"),
    ("KBKF", 39.713, -104.758, "America/Denver"),
    ("KDAL", 32.83836, -96.83584, "America/Chicago"),
    ("KHOU", 29.64582, -95.28214, "America/Chicago"),
    ("KLAX", 33.93817, -118.3866, "America/Los_Angeles"),
    ("KLGA", 40.77945, -73.88027, "America/New_York"),
    ("KMIA", 25.78806, -80.31692, "America/New_York"),
    ("KORD", 41.96017, -87.93161, "America/Chicago"),
    ("KSEA", 47.44467, -122.31442, "America/Los_Angeles"),
    ("KSFO", 37.61961, -122.36561, "America/Los_Angeles"),
)
# Not in the United States, and inside the CONUS grid anyway: the HRRR and
# NBM domains both run well north of the border, and Toronto is the only
# station in the live roster that sits under them without being in
# ``_US_STATIONS``.  Kept in its own tuple so the name of the one above stays
# true.  Whether the transport actually returns a local day for it is a
# question for the transport, not for this list -- the capture answers it, and
# the serving gate rules on admission separately.
_IN_DOMAIN_NON_US_STATIONS = (
    ("CYYZ", 43.679, -79.629, "America/Toronto"),
)
# The roster each product is captured at, which is the sweep's verdict and
# not a progress report.  It lives here rather than in the capture script
# because two readers need the same answer: the capture, and the board
# that reports which pairs were ruled in -- and a board that infers the
# roster from which rows exist yet calls a station that has not had its
# turn "measured reversed", which is a different claim entirely.
CAPTURE_STATIONS = {
    # KLAX/HRRR measured reversed in the 46-pair sweep (the served
    # posterior beat it); it stays in NBM, where it did not.
    "HRRR": (
        tuple(s for s in _US_STATIONS if s[0] != "KLAX")
        + _IN_DOMAIN_NON_US_STATIONS
    ),
    "NBM": _US_STATIONS + _IN_DOMAIN_NON_US_STATIONS,
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


class NoaaConusPassDeadlineExceeded(PassDeadlineExceeded):
    """The pass ran out of wall clock inside a run's fetch."""


def _refuse_if_spent() -> None:
    # Before each attempt, never mid-response.  One HRRR run is 49 forecast
    # hours of an idx read and a ranged read each, every one allowed three
    # attempts against a 45-second between-bytes timeout, and the lane's
    # seconds budget was checked only between runs -- so one slow run could
    # carry the pass past its 1,800-second subprocess budget.  Measured
    # 2026-09-14: ``timeout_killed`` at 03:15Z, nothing written, the exclusive
    # SSOT domain held for the half hour.  The MSM lane closed the same gap
    # on 2026-09-11; this lane had been left on the bet.
    if deadline_spent():
        raise NoaaConusPassDeadlineExceeded(
            "noaa_conus_archive_pass_deadline_exceeded"
        )


def _get(url: str) -> bytes | None:
    for attempt in range(_ATTEMPTS):
        _refuse_if_spent()
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
        _refuse_if_spent()
        try:
            response = _client().get(url, headers={"Range": header})
        except httpx.HTTPError:
            continue
        if response.status_code in (200, 206):
            return response.content
    return None


def _field_range(url: str) -> tuple[int, int | None] | None:
    """(start, end) byte range of the 2m temperature message, from the idx."""

    return _mark_range(url, _FIELD_MARK)


def _mark_range(url: str, mark: str) -> tuple[int, int | None] | None:
    """(start, end) byte range of the first message whose idx line has ``mark``."""

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
        if mark in line and not line.rstrip(":").endswith(
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


# How a CONUS grid's ``values`` array is ordered against its
# latitudes/longitudes iterator, for the one layout this reader has been
# checked against.  Lambert conformal (template 30), no bitmap, i ascending,
# j ascending; ``alternativeRowScanning`` either way.  Anything else is read
# by the eccodes search, unchanged.
_LAMBERT_CONFORMAL_TEMPLATE = 30
_GRID_LAYOUT_KEYS = (
    b"gridDefinitionTemplateNumber", b"iScansNegatively", b"jScansPositively",
    b"bitmapPresent", b"alternativeRowScanning", b"Nx",
)


def _values_index(nearest: int, nx: int, alternate: bool) -> int:
    """Map an index in iterator (geographic) order to one in ``values``.

    With ``alternativeRowScanning=1`` the odd rows of ``values`` run the
    other way: measured on the NBM, flipping odd rows made 72 of 72 values
    bit-identical to the eccodes search, and flipping none or the even rows
    did not.
    """

    row, column = divmod(int(nearest), int(nx))
    if alternate and row % 2 == 1:
        return row * nx + (nx - 1 - column)
    return int(nearest)


class GridPointReader:
    """Many stations out of many messages that share one CONUS grid.

    ``nearest_point_kelvin`` is correct and it is slow: eccodes searches the
    whole grid for every point, and on a Lambert grid that cost does not
    amortise.  Measured 2026-09-16 on real messages -- NBM 1.24s a station,
    HRRR 0.46s -- and a P1 run decodes every (station, hour) it wants, so one
    NBM run of 12 stations over ~40 hours spent about 600 seconds here.  That
    ran past its 240s phase clock (the downloads check the clock, the decode
    did not) and left the member-run loop 0 of 6 attempts.

    Three cheaper reads were measured and refused before this one:

      * one handle with SAME_GRID|SAME_DATA -- identical values, no faster;
      * codes_grib_nearest_find_multiple -- identical values, no faster;
      * eccodes' own nearest ``indexes`` read back out of ``values`` -- the
        WRONG cell at 5 of 12 NBM stations, by up to 10.6 K.

    The last one is why this reader computes its own index from the
    latitudes/longitudes arrays and maps it into ``values`` itself: the NBM
    is ``alternativeRowScanning=1``, and its odd rows run the other way in
    ``values`` than in the iterator.  With that mapping, 44 of 44 HRRR and 72
    of 72 NBM (station, hour) values were bit-identical to
    ``nearest_point_kelvin`` -- and anything that holds that on the day it
    was measured is not enough to trust on a day NOAA changes a grid.  So it
    is not trusted: the first message of every grid in a pass re-derives
    every station through ``nearest_point_kelvin`` and keeps an index only
    where the two agree exactly.  A station that disagrees, or a layout this
    reader has never been checked against, is read the slow way for the rest
    of the pass.  The fast path can therefore never return a value the
    eccodes search would not have.
    """

    def __init__(self, points: "list[tuple[float, float]]") -> None:
        self._points = [(float(lat), float(lon)) for lat, lon in points]
        # grid identity -> per-point index into ``values``, None = slow path
        self._indexes: dict[str, list[int | None]] = {}
        self.slow_points = 0

    @staticmethod
    def _long(library: ctypes.CDLL, handle: Any, key: bytes) -> int | None:
        value = ctypes.c_long()
        if library.codes_get_long(handle, key, ctypes.byref(value)) != 0:
            return None
        return int(value.value)

    @staticmethod
    def _array(library: ctypes.CDLL, handle: Any, key: bytes) -> "Any":
        import numpy as np  # noqa: PLC0415

        size = ctypes.c_size_t()
        if library.codes_get_size(handle, key, ctypes.byref(size)) != 0:
            raise ContractError("noaa_conus_grid_array_size_unreadable")
        out = np.empty(size.value, dtype=np.float64)
        length = ctypes.c_size_t(size.value)
        if library.codes_get_double_array(
            handle, key,
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.byref(length),
        ) != 0:
            raise ContractError("noaa_conus_grid_array_unreadable")
        return out

    @staticmethod
    def _grid_identity(library: ctypes.CDLL, handle: Any) -> str:
        library.codes_get_string.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        library.codes_get_string.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(64)
        length = ctypes.c_size_t(64)
        if library.codes_get_string(
            handle, b"md5GridSection", buffer, ctypes.byref(length)
        ) != 0:
            raise ContractError("noaa_conus_grid_identity_unreadable")
        return buffer.value.decode("ascii")

    def _derive(
        self, library: ctypes.CDLL, handle: Any, message: bytes,
    ) -> list[int | None]:
        """Each point's index into ``values``, proven against eccodes."""

        import math  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415

        layout = {key: self._long(library, handle, key) for key in _GRID_LAYOUT_KEYS}
        supported = (
            layout[b"gridDefinitionTemplateNumber"] == _LAMBERT_CONFORMAL_TEMPLATE
            and layout[b"iScansNegatively"] == 0
            and layout[b"jScansPositively"] == 1
            and layout[b"bitmapPresent"] == 0
            and layout[b"alternativeRowScanning"] in (0, 1)
            and (layout[b"Nx"] or 0) > 0
        )
        if not supported:
            return [None] * len(self._points)
        nx = int(layout[b"Nx"])
        alternate = layout[b"alternativeRowScanning"] == 1
        lat = np.radians(self._array(library, handle, b"latitudes"))
        lon = np.radians(self._array(library, handle, b"longitudes") % 360.0)
        values = self._array(library, handle, b"values")
        sin_lat, cos_lat = np.sin(lat), np.cos(lat)
        indexes: list[int | None] = []
        for point_lat, point_lon in self._points:
            p_lat = math.radians(point_lat)
            p_lon = math.radians(point_lon % 360.0)
            # Largest cosine of the central angle is the nearest cell.
            nearest = int(np.argmax(
                math.sin(p_lat) * sin_lat
                + math.cos(p_lat) * cos_lat * np.cos(lon - p_lon)
            ))
            index = _values_index(nearest, nx, alternate)
            try:
                reference, _distance = nearest_point_kelvin(
                    message, latitude=point_lat, longitude=point_lon,
                )
            except ContractError:
                # The slow path raises for this point today; let it keep
                # doing so rather than guess what it should have returned.
                indexes.append(None)
                continue
            indexes.append(index if float(values[index]) == reference else None)
        return indexes

    def kelvin(self, message: bytes) -> list[float | None]:
        """Each point's value in kelvin; None where it must be read slowly.

        No range check here: a point nobody consumes from this message must
        not raise where the per-(station, hour) decode it replaces would not
        have been called at all.  The caller checks what it takes.
        """

        library = _eccodes()
        buffer = ctypes.create_string_buffer(message, len(message))
        handle = library.codes_handle_new_from_message(
            None, buffer, ctypes.c_size_t(len(message))
        )
        if not handle:
            raise ContractError("noaa_conus_message_unreadable")
        try:
            identity = self._grid_identity(library, handle)
            if identity not in self._indexes:
                self._indexes[identity] = self._derive(library, handle, message)
                self.slow_points += sum(
                    1 for index in self._indexes[identity] if index is None
                )
            indexes = self._indexes[identity]
            if all(index is None for index in indexes):
                return [None] * len(indexes)
            values = self._array(library, handle, b"values")
            return [
                None if index is None else float(values[index])
                for index in indexes
            ]
        finally:
            library.codes_handle_delete(handle)


# Charter 200 (2026-09-23, user ruling 「先 3 修」): the cell a station is read
# at is the nearest cell the model itself calls land.
#
# The nearest cell is not always the station's.  KSFO sits on the bay shore,
# and HRRR's 3 km cell nearest the ASOS (37.6075 N 122.3577 W, 1.5 km) is
# water in HRRR's own LAND:surface mask.  Its day maximum read 3.75 C below
# settled truth (sd 1.64) on the P1 lane's peak-complete rows, against -1 to
# +2 C at the other eleven HRRR stations, all of whose nearest cells are land;
# the nearest land cell (37.6003 N 122.3905 W, 3.1 km) is where the airport's
# afternoon actually happens.  Audit: docs/research/
# station_regional_features_20260922/conus_cell_audit/.
#
# The rule, one for every station: take the geographically nearest cell; if
# the product's own land mask calls it land (fraction >= LAND_FRACTION_MIN)
# the station's own coordinates are used, unchanged -- so every station whose
# nearest cell is land reads exactly the cell and the value it read before,
# bit for bit.  Otherwise the station is read at the centre of the nearest
# land cell within MAX_LAND_SEARCH_KM (ties: the lower grid index), and a
# station with no land cell that close is not read at all -- a water value is
# never returned as a land station's.  Reading at the chosen cell's centre
# rather than at an index keeps every downstream read unchanged: the fast
# reader and the eccodes search both find a cell nearest to its own centre.
#
# Only a product that publishes a mask in the files the lane already reads is
# subject to it.  HRRR carries LAND:surface in every wrfsfc file (a ~50 KB
# ranged read beside the temperature field).  The NBM core publishes no land
# mask; its nearest cells sit 0.4-1.4 km from every roster station and read
# truth at -0.3 to +1.7 C (KSFO +0.18), so the NBM keeps plain nearest.
LAND_MARK = ":LAND:surface:"
LAND_MASK_PRODUCTS = frozenset({"HRRR"})
LAND_FRACTION_MIN = 0.5
MAX_LAND_SEARCH_KM = 10.0
# Charter 201: on a coarse grid the next land cell can be a whole spacing
# away (0.25 degrees: FACT, NZWN, WSSS at 14-19 km), so the search reaches
# 1.5 local grid spacings when that is further than the fixed 10 km.  On
# HRRR's 3 km grid it is not, and the reach is exactly charter 200's.
LAND_SEARCH_SPACINGS = 1.5
SAMPLING_RULE = "nearest_land_cell_v1"
_EARTH_RADIUS_KM = 6371.0
# (grid identity, latitude, longitude) -> resolved point; the mask is static
# for a grid, so a pass resolves each station once.
_SAMPLING_CACHE: dict[tuple[str, float, float], dict[str, Any] | None] = {}


def land_mask_message(
    product: str, run_init: datetime, forecast_hour: int,
) -> bytes | None:
    """The product's land mask from one file of this run; None if it has none.

    ``ContractError`` when the product publishes a mask and it cannot be
    read: falling back to the plain nearest cell would put a shore station
    back on the water without anyone knowing.
    """

    if product not in LAND_MASK_PRODUCTS:
        return None
    url = file_url(product, run_init, forecast_hour)
    span = _mark_range(url, LAND_MARK)
    message = None if span is None else _range_get(url, span[0], span[1])
    if not message:
        raise ContractError("noaa_conus_land_mask_unavailable")
    return message


def nearest_land_points(
    land_message: bytes, points: "list[tuple[float, float]]",
) -> "list[dict[str, Any] | None]":
    """Each point's sampling coordinates under ``SAMPLING_RULE``.

    ``{"latitude", "longitude", "moved", ...}``: ``moved`` False returns the
    point's own coordinates (the nearest cell is land); True returns the
    centre of the nearest land cell with its distance.  None when no land
    cell lies within ``MAX_LAND_SEARCH_KM``.
    """

    import math  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    library = _eccodes()
    buffer = ctypes.create_string_buffer(land_message, len(land_message))
    handle = library.codes_handle_new_from_message(
        None, buffer, ctypes.c_size_t(len(land_message))
    )
    if not handle:
        raise ContractError("noaa_conus_land_mask_unreadable")
    try:
        identity = GridPointReader._grid_identity(library, handle)
        pending = [
            (lat, lon) for lat, lon in points
            if (identity, float(lat), float(lon)) not in _SAMPLING_CACHE
        ]
        if pending:
            nx = GridPointReader._long(library, handle, b"Nx")
            alternate = GridPointReader._long(
                library, handle, b"alternativeRowScanning"
            ) == 1
            if not nx or GridPointReader._long(
                library, handle, b"bitmapPresent"
            ):
                raise ContractError("noaa_conus_land_mask_layout_unsupported")
            lat_deg = GridPointReader._array(library, handle, b"latitudes")
            lon_deg = GridPointReader._array(library, handle, b"longitudes") % 360.0
            stored = GridPointReader._array(library, handle, b"values")
            # Land fraction in geographic (iterator) order.
            order = np.arange(stored.size)
            if alternate:
                rows, cols = np.divmod(order, int(nx))
                odd = rows % 2 == 1
                order = np.where(odd, rows * int(nx) + (int(nx) - 1 - cols), order)
            land = stored[order] >= LAND_FRACTION_MIN
            lat_r, lon_r = np.radians(lat_deg), np.radians(lon_deg)
            sin_lat, cos_lat = np.sin(lat_r), np.cos(lat_r)
            for lat, lon in pending:
                p_lat, p_lon = math.radians(float(lat)), math.radians(float(lon) % 360.0)
                cosine = np.clip(
                    math.sin(p_lat) * sin_lat
                    + math.cos(p_lat) * cos_lat * np.cos(lon_r - p_lon),
                    -1.0, 1.0,
                )
                nearest = int(np.argmax(cosine))
                key = (identity, float(lat), float(lon))
                if bool(land[nearest]):
                    _SAMPLING_CACHE[key] = {
                        "latitude": float(lat), "longitude": float(lon),
                        "moved": False, "rule": SAMPLING_RULE,
                    }
                    continue
                distance = _EARTH_RADIUS_KM * np.arccos(cosine)
                # Local grid spacing: the nearest cell's own nearest neighbour.
                to_nearest = np.clip(
                    sin_lat[nearest] * sin_lat
                    + cos_lat[nearest] * cos_lat * np.cos(lon_r - lon_r[nearest]),
                    -1.0, 1.0,
                )
                to_nearest[nearest] = -1.0
                spacing = _EARTH_RADIUS_KM * float(np.arccos(to_nearest.max()))
                reach = max(MAX_LAND_SEARCH_KM, LAND_SEARCH_SPACINGS * spacing)
                candidates = np.flatnonzero(land & (distance <= reach))
                if candidates.size == 0:
                    _SAMPLING_CACHE[key] = None
                    continue
                # argmin returns the first minimum: ties go to the lower index.
                best = int(candidates[int(np.argmin(distance[candidates]))])
                cell_lon = float(lon_deg[best])
                _SAMPLING_CACHE[key] = {
                    "latitude": float(lat_deg[best]),
                    "longitude": cell_lon - 360.0 if cell_lon > 180.0 else cell_lon,
                    "moved": True, "rule": SAMPLING_RULE,
                    "distance_km": round(float(distance[best]), 3),
                    "water_cell_distance_km": round(float(distance[nearest]), 3),
                    "grid_spacing_km": round(spacing, 3),
                }
        return [
            _SAMPLING_CACHE[(identity, float(lat), float(lon))]
            for lat, lon in points
        ]
    finally:
        library.codes_handle_delete(handle)


def sampling_points(
    *, product: str, run_init: datetime, forecast_hour: int,
    points: "list[tuple[float, float]]",
) -> "list[dict[str, Any] | None]":
    """Where each point is read in this run, under ``SAMPLING_RULE``.

    A product without a land mask reads every point where it stands.
    """

    message = land_mask_message(product, run_init, forecast_hour)
    if message is None:
        return [
            {"latitude": float(lat), "longitude": float(lon), "moved": False,
             "rule": "nearest_cell_no_mask"}
            for lat, lon in points
        ]
    return nearest_land_points(message, points)


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
        # Charter 95 (P1 lane): the curve itself, forecast hour -> °C.
        "hourly_c": dict(sorted(values.items())),
        "bytes_downloaded": downloaded,
        "source_urls": urls,
        # The Lambert grid has no row/column arithmetic worth citing; the
        # provenance that matters is which product and cycle.
        "grid_geometry": f"{product} lambert nearest-cell",
    }
