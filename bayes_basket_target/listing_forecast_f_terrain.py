"""Charter 267 补记六: the terrain table the station-value F_v3 reads.

A member's value is read at a grid cell whose model terrain height is not the
station's: a cell 200 m above the airport reads ~1.3 C cold every day.  The
benchmark cluster's centre follows station values -- the grid value brought to
the station's elevation at the standard lapse rate (research rounds 54-56:
GFS on its 13 km flux grid, nearest land cell + 0.0065 C/m x (cell height -
station height), reproduces Open-Meteo's station value with corr 0.94; the
same rule lifts the raw GFS member's MAE from 1.78 to 1.27 C).  This module
builds, once, the table ``listing_forecast_f`` reads for F_v3: for every F_v3
member and roster station, the model terrain height at the very cell that
member's rows are sampled at, the station's elevation (AWC station information,
the official METAR source's own metadata) and the offset
``LAPSE_RATE_C_PER_M x (cell height - station height)`` -- positive when the
cell is higher (colder) than the station.  Terrain is time-invariant: rebuilt
only on demand (``scripts/build_listing_forecast_f_terrain.py``) or when a
member's cell rule changes.

Sources, one per member, each from its own publisher:
  AIFS / IFS_HRES  the run's own surface ``z`` (geopotential / g), cell by its own ``lsm``
  GFS / AIGFS      GFS ``HGT:surface`` at f000, cell by GFS ``LAND`` (AIGFS runs on GFS's grid)
  GFS_SFLUX        the flux grid's own ``HGT:surface`` at f000, cell by its own ``LAND``
  GDPS             MSC Datamart ``GeopotentialHeight_Sfc`` at PT000H, GDPS cell nearest the GFS land cell
  ICON_GLOBAL      the time-invariant ``HSURF`` at the placed icosahedral cell
Read-only on the SSOT; writes one runtime file; never the SSOT.  Execution
layer only: F_v3 is not a probability and enters no P0/P0-M/P1 or authority.
"""

from __future__ import annotations

import ctypes
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import ContractError

ROOT = Path(__file__).resolve().parents[1]
TERRAIN_PATH = Path(
    os.environ.get("PM_LISTING_FORECAST_F_TERRAIN_PATH") or ROOT / "data" / "runtime" / "listing_forecast_f_terrain_v1.json"
)
SCHEMA = "listing_forecast_f_terrain_v1"
LAPSE_RATE_C_PER_M = 0.0065
GRAVITY = 9.80665
AWC_STATION_INFO_URL = "https://aviationweather.gov/api/data/stationinfo"
GDPS_OROGRAPHY = "GeopotentialHeight_Sfc"
# The F_v3 members the table carries (products of ``regional_point_lane.SYSTEM_IDS``).
PRODUCTS = ("AIFS", "IFS_HRES", "GFS", "GFS_SFLUX", "AIGFS", "GDPS", "ICON_GLOBAL")


def nearest_value(message: bytes, *, latitude: float, longitude: float) -> float:
    """The value of the grid cell eccodes finds nearest the point (any field)."""

    from .noaa_conus_archive import _bind_nearest, _eccodes  # noqa: PLC0415

    library = _eccodes()
    _bind_nearest(library)
    buffer = ctypes.create_string_buffer(message, len(message))
    handle = library.codes_handle_new_from_message(None, buffer, ctypes.c_size_t(len(message)))
    if not handle:
        raise ContractError("listing_terrain_message_unreadable")
    try:
        err = ctypes.c_int()
        nearest = library.codes_grib_nearest_new(handle, ctypes.byref(err))
        if not nearest or err.value != 0:
            raise ContractError("listing_terrain_nearest_unavailable")
        try:
            lats, lons = (ctypes.c_double * 4)(), (ctypes.c_double * 4)()
            values, distances = (ctypes.c_double * 4)(), (ctypes.c_double * 4)()
            indexes = (ctypes.c_int * 4)()
            length = ctypes.c_size_t(4)
            if library.codes_grib_nearest_find(
                nearest, handle, ctypes.c_double(latitude), ctypes.c_double(longitude % 360.0), 0,
                lats, lons, values, distances, indexes, ctypes.byref(length),
            ) != 0:
                raise ContractError("listing_terrain_nearest_find_failed")
            best = min(range(int(length.value)), key=lambda i: distances[i])
            return float(values[best])
        finally:
            library.codes_grib_nearest_delete(nearest)
    finally:
        library.codes_handle_delete(handle)


def station_elevations(stations: Sequence[str], *, get: Callable[..., Any] | None = None) -> dict[str, float]:
    """Elevation (m) of each station from AWC station information."""

    if get is None:
        from .native_deterministic_point_archive import _client  # noqa: PLC0415

        get = _client().get
    response = get(AWC_STATION_INFO_URL, params={"ids": ",".join(stations), "format": "json"})
    if getattr(response, "status_code", 200) != 200:
        raise ContractError("listing_terrain_station_info_unavailable")
    out: dict[str, float] = {}
    for row in response.json():
        station, elevation = str(row.get("icaoId") or "").upper(), row.get("elev")
        if station and elevation is not None:
            out[station] = float(elevation)
    return out


def _land_cells(mask: bytes, points: Mapping[str, tuple[float, float]]) -> dict[str, dict[str, Any] | None]:
    from .noaa_conus_archive import nearest_land_points  # noqa: PLC0415

    order = sorted(points)
    return dict(zip(order, nearest_land_points(mask, [points[s] for s in order])))


def model_heights(product: str, run_init: datetime, points: Mapping[str, tuple[float, float]]) -> dict[str, dict[str, Any]]:
    """station -> {cell_lat, cell_lon, height_m, source} for one member."""

    from . import native_deterministic_point_archive as native  # noqa: PLC0415

    out: dict[str, dict[str, Any]] = {}
    if product in ("AIFS", "IFS_HRES"):
        if product == "AIFS":
            from . import aifs_single_point_archive as aifs  # noqa: PLC0415

            mask, orog = aifs._message(run_init, 0, "lsm"), aifs._message(run_init, 0, "z")
            source = aifs.step_stem(run_init, 0)
        else:
            mask = native.read_message("IFS_HRES", run_init, 0, field="lsm")
            orog = native.read_message("IFS_HRES", run_init, 0, field="z")
            source = native.step_url("IFS_HRES", run_init, 0)
        if mask is None or orog is None:
            raise ContractError(f"listing_terrain_orography_unavailable:{product}")
        for station, cell in _land_cells(mask, points).items():
            if cell is None:
                continue
            z = nearest_value(orog, latitude=cell["latitude"], longitude=cell["longitude"]) / GRAVITY
            out[station] = {"cell_lat": cell["latitude"], "cell_lon": cell["longitude"], "height_m": round(z, 1),
                            "source": f"{source}#z"}
        return out
    if product in ("GFS", "AIGFS", "GDPS", "GFS_SFLUX"):
        grid = "GFS_SFLUX" if product == "GFS_SFLUX" else "GFS"
        mask = native.mask_message(grid, run_init)
        hgt = native.read_message(grid, run_init, 0, field="HGT:surface")
        source = native.step_url(grid, run_init, 0) + "#HGT:surface"
        if product == "GDPS":
            url = native.step_url("GDPS", run_init, 0).replace("AirTemp_AGL-2m", GDPS_OROGRAPHY)
            hgt = native._get(url, cap=native.MAX_MESSAGE_BYTES)
            source = url
        if mask is None or hgt is None:
            raise ContractError(f"listing_terrain_orography_unavailable:{product}")
        for station, cell in _land_cells(mask, points).items():
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
            raise ContractError("listing_terrain_orography_unavailable:ICON_GLOBAL")
        field = native._decode_values(message)
        cells = native.icon_station_cells(run_init, points, point_count=int(len(field)))
        for station, cell in cells.items():
            if cell is None:
                continue
            out[station] = {"cell_lat": cell["latitude"], "cell_lon": cell["longitude"],
                            "height_m": round(float(field[int(cell["index"])]), 1), "source": url}
        return out
    raise ContractError(f"listing_terrain_product_unsupported:{product}")


def build(conn: Any, *, run_init: datetime, products: Sequence[str] = PRODUCTS) -> dict[str, Any]:
    """The table for ``products`` at every roster station."""

    from .deterministic_forecast_family import global_roster  # noqa: PLC0415
    from .regional_point_lane import SYSTEM_IDS  # noqa: PLC0415

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


def write(payload: Mapping[str, Any], path: Path = TERRAIN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=1))
    os.replace(tmp, path)


def read(path: Path = TERRAIN_PATH) -> dict[str, Any] | None:
    """The table as written, or None (missing / unreadable / wrong schema): F_v3 then fails closed."""

    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA or not isinstance(payload.get("systems"), dict):
        return None
    return payload


def offset_c(table: Mapping[str, Any], *, system_id: str, station_id: str) -> float | None:
    """The member's lapse-rate offset at the station, or None when the table has no cell for it."""

    entry = (table.get("systems") or {}).get(system_id, {}).get(station_id)
    if not entry or entry.get("offset_c") is None:
        return None
    return float(entry["offset_c"])


__all__ = [
    "LAPSE_RATE_C_PER_M", "PRODUCTS", "SCHEMA", "TERRAIN_PATH", "build", "model_heights", "nearest_value",
    "offset_c", "read", "station_elevations", "write",
]
