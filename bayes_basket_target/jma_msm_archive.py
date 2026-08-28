"""Ranged capture of JMA MSM 2-metre temperature for one grid cell.

Why this family exists at all: RCSS is the one station that failed all five
dimensions of the systemic-failure diagnosis, and the bias layer has been
unable to correct it all month -- +5.75C of correction applied and +1.4C of
centre error still standing.  The reason is resolution.  Songshan sits on the
floor of the Taipei basin at five metres, ringed by 500-1000m hills; a 25 km
global cell averages the hills into the airfield and runs structurally cold.
MSM's 5 km cell for the same point reports an elevation of four metres -- it
can see the basin.  Measured day-ahead against sixty days of settled truth,
its distribution beat the served posterior on CRPS, NLL and Brier, and kept
beating it with the three best days removed.

Why the capture is ranged.  JMA writes each file as a *single* GRIB2 message
with sections 4-5-6-7 repeated once per field, so a message-level walk finds
one message the size of the whole object and concludes there is nothing to
range into.  There is: a field's section group is about 356 KB against the
file's 75 MB, and the group can be lifted out and re-assembled with the
shared sections into a single-field message eccodes opens.  Measured
2026-08-27 on a real file: 366 KB downloaded instead of 75 MB, 210x.

Groups are near-regular in size, so the field wanted is found by estimating
its index from the stride and walking the short distance from there rather
than from the front -- a hundred groups of walking is a hundred round trips
that buy nothing.

One connection with keep-alive, for the reason the ECMWF archive states in
its own client: a fresh client per range re-handshakes every time, and the
first version of this took 146 seconds for one field because of it.
"""

from __future__ import annotations

import ctypes
import struct
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from .contracts import ContractError
from .dwd_icon_eps_archive import _eccodes

SYSTEM_ID = "JMA_MSM_POINT"
FAMILY_ID = "JMA_MSM"
PROVIDER_ID = "JMA_RJTD"
TRANSPORT_PROVIDER_ID = "RISH_KYOTO_JMA_ARCHIVE"
# The roster this lane is ruled in at, from the 46-pair sweep.  RKSI
# missed the NLL gate on that window and is captured anyway: the corpus
# is what future gates re-ask, and a station that stops being captured
# can never earn the seat back.  It lives here, not in the capture
# script, because the board reports the ruling too -- and a board that
# infers the roster from which rows exist yet would call a station whose
# turn has not come "measured reversed", which is a different claim.
CAPTURE_STATIONS = (
    ("RCSS", 25.0697, 121.5525, "Asia/Taipei"),
    ("RJTT", 35.553, 139.781, "Asia/Tokyo"),
    ("RKPK", 35.179, 128.938, "Asia/Seoul"),
    ("RKSI", 37.469, 126.451, "Asia/Seoul"),
)
ARCHIVE_ROOT = (
    "http://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original"
)
# The published surface products, and the forecast-hour span each covers.
# Named rather than derived: a file that does not exist is a capture gap and
# should say so, not be silently skipped by a range computation.
SURFACE_SPANS = (
    (0, 15, "FH00-15"),
    (16, 33, "FH16-33"),
    (34, 39, "FH34-39"),
    (40, 51, "FH40-51"),
    (52, 78, "FH52-78"),
)
# 2-metre temperature: discipline 0, category 0, number 0, above ground.
PARAM_CATEGORY = 0
PARAM_NUMBER = 0
SURFACE_TYPE = 103
# Measured on 2026-08-26: 34 + 21 + 6 + 364363.  Only ever a starting guess
# for where to look; every candidate is confirmed by reading its own header.
NOMINAL_GROUP_STRIDE = 364_424
FIRST_GROUP_OFFSET = 109
MAX_WALK_GROUPS = 24

_CLIENT_LOCAL = threading.local()


def _client() -> httpx.Client:
    client = getattr(_CLIENT_LOCAL, "client", None)
    if client is None:
        client = httpx.Client(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(60.0),
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
                keepalive_expiry=60.0,
            ),
        )
        _CLIENT_LOCAL.client = client
    return client


def close_client() -> None:
    """Release this thread's connection.  Callers own process lifetime."""

    client = getattr(_CLIENT_LOCAL, "client", None)
    if client is not None:
        client.close()
        _CLIENT_LOCAL.client = None


@dataclass(frozen=True)
class MsmField:
    """One decoded field and the provenance that makes it citable."""

    run_init_utc: str
    forecast_hour: int
    value_c: float
    grid_row: int
    grid_col: int
    grid_elevation_hint: str
    source_url: str
    bytes_downloaded: int


def file_url(run_init: datetime, span_label: str) -> str:
    stamp = run_init.strftime("%Y%m%d%H%M%S")
    return (
        f"{ARCHIVE_ROOT}/{run_init:%Y/%m/%d}"
        f"/Z__C_RJTD_{stamp}_MSM_GPV_Rjp_Lsurf_{span_label}_grib2.bin"
    )


# A third-party mirror times out and drops connections; the ICON lane is on
# record for how brittle a regional capture path can be.  A transient network
# fault is not evidence about the archive and must not be allowed to look
# like one, so ranges retry with backoff and only a real status refuses.
RANGE_ATTEMPTS = 4
RANGE_BACKOFF_SECONDS = 2.0


def _range(url: str, start: int, length: int) -> bytes:
    if length <= 0:
        return b""
    last: Exception | None = None
    for attempt in range(RANGE_ATTEMPTS):
        try:
            response = _client().get(
                url,
                headers={"Range": f"bytes={start}-{start + length - 1}"},
            )
        except httpx.HTTPError as exc:
            last = exc
            # The pooled connection may be the thing that died; drop it so
            # the retry dials fresh rather than reusing a broken socket.
            close_client()
            time.sleep(RANGE_BACKOFF_SECONDS * (attempt + 1))
            continue
        if response.status_code in (200, 206):
            return response.content
        if response.status_code in (429, 500, 502, 503, 504):
            last = ContractError(
                f"jma_msm_archive_range_status_{response.status_code}"
            )
            time.sleep(RANGE_BACKOFF_SECONDS * (attempt + 1))
            continue
        raise ContractError(
            f"jma_msm_archive_range_status_{response.status_code}"
        )
    raise ContractError(
        f"jma_msm_archive_range_unreachable:{type(last).__name__}"
    )


def _section_header(url: str, offset: int) -> tuple[int, int]:
    head = _range(url, offset, 5)
    if len(head) < 5:
        raise ContractError("jma_msm_archive_section_header_short")
    return struct.unpack(">I", head[0:4])[0], head[4]


def _shared_prefix(url: str) -> tuple[bytes, dict[int, bytes], int]:
    """Message header plus the sections every field in the file shares."""

    head = _range(url, 0, 16)
    if len(head) < 16 or head[:4] != b"GRIB" or head[7] != 2:
        raise ContractError("jma_msm_archive_not_grib2")
    prefix = _range(url, 16, 4096)
    position = 0
    shared: dict[int, bytes] = {}
    while position + 5 <= len(prefix):
        length = struct.unpack(">I", prefix[position:position + 4])[0]
        section = prefix[position + 4]
        if section not in (1, 2, 3) or length <= 0:
            break
        if position + length > len(prefix):
            raise ContractError("jma_msm_archive_shared_section_truncated")
        shared[section] = prefix[position:position + length]
        position += length
    if 1 not in shared or 3 not in shared:
        raise ContractError("jma_msm_archive_shared_sections_missing")
    return head, shared, 16 + position


def _group_at(url: str, offset: int) -> tuple[int, int, int] | None:
    """Read one section-4 product definition: (category, number, hour)."""

    length, section = _section_header(url, offset)
    if section != 4:
        return None
    body = _range(url, offset, length)
    if len(body) < 23:
        raise ContractError("jma_msm_archive_product_section_short")
    return body[9], body[10], struct.unpack(">I", body[18:22])[0], body[22]


def _group_total(url: str, offset: int) -> int:
    total = 0
    cursor = offset
    for _ in range(4):
        length, _section = _section_header(url, cursor)
        total += length
        cursor += length
    return total


def _decode_cell(
    head: bytes, shared: dict[int, bytes], group: bytes,
    latitude: float, longitude: float,
) -> tuple[float, str]:
    """One cell out of a re-assembled single-field message."""

    body = b"".join(shared[key] for key in sorted(shared))
    message = (
        head[:8]
        + struct.pack(">Q", 16 + len(body) + len(group) + 4)
        + body + group + b"7777"
    )
    library = _eccodes()
    buffer = ctypes.create_string_buffer(message, len(message))
    handle = library.codes_handle_new_from_message(
        None, buffer, ctypes.c_size_t(len(message))
    )
    if not handle:
        raise ContractError("jma_msm_archive_message_unreadable")
    try:
        def gl(key: str) -> int:
            out = ctypes.c_long()
            if library.codes_get_long(
                handle, key.encode(), ctypes.byref(out)
            ) != 0:
                raise ContractError(f"jma_msm_archive_key_missing_{key}")
            return out.value

        size = ctypes.c_size_t()
        library.codes_get_size(handle, b"values", ctypes.byref(size))
        values = (ctypes.c_double * size.value)()
        count = ctypes.c_size_t(size.value)
        library.codes_get_double_array(
            handle, b"values", values, ctypes.byref(count)
        )
        ni = gl("Ni")
        first_lat = gl("latitudeOfFirstGridPoint") / 1e6
        first_lon = gl("longitudeOfFirstGridPoint") / 1e6
        di = gl("iDirectionIncrement") / 1e6
        dj = gl("jDirectionIncrement") / 1e6
        row = round(abs(latitude - first_lat) / dj)
        col = round(abs(longitude - first_lon) / di)
        cell = row * ni + col
        if not 0 <= cell < size.value:
            raise ContractError("jma_msm_archive_station_outside_grid")
        kelvin = float(values[cell])
        if not 180.0 <= kelvin <= 340.0:
            raise ContractError("jma_msm_archive_value_out_of_range")
        return (
            kelvin - 273.15,
            f"[{row},{col}] {first_lat:.3f}N/{first_lon:.3f}E"
            f" step {di:.4f}x{dj:.4f}",
        )
    finally:
        library.codes_handle_delete(handle)


def fetch_field(
    *, run_init: datetime, forecast_hour: int, latitude: float,
    longitude: float,
) -> MsmField:
    """The 2-metre temperature at one grid cell, in Celsius."""

    span = next(
        (label for low, high, label in SURFACE_SPANS
         if low <= forecast_hour <= high),
        None,
    )
    if span is None:
        raise ContractError("jma_msm_archive_forecast_hour_unpublished")
    url = file_url(run_init, span)
    downloaded = 0

    head, shared, first_group = _shared_prefix(url)
    downloaded += 16 + 4096
    total = struct.unpack(">Q", head[8:16])[0]

    # Walk group by group from the first one, reading each group's whole
    # front matter in a single range: sections 4, 5 and 6 are 34, 21 and 6
    # bytes and section 7's header is five more, so seventy bytes yields both
    # the product definition and every length needed to step over the field.
    # An estimate-and-jump was tried first and is not sound -- an estimated
    # offset does not land on a section boundary, and a walk that starts off
    # one reads garbage where a header should be.  One request per group over
    # a hundred groups is a second or two on a kept-alive connection; it was
    # only slow when every range spawned its own process.
    found: int | None = None
    offset = first_group
    groups = 0
    while offset < total - 4 and groups < 400:
        front = _range(url, offset, 70)
        downloaded += 70
        if len(front) < 66:
            break
        length4 = struct.unpack(">I", front[0:4])[0]
        if front[4] != 4 or length4 < 23:
            break
        category, number = front[9], front[10]
        hour = struct.unpack(">I", front[18:22])[0]
        surface = front[22]
        groups += 1
        if (
            category == PARAM_CATEGORY
            and number == PARAM_NUMBER
            and surface == SURFACE_TYPE
            and hour == forecast_hour
        ):
            found = offset
            break
        cursor = offset + length4
        stride = length4
        ok = True
        for _ in range(3):
            relative = cursor - offset
            if relative + 5 <= len(front):
                length = struct.unpack(
                    ">I", front[relative:relative + 4]
                )[0]
            else:
                length, _section = _section_header(url, cursor)
                downloaded += 5
            if length <= 0:
                ok = False
                break
            cursor += length
            stride += length
        if not ok:
            break
        offset += stride

    group = b""
    cursor = found
    for _ in range(4):
        length, _section = _section_header(url, cursor)
        group += _range(url, cursor, length)
        downloaded += length + 5
        cursor += length

    body = b"".join(shared[key] for key in sorted(shared))
    message = (
        head[:8]
        + struct.pack(">Q", 16 + len(body) + len(group) + 4)
        + body + group + b"7777"
    )
    library = _eccodes()
    buffer = ctypes.create_string_buffer(message, len(message))
    handle = library.codes_handle_new_from_message(
        None, buffer, ctypes.c_size_t(len(message))
    )
    if not handle:
        raise ContractError("jma_msm_archive_message_unreadable")
    try:
        def gl(key: str) -> int:
            out = ctypes.c_long()
            if library.codes_get_long(
                handle, key.encode(), ctypes.byref(out)
            ) != 0:
                raise ContractError(f"jma_msm_archive_key_missing_{key}")
            return out.value

        size = ctypes.c_size_t()
        library.codes_get_size(handle, b"values", ctypes.byref(size))
        values = (ctypes.c_double * size.value)()
        count = ctypes.c_size_t(size.value)
        library.codes_get_double_array(
            handle, b"values", values, ctypes.byref(count)
        )
        ni = gl("Ni")
        first_lat = gl("latitudeOfFirstGridPoint") / 1e6
        first_lon = gl("longitudeOfFirstGridPoint") / 1e6
        di = gl("iDirectionIncrement") / 1e6
        dj = gl("jDirectionIncrement") / 1e6
        row = round(abs(latitude - first_lat) / dj)
        col = round(abs(longitude - first_lon) / di)
        cell = row * ni + col
        if not 0 <= cell < size.value:
            raise ContractError("jma_msm_archive_station_outside_grid")
        kelvin = float(values[cell])
        # A physically impossible value is a decode fault, not weather.
        if not 180.0 <= kelvin <= 340.0:
            raise ContractError("jma_msm_archive_value_out_of_range")
    finally:
        library.codes_handle_delete(handle)

    return MsmField(
        run_init_utc=run_init.astimezone(timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        forecast_hour=forecast_hour,
        value_c=kelvin - 273.15,
        grid_row=row,
        grid_col=col,
        grid_elevation_hint=f"{first_lat:.3f}N/{first_lon:.3f}E"
                            f" step {di:.4f}x{dj:.4f}",
        source_url=url,
        bytes_downloaded=downloaded,
    )


def local_day_hours(
    *, run_init: datetime, event_date: date, utc_offset_hours: int
) -> list[int]:
    """Forecast hours of this run that fall inside the station's local day.

    The day a market settles on is the station's own calendar day, so which
    forecast hours matter is a property of the station's offset and not of
    the run.  Hours outside what the run publishes are dropped by the caller
    when the file that would hold them does not exist.
    """

    start_local = datetime.combine(
        event_date, datetime.min.time(), tzinfo=timezone.utc
    ) - timedelta(hours=utc_offset_hours)
    hours = []
    for step in range(0, 79):
        stamp = run_init + timedelta(hours=step)
        if start_local <= stamp < start_local + timedelta(days=1):
            hours.append(step)
    return hours


def fetch_day_maximum(
    *, run_init: datetime, event_date: date, utc_offset_hours: int,
    latitude: float, longitude: float,
) -> dict[str, Any]:
    """The forecast daily maximum for one station's local day, in one walk.

    Calling ``fetch_field`` once per hour restarts the section walk every
    time, and the walk is the expensive half -- measured, twenty-four hours
    cost 478 seconds that way against twenty-two for a single field.  Every
    hour a file holds is collected in one pass instead, so the cost is one
    walk per file rather than one per hour.
    """

    wanted = set(local_day_hours(
        run_init=run_init, event_date=event_date,
        utc_offset_hours=utc_offset_hours,
    ))
    by_span: dict[str, list[int]] = {}
    for hour in sorted(wanted):
        for low, high, label in SURFACE_SPANS:
            if low <= hour <= high:
                by_span.setdefault(label, []).append(hour)
                break

    values: dict[int, float] = {}
    downloaded = 0
    geometry = ""
    for label, hours in sorted(by_span.items()):
        url = file_url(run_init, label)
        try:
            head, shared, first_group = _shared_prefix(url)
        except ContractError:
            # A file the archive has not published is a gap in the day, and
            # the caller decides whether a partial day is usable.  It is not
            # an error here.
            continue
        downloaded += 16 + 4096
        total = struct.unpack(">Q", head[8:16])[0]
        remaining = set(hours)
        offset = first_group
        groups = 0
        while offset < total - 4 and groups < 400 and remaining:
            front = _range(url, offset, 70)
            downloaded += 70
            if len(front) < 66 or front[4] != 4:
                break
            length4 = struct.unpack(">I", front[0:4])[0]
            category, number = front[9], front[10]
            hour = struct.unpack(">I", front[18:22])[0]
            surface = front[22]
            groups += 1
            lengths = [length4]
            cursor = offset + length4
            for _ in range(3):
                relative = cursor - offset
                if relative + 5 <= len(front):
                    length = struct.unpack(
                        ">I", front[relative:relative + 4]
                    )[0]
                else:
                    length, _section = _section_header(url, cursor)
                    downloaded += 5
                if length <= 0:
                    lengths = []
                    break
                lengths.append(length)
                cursor += length
            if not lengths:
                break
            if (
                category == PARAM_CATEGORY
                and number == PARAM_NUMBER
                and surface == SURFACE_TYPE
                and hour in remaining
            ):
                group = b""
                inner = offset
                for length in lengths:
                    group += _range(url, inner, length)
                    downloaded += length
                    inner += length
                value, geometry = _decode_cell(
                    head, shared, group, latitude, longitude
                )
                values[hour] = value
                remaining.discard(hour)
            offset += sum(lengths)

    if not values:
        raise ContractError("jma_msm_archive_day_empty")
    return {
        "run_init_utc": run_init.astimezone(timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "event_date": event_date.isoformat(),
        "forecast_tmax_c": max(values.values()),
        "hours_covered": sorted(values),
        "hours_wanted": sorted(wanted),
        "complete": set(values) == wanted,
        "grid_geometry": geometry,
        "bytes_downloaded": downloaded,
    }
