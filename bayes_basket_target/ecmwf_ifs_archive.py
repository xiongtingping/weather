"""Official ECMWF IFS ensemble open-data history for calibration OOS."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import tempfile
import threading
import time as time_module
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .contracts import ContractError, content_sha256
from .native_ensemble_store import _insert_content_addressed
from .noaa_gefs_archive import decode_nearest_temperatures_c
from .p0_producer import bucket_intervals_from_registry_rows
from .resource_bounds import assert_process_memory_budget
from .ssot import native_ensemble_foundation_schema_status


SCHEMA = "ecmwf_ifs_open_pf50_historical_station_members_v1"
MULTISTATION_SCHEMA = (
    "ecmwf_ifs_open_pf50_historical_multistation_members_v1"
)
SYSTEM_ID = "ECMWF_IFS_ENS_OPEN_PF50"
RUN_CYCLE = "18Z"
ARCHIVE_LEAD_BUCKET = "d2"
EXTRACTION_VERSION = (
    "ecmwf_open_data_ifs_0p25_enfo_pf50_mx2t3_18z_d2_v1"
)
# Same official ``ecmwf-forecasts`` S3 bucket through the global endpoint.
# The regional virtual-host endpoint can return a persistent ``503 Slow
# Down`` while this endpoint still serves the identical object successfully.
BASE_URL = "https://ecmwf-forecasts.s3.amazonaws.com"
MAX_INDEX_BYTES = 3 * 1024 * 1024
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 768 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
MAX_STATION_COUNT = 64
# ECMWF's public archive throttles sustained parallel range requests with
# ``503 Slow Down``.  Historical recovery is deliberately conservative: one
# process with four bounded workers is fast enough while avoiding the repeated
# eight-way retry storms observed during the 45-station PF50 backfill.
MAX_DOWNLOAD_WORKERS = 4
EXPECTED_MEMBER_COUNT = 50
_HTTP_CLIENT_LOCAL = threading.local()


def _thread_http_client() -> httpx.Client:
    """Reuse one bounded connection per download thread.

    A 45-station day needs roughly 850 small byte-range requests. Creating a
    fresh client for every range forced a fresh proxy/TLS handshake each
    time and made one settled day take many minutes. Thread-local clients
    preserve the existing four-connection ceiling while allowing HTTP
    keep-alive within each worker. The short-lived backfill process owns and
    releases all sockets at process exit.
    """

    client = getattr(_HTTP_CLIENT_LOCAL, "client", None)
    if client is None:
        client = httpx.Client(
            follow_redirects=False,
            # Public AWS S3 is directly reachable. Routing hundreds of small
            # range requests through the desktop proxy caused minute-long
            # stalls and unnecessary TLS handshakes.
            trust_env=False,
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
                keepalive_expiry=30.0,
            ),
        )
        _HTTP_CLIENT_LOCAL.client = client
    return client


def _get_bytes(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
) -> tuple[bytes, Mapping[str, str]]:
    last_error: Exception | None = None
    for attempt in range(10):
        try:
            request_headers = {
                "User-Agent": "weather-ecmwf-history/1",
                **dict(headers or {}),
            }
            client = _thread_http_client()
            with client.stream(
                "GET",
                url,
                headers=request_headers,
                timeout=timeout_seconds,
            ) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise ContractError(
                        "ecmwf_archive_response_too_large"
                    )
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ContractError(
                            "ecmwf_archive_response_too_large"
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
                and exc.response.status_code
                not in {429, 500, 502, 503, 504}
            ):
                raise
            last_error = exc
            if attempt < 9:
                slow_down = (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code == 503
                )
                time_module.sleep(
                    min(
                        (2.0 if slow_down else 0.5) * (2**attempt),
                        30.0,
                    )
                )
    raise ContractError(
        "ecmwf_archive_transport_failed:"
        + type(last_error).__name__
    ) from last_error


def _period_end_steps(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init_utc: datetime,
) -> list[int]:
    target = date.fromisoformat(event_date)
    zone = ZoneInfo(settlement_timezone)
    result = [
        step
        for step in range(3, 73, 3)
        if (
            run_init_utc + timedelta(hours=step)
        ).astimezone(zone).date() == target
    ]
    if not 7 <= len(result) <= 9:
        raise ContractError("ecmwf_archive_local_day_grid_incomplete")
    return result


def _object_urls(
    *,
    run_init: datetime,
    forecast_step: int,
) -> tuple[str, str, str]:
    run_day = run_init.strftime("%Y%m%d")
    cycle = run_init.strftime("%H")
    timestamp = run_init.strftime("%Y%m%d%H%M%S")
    prefix = (
        f"{run_day}/{cycle}z/ifs/0p25/enfo/"
        f"{timestamp}-{forecast_step}h-enfo-ef"
    )
    return (
        f"{BASE_URL}/{prefix}.index",
        f"{BASE_URL}/{prefix}.grib2",
        prefix,
    )


def _parse_index(
    raw: bytes,
    *,
    forecast_step: int,
) -> list[dict[str, Any]]:
    if len(raw) > MAX_INDEX_BYTES:
        raise ContractError("ecmwf_archive_index_too_large")
    rows = []
    try:
        lines = raw.decode("utf-8").splitlines()
        if len(lines) > 20_000:
            raise ContractError("ecmwf_archive_index_row_limit")
        for line in lines:
            row = json.loads(line)
            if (
                isinstance(row, Mapping)
                and row.get("param") == "mx2t3"
                and row.get("type") == "pf"
                and row.get("levtype") == "sfc"
                and int(row.get("step") or -1) == forecast_step
            ):
                rows.append(dict(row))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError("ecmwf_archive_index_invalid") from exc
    numbers = {int(row.get("number") or 0) for row in rows}
    if (
        len(rows) != EXPECTED_MEMBER_COUNT
        or numbers != set(range(1, EXPECTED_MEMBER_COUNT + 1))
    ):
        raise ContractError("ecmwf_archive_member_index_incomplete")
    for row in rows:
        offset = int(row.get("_offset") or -1)
        length = int(row.get("_length") or 0)
        if (
            offset < 0
            or length <= 16
            or length > MAX_MESSAGE_BYTES
        ):
            raise ContractError("ecmwf_archive_message_range_invalid")
    return sorted(rows, key=lambda row: int(row["number"]))


def _download_message(
    *,
    grib_url: str,
    index_sha256: str,
    object_key: str,
    row: Mapping[str, Any],
    timeout_seconds: float,
) -> tuple[bytes, dict[str, Any]]:
    offset = int(row["_offset"])
    length = int(row["_length"])
    message, headers = _get_bytes(
        grib_url,
        timeout_seconds=timeout_seconds,
        max_bytes=MAX_MESSAGE_BYTES,
        headers={"Range": f"bytes={offset}-{offset + length - 1}"},
    )
    if len(message) != length:
        raise ContractError("ecmwf_archive_range_length_mismatch")
    member_number = int(row["number"])
    return message, {
        "member_id": f"perturbed_{member_number:02d}",
        "member_number": member_number,
        "forecast_step": int(row["step"]),
        "object_key": object_key,
        "index_sha256": index_sha256,
        "index_row_sha256": content_sha256(dict(row)),
        "range_start": offset,
        "range_end": offset + length - 1,
        "message_sha256": hashlib.sha256(message).hexdigest(),
        "last_modified": str(headers.get("last-modified") or ""),
    }


def _build_station_capture(
    *,
    station: str,
    event_date: str,
    scope: Mapping[str, Any],
    run_init: datetime,
    query_at: datetime,
    member_values: Mapping[str, Sequence[float]],
    receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    steps = list(scope["forecast_steps"])
    station_receipts = [
        dict(row)
        for row in receipts
        if int(row["forecast_step"]) in set(steps)
    ]
    if len(station_receipts) != EXPECTED_MEMBER_COUNT * len(steps):
        raise ContractError("ecmwf_archive_station_receipt_incomplete")
    members = [
        {
            "ensemble_system_id": SYSTEM_ID,
            "member_id": member_id,
            "member_role": "PERTURBED",
            "raw_tmax_c": max(float(value) for value in values),
        }
        for member_id, values in sorted(member_values.items())
    ]
    if (
        len(members) != EXPECTED_MEMBER_COUNT
        or any(
            len(member_values[row["member_id"]]) != len(steps)
            for row in members
        )
    ):
        raise ContractError("ecmwf_archive_station_member_incomplete")
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
    member_identity = [
        {
            "member_id": row["member_id"],
            "member_role": row["member_role"],
            "raw_tmax_c": row["raw_tmax_c"],
        }
        for row in members
    ]
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": scope["settlement_timezone"],
        "latitude": float(scope["latitude"]),
        "longitude": float(scope["longitude"]),
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "run_cycle": RUN_CYCLE,
        "archive_lead_bucket": ARCHIVE_LEAD_BUCKET,
        "forecast_steps": steps,
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": "ECMWF",
        "member_count": len(members),
        "member_availability_contract": (
            "OFFICIAL_OPEN_DATA_PERTURBED_1_TO_50_NO_CONTROL"
        ),
        "members": members,
        "member_set_sha256": content_sha256(member_identity),
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
            ) / len(members)
            for bucket, bounds in frozen_intervals.items()
        },
        "field_receipt_count": len(station_receipts),
        "field_receipt_set_sha256": content_sha256(
            station_receipts
        ),
        "field_receipts": station_receipts,
        "archive_query_at_utc": query_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "source_prefix": (
            f"{BASE_URL}/{run_init.strftime('%Y%m%d')}/"
            f"{run_init.strftime('%H')}z/ifs/0p25/enfo/"
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


def capture_ecmwf_station_days(
    *,
    event_date: str,
    station_scopes: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    timeout_seconds: float = 45.0,
) -> dict[str, Any]:
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
        query_at <= run_init
        or not 1 <= len(station_scopes) <= MAX_STATION_COUNT
        or shutil.disk_usage(Path(tempfile.gettempdir())).free
        < MIN_FREE_BYTES
    ):
        raise ContractError("ecmwf_archive_scope_invalid")
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
            raise ContractError("ecmwf_archive_station_scope_invalid")
        scopes[station] = {
            "station_id": station,
            "settlement_timezone": timezone_name,
            "latitude": latitude,
            "longitude": longitude,
            "registry_rows": list(registry_rows),
            "forecast_steps": _period_end_steps(
                event_date=event_date,
                settlement_timezone=timezone_name,
                run_init_utc=run_init,
            ),
        }
    union_steps = sorted({
        step
        for scope in scopes.values()
        for step in scope["forecast_steps"]
    })
    if not union_steps or len(union_steps) > 17:
        raise ContractError("ecmwf_archive_union_grid_invalid")
    member_values = {
        station: {
            f"perturbed_{number:02d}": []
            for number in range(1, EXPECTED_MEMBER_COUNT + 1)
        }
        for station in scopes
    }
    receipts = []
    total_bytes = 0
    index_rows = []

    def fetch_index(
        step: int,
    ) -> tuple[int, str, str, bytes]:
        index_url, grib_url, object_prefix = _object_urls(
            run_init=run_init,
            forecast_step=step,
        )
        index_raw, _headers = _get_bytes(
            index_url,
            timeout_seconds=timeout_seconds,
            max_bytes=MAX_INDEX_BYTES,
        )
        return step, grib_url, object_prefix, index_raw

    # Index objects are about 2 MiB each and historical S3 throughput is
    # intentionally modest. Fetch at the same four-connection ceiling used
    # for message ranges instead of serializing 15-17 independent objects.
    # Results are sorted before parsing so output identity remains stable.
    with ThreadPoolExecutor(
        max_workers=MAX_DOWNLOAD_WORKERS,
        thread_name_prefix="ecmwf-index",
    ) as index_executor:
        index_futures = [
            index_executor.submit(fetch_index, step)
            for step in union_steps
        ]
        fetched_indexes = [
            future.result()
            for future in index_futures
        ]
    for step, grib_url, object_prefix, index_raw in sorted(
        fetched_indexes,
        key=lambda row: row[0],
    ):
        total_bytes += len(index_raw)
        index_sha = hashlib.sha256(index_raw).hexdigest()
        for row in _parse_index(
            index_raw,
            forecast_step=step,
        ):
            index_rows.append((
                grib_url,
                index_sha,
                object_prefix + ".grib2",
                row,
            ))
    with ThreadPoolExecutor(
        max_workers=MAX_DOWNLOAD_WORKERS,
        thread_name_prefix="ecmwf-download",
    ) as executor:
        pending: dict[Any, int] = {}
        row_iter = iter(index_rows)

        def submit_next() -> bool:
            try:
                grib_url, index_sha, object_key, row = next(
                    row_iter
                )
            except StopIteration:
                return False
            future = executor.submit(
                _download_message,
                grib_url=grib_url,
                index_sha256=index_sha,
                object_key=object_key,
                row=row,
                timeout_seconds=timeout_seconds,
            )
            pending[future] = int(row["step"])
            return True

        for _index in range(MAX_DOWNLOAD_WORKERS):
            submit_next()
        while pending:
            done, _not_done = wait(
                pending,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                step = pending.pop(future)
                message, receipt = future.result()
                total_bytes += len(message)
                if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                    raise ContractError(
                        "ecmwf_archive_total_response_too_large"
                    )
                active = [
                    (
                        station,
                        float(scope["latitude"]),
                        float(scope["longitude"]),
                    )
                    for station, scope in scopes.items()
                    if step in scope["forecast_steps"]
                ]
                decoded = decode_nearest_temperatures_c(
                    message,
                    points=active,
                )
                member_id = str(receipt["member_id"])
                for station, value in decoded.items():
                    member_values[station][member_id].append(value)
                receipts.append(receipt)
                assert_process_memory_budget(
                    projected_bytes=(
                        MAX_DOWNLOAD_WORKERS * MAX_MESSAGE_BYTES
                    )
                )
                submit_next()
    receipts.sort(
        key=lambda row: (
            int(row["member_number"]),
            int(row["forecast_step"]),
        )
    )
    captures = [
        _build_station_capture(
            station=station,
            event_date=event_date,
            scope=scope,
            run_init=run_init,
            query_at=query_at,
            member_values=member_values[station],
            receipts=receipts,
        )
        for station, scope in sorted(scopes.items())
    ]
    identity = {
        "schema": MULTISTATION_SCHEMA,
        "event_date": event_date,
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "run_cycle": RUN_CYCLE,
        "archive_lead_bucket": ARCHIVE_LEAD_BUCKET,
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": "ECMWF",
        "member_count": EXPECTED_MEMBER_COUNT,
        "member_availability_contract": (
            "OFFICIAL_OPEN_DATA_PERTURBED_1_TO_50_NO_CONTROL"
        ),
        "station_count": len(captures),
        "station_ids": [
            capture["station_id"] for capture in captures
        ],
        "union_forecast_steps": union_steps,
        "downloaded_field_receipt_count": len(receipts),
        "downloaded_field_receipt_set_sha256": content_sha256(
            receipts
        ),
        "total_response_bytes": total_bytes,
        "captures": captures,
        "archive_query_at_utc": query_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "single_download_per_global_member_field": True,
        "training_only": True,
        "holdout_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _ensure_system(conn: sqlite3.Connection) -> tuple[str, str]:
    identity = {
        "ensemble_system_id": SYSTEM_ID,
        "canonical_name": "ECMWF IFS Open Data PF50",
        "independence_family_id": "ECMWF",
        "provider_id": "ECMWF",
        "source_class": "NATIVE_ENSEMBLE_PROVIDER",
        "aggregator_or_republisher": 0,
        "expected_member_count": EXPECTED_MEMBER_COUNT,
        "member_identity_contract_version": (
            "ecmwf_open_data_perturbed_1_to_50_no_control_v1"
        ),
        "publication_contract_version": (
            "native_run_init_known_archive_publication_unknown_v1"
        ),
        "valid_from_utc": "2026-06-01T00:00:00Z",
        "valid_until_utc": "9999-12-31T23:59:59Z",
    }
    row = {
        **identity,
        "content_sha256": content_sha256(identity),
    }
    _insert_content_addressed(
        conn,
        table="calibration_native_ensemble_system",
        primary_key="ensemble_system_id",
        row=row,
    )
    actual = conn.execute(
        "SELECT provider_id,independence_family_id,"
        "expected_member_count FROM calibration_native_ensemble_system "
        "WHERE ensemble_system_id=?",
        (SYSTEM_ID,),
    ).fetchone()
    if (
        actual is None
        or str(actual[0]) != "ECMWF"
        or str(actual[1]) != "ECMWF"
        or int(actual[2]) != EXPECTED_MEMBER_COUNT
    ):
        raise ContractError("ecmwf_archive_system_registration_invalid")
    return str(actual[0]), str(actual[1])


def ingest_ecmwf_station_day(
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
    members = capture.get("members")
    if (
        capture.get("schema") != SCHEMA
        or capture.get("source_class") != "OFFICIAL_NATIVE_ARCHIVE"
        or capture.get("member_availability_contract")
        != "OFFICIAL_OPEN_DATA_PERTURBED_1_TO_50_NO_CONTROL"
        or capture.get("training_only") is not True
        or capture.get("holdout_eligible") is not False
        or capture.get("content_sha256") != content_sha256(identity)
        or not isinstance(members, list)
        or len(members) != EXPECTED_MEMBER_COUNT
    ):
        raise ContractError("ecmwf_archive_capture_invalid")
    provider_id, _family = _ensure_system(conn)
    station = str(capture["station_id"])
    event_date = str(capture["event_date"])
    existing = conn.execute(
        "SELECT member_id,member_role,raw_tmax_c "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE ensemble_system_id=? AND station_id=? "
        "AND target_local_date=? AND archive_lead_bucket=? "
        "AND run_cycle=? AND extraction_version=? ORDER BY member_id",
        (
            SYSTEM_ID,
            station,
            event_date,
            ARCHIVE_LEAD_BUCKET,
            RUN_CYCLE,
            EXTRACTION_VERSION,
        ),
    ).fetchall()
    expected = sorted(
        (
            str(row["member_id"]),
            str(row["member_role"]),
            float(row["raw_tmax_c"]),
        )
        for row in members
    )
    if existing:
        if len(existing) != EXPECTED_MEMBER_COUNT or any(
            (
                str(actual[0]) != wanted[0]
                or str(actual[1]) != wanted[1]
                or abs(float(actual[2]) - wanted[2]) > 1e-9
            )
            for actual, wanted in zip(existing, expected)
        ):
            raise ContractError("ecmwf_archive_frozen_fact_conflict")
        return {
            "state": "REPLAY",
            "member_run_wrote_count": 0,
            "member_bucket_wrote_count": 0,
        }
    member_writes = 0
    for row in members:
        member_identity = {
            "ensemble_system_id": SYSTEM_ID,
            "station_id": station,
            "target_local_date": event_date,
            "archive_lead_bucket": ARCHIVE_LEAD_BUCKET,
            "run_cycle": RUN_CYCLE,
            "member_id": str(row["member_id"]),
            "member_role": "PERTURBED",
            "raw_tmax_c": float(row["raw_tmax_c"]),
            "settlement_definition": "official_daily_tmax_c",
            "extraction_version": EXTRACTION_VERSION,
            "provider_id": provider_id,
            "transport_provider_id": "ECMWF_OPEN_DATA_S3",
            "native_run_init_utc": str(capture["run_init_utc"]),
            "offline_oos_eligible": 1,
            "archive_query_at_utc": str(
                capture["archive_query_at_utc"]
            ),
            "source_url": str(capture["source_prefix"]),
            "raw_object_ref": (
                "ecmwf_field_receipt_set_sha256:"
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
                    "ecmwf-history-member-" + row_sha[:28]
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
        "member_count": EXPECTED_MEMBER_COUNT,
        "bucket_contract_sha256": str(
            capture["bucket_contract_sha256"]
        ),
        "bucket_intervals_json": json.dumps(
            capture["bucket_intervals"],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "raw_bucket_probabilities_json": json.dumps(
            capture["raw_bucket_probabilities"],
            sort_keys=True,
            separators=(",", ":"),
        ),
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
                "ecmwf-history-bucket-" + bucket_sha[:28]
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


def ingest_ecmwf_station_days(
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
        or archive.get("content_sha256") != content_sha256(identity)
        or not isinstance(captures, list)
        or len(captures) != int(archive.get("station_count") or 0)
    ):
        raise ContractError("ecmwf_archive_multistation_invalid")
    results = [
        ingest_ecmwf_station_day(conn, capture)
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
    }


__all__ = [
    "ARCHIVE_LEAD_BUCKET",
    "EXTRACTION_VERSION",
    "MULTISTATION_SCHEMA",
    "SCHEMA",
    "SYSTEM_ID",
    "capture_ecmwf_station_days",
    "ingest_ecmwf_station_days",
]
