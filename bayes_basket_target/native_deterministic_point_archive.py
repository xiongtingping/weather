"""Charter 267 (二): native deterministic global runs, 2 m temperature at points.

User ruling 2026-10-11 「接入确定性预报源」.  The benchmark cluster's first trade
on a station-day sits at the bucket of a calibrated centre of the deterministic
global models (benchmark research rounds 32-35), and it trades minutes after
listing, before the market has a mode.  The deterministic family (charter 201)
already holds AIFS Single natively and IFS 9 km / UKMO / CMA through the
aggregator relay; this module adds the deterministic runs the publishers serve
themselves, so the listing-time forecast F (charter 267 (三)) can be built from
native bytes only:

* ``IFS_HRES`` -- ECMWF IFS HRES 0.25 degree, ECMWF open data on the GCS mirror
  the AIFS lane already reads (``ifs/0p25/oper``, 00Z/12Z, 3-hourly steps; JSON
  ``.index``); cell by the run's own ``lsm``.
* ``GFS`` -- NOAA GFS 0.25 degree, ``noaa-gfs-bdp-pds`` (four cycles, 3-hourly;
  wgrib2 ``.idx``); cell by the run's own ``LAND`` at f000.
* ``AIGFS`` -- NOAA's AI GFS 0.25 degree, ``noaa-nws-graphcastgfs-pds``
  (``aigfs.YYYYMMDD/HH/model/atmos/grib2/aigfs.tHHz.sfc.fFFF.grib2``, four cycles,
  6-hourly; ``.idx``); cell by GFS's ``LAND`` of the same init.
* ``GDPS`` -- ECCC GEM global 15 km, the MSC Datamart
  (``YYYYMMDD/WXO-DD/model_gdps/15km/HH/FFF/..._AirTemp_AGL-2m_LatLon0.15_PTFFFH.grib2``,
  00Z/12Z, 3-hourly, one message a file); cell by GFS's ``LAND`` of the same init.

Each run is read one step at a time (one ranged read or one small file), the
field decoded once for every station that wants the step and dropped -- a pass
holds one field per worker.  A day's maximum is the largest 2 m value on the
local day's steps, the same form as AIFS Single (charter 201): the instantaneous
3/6-hourly values sit below the true maximum, a standing offset the F
calibration takes.  Rows land in ``calibration_regional_point_run`` beside the
family; none is an ensemble member, none reaches P0, P0-M or P1 (charter 267
keeps them out of ``deterministic_forecast_family.FAMILY_SYSTEM_IDS``).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

import httpx

from .contracts import ContractError
from .noaa_conus_archive import GridPointReader, nearest_land_points, nearest_point_kelvin  # noqa: F401
from .pass_deadline import PassDeadlineExceeded, deadline_spent

ECMWF_BASE_URL = "https://storage.googleapis.com/ecmwf-open-data"
GFS_BASE_URL = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
AIGFS_BASE_URL = "https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com"
ECCC_BASE_URL = "https://dd.weather.gc.ca"
# D+2's afternoon at every roster station from a run issued the day before D+1.
MAX_CAPTURE_STEP = 96
FETCH_WORKERS = 4
# One GRIB message: the largest field read here (GDPS 0.15 degree, 2.9 M points)
# is about 1.3 MB; anything far above that is not a single 2 m field.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_INDEX_BYTES = 2 * 1024 * 1024
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_ATTEMPTS = 3
_client_instance: httpx.Client | None = None


class NativeDeterministicPassDeadlineExceeded(PassDeadlineExceeded):
    """The pass ran out of wall clock inside a native deterministic fetch."""


@dataclass(frozen=True)
class ProductSpec:
    """Where one product's runs live and how one step is read."""

    product: str
    cycles: tuple[int, ...]
    step_hours: int
    index_kind: str  # "ecmwf_json" | "noaa_idx" | "whole_file"
    mask: str  # "ecmwf_lsm" | "gfs_land"

    def steps(self) -> list[int]:
        return list(range(0, MAX_CAPTURE_STEP + 1, self.step_hours))


SPECS: Mapping[str, ProductSpec] = {
    "IFS_HRES": ProductSpec("IFS_HRES", (0, 12), 3, "ecmwf_json", "ecmwf_lsm"),
    "GFS": ProductSpec("GFS", (0, 6, 12, 18), 3, "noaa_idx", "gfs_land"),
    "AIGFS": ProductSpec("AIGFS", (0, 6, 12, 18), 6, "noaa_idx", "gfs_land"),
    "GDPS": ProductSpec("GDPS", (0, 12), 3, "whole_file", "gfs_land"),
}


def _utc(run_init: datetime) -> datetime:
    return run_init.astimezone(timezone.utc)


def step_url(product: str, run_init: datetime, step: int) -> str:
    """The GRIB object that carries ``product``'s 2 m field at ``step``."""

    stamp = _utc(run_init)
    step = int(step)
    if product == "IFS_HRES":
        return (
            f"{ECMWF_BASE_URL}/{stamp:%Y%m%d}/{stamp:%H}z/ifs/0p25/oper/"
            f"{stamp:%Y%m%d%H}0000-{step}h-oper-fc.grib2"
        )
    if product == "GFS":
        return (
            f"{GFS_BASE_URL}/gfs.{stamp:%Y%m%d}/{stamp:%H}/atmos/"
            f"gfs.t{stamp:%H}z.pgrb2.0p25.f{step:03d}"
        )
    if product == "AIGFS":
        return (
            f"{AIGFS_BASE_URL}/aigfs.{stamp:%Y%m%d}/{stamp:%H}/model/atmos/grib2/"
            f"aigfs.t{stamp:%H}z.sfc.f{step:03d}.grib2"
        )
    if product == "GDPS":
        return (
            f"{ECCC_BASE_URL}/{stamp:%Y%m%d}/WXO-DD/model_gdps/15km/{stamp:%H}/{step:03d}/"
            f"{stamp:%Y%m%d}T{stamp:%H}Z_MSC_GDPS_AirTemp_AGL-2m_LatLon0.15_PT{step:03d}H.grib2"
        )
    raise ContractError("native_deterministic_product_unknown")


def index_url(product: str, run_init: datetime, step: int) -> str | None:
    url = step_url(product, run_init, step)
    kind = SPECS[product].index_kind
    if kind == "ecmwf_json":
        return url[: -len(".grib2")] + ".index"
    if kind == "noaa_idx":
        return url + ".idx"
    return None


def run_directory_url(product: str, run_init: datetime) -> str:
    url = step_url(product, run_init, 0)
    return url.rsplit("/", 2 if product == "GDPS" else 1)[0] + "/"


def _client() -> httpx.Client:
    global _client_instance
    if _client_instance is None:
        proxy = None
        try:
            from official_nwp_https_transport import official_nwp_proxy_url  # noqa: PLC0415

            proxy = official_nwp_proxy_url() or None
        except ImportError:
            proxy = None
        _client_instance = httpx.Client(timeout=_TIMEOUT, proxy=proxy)
    return _client_instance


def close_client() -> None:
    global _client_instance
    if _client_instance is not None:
        _client_instance.close()
        _client_instance = None


def _get(url: str, headers: Mapping[str, str] | None = None, *, cap: int) -> bytes | None:
    for _attempt in range(_ATTEMPTS):
        if deadline_spent():
            raise NativeDeterministicPassDeadlineExceeded(
                "native_deterministic_pass_deadline_exceeded"
            )
        try:
            response = _client().get(url, headers=dict(headers or {}))
        except httpx.HTTPError:
            continue
        if response.status_code in (403, 404):
            return None
        if response.status_code in (200, 206):
            body = response.content
            if len(body) > cap:
                raise ContractError("native_deterministic_object_over_cap")
            return body
    return None


def parse_ecmwf_index(text: str, *, param: str) -> tuple[int, int] | None:
    """(offset, length) of the surface message ``param`` in an ECMWF JSON index."""

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("param") == param and item.get("levtype") == "sfc":
            return int(item["_offset"]), int(item["_length"])
    return None


def parse_noaa_idx(text: str, *, field: str) -> tuple[int, int | None] | None:
    """(offset, length or None for the file's last message) of ``field``.

    ``field`` is the wgrib2 inventory's ``VAR:LEVEL`` pair, e.g.
    ``TMP:2 m above ground``; the length runs to the next record's offset.
    """

    records: list[tuple[int, str]] = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 5:
            continue
        try:
            records.append((int(parts[1]), f"{parts[3]}:{parts[4]}"))
        except ValueError:
            continue
    for position, (offset, name) in enumerate(records):
        if name == field:
            if position + 1 < len(records):
                return offset, records[position + 1][0] - offset
            return offset, None
    return None


FIELD_2T = {"IFS_HRES": "2t", "GFS": "TMP:2 m above ground", "AIGFS": "TMP:2 m above ground"}


def read_message(product: str, run_init: datetime, step: int, *, field: str | None = None) -> bytes | None:
    """One 2 m (or mask) message at one step, by the product's own index."""

    spec = SPECS[product]
    url = step_url(product, run_init, step)
    if spec.index_kind == "whole_file":
        return _get(url, cap=MAX_MESSAGE_BYTES)
    raw = _get(str(index_url(product, run_init, step)), cap=MAX_INDEX_BYTES)
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace")
    wanted = field or FIELD_2T[product]
    if spec.index_kind == "ecmwf_json":
        span = parse_ecmwf_index(text, param=wanted)
        if span is None:
            return None
        start, length = span
        rng = f"bytes={start}-{start + length - 1}"
    else:
        span2 = parse_noaa_idx(text, field=wanted)
        if span2 is None:
            return None
        start, length2 = span2
        rng = f"bytes={start}-" if length2 is None else f"bytes={start}-{start + length2 - 1}"
    return _get(url, {"Range": rng}, cap=MAX_MESSAGE_BYTES)


def mask_message(product: str, run_init: datetime) -> bytes | None:
    """The land field that places the stations for ``product``'s run."""

    spec = SPECS[product]
    if spec.mask == "ecmwf_lsm":
        return read_message(product, run_init, 0, field="lsm")
    # GFS's own analysis LAND at the same init; AIGFS and GDPS share the GFS
    # cycle of their init (every init they issue is a GFS cycle too).
    return read_message("GFS", run_init, 0, field="LAND:surface")


def run_published(product: str, run_init: datetime, step: int | None = None) -> bool:
    """Is the run out to the step the lane needs?  One small read."""

    spec = SPECS[product]
    wanted = int(step if step is not None else spec.steps()[-1])
    url = index_url(product, run_init, wanted)
    if url is None:
        try:
            response = _client().head(step_url(product, run_init, wanted))
        except httpx.HTTPError:
            return False
        return response.status_code == 200
    return _get(url, cap=MAX_INDEX_BYTES) is not None


def local_day_steps(
    *, product: str, run_init: datetime, event_date: date, utc_offset_hours: int,
) -> list[int]:
    start = datetime.combine(event_date, datetime.min.time(), tzinfo=timezone.utc) - timedelta(
        hours=utc_offset_hours
    )
    stamp = _utc(run_init)
    return [
        step for step in SPECS[product].steps()
        if start <= stamp + timedelta(hours=step) < start + timedelta(days=1)
    ]


class RegularLatLonReader:
    """Stations out of global regular latitude/longitude fields, fast.

    ``GridPointReader`` proves CONUS layouts and leaves an unproven layout to
    the eccodes search, which is right and slow: the GDPS 0.15 degree field
    (2400 x 1201) cost 0.27 s a station a step, 442 s for one run of the
    roster (measured 2026-10-11).  On a ``regular_ll`` grid the nearest cell
    is arithmetic -- the rounded offset from the first grid point in each
    direction -- so one ``values`` read serves every station.  Not trusted
    either: on the first message of each grid every station is re-read by
    ``nearest_point_kelvin`` and keeps its index only where the two values
    agree exactly; any other station, or any non-``regular_ll`` grid, is read
    the slow way.  The fast path never returns a value the search would not.
    """

    def __init__(self, points: "list[tuple[float, float]]", *, slow=nearest_point_kelvin) -> None:
        self._points = [(float(lat), float(lon)) for lat, lon in points]
        self._indexes: dict[str, list[int | None]] = {}
        self._slow = slow

    @staticmethod
    def _string(library: Any, handle: Any, key: bytes) -> str | None:
        import ctypes  # noqa: PLC0415

        library.codes_get_string.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t),
        ]
        library.codes_get_string.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(128)
        length = ctypes.c_size_t(128)
        if library.codes_get_string(handle, key, buffer, ctypes.byref(length)) != 0:
            return None
        return buffer.value.decode("ascii", "replace")

    @staticmethod
    def _double(library: Any, handle: Any, key: bytes) -> float | None:
        import ctypes  # noqa: PLC0415

        value = ctypes.c_double()
        library.codes_get_double.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_double)]
        library.codes_get_double.restype = ctypes.c_int
        if library.codes_get_double(handle, key, ctypes.byref(value)) != 0:
            return None
        return float(value.value)

    def _derive(self, library: Any, handle: Any, message: bytes, values: Any) -> list[int | None]:
        long_ = GridPointReader._long
        if self._string(library, handle, b"gridType") != "regular_ll":
            return [None] * len(self._points)
        ni, nj = long_(library, handle, b"Ni"), long_(library, handle, b"Nj")
        lat0 = self._double(library, handle, b"latitudeOfFirstGridPointInDegrees")
        lon0 = self._double(library, handle, b"longitudeOfFirstGridPointInDegrees")
        di = self._double(library, handle, b"iDirectionIncrementInDegrees")
        dj = self._double(library, handle, b"jDirectionIncrementInDegrees")
        i_neg = long_(library, handle, b"iScansNegatively")
        j_pos = long_(library, handle, b"jScansPositively")
        j_cons = long_(library, handle, b"jPointsAreConsecutive")
        if None in (ni, nj, lat0, lon0, di, dj, i_neg, j_pos, j_cons) or i_neg or j_cons or not di or not dj:
            return [None] * len(self._points)
        if int(ni) * int(nj) != len(values):
            return [None] * len(self._points)
        out: list[int | None] = []
        for lat, lon in self._points:
            i = int(round(((lon - lon0) % 360.0) / di)) % int(ni)
            j = int(round(((lat - lat0) if j_pos else (lat0 - lat)) / dj))
            if not 0 <= j < int(nj):
                out.append(None)
                continue
            index = j * int(ni) + i
            # Proof: the eccodes search must return this very value.
            kelvin, _distance = self._slow(message, latitude=lat, longitude=lon)
            out.append(index if float(values[index]) == float(kelvin) else None)
        return out

    def kelvin(self, message: bytes) -> "list[float | None]":
        import ctypes  # noqa: PLC0415

        from .dwd_icon_eps_archive import _eccodes  # noqa: PLC0415

        library = _eccodes()
        buffer = ctypes.create_string_buffer(message, len(message))
        handle = library.codes_handle_new_from_message(None, buffer, ctypes.c_size_t(len(message)))
        if not handle:
            raise ContractError("native_deterministic_message_unreadable")
        try:
            if GridPointReader._long(library, handle, b"bitmapPresent"):
                return [None] * len(self._points)
            identity = GridPointReader._grid_identity(library, handle)
            values = GridPointReader._array(library, handle, b"values")
            if identity not in self._indexes:
                self._indexes[identity] = self._derive(library, handle, message, values)
            return [
                float(values[index]) if index is not None else None
                for index in self._indexes[identity]
            ]
        finally:
            library.codes_handle_delete(handle)


def fetch_run_for_targets(
    *,
    product: str,
    run_init: datetime,
    targets: Sequence[Mapping[str, Any]],
    reader: Callable[[str, datetime, int], bytes | None] | None = None,
    mask_reader: Callable[[str, datetime], bytes | None] | None = None,
    decode: Callable[..., tuple[float, float]] = nearest_point_kelvin,
    land: Callable[[bytes, list[tuple[float, float]]], list[Any]] = nearest_land_points,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Every target's local-day curve (step -> C) from one read per step.

    ``reader``/``mask_reader``/``decode``/``land`` are the network and eccodes
    seams (tests replace them).  A station no land cell within reach can place
    is left out; a run without its mask is not read at all rather than read at
    an unchecked cell.
    """

    if product not in SPECS:
        raise ContractError("native_deterministic_product_unknown")
    read = reader or (lambda p, r, s: read_message(p, r, s))
    read_mask = mask_reader or mask_message
    wanted: dict[tuple[str, str], list[int]] = {}
    for target in targets:
        key = (str(target["station_id"]), target["event_date"].isoformat())
        wanted[key] = local_day_steps(
            product=product, run_init=run_init, event_date=target["event_date"],
            utc_offset_hours=int(target["utc_offset_hours"]),
        )
    union = sorted({step for steps in wanted.values() for step in steps})
    if not union:
        return {}
    mask = read_mask(product, run_init)
    if mask is None:
        return {}
    stations: dict[str, tuple[float, float]] = {}
    for target in targets:
        stations.setdefault(
            str(target["station_id"]), (float(target["latitude"]), float(target["longitude"]))
        )
    order = sorted(stations)
    sampled = dict(zip(order, land(mask, [stations[s] for s in order])))
    values: dict[tuple[str, str], dict[int, float]] = {key: {} for key in wanted}
    placed = [s for s in order if sampled.get(s) is not None]
    # One grid search per station per run, proven against eccodes on the
    # first message of each grid (``GridPointReader``); a station it cannot
    # prove is read by the eccodes search itself.  Only on the real decoder:
    # a test seam replaces ``decode`` and the fast path with it.
    grid_reader = (
        RegularLatLonReader([
            (float(sampled[s]["latitude"]), float(sampled[s]["longitude"])) for s in placed
        ])
        if decode is nearest_point_kelvin else None
    )
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for step, message in pool.map(lambda s: (s, read(product, run_init, s)), union):
            if message is None:
                continue
            fast = grid_reader.kelvin(message) if grid_reader is not None else [None] * len(placed)
            by_station = dict(zip(placed, fast))
            cache: dict[str, float] = {}
            for key, steps in wanted.items():
                if step not in steps:
                    continue
                point = sampled.get(key[0])
                if point is None:
                    continue
                if key[0] not in cache:
                    kelvin = by_station.get(key[0])
                    if kelvin is None:
                        kelvin, _distance = decode(
                            message, latitude=float(point["latitude"]), longitude=float(point["longitude"]),
                        )
                    if not 180.0 <= float(kelvin) <= 340.0:
                        raise ContractError("native_deterministic_value_out_of_range")
                    cache[key[0]] = float(kelvin) - 273.15
                values[key][step] = cache[key[0]]
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for key, steps in wanted.items():
        point = sampled.get(key[0])
        if point is None:
            continue
        url = run_directory_url(product, run_init)
        if point.get("moved"):
            url += f"#land_cell={float(point['latitude']):.5f},{float(point['longitude']):.5f}"
        results[key] = {"hourly_c": values[key], "hours_wanted": len(steps), "source_url": url}
    return results


__all__ = [
    "MAX_CAPTURE_STEP", "SPECS", "close_client", "fetch_run_for_targets",
    "index_url", "local_day_steps", "mask_message", "parse_ecmwf_index",
    "parse_noaa_idx", "read_message", "run_directory_url", "run_published",
    "step_url",
]
