"""Decode target-only native GRIB temperature points without retaining payloads.

This module is deliberately downstream of the prospective native-provider
arrival collector.  It reads an already retained, content-addressed GRIB
object, asks the official ecCodes ``grib_get`` binary for the nearest point,
and returns only small lineage-bound facts.  It never contacts a provider,
opens SQLite, trains a model, or creates production authority.

Point decoding and daily-curve eligibility are separate contracts.  A single
valid time is useful transport evidence, but it is not a daily maximum and is
therefore never returned as an eligible calibration row.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import bz2
import gzip
import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError, content_sha256
from .station_registry import station_coordinates


SCHEMA = "bayes_basket_target_native_model_point_evidence_v1"
CURVE_SCHEMA = "bayes_basket_target_native_model_daily_curve_v1"
MODEL_BY_PROVIDER = {
    "ecmwf_ifs025_official": "ECMWF",
    "ecmwf_aifs025_official": "ECMWF",
    "gfs_0p25_official": "GFS",
    "icon_global_official": "ICON",
    "gem_gdps_official": "GEM",
    "cma_grapes_global_open_meteo": "CMA",
}
# These limits apply only to transient decompression/decoder output.  The
# retained raw budget is enforced by the arrival collector itself.
MAX_DECOMPRESSED_PAYLOAD_BYTES = 256 * 1024 * 1024
MAX_GRIB_GET_STDOUT_BYTES = 4 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
MAX_CURVE_GAP_HOURS = 6.0
MIN_CURVE_POINTS = 4
MIN_CURVE_POINTS_BY_PROVIDER = {
    # A local day whose two endpoints fall exactly between AIFS Single's
    # six-hour publication steps has three in-day points (for example NZWN
    # at 18Z/00Z/06Z).  The independent max-gap and ±6h endpoint gates still
    # prove complete 24-hour coverage; a fourth in-day point does not exist.
    "ecmwf_aifs025_official": 3,
}
DEFAULT_CURVE_ENDPOINT_TOLERANCE_HOURS = 3.0
CURVE_ENDPOINT_TOLERANCE_HOURS_BY_PROVIDER = {
    # AIFS Single Open Data is natively six-hourly.  Requiring a point within
    # three hours of both timezone-dependent day edges makes half of all
    # settlement zones impossible despite a complete native cadence.  The
    # endpoint tolerance may match one native interval; the separate six-hour
    # max-gap gate remains unchanged.
    "ecmwf_aifs025_official": 6.0,
}
DEFAULT_ICON_SUPPORT_ROOT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "runtime"
    / "official_nwp_sources"
    / "icon"
    / "support"
    / "ICON_GLOBAL2WORLD_025_EASY"
)
_ICON_SOURCE_INDEX_CACHE: dict[str, dict[str, int]] = {}


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> datetime:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _free_bytes(path: Path) -> int:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return int(shutil.disk_usage(candidate).free)


def _bounded_binary(value: str | None = None) -> str:
    candidates = [
        str(value or "").strip(),
        str(os.environ.get("PM_BAYES_BASKET_GRIB_GET") or "").strip(),
        shutil.which("grib_get") or "",
        "/opt/homebrew/opt/eccodes/bin/grib_get",
        "/usr/local/bin/grib_get",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ContractError("native_grib_get_unavailable")


def _bounded_cdo(value: str | None = None) -> str:
    candidates = [
        str(value or "").strip(),
        str(os.environ.get("PM_BAYES_BASKET_CDO") or "").strip(),
        shutil.which("cdo") or "",
        "/opt/homebrew/bin/cdo",
        "/usr/local/bin/cdo",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ContractError("native_icon_cdo_unavailable")


def _eccodes_modules() -> tuple[Any, Any] | None:
    """Load the native ecCodes stack only when the target runtime provides it."""

    try:
        import eccodes  # type: ignore[import-not-found]
        import netCDF4  # type: ignore[import-not-found]
    except ImportError:
        return None
    return eccodes, netCDF4


def _icon_source_indices(weights_path: Path) -> dict[str, int]:
    """Resolve canonical stations through the official DWD nearest-neighbour map."""

    cache_key = str(weights_path.resolve())
    cached = _ICON_SOURCE_INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    modules = _eccodes_modules()
    if modules is None:
        raise ContractError("native_icon_eccodes_dependencies_missing")
    _, netcdf4 = modules
    try:
        from .station_registry import coordinate_registry_manifest

        with netcdf4.Dataset(str(weights_path), "r") as dataset:
            dimensions = [int(value) for value in dataset.variables["dst_grid_dims"][:]]
            src_address = dataset.variables["src_address"]
            if len(dimensions) != 2 or dimensions[0] <= 0 or dimensions[1] <= 0:
                raise ContractError("native_icon_weights_dimensions_invalid")
            x_size, y_size = dimensions
            station_indices: dict[str, int] = {}
            for station_id in coordinate_registry_manifest()["station_ids"]:
                coordinates = station_coordinates(station_id)
                # The supplied DWD target grid is lon=0..359.75, lat=-90..90
                # in row-major order.  The weights file's dst_address is
                # one-based, as required by the SCRIP/NetCDF contract.
                x_index = int(round((float(coordinates["longitude"]) % 360.0) / 0.25)) % x_size
                y_index = int(round((float(coordinates["latitude"]) + 90.0) / 0.25))
                if not 0 <= y_index < y_size:
                    raise ContractError("native_icon_station_grid_index_invalid")
                destination_index = y_index * x_size + x_index
                source_index = int(src_address[destination_index]) - 1
                if source_index < 0:
                    raise ContractError("native_icon_weights_source_index_invalid")
                station_indices[str(station_id)] = source_index
    except ContractError:
        raise
    except (OSError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise ContractError("native_icon_weights_read_failed") from exc
    _ICON_SOURCE_INDEX_CACHE[cache_key] = station_indices
    return station_indices


def _grib_time(handle: Any, eccodes: Any, *, date_key: str, time_key: str, field: str) -> datetime:
    try:
        return _grib_datetime(str(eccodes.codes_get(handle, date_key)), str(eccodes.codes_get(handle, time_key)), field)
    except Exception as exc:
        raise ContractError(f"native_grib_{field}_timestamp_invalid") from exc


def _decode_with_eccodes(
    raw_path: Path,
    *,
    provider_id: str,
    station_id: str,
    icon_grid_path: Path | None = None,
    icon_weights_path: Path | None = None,
) -> dict[str, Any] | None:
    """Decode one native message without shell binaries.

    ``None`` means the optional ecCodes runtime is absent; callers may use the
    explicit legacy test/runtime adapter in that case.  Once ecCodes is
    present, parser or lineage failures are hard contract errors and do not
    fall through to another decoder.
    """

    modules = _eccodes_modules()
    if modules is None:
        return None
    eccodes, _ = modules
    provider = _text("provider_id", provider_id).lower()
    coordinates = station_coordinates(station_id)
    with _prepared_payload(raw_path) as prepared:
        handle = None
        try:
            with prepared.open("rb") as stream:
                while True:
                    handle = eccodes.codes_grib_new_from_file(stream)
                    if handle is None:
                        break
                    try:
                        short_name = str(eccodes.codes_get(handle, "shortName") or "")
                        type_of_level = str(eccodes.codes_get(handle, "typeOfLevel") or "")
                        level = int(eccodes.codes_get(handle, "level"))
                        if short_name == "2t" and type_of_level == "heightAboveGround" and level == 2:
                            run_init = _grib_time(handle, eccodes, date_key="dataDate", time_key="dataTime", field="run")
                            valid_time = _grib_time(handle, eccodes, date_key="validityDate", time_key="validityTime", field="valid")
                            try:
                                step = int(eccodes.codes_get(handle, "step"))
                            except Exception:
                                step = 0
                            values = eccodes.codes_get_array(handle, "values")
                            if provider == "icon_global_official":
                                weights = (icon_weights_path or DEFAULT_ICON_SUPPORT_ROOT / "weights_icogl2world_025.nc").expanduser().resolve()
                                if not weights.is_file():
                                    raise ContractError("native_icon_remap_assets_missing")
                                source_index = _icon_source_indices(weights).get(str(coordinates["station_id"]))
                                if source_index is None or source_index >= len(values):
                                    raise ContractError("native_icon_source_value_index_invalid")
                                value = float(values[source_index])
                                decoder_id = "eccodes_icon_dwd_weights_nearest_v1"
                                remap_contract = "DWD_ICON_GLOBAL2WORLD_025_EASY"
                            else:
                                nearest = eccodes.codes_grib_find_nearest(
                                    handle,
                                    float(coordinates["latitude"]),
                                    float(coordinates["longitude"]),
                                    False,
                                    1,
                                )
                                if not nearest:
                                    raise ContractError("native_grib_nearest_point_missing")
                                value = float(nearest[0]["value"])
                                decoder_id = "eccodes_grib_nearest_point_temperature_2m_v1"
                                remap_contract = None
                            if not math.isfinite(value):
                                raise ContractError("native_grib_temperature_value_invalid")
                            return {
                                "schema": "bayes_basket_target_native_temperature_decode_v1",
                                "provider_id": provider,
                                "model_id": MODEL_BY_PROVIDER[provider],
                                "station_id": coordinates["station_id"],
                                "raw_path": str(raw_path.resolve()),
                                "selector": "shortName=2t,typeOfLevel=heightAboveGround,level=2",
                                "latitude": coordinates["latitude"],
                                "longitude": coordinates["longitude"],
                                "decoder_id": decoder_id,
                                "remap_contract": remap_contract,
                                "coordinate_lineage": coordinates,
                                "points": [{
                                    "grib_run_initialization_utc": _timestamp_text(run_init),
                                    "grib_valid_time_utc": _timestamp_text(valid_time),
                                    "step_hours": step,
                                    "temperature_2m_c": round(value - 273.15 if value > 150.0 else value, 6),
                                    "native_units": "K" if value > 150.0 else "C",
                                }],
                            }
                    finally:
                        eccodes.codes_release(handle)
                        handle = None
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError("native_eccodes_temperature_decode_failed") from exc
    raise ContractError("native_grib_temperature_field_missing")


@contextmanager
def _prepared_payload(raw_path: Path) -> Iterator[Path]:
    """Yield a GRIB path, decompressing only bounded compressed inputs."""

    if not raw_path.is_file():
        raise ContractError("native_raw_payload_missing")
    suffix = raw_path.suffix.lower()
    if suffix not in {".bz2", ".gz"}:
        yield raw_path
        return
    if _free_bytes(raw_path.parent) < MIN_FREE_BYTES + MAX_DECOMPRESSED_PAYLOAD_BYTES:
        raise ContractError("native_decode_min_free_space_guard")
    temporary = tempfile.NamedTemporaryFile(prefix="bayes-basket-native-", suffix=".grib2", delete=False)
    temporary_path = Path(temporary.name)
    size = 0
    try:
        with temporary:
            opener = bz2.open if suffix == ".bz2" else gzip.open
            with opener(raw_path, "rb") as source:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_DECOMPRESSED_PAYLOAD_BYTES:
                        raise ContractError("native_decompressed_payload_too_large")
                    if _free_bytes(raw_path.parent) < MIN_FREE_BYTES + len(block):
                        raise ContractError("native_decode_min_free_space_guard")
                    temporary.write(block)
            temporary.flush()
            os.fsync(temporary.fileno())
        yield temporary_path
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _prepared_provider_payload(
    raw_path: Path,
    *,
    provider_id: str,
    cdo_binary: str | None = None,
    icon_grid_path: Path | None = None,
    icon_weights_path: Path | None = None,
) -> Iterator[Path]:
    """Prepare a regular-grid payload only for native ICON's unstructured grid."""

    with _prepared_payload(raw_path) as prepared:
        if provider_id != "icon_global_official":
            yield prepared
            return
        cdo = _bounded_cdo(cdo_binary)
        grid = (icon_grid_path or DEFAULT_ICON_SUPPORT_ROOT / "target_grid_world_025.txt").expanduser().resolve()
        weights = (icon_weights_path or DEFAULT_ICON_SUPPORT_ROOT / "weights_icogl2world_025.nc").expanduser().resolve()
        if not grid.is_file() or not weights.is_file():
            raise ContractError("native_icon_remap_assets_missing")
        if _free_bytes(raw_path.parent) < MIN_FREE_BYTES + MAX_DECOMPRESSED_PAYLOAD_BYTES:
            raise ContractError("native_icon_remap_min_free_space_guard")
        remapped_handle = tempfile.NamedTemporaryFile(prefix="bayes-basket-icon-remap-", suffix=".grib2", delete=False)
        remapped_path = Path(remapped_handle.name)
        remapped_handle.close()
        try:
            command = [
                cdo,
                "-s",
                "-f",
                "grb2",
                f"remap,{grid},{weights}",
                str(prepared),
                str(remapped_path),
            ]
            completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)
            if completed.returncode != 0 or not remapped_path.is_file() or remapped_path.stat().st_size <= 0:
                raise ContractError("native_icon_remap_failed")
            if remapped_path.stat().st_size > MAX_DECOMPRESSED_PAYLOAD_BYTES:
                raise ContractError("native_icon_remap_output_too_large")
            yield remapped_path
        finally:
            remapped_path.unlink(missing_ok=True)


@contextmanager
def prepare_native_temperature_payload(
    raw_path: Path,
    *,
    provider_id: str,
    cdo_binary: str | None = None,
    icon_grid_path: Path | None = None,
    icon_weights_path: Path | None = None,
) -> Iterator[Path]:
    """Prepare one bounded payload for repeated station-point decoding."""

    with _prepared_provider_payload(
        raw_path,
        provider_id=provider_id,
        cdo_binary=cdo_binary,
        icon_grid_path=icon_grid_path,
        icon_weights_path=icon_weights_path,
    ) as prepared:
        yield prepared


def _grib_datetime(date_token: str, time_token: str, field: str) -> datetime:
    try:
        day = datetime.strptime(str(date_token), "%Y%m%d").date()
        clock = int(str(time_token))
        hour, minute = divmod(clock, 100)
        if hour > 23 or minute > 59:
            raise ValueError
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"native_grib_{field}_timestamp_invalid") from exc


def parse_grib_get_temperature_output(stdout: str) -> list[dict[str, Any]]:
    """Parse ecCodes rows emitted by the fixed native temperature selector."""

    rows: list[dict[str, Any]] = []
    for line in str(stdout or "").splitlines():
        tokens = line.split()
        # dataDate dataTime validityDate validityTime step value_K
        if len(tokens) < 6:
            continue
        try:
            value_k = float(tokens[-1])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value_k):
            continue
        run_init = _grib_datetime(tokens[0], tokens[1], "run")
        valid_time = _grib_datetime(tokens[2], tokens[3], "valid")
        try:
            step = int(tokens[4])
        except (TypeError, ValueError) as exc:
            raise ContractError("native_grib_step_invalid") from exc
        rows.append(
            {
                "grib_run_initialization_utc": _timestamp_text(run_init),
                "grib_valid_time_utc": _timestamp_text(valid_time),
                "step_hours": step,
                "temperature_2m_c": round(value_k - 273.15 if value_k > 150.0 else value_k, 6),
                "native_units": "K" if value_k > 150.0 else "C",
            }
        )
    if not rows:
        raise ContractError("native_grib_temperature_field_missing")
    return rows


def decode_native_temperature_points(
    raw_path: Path,
    *,
    provider_id: str,
    station_id: str,
    grib_get_binary: str | None = None,
    cdo_binary: str | None = None,
    icon_grid_path: Path | None = None,
    icon_weights_path: Path | None = None,
    prepared_payload_path: Path | None = None,
) -> dict[str, Any]:
    """Decode only native 2-m temperature points from one retained payload."""

    provider = _text("provider_id", provider_id).lower()
    if provider not in MODEL_BY_PROVIDER:
        raise ContractError("native_model_provider_not_allowed")
    if prepared_payload_path is None:
        native_eccodes = _decode_with_eccodes(
            raw_path,
            provider_id=provider,
            station_id=station_id,
            icon_grid_path=icon_grid_path,
            icon_weights_path=icon_weights_path,
        )
        if native_eccodes is not None:
            return native_eccodes
    elif provider != "icon_global_official":
        raise ContractError("native_prepared_payload_icon_only")
    coordinates = station_coordinates(station_id)
    binary = _bounded_binary(grib_get_binary)
    if not raw_path.is_file():
        raise ContractError("native_raw_payload_missing")
    selector = "shortName=2t,typeOfLevel=heightAboveGround,level=2"
    @contextmanager
    def _prepared() -> Iterator[Path]:
        if prepared_payload_path is not None:
            candidate = prepared_payload_path.expanduser().resolve()
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                raise ContractError("native_prepared_payload_missing")
            yield candidate
            return
        with _prepared_provider_payload(
            raw_path,
            provider_id=provider,
            cdo_binary=cdo_binary,
            icon_grid_path=icon_grid_path,
            icon_weights_path=icon_weights_path,
        ) as generated:
            yield generated

    with _prepared() as prepared:
        command = [
            binary,
            "-w",
            selector,
            "-p",
            "dataDate,dataTime,validityDate,validityTime,step",
            "-l",
            f"{coordinates['latitude']:.6f},{coordinates['longitude']:.6f},1",
            str(prepared),
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        stdout = str(completed.stdout or "")
        if len(stdout.encode("utf-8")) > MAX_GRIB_GET_STDOUT_BYTES:
            raise ContractError("native_grib_decoder_output_too_large")
        if completed.returncode != 0:
            raise ContractError("native_grib_temperature_decode_failed")
    rows = parse_grib_get_temperature_output(stdout)
    return {
        "schema": "bayes_basket_target_native_temperature_decode_v1",
        "provider_id": provider,
        "model_id": MODEL_BY_PROVIDER[provider],
        "station_id": coordinates["station_id"],
        "raw_path": str(raw_path.resolve()),
        "selector": selector,
        "latitude": coordinates["latitude"],
        "longitude": coordinates["longitude"],
        "coordinate_lineage": coordinates,
        "decoder_id": "eccodes_grib_get_nearest_point_temperature_2m_v1",
        "decoder_binary": binary,
        "remap_contract": "DWD_ICON_GLOBAL2WORLD_025_EASY" if provider == "icon_global_official" else None,
        "points": rows,
    }


def _target_bounds(event_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ContractError("native_target_timezone_invalid") from exc
    try:
        local_day = date.fromisoformat(_text("event_date", event_date))
    except ValueError as exc:
        raise ContractError("native_event_date_invalid") from exc
    start = datetime.combine(local_day, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def materialize_native_daily_curves(
    points: Iterable[Mapping[str, Any]],
    *,
    target_timezone: str,
) -> dict[str, Any]:
    """Aggregate decoded points and require full local-day coverage."""

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for raw in points:
        if not isinstance(raw, Mapping):
            raise ContractError("native_point_mapping_required")
        provider = _text("provider_id", raw.get("provider_id")).lower()
        if provider not in MODEL_BY_PROVIDER:
            raise ContractError("native_model_provider_not_allowed")
        station = _text("station_id", raw.get("station_id"))
        event_date = _text("event_date", raw.get("event_date"))
        run_init = _timestamp("model_run_initialization_utc", raw.get("model_run_initialization_utc"))
        valid_time = _timestamp("valid_time_utc", raw.get("valid_time_utc"))
        start, end = _target_bounds(event_date, target_timezone)
        if not start <= valid_time < end:
            raise ContractError("native_point_outside_target_local_day")
        try:
            temperature = float(raw.get("temperature_2m_c"))
        except (TypeError, ValueError) as exc:
            raise ContractError("native_point_temperature_invalid") from exc
        if not math.isfinite(temperature):
            raise ContractError("native_point_temperature_invalid")
        key = (station, event_date, provider, _timestamp_text(run_init))
        grouped.setdefault(key, []).append(
            {
                "station_id": station,
                "event_date": event_date,
                "provider_id": provider,
                "model_id": MODEL_BY_PROVIDER[provider],
                "model_run_initialization_utc": _timestamp_text(run_init),
                "valid_time_utc": _timestamp_text(valid_time),
                "temperature_2m_c": round(temperature, 6),
                "raw_message_sha256": _text("raw_message_sha256", raw.get("raw_message_sha256")),
                "receipt_sha256": _text("receipt_sha256", raw.get("receipt_sha256")),
                "published_at_utc": str(raw.get("published_at_utc") or ""),
                "received_at_utc": str(raw.get("received_at_utc") or ""),
            }
        )

    curves: list[dict[str, Any]] = []
    eligible_rows: list[dict[str, Any]] = []
    for key, raw_rows in sorted(grouped.items()):
        station, event_date, provider, run_init = key
        by_valid: dict[str, dict[str, Any]] = {}
        conflict = False
        for row in raw_rows:
            previous = by_valid.get(row["valid_time_utc"])
            if previous is not None and previous["temperature_2m_c"] != row["temperature_2m_c"]:
                conflict = True
            by_valid[row["valid_time_utc"]] = row
        ordered = [by_valid[item] for item in sorted(by_valid)]
        start, end = _target_bounds(event_date, target_timezone)
        times = [_timestamp("valid_time_utc", row["valid_time_utc"]) for row in ordered]
        endpoint_tolerance_hours = CURVE_ENDPOINT_TOLERANCE_HOURS_BY_PROVIDER.get(
            provider,
            DEFAULT_CURVE_ENDPOINT_TOLERANCE_HOURS,
        )
        minimum_point_count = MIN_CURVE_POINTS_BY_PROVIDER.get(
            provider,
            MIN_CURVE_POINTS,
        )
        gaps = [
            (later - earlier).total_seconds() / 3600.0
            for earlier, later in zip(times, times[1:])
        ]
        coverage = {
            "point_count": len(ordered),
            "first_valid_time_utc": ordered[0]["valid_time_utc"] if ordered else "",
            "last_valid_time_utc": ordered[-1]["valid_time_utc"] if ordered else "",
            "max_gap_hours": max(gaps) if gaps else None,
            "starts_within_hours": (times[0] - start).total_seconds() / 3600.0 if times else None,
            "ends_within_hours": (end - times[-1]).total_seconds() / 3600.0 if times else None,
            "endpoint_tolerance_hours": endpoint_tolerance_hours,
        }
        blockers: list[str] = []
        if conflict:
            blockers.append("native_duplicate_valid_time_conflict")
        if len(ordered) < minimum_point_count:
            blockers.append("native_daily_curve_point_count_insufficient")
        if times and times[0] > start + timedelta(hours=endpoint_tolerance_hours):
            blockers.append("native_daily_curve_start_coverage_incomplete")
        if times and times[-1] < end - timedelta(hours=endpoint_tolerance_hours):
            blockers.append("native_daily_curve_end_coverage_incomplete")
        if not times:
            blockers.append("native_daily_curve_empty")
        if gaps and max(gaps) > MAX_CURVE_GAP_HOURS:
            blockers.append("native_daily_curve_gap_too_large")
        curve_identity = {
            "station_id": station,
            "event_date": event_date,
            "provider_id": provider,
            "model_id": MODEL_BY_PROVIDER[provider],
            "model_run_initialization_utc": run_init,
            "points": ordered,
            "coverage": coverage,
            "target_timezone": target_timezone,
            "blockers": sorted(set(blockers)),
        }
        curve = {
            "schema": CURVE_SCHEMA,
            **curve_identity,
            "state": "COMPLETE" if not blockers else "BLOCKED_INCOMPLETE_CURVE",
            "forecast_daily_max_c": max(row["temperature_2m_c"] for row in ordered) if ordered else None,
            "curve_sha256": content_sha256(curve_identity),
        }
        curves.append(curve)
        if not blockers:
            # A daily curve is assembled from multiple native messages.  The
            # providers publish those messages at different times, so a
            # single timestamp is a derived maturity bound, not an equality
            # join key.  Use the latest component timestamp: the curve is not
            # fully available before its last constituent arrives.  Keep the
            # per-point timestamps in ``temperature_points`` for lineage and
            # strict-as-of replay; a partial timestamp set remains blank and
            # therefore fail-closes downstream validation.
            component_published = [
                str(row.get("published_at_utc") or "").strip()
                for row in ordered
            ]
            component_received = [
                str(row.get("received_at_utc") or "").strip()
                for row in ordered
            ]
            curve_published_at = (
                max(component_published)
                if component_published and all(component_published)
                else ""
            )
            curve_received_at = (
                max(component_received)
                if component_received and all(component_received)
                else ""
            )
            max_point = max(ordered, key=lambda row: row["temperature_2m_c"])
            component_raw_sha256 = sorted({
                row["raw_message_sha256"] for row in ordered
            })
            component_receipt_sha256 = sorted({
                row["receipt_sha256"] for row in ordered
            })
            eligible_rows.append(
                {
                    "station_id": station,
                    "event_date": event_date,
                    "target_timezone": target_timezone,
                    "provider_id": provider,
                    "model_run_initialization_utc": run_init,
                    "valid_time_utc": max_point["valid_time_utc"],
                    "temperature_2m_c": max_point["temperature_2m_c"],
                    "forecast_daily_max_c": curve["forecast_daily_max_c"],
                    "daily_curve_complete": True,
                    "sample_count": len(ordered),
                    "temperature_points": ordered,
                    "raw_message_sha256": content_sha256({
                        "sha256": component_raw_sha256,
                    }),
                    "receipt_sha256": content_sha256({
                        "sha256": component_receipt_sha256,
                    }),
                    "component_raw_message_sha256": component_raw_sha256,
                    "component_receipt_sha256": component_receipt_sha256,
                    "published_at_utc": curve_published_at,
                    "received_at_utc": curve_received_at,
                    "component_published_at_utc": component_published,
                    "component_received_at_utc": component_received,
                    "model_run_lineage_sha256": content_sha256(
                        {
                            "station_id": station,
                            "event_date": event_date,
                            "provider_id": provider,
                            "model_run_initialization_utc": run_init,
                            "raw_message_sha256": sorted({row["raw_message_sha256"] for row in ordered}),
                            "receipt_sha256": sorted({row["receipt_sha256"] for row in ordered}),
                        }
                    ),
                }
            )

    return {
        "schema": CURVE_SCHEMA,
        "target_timezone": target_timezone,
        "curves": curves,
        "eligible_model_rows": eligible_rows,
        "curve_count": len(curves),
        "complete_curve_count": len(eligible_rows),
        "all_complete": bool(curves) and len(curves) == len(eligible_rows),
        "promotion_allowed": False,
        "production_authority": False,
    }


def materialize_native_daily_curves_by_station_timezone(
    points: Iterable[Mapping[str, Any]],
    *,
    station_timezones: Mapping[str, str],
) -> dict[str, Any]:
    """Aggregate mixed-registry stations without imposing one timezone.

    Each station is materialized through the existing single-timezone
    contract.  The combined result is only a transport-efficient wrapper; it
    does not relax local-day coverage or permit timezone fallback.
    """

    normalized = {
        str(station).strip().upper(): str(timezone_name).strip()
        for station, timezone_name in station_timezones.items()
        if str(station).strip() and str(timezone_name).strip()
    }
    if not normalized:
        raise ContractError("native_station_timezone_registry_empty")
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for raw in points:
        if not isinstance(raw, Mapping):
            raise ContractError("native_point_mapping_required")
        station = _text("station_id", raw.get("station_id")).upper()
        timezone_name = normalized.get(station)
        if timezone_name is None:
            raise ContractError("native_station_timezone_missing")
        grouped.setdefault(timezone_name, []).append(raw)
    curves: list[dict[str, Any]] = []
    eligible_rows: list[dict[str, Any]] = []
    for timezone_name, rows in sorted(grouped.items()):
        materialized = materialize_native_daily_curves(
            rows,
            target_timezone=timezone_name,
        )
        curves.extend(materialized["curves"])
        eligible_rows.extend(materialized["eligible_model_rows"])
    return {
        "schema": CURVE_SCHEMA,
        "target_timezone": "REGISTRY_BOUND_PER_STATION",
        "station_timezones": dict(sorted(normalized.items())),
        "curves": curves,
        "eligible_model_rows": eligible_rows,
        "curve_count": len(curves),
        "complete_curve_count": len(eligible_rows),
        "all_complete": bool(curves) and len(curves) == len(eligible_rows),
        "promotion_allowed": False,
        "production_authority": False,
    }


__all__ = [
    "CURVE_SCHEMA",
    "MAX_CURVE_GAP_HOURS",
    "MIN_CURVE_POINTS",
    "MIN_CURVE_POINTS_BY_PROVIDER",
    "MODEL_BY_PROVIDER",
    "SCHEMA",
    "decode_native_temperature_points",
    "materialize_native_daily_curves",
    "materialize_native_daily_curves_by_station_timezone",
    "parse_grib_get_temperature_output",
    "prepare_native_temperature_payload",
]
