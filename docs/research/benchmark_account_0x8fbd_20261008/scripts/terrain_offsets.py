"""Round 39h tool (research, not production): terrain offsets of F's grid values.

Built for the user's 10-12 ruling 「在格点值上做显式海拔订正」; the paired test
(``prod_f_elevation_r39h.py``) found the adjustment moves F away from the
cluster, so it is not in production.  Original rationale follows.

User ruling 2026-10-12 「在格点值上做显式海拔订正」.  F's members are read at a
grid cell (the nearest land cell, ``nearest_land_cell_v1``), whose model
terrain height is not the station's: a cell 200 m above the airport reads
~1.3 C cold, every day, in every run.  The research F (Open-Meteo point values,
which the publisher adjusts to the point's elevation) did not carry that
offset; ours does, and F applies only part of each station's bias (lambda),
so most of it reached F's bucket (benchmark research round 39e/39f).

This module builds, once, the table F reads: for every F member and roster
station, the model terrain height at the very cell that member's rows are
sampled at, the station's elevation (AWC station information, the official
METAR source's own metadata), and the adjustment
``LAPSE_RATE_C_PER_M x (cell height - station height)`` (standard atmosphere;
positive when the cell is higher, i.e. colder).  Terrain is time-invariant:
the table is rebuilt only when asked (or when a member's cell rule changes).

Sources, one per member, each from its own publisher:
  AIFS / IFS_HRES -- the run's own surface ``z`` (geopotential / g), cell by its own ``lsm``
  GFS / AIGFS     -- GFS ``HGT:surface`` at f000, cell by GFS ``LAND`` (AIGFS runs on GFS's grid)
  GDPS            -- the MSC Datamart ``GeopotentialHeight_Sfc`` at PT000H, GDPS cell nearest the GFS land cell
  ICON_GLOBAL     -- the time-invariant ``HSURF`` at the placed icosahedral cell
Read-only on the SSOT; writes one runtime file.  Execution layer only.
"""

from __future__ import annotations

import ctypes
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from bayes_basket_target.contracts import ContractError  # noqa: E402

TABLE_PATH = Path(__file__).resolve().parents[1] / "terrain_offsets_r39h.json"
SCHEMA = "listing_forecast_f_elevation_v1"
LAPSE_RATE_C_PER_M = 0.0065
GRAVITY = 9.80665
AWC_STATION_INFO_URL = "https://aviationweather.gov/api/data/stationinfo"
GDPS_OROGRAPHY = "GeopotentialHeight_Sfc"


def nearest_value(message: bytes, *, latitude: float, longitude: float) -> float:
    """The value of the grid cell eccodes finds nearest the point (any field)."""

    from bayes_basket_target.noaa_conus_archive import _bind_nearest, _eccodes  # noqa: PLC0415

    library = _eccodes()
    _bind_nearest(library)
    buffer = ctypes.create_string_buffer(message, len(message))
    handle = library.codes_handle_new_from_message(None, buffer, ctypes.c_size_t(len(message)))
    if not handle:
        raise ContractError("listing_elevation_message_unreadable")
    try:
        err = ctypes.c_int()
        nearest = library.codes_grib_nearest_new(handle, ctypes.byref(err))
        if not nearest or err.value != 0:
            raise ContractError("listing_elevation_nearest_unavailable")
        try:
            lats, lons = (ctypes.c_double * 4)(), (ctypes.c_double * 4)()
            values, distances = (ctypes.c_double * 4)(), (ctypes.c_double * 4)()
            indexes = (ctypes.c_int * 4)()
            length = ctypes.c_size_t(4)
            if library.codes_grib_nearest_find(
                nearest, handle, ctypes.c_double(latitude), ctypes.c_double(longitude % 360.0), 0,
                lats, lons, values, distances, indexes, ctypes.byref(length),
            ) != 0:
                raise ContractError("listing_elevation_nearest_find_failed")
            best = min(range(int(length.value)), key=lambda i: distances[i])
            return float(values[best])
        finally:
            library.codes_grib_nearest_delete(nearest)
    finally:
        library.codes_handle_delete(handle)


def station_elevations(stations: Sequence[str], *, get: Callable[..., Any] | None = None) -> dict[str, float]:
    """Elevation (m) of each station from AWC station information."""

    if get is None:
        from bayes_basket_target.native_deterministic_point_archive import _client  # noqa: PLC0415

        get = _client().get
    response = get(AWC_STATION_INFO_URL, params={"ids": ",".join(stations), "format": "json"})
    if getattr(response, "status_code", 200) != 200:
        raise ContractError("listing_elevation_station_info_unavailable")
    out = {}
    for row in response.json():
        station, elevation = str(row.get("icaoId") or "").upper(), row.get("elev")
        if station and elevation is not None:
            out[station] = float(elevation)
    return out


def _regular_cells(mask: bytes, points: Mapping[str, tuple[float, float]]) -> dict[str, dict[str, Any] | None]:
    from bayes_basket_target.noaa_conus_archive import nearest_land_points  # noqa: PLC0415

    order = sorted(points)
    return dict(zip(order, nearest_land_points(mask, [points[s] for s in order])))


def model_heights(product: str, run_init: datetime, points: Mapping[str, tuple[float, float]]) -> dict[str, dict[str, Any]]:
    """station -> {cell_lat, cell_lon, height_m, source} for one F member."""

    from bayes_basket_target import native_deterministic_point_archive as native  # noqa: PLC0415

    out: dict[str, dict[str, Any]] = {}
    if product in ("AIFS", "IFS_HRES"):
        if product == "AIFS":
            from bayes_basket_target import aifs_single_point_archive as aifs  # noqa: PLC0415

            mask, orog = aifs._message(run_init, 0, "lsm"), aifs._message(run_init, 0, "z")
            source = aifs.step_stem(run_init, 0)
        else:
            mask = native.read_message("IFS_HRES", run_init, 0, field="lsm")
            orog = native.read_message("IFS_HRES", run_init, 0, field="z")
            source = native.step_url("IFS_HRES", run_init, 0)
        if mask is None or orog is None:
            raise ContractError(f"listing_elevation_orography_unavailable:{product}")
        for station, cell in _regular_cells(mask, points).items():
            if cell is None:
                continue
            z = nearest_value(orog, latitude=cell["latitude"], longitude=cell["longitude"]) / GRAVITY
            out[station] = {"cell_lat": cell["latitude"], "cell_lon": cell["longitude"], "height_m": round(z, 1),
                            "source": f"{source}#z"}
        return out
    if product in ("GFS", "AIGFS", "GDPS"):
        mask = native.mask_message("GFS", run_init)
        hgt = native.read_message("GFS", run_init, 0, field="HGT:surface")
        if product == "GDPS":
            url = native.step_url("GDPS", run_init, 0).replace("AirTemp_AGL-2m", GDPS_OROGRAPHY)
            hgt = native._get(url, cap=native.MAX_MESSAGE_BYTES)
            source = url
        else:
            source = native.step_url("GFS", run_init, 0) + "#HGT:surface"
        if mask is None or hgt is None:
            raise ContractError(f"listing_elevation_orography_unavailable:{product}")
        for station, cell in _regular_cells(mask, points).items():
            if cell is None:
                continue
            z = nearest_value(hgt, latitude=cell["latitude"], longitude=cell["longitude"])
            out[station] = {"cell_lat": cell["latitude"], "cell_lon": cell["longitude"], "height_m": round(z, 1),
                            "source": source}
        return out
    if product == "ICON_GLOBAL":
        url = native.icon_invariant_url(run_init, "HSURF")
        message = native._read_bz2_message(url)
        if message is None:
            raise ContractError("listing_elevation_orography_unavailable:ICON_GLOBAL")
        field = native._decode_values(message)
        cells = native.icon_station_cells(run_init, points, point_count=int(len(field)))
        for station, cell in cells.items():
            if cell is None:
                continue
            out[station] = {"cell_lat": cell["latitude"], "cell_lon": cell["longitude"],
                            "height_m": round(float(field[int(cell["index"])]), 1), "source": url}
        return out
    raise ContractError(f"listing_elevation_product_unsupported:{product}")


def build(conn: Any, *, run_init: datetime, products: Sequence[str]) -> dict[str, Any]:
    """The table for ``products`` at every roster station."""

    from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: PLC0415
    from bayes_basket_target.regional_point_lane import SYSTEM_IDS  # noqa: PLC0415

    roster = global_roster(conn)
    points = {s: (lat, lon) for s, lat, lon, _tz in roster}
    elevations = station_elevations(sorted(points))
    systems: dict[str, Any] = {}
    for product in products:
        heights = model_heights(product, run_init, points)
        systems[SYSTEM_IDS[product]] = {
            station: {**h, "station_elevation_m": elevations[station],
                      "offset_c": round(LAPSE_RATE_C_PER_M * (h["height_m"] - elevations[station]), 3)}
            for station, h in sorted(heights.items()) if station in elevations
        }
    return {
        "schema": SCHEMA, "built_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lapse_rate_c_per_m": LAPSE_RATE_C_PER_M, "terrain_run_init_utc": run_init.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "station_elevation_source": AWC_STATION_INFO_URL, "systems": systems,
        "authority": "EXECUTION_LAYER_ONLY",
    }


def write(payload: Mapping[str, Any], path: Path = TABLE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=1))
    os.replace(tmp, path)


def read(path: Path = TABLE_PATH) -> dict[str, dict[str, float]]:
    """system -> station -> offset (C); empty when the table is missing."""

    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {
        system: {station: float(cell["offset_c"]) for station, cell in cells.items()}
        for system, cells in (payload.get("systems") or {}).items()
    }


__all__ = ["LAPSE_RATE_C_PER_M", "TABLE_PATH", "build", "model_heights", "nearest_value", "read",
           "station_elevations", "write"]
