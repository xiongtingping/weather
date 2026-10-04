"""Canonical station coordinates for the isolated target model decoder.

Coordinates are station metadata, not a forecast input.  The registry is a
small, immutable projection of the official AWC ``stationinfo`` snapshot used
by the target's 45-station arrival corpus.  A station absent from this
registry is deliberately unusable: callers must not silently decode it at the
ZUUU coordinate or at any other guessed location.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .contracts import ContractError


SCHEMA = "bayes_basket_target_station_coordinate_registry_v1"
SOURCE_SCHEMA = "awc_stationinfo_v1"
SOURCE_ID = "awc_stationinfo"
SOURCE_URL = "https://aviationweather.gov/api/data/stationinfo"
SOURCE_SNAPSHOT_SHA256 = "baddc3d44bdd441536be23b6d5dc88477bc3e1da1aecf564f6ca1fb068e4e343"

# Official AWC stationinfo lat/lon values.  Keep this projection intentionally
# small: elevation, city labels, and provider fields do not belong in the
# native model decoder's coordinate contract.
_COORDINATES: dict[str, tuple[float, float]] = {
    "CYYZ": (43.679, -79.629),
    "EDDM": (48.348, 11.813),
    "EFHK": (60.327, 24.957),
    "EGLC": (51.505, 0.055),
    "EHAM": (52.315, 4.79),
    "EPWA": (52.163, 20.961),
    "FACT": (-33.965, 18.602),
    "KATL": (33.62972, -84.44223),
    "KAUS": (30.1831, -97.68063),
    "KBKF": (39.713, -104.758),
    "KDAL": (32.83836, -96.83584),
    "KHOU": (29.64582, -95.28214),
    "KLAX": (33.93817, -118.3866),
    "KLGA": (40.77945, -73.88027),
    "KMIA": (25.78806, -80.31692),
    "KORD": (41.96017, -87.93161),
    "KSEA": (47.44467, -122.31442),
    "KSFO": (37.61961, -122.36561),
    "LEMD": (40.466, -3.555),
    "LFPB": (48.967, 2.428),
    "LIMC": (45.631, 8.728),
    "LLBG": (32.011, 34.887),
    "LTAC": (40.128, 32.995),
    "LTFM": (41.262, 28.74),
    "MMMX": (19.436, -99.072),
    "MPMG": (8.967, -79.555),
    "NZWN": (-41.331, 174.806),
    "OEJN": (21.685, 39.166),
    "OPKC": (24.902, 67.139),
    "RCSS": (25.069, 121.552),
    "RJTT": (35.553, 139.781),
    "RKPK": (35.179, 128.938),
    "RKSI": (37.469, 126.451),
    "RPLL": (14.507, 121.004),
    "SAEZ": (-34.822, -58.536),
    "SBGR": (-23.432, -46.469),
    "UUWW": (55.592, 37.261),
    "VILK": (26.761, 80.889),
    "WMKK": (2.747, 101.714),
    "WSSS": (1.368, 103.982),
    "ZBAA": (40.082, 116.603),
    "ZGGG": (23.392, 113.307),
    "ZGSZ": (22.639, 113.803),
    "ZHHH": (30.783, 114.205),
    "ZSPD": (31.146, 121.8),
    "ZSQD": (36.362, 120.087),
    "ZUCK": (29.718, 106.639),
    "ZUUU": (30.576, 103.95),
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


REGISTRY_SHA256 = hashlib.sha256(_canonical(_COORDINATES)).hexdigest()


def station_coordinates(station_id: str) -> dict[str, Any]:
    """Return one validated coordinate plus its immutable source lineage."""

    station = str(station_id or "").strip().upper()
    pair = _COORDINATES.get(station)
    if pair is None:
        raise ContractError("native_station_coordinates_missing")
    latitude, longitude = pair
    if not all((math.isfinite(latitude), math.isfinite(longitude))) or not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise ContractError("native_station_coordinates_invalid")
    return {
        "station_id": station,
        "latitude": latitude,
        "longitude": longitude,
        "registry_schema": SCHEMA,
        "registry_sha256": REGISTRY_SHA256,
        "source_schema": SOURCE_SCHEMA,
        "source_id": SOURCE_ID,
        "source_url": SOURCE_URL,
        "source_snapshot_sha256": SOURCE_SNAPSHOT_SHA256,
    }


def coordinate_registry_manifest() -> dict[str, Any]:
    """Return a compact audit manifest without duplicating station metadata."""

    return {
        "schema": SCHEMA,
        "registry_sha256": REGISTRY_SHA256,
        "source_schema": SOURCE_SCHEMA,
        "source_id": SOURCE_ID,
        "source_url": SOURCE_URL,
        "source_snapshot_sha256": SOURCE_SNAPSHOT_SHA256,
        "station_count": len(_COORDINATES),
        "station_ids": sorted(_COORDINATES),
    }


__all__ = ["REGISTRY_SHA256", "SCHEMA", "coordinate_registry_manifest", "station_coordinates"]
