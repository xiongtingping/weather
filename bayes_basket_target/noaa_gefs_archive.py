"""Official NOAA GEFS historical member capture for calibration training."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time as time_module
import ctypes
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .contracts import ContractError, content_sha256
from .future_temperature_curve import build_native_family_temperature_curve
from .gefs_raw_reuse import RetainedGEFSField, load_retained_gefs_field_index
from .native_raw_object_store import (
    find_content_addressed_message,
    store_content_addressed_message,
)
from .native_ensemble_store import _insert_content_addressed
from .open_meteo_ensemble import ENSEMBLE_SYSTEMS
from official_nwp_https_transport import official_nwp_proxy_url
from .p0_producer import bucket_intervals_from_registry_rows
from .resource_bounds import assert_process_memory_budget
from .ssot import native_ensemble_foundation_schema_status


SCHEMA = "noaa_gefs_official_historical_station_members_v2"
MULTISTATION_SCHEMA = (
    "noaa_gefs_official_historical_multistation_members_v2"
)
SYSTEM_ID = "NOAA_GEFS_05"
RUN_CYCLE = "18Z"
GRID = "0p50"
# P0 follows the freshest run, so this collector is asked for a shorter lead as
# the event day approaches.  The default keeps existing d2 callers unchanged.
SUPPORTED_LEAD_DAYS = (0, 1, 2)
DEFAULT_LEAD_DAYS = 2
ARCHIVE_LEAD_BUCKET = f"d{DEFAULT_LEAD_DAYS}"


def extraction_version_for_lead(lead_days: int) -> str:
    """Extraction identity is lead-specific.

    A d1 row read the same way as a d2 row is still different evidence, drawn
    from a different run with different skill.  One shared version string would
    let the OOS scope check accept it against d2's contract and train both
    together.
    """

    return f"noaa_gefs_pds_0p50_18z_d{int(lead_days)}_station_tmax_v2"


EXTRACTION_VERSION = extraction_version_for_lead(DEFAULT_LEAD_DAYS)
BASE_URL = "https://noaa-gefs-pds.s3.amazonaws.com"
ECCODES_GRIB_GET = Path("/opt/homebrew/bin/grib_get")
ECCODES_LIBRARY = Path("/opt/homebrew/lib/libeccodes.dylib")
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_BYTES = 1024 * 1024
# A complete 45-station GEFS d2 extraction currently consumes about 70 MiB
# across its bounded index/range requests.  Keep the single-station legacy
# capture compatible with the same official field set without weakening its
# per-request (1 MiB), bounded-stream, free-space, or RSS guards.
MAX_TOTAL_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_MULTISTATION_TOTAL_RESPONSE_BYTES = 128 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
MAX_FIELD_RECEIPTS = 31 * 25
MAX_STATION_COUNT = 64
# Sized against measured request latency, not bandwidth.  One capture issues
# ~500 bounded index/range requests here and each costs 2.4-4.6s round trip,
# so throughput is latency-bound: at 8 workers a capture could not finish
# inside any sane deadline.  Payloads are small (see MAX_MESSAGE_BYTES), so
# the projected RSS stays far under the process budget.
MAX_DOWNLOAD_WORKERS = 24
DEFAULT_RETAINED_PACKET_DIR = Path("output/audits/native_ensemble_history")
DEFAULT_RAW_OBJECT_DIR = Path("data/runtime/official_nwp_sources/gefs/raw")


def _get_bytes(
    url: str,
    *,
    client: httpx.Client,
    timeout_seconds: float,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
) -> tuple[bytes, Mapping[str, str]]:
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            with client.stream(
                "GET",
                url,
                headers=dict(headers or {}),
                timeout=timeout_seconds,
            ) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ContractError(
                        "gefs_archive_response_too_large"
                    )
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ContractError(
                            "gefs_archive_response_too_large"
                        )
                    chunks.append(chunk)
                return b"".join(chunks), dict(response.headers)
        except (
            httpx.TransportError,
            httpx.TimeoutException,
            httpx.HTTPStatusError,
        ) as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code not in {429, 500, 502, 503, 504}
            ):
                raise
            last_error = exc
            if attempt < 5:
                time_module.sleep(min(0.5 * (2 ** attempt), 8.0))
    raise ContractError(
        "gefs_archive_transport_failed:"
        + type(last_error).__name__
    ) from last_error


def _message_range(index_raw: bytes) -> tuple[int, int, str]:
    try:
        lines = index_raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ContractError("gefs_archive_index_invalid") from exc
    matches = [
        (index, line)
        for index, line in enumerate(lines)
        if ":TMP:2 m above ground:" in line
    ]
    if len(matches) != 1:
        raise ContractError("gefs_archive_2m_temperature_not_unique")
    index, line = matches[0]
    if index + 1 >= len(lines):
        raise ContractError("gefs_archive_index_range_incomplete")
    try:
        start = int(line.split(":", 2)[1])
        end = int(lines[index + 1].split(":", 2)[1]) - 1
    except (IndexError, ValueError) as exc:
        raise ContractError("gefs_archive_index_range_invalid") from exc
    if start < 0 or end < start or end - start + 1 > MAX_MESSAGE_BYTES:
        raise ContractError("gefs_archive_message_range_invalid")
    return start, end, line


def _download_gefs_field(
    *,
    client: httpx.Client,
    run_day: str,
    cycle: str,
    object_member: str,
    member_id: str,
    forecast_hour: int,
    timeout_seconds: float,
    retained: RetainedGEFSField | None = None,
    raw_roots: Sequence[Path] = (),
    raw_store: Path | None = None,
) -> tuple[bytes, dict[str, Any], int]:
    filename = (
        f"{object_member}.t{cycle}z.pgrb2a."
        f"{GRID}.f{forecast_hour:03d}"
    )
    object_key = (
        f"gefs.{run_day}/{cycle}/atmos/pgrb2ap5/" + filename
    )
    url = f"{BASE_URL}/{object_key}"
    if retained is not None:
        receipt = retained.receipt
        try:
            receipt_hour = int(receipt["forecast_hour"])
            start = int(receipt["range_start"])
            end = int(receipt["range_end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("gefs_archive_retained_range_invalid") from exc
        index_line = str(receipt.get("index_line") or "")
        if (
            retained.member_id != member_id
            or retained.forecast_hour != forecast_hour
            or str(receipt.get("member_id") or "") != member_id
            or receipt_hour != forecast_hour
            or str(receipt.get("object_key") or "") != object_key
            or end < start
            or not index_line
        ):
            raise ContractError("gefs_archive_retained_scope_mismatch")
        local = find_content_addressed_message(retained.message_sha256, raw_roots)
        if local is not None:
            message = local.read_bytes()
            if len(message) != end - start + 1:
                raise ContractError("gefs_archive_retained_range_length_mismatch")
            return message, dict(receipt), 0
        index_raw = b""
    else:
        index_raw, _index_headers = _get_bytes(
            url + ".idx",
            client=client,
            timeout_seconds=timeout_seconds,
            max_bytes=MAX_INDEX_BYTES,
        )
        start, end, index_line = _message_range(index_raw)
    message, headers = _get_bytes(
        url,
        client=client,
        timeout_seconds=timeout_seconds,
        max_bytes=MAX_MESSAGE_BYTES,
        headers={"Range": f"bytes={start}-{end}"},
    )
    if len(message) != end - start + 1:
        raise ContractError("gefs_archive_range_length_mismatch")
    new_receipt = {
        "member_id": member_id,
        "forecast_hour": forecast_hour,
        "object_key": object_key,
        "index_sha256": hashlib.sha256(index_raw).hexdigest(),
        "index_line": index_line,
        "range_start": start,
        "range_end": end,
        "message_sha256": hashlib.sha256(message).hexdigest(),
        "last_modified": str(headers.get("last-modified") or ""),
    }
    if retained is not None:
        if hashlib.sha256(message).hexdigest() != retained.message_sha256:
            raise ContractError("gefs_archive_retained_message_sha_mismatch")
        new_receipt = dict(retained.receipt)
    if raw_store is not None:
        store_content_addressed_message(
            message,
            root=raw_store,
            expected_sha256=str(new_receipt["message_sha256"]),
        )
    return message, new_receipt, len(index_raw) + len(message)


@lru_cache(maxsize=2)
def _retained_fields() -> dict[tuple[str, str, str, int], RetainedGEFSField]:
    directory = Path(
        os.environ.get("GEFS_RETAINED_PACKET_DIR", str(DEFAULT_RETAINED_PACKET_DIR))
    ).expanduser().resolve()
    if not directory.is_dir():
        return {}
    return load_retained_gefs_field_index(
        [
            *directory.glob("GEFS_45station_*_official_d2.json"),
            *directory.glob("NOAA_GEFS_45station_*_official_d2.json"),
            *directory.glob("official_noaa_gefs_forward_*.json"),
        ]
    )


def _gefs_raw_store() -> Path:
    return Path(
        os.environ.get("GEFS_RAW_OBJECT_DIR", str(DEFAULT_RAW_OBJECT_DIR))
    ).expanduser().resolve()


def _nearest_temperature_c(
    message: bytes,
    *,
    latitude: float,
    longitude: float,
) -> float:
    if (
        not ECCODES_GRIB_GET.is_file()
        or len(message) <= 16
        or len(message) > MAX_MESSAGE_BYTES
    ):
        raise ContractError("gefs_archive_decoder_unavailable")
    with tempfile.NamedTemporaryFile(
        prefix="gefs-2t-",
        suffix=".grib2",
        delete=True,
    ) as handle:
        handle.write(message)
        handle.flush()
        result = subprocess.run(
            [
                str(ECCODES_GRIB_GET),
                "-l",
                f"{float(latitude)},{float(longitude)},1",
                handle.name,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    if result.returncode != 0 or len(result.stdout) > 1024:
        raise ContractError("gefs_archive_decoder_failed")
    try:
        kelvin = float(result.stdout.strip().split()[-1])
    except (IndexError, ValueError) as exc:
        raise ContractError("gefs_archive_decoder_output_invalid") from exc
    value = kelvin - 273.15
    if not math.isfinite(value) or not -100.0 <= value <= 70.0:
        raise ContractError("gefs_archive_temperature_invalid")
    return value


def decode_nearest_temperatures_c(
    message: bytes,
    *,
    points: Sequence[tuple[str, float, float]],
) -> dict[str, float]:
    """Decode many nearest grid points from one message through ecCodes."""

    if (
        not ECCODES_LIBRARY.is_file()
        or len(message) <= 16
        or len(message) > MAX_MESSAGE_BYTES
        or not points
        or len(points) > MAX_STATION_COUNT
    ):
        raise ContractError("gefs_archive_multidecoder_unavailable")
    station_ids = []
    for station_id, latitude, longitude in points:
        station = str(station_id or "").strip().upper()
        if (
            not station
            or station in station_ids
            or not math.isfinite(float(latitude))
            or not math.isfinite(float(longitude))
        ):
            raise ContractError("gefs_archive_multidecoder_point_invalid")
        station_ids.append(station)
    count = len(points)
    library = ctypes.CDLL(str(ECCODES_LIBRARY))
    library.codes_handle_new_from_message.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    library.codes_handle_new_from_message.restype = ctypes.c_void_p
    library.codes_grib_nearest_find_multiple.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.c_long,
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_int),
    ]
    library.codes_grib_nearest_find_multiple.restype = ctypes.c_int
    library.codes_handle_delete.argtypes = [ctypes.c_void_p]
    library.codes_handle_delete.restype = ctypes.c_int
    raw = ctypes.create_string_buffer(message)
    handle = library.codes_handle_new_from_message(
        None,
        ctypes.cast(raw, ctypes.c_void_p),
        len(message),
    )
    if not handle:
        raise ContractError("gefs_archive_multidecoder_handle_failed")
    inlats = (ctypes.c_double * count)(
        *(float(row[1]) for row in points)
    )
    inlons = (ctypes.c_double * count)(
        *(float(row[2]) for row in points)
    )
    outlats = (ctypes.c_double * count)()
    outlons = (ctypes.c_double * count)()
    values = (ctypes.c_double * count)()
    distances = (ctypes.c_double * count)()
    indexes = (ctypes.c_int * count)()
    try:
        error = library.codes_grib_nearest_find_multiple(
            handle,
            0,
            inlats,
            inlons,
            count,
            outlats,
            outlons,
            values,
            distances,
            indexes,
        )
    finally:
        delete_error = library.codes_handle_delete(handle)
    if error != 0 or delete_error != 0:
        raise ContractError("gefs_archive_multidecoder_failed")
    result = {}
    for index, station in enumerate(station_ids):
        value = float(values[index]) - 273.15
        if (
            not math.isfinite(value)
            or not -100.0 <= value <= 70.0
            or not math.isfinite(float(distances[index]))
            or float(distances[index]) > 100.0
        ):
            raise ContractError(
                "gefs_archive_multidecoder_temperature_invalid"
            )
        result[station] = value
    return result


def _member_objects() -> list[tuple[str, str, str]]:
    return [
        (
            "control" if index == 0 else f"perturbed_{index:02d}",
            "CONTROL" if index == 0 else "PERTURBED",
            "gec00" if index == 0 else f"gep{index:02d}",
        )
        for index in range(31)
    ]


def _forecast_hours(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init_utc: datetime,
) -> list[int]:
    target = date.fromisoformat(event_date)
    zone = ZoneInfo(settlement_timezone)
    result = [
        forecast_hour
        for forecast_hour in range(0, 73, 3)
        if (
            run_init_utc + timedelta(hours=forecast_hour)
        ).astimezone(zone).date() == target
    ]
    if not 7 <= len(result) <= 9:
        raise ContractError("gefs_archive_local_day_grid_incomplete")
    return result


def capture_gefs_station_day(
    *,
    station_id: str,
    event_date: str,
    latitude: float,
    longitude: float,
    settlement_timezone: str,
    registry_rows: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    station = str(station_id or "").strip().upper()
    target = date.fromisoformat(event_date)
    run_init = datetime.combine(
        target - timedelta(days=2),
        time(hour=18),
        tzinfo=timezone.utc,
    )
    query_at = datetime.fromisoformat(
        archive_query_at_utc.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if (
        not station
        or not settlement_timezone
        or not math.isfinite(float(latitude))
        or not math.isfinite(float(longitude))
        or query_at <= run_init
    ):
        raise ContractError("gefs_archive_scope_invalid")
    if shutil.disk_usage(Path(tempfile.gettempdir())).free < MIN_FREE_BYTES:
        raise ContractError("gefs_archive_free_space_too_low")
    intervals = bucket_intervals_from_registry_rows(registry_rows)
    frozen_intervals = {
        bucket: [
            None if lower is None else float(lower),
            None if upper is None else float(upper),
        ]
        for bucket, (lower, upper) in intervals.items()
    }
    hours = _forecast_hours(
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        run_init_utc=run_init,
    )
    member_rows = []
    receipts = []
    total_bytes = 0
    run_day = run_init.strftime("%Y%m%d")
    cycle = run_init.strftime("%H")
    # Official NWP transport is deliberately explicit: use the configured
    # research proxy (currently 127.0.0.1:1082) and never inherit ambient
    # desktop proxy variables.  This keeps the provider lineage auditable and
    # makes the capture command behave identically from launchd or a shell.
    with httpx.Client(
        headers={"User-Agent": "weather-gefs-history/1"},
        proxy=official_nwp_proxy_url() or None,
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=2,
            max_keepalive_connections=2,
        ),
    ) as client:
        for member_id, member_role, object_member in _member_objects():
            values = []
            member_receipts = []
            for forecast_hour in hours:
                filename = (
                    f"{object_member}.t{cycle}z.pgrb2a."
                    f"{GRID}.f{forecast_hour:03d}"
                )
                object_key = (
                    f"gefs.{run_day}/{cycle}/atmos/pgrb2ap5/"
                    + filename
                )
                url = f"{BASE_URL}/{object_key}"
                index_raw, _index_headers = _get_bytes(
                    url + ".idx",
                    client=client,
                    timeout_seconds=timeout_seconds,
                    max_bytes=MAX_INDEX_BYTES,
                )
                start, end, index_line = _message_range(index_raw)
                message, headers = _get_bytes(
                    url,
                    client=client,
                    timeout_seconds=timeout_seconds,
                    max_bytes=MAX_MESSAGE_BYTES,
                    headers={"Range": f"bytes={start}-{end}"},
                )
                expected = end - start + 1
                if len(message) != expected:
                    raise ContractError(
                        "gefs_archive_range_length_mismatch"
                    )
                total_bytes += len(index_raw) + len(message)
                if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                    raise ContractError(
                        "gefs_archive_total_response_too_large"
                    )
                values.append(_nearest_temperature_c(
                    message,
                    latitude=latitude,
                    longitude=longitude,
                ))
                member_receipts.append({
                    "forecast_hour": forecast_hour,
                    "object_key": object_key,
                    "index_sha256": hashlib.sha256(
                        index_raw
                    ).hexdigest(),
                    "index_line": index_line,
                    "range_start": start,
                    "range_end": end,
                    "message_sha256": hashlib.sha256(
                        message
                    ).hexdigest(),
                    "last_modified": str(
                        headers.get("last-modified") or ""
                    ),
                })
                assert_process_memory_budget()
            member_rows.append({
                "ensemble_system_id": SYSTEM_ID,
                "member_id": member_id,
                "member_role": member_role,
                "raw_tmax_c": max(values),
            })
            receipts.extend({
                "member_id": member_id,
                **receipt,
            } for receipt in member_receipts)
    if len(receipts) > MAX_FIELD_RECEIPTS:
        raise ContractError("gefs_archive_field_receipt_limit_exceeded")
    member_identity = [
        {
            "member_id": row["member_id"],
            "member_role": row["member_role"],
            "raw_tmax_c": row["raw_tmax_c"],
        }
        for row in member_rows
    ]
    member_set_sha = content_sha256(member_identity)
    receipt_set_sha = content_sha256(receipts)
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "latitude": float(latitude),
        "longitude": float(longitude),
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "run_cycle": RUN_CYCLE,
        "forecast_hours": hours,
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": "NOAA_GEFS",
        "member_count": len(member_rows),
        "members": member_rows,
        "member_set_sha256": member_set_sha,
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
                for row in member_rows
            ) / len(member_rows)
            for bucket, bounds in frozen_intervals.items()
        },
        "field_receipt_count": len(receipts),
        "field_receipt_set_sha256": receipt_set_sha,
        "field_receipts": receipts,
        "archive_query_at_utc": query_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "source_prefix": (
            f"{BASE_URL}/gefs.{run_day}/{cycle}/atmos/pgrb2ap5/"
        ),
        "source_class": "OFFICIAL_NATIVE_ARCHIVE",
        "native_run_init_known": True,
        "native_publication_at_known": False,
        "training_only": True,
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _build_station_capture(
    *,
    station_id: str,
    event_date: str,
    latitude: float,
    longitude: float,
    settlement_timezone: str,
    registry_rows: Sequence[Mapping[str, Any]],
    run_init: datetime,
    query_at: datetime,
    forecast_hours: Sequence[int],
    member_values: Mapping[str, Sequence[float] | Mapping[int, float]],
    field_receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    intervals = bucket_intervals_from_registry_rows(registry_rows)
    frozen_intervals = {
        bucket: [
            None if lower is None else float(lower),
            None if upper is None else float(upper),
        ]
        for bucket, (lower, upper) in intervals.items()
    }
    member_roles = {
        member_id: member_role
        for member_id, member_role, _object_member in _member_objects()
    }
    values_by_member = {
        member_id: (
            {int(hour): float(value) for hour, value in values.items()}
            if isinstance(values, Mapping)
            else {
                int(hour): float(value)
                for hour, value in zip(forecast_hours, values)
            }
        )
        for member_id, values in member_values.items()
    }
    member_rows = [
        {
            "ensemble_system_id": SYSTEM_ID,
            "member_id": member_id,
            "member_role": member_roles[member_id],
            "raw_tmax_c": max(values.values()),
        }
        for member_id, values in sorted(values_by_member.items())
    ]
    if (
        len(member_rows) != 31
        or any(
            set(values_by_member[row["member_id"]])
            != set(forecast_hours)
            for row in member_rows
        )
    ):
        raise ContractError("gefs_archive_multistation_member_incomplete")
    receipts = [
        dict(row)
        for row in field_receipts
        if int(row["forecast_hour"]) in set(forecast_hours)
    ]
    if len(receipts) != 31 * len(forecast_hours):
        raise ContractError("gefs_archive_multistation_receipt_incomplete")
    member_identity = [
        {
            "member_id": row["member_id"],
            "member_role": row["member_role"],
            "raw_tmax_c": row["raw_tmax_c"],
        }
        for row in member_rows
    ]
    member_set_sha = content_sha256(member_identity)
    receipt_set_sha = content_sha256(receipts)
    run_day = run_init.strftime("%Y%m%d")
    cycle = run_init.strftime("%H")
    family_curve = build_native_family_temperature_curve({
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": "NOAA_GEFS",
        "available_at_utc": query_at.isoformat().replace("+00:00", "Z"),
        "source_field_receipt_set_sha256": receipt_set_sha,
        "points": [
            {
                "valid_time_utc": (run_init + timedelta(hours=hour)).isoformat().replace("+00:00", "Z"),
                "temperature_c": sum(
                    values_by_member[member_id][hour]
                    for member_id in values_by_member
                ) / len(values_by_member),
            }
            for hour in forecast_hours
        ],
    })
    identity = {
        "schema": SCHEMA,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "latitude": float(latitude),
        "longitude": float(longitude),
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "run_cycle": RUN_CYCLE,
        "forecast_hours": list(forecast_hours),
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": "NOAA_GEFS",
        "member_count": len(member_rows),
        "members": member_rows,
        "member_set_sha256": member_set_sha,
        "family_temperature_curve": family_curve,
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
                for row in member_rows
            ) / len(member_rows)
            for bucket, bounds in frozen_intervals.items()
        },
        "field_receipt_count": len(receipts),
        "field_receipt_set_sha256": receipt_set_sha,
        "field_receipts": receipts,
        "archive_query_at_utc": query_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "source_prefix": (
            f"{BASE_URL}/gefs.{run_day}/{cycle}/atmos/pgrb2ap5/"
        ),
        "source_class": "OFFICIAL_NATIVE_ARCHIVE",
        "native_run_init_known": True,
        "native_publication_at_known": False,
        "offline_chronological_oos_eligible": True,
        "training_only": True,
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def capture_gefs_station_days(
    *,
    event_date: str,
    station_scopes: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    timeout_seconds: float = 30.0,
    lead_days: int = DEFAULT_LEAD_DAYS,
) -> dict[str, Any]:
    """Download each global GEFS field once and decode every scoped station."""

    if lead_days not in SUPPORTED_LEAD_DAYS:
        raise ContractError("noaa_gefs_archive_lead_days_invalid")
    lead_bucket = f"d{lead_days}"
    target = date.fromisoformat(event_date)
    run_init = datetime.combine(
        target - timedelta(days=lead_days),
        time(hour=18),
        tzinfo=timezone.utc,
    )
    query_at = datetime.fromisoformat(
        archive_query_at_utc.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    projection_generated_at = query_at
    if (
        query_at <= run_init
        or not 1 <= len(station_scopes) <= MAX_STATION_COUNT
        or shutil.disk_usage(Path(tempfile.gettempdir())).free
        < MIN_FREE_BYTES
    ):
        raise ContractError("gefs_archive_multistation_scope_invalid")
    scopes: dict[str, dict[str, Any]] = {}
    for raw in station_scopes:
        station = str(raw.get("station_id") or "").strip().upper()
        timezone_name = str(
            raw.get("settlement_timezone") or ""
        ).strip()
        latitude = float(raw.get("latitude"))
        longitude = float(raw.get("longitude"))
        registry_rows = raw.get("registry_rows")
        if (
            not station
            or station in scopes
            or not timezone_name
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not isinstance(registry_rows, Sequence)
            or isinstance(registry_rows, (str, bytes))
            or not registry_rows
        ):
            raise ContractError(
                "gefs_archive_multistation_station_invalid"
            )
        hours = _forecast_hours(
            event_date=event_date,
            settlement_timezone=timezone_name,
            run_init_utc=run_init,
        )
        scopes[station] = {
            "station_id": station,
            "settlement_timezone": timezone_name,
            "latitude": latitude,
            "longitude": longitude,
            "registry_rows": list(registry_rows),
            "forecast_hours": hours,
        }
    union_hours = sorted({
        hour
        for scope in scopes.values()
        for hour in scope["forecast_hours"]
    })
    if not union_hours or len(union_hours) > 17:
        raise ContractError("gefs_archive_multistation_grid_invalid")
    member_values: dict[str, dict[str, dict[int, float]]] = {
        station: {
            member_id: {}
            for member_id, _role, _object in _member_objects()
        }
        for station in scopes
    }
    receipts = []
    total_bytes = 0
    run_day = run_init.strftime("%Y%m%d")
    cycle = run_init.strftime("%H")
    run_init_text = run_init.isoformat().replace("+00:00", "Z")
    retained_fields = _retained_fields()
    selected_retained_fields = [
        retained_fields.get(
            (SYSTEM_ID, run_init_text, member_id, forecast_hour)
        )
        for member_id, _member_role, _object_member in _member_objects()
        for forecast_hour in union_hours
    ]
    retained_arrival_evidence: dict[str, Any] | None = None
    if all(field is not None for field in selected_retained_fields):
        complete_retained_fields = [
            field for field in selected_retained_fields if field is not None
        ]
        retained_at = max(
            datetime.fromisoformat(
                field.retained_at_utc.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            for field in complete_retained_fields
        )
        if retained_at > projection_generated_at:
            raise ContractError("gefs_archive_retained_arrival_after_projection")
        query_at = retained_at
        retained_arrival_evidence = {
            "schema": "official_native_retained_arrival_projection_v1",
            "ensemble_system_id": SYSTEM_ID,
            "retained_at_utc": query_at.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "projection_generated_at_utc": projection_generated_at.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "source_packet_content_sha256s": sorted({
                field.source_packet_content_sha256
                for field in complete_retained_fields
            }),
            "source_packet_refs": sorted({
                field.source_packet_ref for field in complete_retained_fields
            }),
            "field_count": len(complete_retained_fields),
            "network_download_count": 0,
        }
        retained_arrival_evidence["content_sha256"] = content_sha256(
            retained_arrival_evidence
        )
    raw_store = _gefs_raw_store()
    raw_roots = (raw_store,)
    with httpx.Client(
        headers={"User-Agent": "weather-gefs-history/1"},
        proxy=official_nwp_proxy_url() or None,
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=MAX_DOWNLOAD_WORKERS,
            max_keepalive_connections=MAX_DOWNLOAD_WORKERS,
        ),
    ) as client:
        tasks = [
            (member_id, object_member, forecast_hour)
            for member_id, _member_role, object_member
            in _member_objects()
            for forecast_hour in union_hours
        ]
        with ThreadPoolExecutor(
            max_workers=MAX_DOWNLOAD_WORKERS,
            thread_name_prefix="gefs-download",
        ) as executor:
            pending: dict[Any, tuple[str, int]] = {}
            task_iter = iter(tasks)

            def submit_next() -> bool:
                try:
                    member_id, object_member, forecast_hour = next(
                        task_iter
                    )
                except StopIteration:
                    return False
                future = executor.submit(
                    _download_gefs_field,
                    client=client,
                    run_day=run_day,
                    cycle=cycle,
                    object_member=object_member,
                    member_id=member_id,
                    forecast_hour=forecast_hour,
                    timeout_seconds=timeout_seconds,
                    retained=retained_fields.get(
                        (SYSTEM_ID, run_init_text, member_id, forecast_hour)
                    ),
                    raw_roots=raw_roots,
                    raw_store=raw_store,
                )
                pending[future] = (member_id, forecast_hour)
                return True

            for _index in range(MAX_DOWNLOAD_WORKERS):
                submit_next()
            while pending:
                done, _not_done = wait(
                    pending,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    member_id, forecast_hour = pending.pop(future)
                    message, receipt, response_bytes = future.result()
                    total_bytes += response_bytes
                    if (
                        total_bytes
                        > MAX_MULTISTATION_TOTAL_RESPONSE_BYTES
                    ):
                        raise ContractError(
                            "gefs_archive_multistation_response_too_large"
                        )
                    active = [
                        (
                            station,
                            float(scope["latitude"]),
                            float(scope["longitude"]),
                        )
                        for station, scope in scopes.items()
                        if forecast_hour in scope["forecast_hours"]
                    ]
                    decoded = decode_nearest_temperatures_c(
                        message,
                        points=active,
                    )
                    for station, value in decoded.items():
                        member_values[station][member_id][forecast_hour] = value
                    receipts.append(receipt)
                    assert_process_memory_budget(
                        projected_bytes=(
                            MAX_DOWNLOAD_WORKERS * MAX_MESSAGE_BYTES
                        )
                    )
                    submit_next()
    receipts.sort(
        key=lambda row: (
            str(row["member_id"]),
            int(row["forecast_hour"]),
        )
    )
    if len(receipts) != 31 * len(union_hours):
        raise ContractError(
            "gefs_archive_multistation_receipt_limit_exceeded"
        )
    captures = [
        _build_station_capture(
            station_id=station,
            event_date=event_date,
            latitude=float(scope["latitude"]),
            longitude=float(scope["longitude"]),
            settlement_timezone=str(scope["settlement_timezone"]),
            registry_rows=scope["registry_rows"],
            run_init=run_init,
            query_at=query_at,
            forecast_hours=scope["forecast_hours"],
            member_values=member_values[station],
            field_receipts=receipts,
        )
        for station, scope in sorted(scopes.items())
    ]
    identity = {
        "schema": MULTISTATION_SCHEMA,
        "event_date": event_date,
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "run_cycle": RUN_CYCLE,
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": "NOAA_GEFS",
        "station_count": len(captures),
        "station_ids": [
            capture["station_id"] for capture in captures
        ],
        "union_forecast_hours": union_hours,
        "downloaded_field_receipt_count": len(receipts),
        "downloaded_field_receipt_set_sha256": content_sha256(receipts),
        "total_response_bytes": total_bytes,
        "captures": captures,
        "archive_query_at_utc": query_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "projection_generated_at_utc": projection_generated_at.isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "retained_arrival_evidence": retained_arrival_evidence,
        "single_download_per_global_field": True,
        "training_only": True,
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def ingest_gefs_station_day(
    conn: sqlite3.Connection,
    capture: Mapping[str, Any],
) -> dict[str, Any]:
    if native_ensemble_foundation_schema_status(conn)["ok"] is not True:
        raise ContractError("native_ensemble_foundation_schema_required")
    identity = {
        key: value
        for key, value in capture.items()
        if key != "content_sha256"
    }
    if (
        capture.get("schema") != SCHEMA
        or capture.get("source_class") != "OFFICIAL_NATIVE_ARCHIVE"
        or capture.get("training_only") is not True
        or capture.get("holdout_eligible") is not False
        or capture.get("content_sha256") != content_sha256(identity)
    ):
        raise ContractError("gefs_archive_capture_invalid")
    station = str(capture["station_id"])
    event_date = str(capture["event_date"])
    members = capture.get("members")
    if not isinstance(members, list) or len(members) != 31:
        raise ContractError("gefs_archive_member_set_invalid")
    system = conn.execute(
        "SELECT provider_id,independence_family_id,"
        "expected_member_count "
        "FROM calibration_native_ensemble_system "
        "WHERE ensemble_system_id=?",
        (SYSTEM_ID,),
    ).fetchone()
    if (
        system is None
        or str(system[1]) != "NOAA_GEFS"
        or int(system[2]) != 31
    ):
        raise ContractError("gefs_archive_system_registration_invalid")
    existing = conn.execute(
        "SELECT member_id,member_role,raw_tmax_c "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE ensemble_system_id=? AND station_id=? "
        "AND target_local_date=? AND run_cycle=? "
        "AND extraction_version=? ORDER BY member_id",
        (
            SYSTEM_ID,
            station,
            event_date,
            RUN_CYCLE,
            EXTRACTION_VERSION,
        ),
    ).fetchall()
    expected_semantics = sorted(
        (
            str(row["member_id"]),
            str(row["member_role"]),
            float(row["raw_tmax_c"]),
        )
        for row in members
    )
    if existing:
        if len(existing) != 31 or any(
            (
                str(actual[0]) != expected_row[0]
                or str(actual[1]) != expected_row[1]
                or abs(float(actual[2]) - expected_row[2]) > 1e-9
            )
            for actual, expected_row in zip(
                existing,
                expected_semantics,
            )
        ):
            raise ContractError("gefs_archive_frozen_fact_conflict")
        return {
            "state": "REPLAY",
            "member_run_wrote_count": 0,
            "member_bucket_wrote_count": 0,
        }
    bucket_intervals_json = json.dumps(
        capture["bucket_intervals"],
        sort_keys=True,
        separators=(",", ":"),
    )
    raw_probabilities_json = json.dumps(
        capture["raw_bucket_probabilities"],
        sort_keys=True,
        separators=(",", ":"),
    )
    member_writes = 0
    for row in members:
        member_identity = {
            "ensemble_system_id": SYSTEM_ID,
            "station_id": station,
            "target_local_date": event_date,
            "archive_lead_bucket": ARCHIVE_LEAD_BUCKET,
            "run_cycle": RUN_CYCLE,
            "member_id": str(row["member_id"]),
            "member_role": str(row["member_role"]),
            "raw_tmax_c": float(row["raw_tmax_c"]),
            "settlement_definition": "official_daily_tmax_c",
            "extraction_version": EXTRACTION_VERSION,
            "provider_id": str(system[0]),
            "transport_provider_id": "NOAA_GEFS_PDS_S3",
            "native_run_init_utc": str(capture["run_init_utc"]),
            "offline_oos_eligible": 1,
            "archive_query_at_utc": str(
                capture["archive_query_at_utc"]
            ),
            "source_url": str(capture["source_prefix"]),
            "raw_object_ref": (
                "gefs_field_receipt_set_sha256:"
                + str(capture["field_receipt_set_sha256"])
            ),
            "source_object_sha256": str(
                capture["field_receipt_set_sha256"]
            ),
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
                    "gefs-history-member-" + row_sha[:28]
                ),
                **member_identity,
                "content_sha256": row_sha,
            },
        )
    bucket_identity = {
        "ensemble_system_id": SYSTEM_ID,
        "station_id": station,
        "target_local_date": event_date,
        "archive_lead_bucket": ARCHIVE_LEAD_BUCKET,
        "archive_query_at_utc": str(capture["archive_query_at_utc"]),
        "member_set_sha256": str(capture["member_set_sha256"]),
        "member_count": 31,
        "bucket_contract_sha256": str(
            capture["bucket_contract_sha256"]
        ),
        "bucket_intervals_json": bucket_intervals_json,
        "raw_bucket_probabilities_json": raw_probabilities_json,
        "source_role": "official_native_archive",
        "authority_scope": "CALIBRATION_TRAIN_ONLY",
        "holdout_eligible": 0,
    }
    bucket_sha = content_sha256(bucket_identity)
    bucket_writes = _insert_content_addressed(
        conn,
        table="calibration_native_ensemble_historical_member_bucket",
        primary_key="historical_member_bucket_id",
        row={
            "historical_member_bucket_id": (
                "gefs-history-bucket-" + bucket_sha[:28]
            ),
            **bucket_identity,
            "content_sha256": bucket_sha,
        },
    )
    return {
        "state": "COMMITTED",
        "member_run_wrote_count": member_writes,
        "member_bucket_wrote_count": bucket_writes,
    }


def ingest_gefs_station_days(
    conn: sqlite3.Connection,
    archive: Mapping[str, Any],
) -> dict[str, Any]:
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
        raise ContractError("gefs_archive_multistation_capture_invalid")
    results = [
        {
            "station_id": str(capture["station_id"]),
            **ingest_gefs_station_day(conn, capture),
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
        "member_bucket_wrote_count": sum(
            int(row["member_bucket_wrote_count"]) for row in results
        ),
        "station_results": results,
    }


__all__ = [
    "EXTRACTION_VERSION",
    "ARCHIVE_LEAD_BUCKET",
    "MULTISTATION_SCHEMA",
    "SCHEMA",
    "capture_gefs_station_day",
    "capture_gefs_station_days",
    "decode_nearest_temperatures_c",
    "ingest_gefs_station_day",
    "ingest_gefs_station_days",
]
