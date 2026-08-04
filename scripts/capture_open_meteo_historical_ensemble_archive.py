#!/usr/bin/env python3
"""Capture missing closed-day Open-Meteo ensemble history.

The collector is intentionally separate from the resident/forward path.  It
only requests dates that are already closed, retains the response by content
hash, and appends explicit ``aggregator_republisher`` historical member rows.
It never writes native-arrival/P1 tables or production decisions.  Existing
rows are detected by station×system×day and are never re-fetched when the
requested interval is already present.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT_ROOT = (
    ROOT / "data" / "research" / "bayes_basket_target"
    / "open_meteo_historical_ensemble"
)
DEFAULT_OUTPUT = ROOT / "output" / "audits" / (
    "open_meteo_historical_ensemble_capture_20260802.json"
)
# The ensemble endpoint exposes archived member maxima directly.  The
# previous-runs forecast endpoint also returns model-shaped keys, but for
# ensemble history it can legally return an all-null hourly family; treating
# that as a complete receipt would silently create zero samples.  Keep the
# historical collector on the same endpoint/variable contract as the retained
# Open-Meteo ensemble receipts.
API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
MAX_STATIONS = 45
MAX_DAYS = 31
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_RAW_BYTES = 256 * 1024 * 1024
MAX_WORKERS = 4
EXTRACTION_VERSION = "open_meteo_archived_daily_tmax_member_v1"

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.open_meteo_ensemble import ENSEMBLE_SYSTEMS  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _date(value: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise RuntimeError("open_meteo_archive_date_invalid") from exc


def _bounded_response(
    *,
    station: str,
    latitude: float,
    longitude: float,
    timezone_name: str,
    start_date: str,
    end_date: str,
    api_models: str,
    system_ids: list[str] | None = None,
    timeout_seconds: float,
) -> dict[str, Any]:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max",
        "models": api_models,
        "timezone": timezone_name,
        "start_date": start_date,
        "end_date": end_date,
    }
    url = API_URL + "?" + urlencode(params)
    with requests.get(
        API_URL,
        params=params,
        timeout=timeout_seconds,
        stream=True,
        headers={"User-Agent": "weather-basket-open-meteo-history/1"},
    ) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise RuntimeError("open_meteo_archive_response_too_large")
            chunks.append(chunk)
        raw = b"".join(chunks)
        response_url = str(response.url or url)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("open_meteo_archive_response_invalid") from exc
    if not isinstance(payload, Mapping) or payload.get("error"):
        raise RuntimeError(
            "open_meteo_archive_api_error:" + str(
                payload.get("reason") if isinstance(payload, Mapping) else ""
            )
        )
    return {
        "station_id": station,
        # Keep the exact system scope beside the response.  The ensemble API
        # returns generic member keys whenever one model is requested; a
        # multi-model request can therefore not be safely assigned back to
        # multiple system ids even when the payload happens to contain all
        # keys.  The caller creates one request per API model and carries the
        # scope through this field for bounded row materialization.
        "system_ids": list(system_ids or []),
        "request_url": response_url,
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "response_bytes": len(raw),
        "payload": dict(payload),
    }


def _station_timezones(db: Path) -> dict[str, str]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT station_id, settlement_timezone "
            "FROM basket_target_market_registry "
            "WHERE station_id IS NOT NULL AND station_id<>'' "
            "AND settlement_timezone IS NOT NULL AND settlement_timezone<>'' "
            "GROUP BY station_id, settlement_timezone"
        ).fetchall()
    grouped: dict[str, set[str]] = {}
    for station, timezone_name in rows:
        grouped.setdefault(str(station).upper(), set()).add(
            str(timezone_name)
        )
    result: dict[str, str] = {}
    for station, values in grouped.items():
        if len(values) != 1:
            raise RuntimeError("open_meteo_archive_station_timezone_invalid:" + station)
        result[station] = next(iter(values))
    return result


def _existing_days(
    db: Path,
    *,
    stations: list[str],
    systems: list[str],
) -> set[tuple[str, str, str]]:
    if not stations or not systems:
        return set()
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        station_marks = ",".join("?" for _ in stations)
        system_marks = ",".join("?" for _ in systems)
        rows = conn.execute(
            "SELECT DISTINCT station_id, ensemble_system_id, target_local_date "
            "FROM calibration_republisher_ensemble_historical_member_run "
            "WHERE station_id IN (" + station_marks + ") "
            "AND ensemble_system_id IN (" + system_marks + ") "
            "AND transport_provider_id='OPEN_METEO' "
            "AND qc_status='ACCEPTED'",
            tuple(stations) + tuple(systems),
        ).fetchall()
    return {(str(st).upper(), str(sys), str(day)) for st, sys, day in rows}


def _member_series_keys(
    hourly: Mapping[str, Any],
    *,
    api_model: str,
    expected_member_count: int,
    allow_generic: bool,
) -> list[str] | None:
    """Resolve one model's control/member hourly keys.

    ``previous-runs-api`` emits model-suffixed keys when more than one model
    is requested (for example ``temperature_2m_icon_global_eps``), but
    removes that suffix for a single-model request (``temperature_2m`` and
    ``temperature_2m_member01``).  The old collector only handled the former,
    silently quarantining every valid single-model response.  Resolve one
    complete key family atomically and reject partial/mixed families so a
    generic response can never be assigned to the wrong model.
    """

    namespaced = [
        (
            f"temperature_2m_{api_model}"
            if index == 0
            else f"temperature_2m_member{index:02d}_{api_model}"
        )
        for index in range(expected_member_count)
    ]
    if all(
        key in hourly and isinstance(hourly[key], list)
        for key in namespaced
    ):
        return namespaced
    if not allow_generic:
        return None

    # A single-model response uses an unsuffixed control and member keys.
    # Accept the documented zero-padded form, with an unpadded fallback for
    # older previous-runs responses, only when the complete family is present.
    generic_candidates = [
        [
            (
                "temperature_2m"
                if index == 0
                else f"temperature_2m_member{index:02d}"
            )
            for index in range(expected_member_count)
        ],
        [
            (
                "temperature_2m"
                if index == 0
                else f"temperature_2m_member{index}"
            )
            for index in range(expected_member_count)
        ],
    ]
    for candidate in generic_candidates:
        if all(
            key in hourly and isinstance(hourly[key], list)
            for key in candidate
        ):
            return candidate
    return None


def _build_rows(
    *,
    response: Mapping[str, Any],
    station: str,
    event_dates: list[str],
    systems: list[str],
    captured_at_utc: str,
    missing_scopes: set[tuple[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = response["payload"]
    daily = payload.get("daily")
    if isinstance(daily, Mapping):
        return _build_daily_rows(
            response=response,
            station=station,
            event_dates=event_dates,
            systems=systems,
            captured_at_utc=captured_at_utc,
            missing_scopes=missing_scopes,
        )
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise RuntimeError("open_meteo_archive_hourly_missing")
    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        raise RuntimeError("open_meteo_archive_hourly_time_missing")
    date_indices: dict[str, list[int]] = {}
    for index, raw_time in enumerate(times):
        date_indices.setdefault(str(raw_time)[:10], []).append(index)
    response_sha = str(response["response_sha256"])
    response_url = str(response["request_url"])
    rows: list[dict[str, Any]] = []
    model_counts: dict[str, int] = {}
    unavailable: list[dict[str, str]] = []
    # Generic keys are safe only when exactly one distinct API model was
    # requested.  With multiple models a generic family is ambiguous and is
    # therefore quarantined rather than copied into every system.
    requested_api_models = {
        str(ENSEMBLE_SYSTEMS[item]["api_model"])
        for item in systems
        if item in ENSEMBLE_SYSTEMS
    }
    allow_generic = len(requested_api_models) == 1
    for event_date in event_dates:
        for system_id in systems:
            if missing_scopes is not None and (system_id, event_date) not in missing_scopes:
                continue
            config = ENSEMBLE_SYSTEMS[system_id]
            indices = date_indices.get(event_date) or []
            api_model = str(config["api_model"])
            if len(indices) != 24:
                unavailable.append({
                    "event_date": event_date,
                    "system_id": system_id,
                    "reason": "hourly_day_incomplete",
                })
                continue
            members: list[dict[str, Any]] = []
            incomplete = False
            member_keys = _member_series_keys(
                hourly,
                api_model=api_model,
                expected_member_count=int(config["expected_member_count"]),
                allow_generic=allow_generic,
            )
            if member_keys is None:
                unavailable.append({
                    "event_date": event_date,
                    "system_id": system_id,
                    "reason": "member_set_incomplete",
                })
                continue
            for index, key in enumerate(member_keys):
                values = hourly.get(key)
                if not isinstance(values, list):
                    incomplete = True
                    break
                day_values = [values[item] for item in indices]
                if len(day_values) != 24 or any(value is None for value in day_values):
                    incomplete = True
                    break
                members.append({
                    "member_id": "control" if index == 0 else f"perturbed_{index:02d}",
                    "member_role": "CONTROL" if index == 0 else "PERTURBED",
                    "raw_tmax_c": max(float(value) for value in day_values),
                })
            if incomplete:
                unavailable.append({
                    "event_date": event_date,
                    "system_id": system_id,
                    "reason": "member_set_incomplete",
                })
                continue
            model_counts[system_id] = model_counts.get(system_id, 0) + 1
            for member in members:
                identity = {
                    "ensemble_system_id": system_id,
                    "station_id": station,
                    "target_local_date": event_date,
                    "archive_lead_bucket": "d0",
                    "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                    "member_id": str(member["member_id"]),
                    "member_role": str(member["member_role"]),
                    "raw_tmax_c": float(member["raw_tmax_c"]),
                    "settlement_definition": "official_daily_tmax_c",
                    "extraction_version": EXTRACTION_VERSION,
                    "provider_id": str(config["provider_id"]),
                    "transport_provider_id": "OPEN_METEO",
                    "native_run_init_utc": None,
                    "offline_oos_eligible": 0,
                    "archive_query_at_utc": captured_at_utc,
                    "source_url": response_url,
                    "raw_object_ref": "open_meteo_response_sha256:" + response_sha,
                    "source_object_sha256": response_sha,
                    "qc_status": "ACCEPTED",
                    "authority_scope": "CALIBRATION_TRAIN_ONLY",
                    "holdout_eligible": 0,
                    "historical_oos_contract_id": None,
                    "historical_oos_eligible": 0,
                }
                row = {
                    "historical_member_run_id": "om-history-member-" + content_sha256(identity)[:36],
                    **identity,
                    "content_sha256": content_sha256(identity),
                }
                rows.append(row)
    return rows, {
        "station_id": station,
        "response_sha256": response_sha,
        "response_url": response_url,
        "response_bytes": int(response["response_bytes"]),
        "model_day_counts": model_counts,
        "captured_at_utc": captured_at_utc,
        "unavailable": unavailable,
    }


def _build_daily_rows(
    *,
    response: Mapping[str, Any],
    station: str,
    event_dates: list[str],
    systems: list[str],
    captured_at_utc: str,
    missing_scopes: set[tuple[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Materialize archived daily member maxima from the ensemble endpoint."""

    payload = response["payload"]
    daily = payload.get("daily")
    if not isinstance(daily, Mapping):
        raise RuntimeError("open_meteo_archive_daily_missing")
    times = daily.get("time")
    if not isinstance(times, list) or not times:
        raise RuntimeError("open_meteo_archive_daily_time_missing")
    date_indices = {str(day): index for index, day in enumerate(times)}
    response_sha = str(response["response_sha256"])
    response_url = str(response["request_url"])
    requested_api_models = {
        str(ENSEMBLE_SYSTEMS[item]["api_model"])
        for item in systems
        if item in ENSEMBLE_SYSTEMS
    }
    allow_generic = len(requested_api_models) == 1
    rows: list[dict[str, Any]] = []
    model_counts: dict[str, int] = {}
    unavailable: list[dict[str, str]] = []
    for event_date in event_dates:
        index = date_indices.get(event_date)
        if index is None:
            for system_id in systems:
                if missing_scopes is None or (system_id, event_date) in missing_scopes:
                    unavailable.append({
                        "event_date": event_date,
                        "system_id": system_id,
                        "reason": "daily_day_missing",
                    })
            continue
        for system_id in systems:
            if missing_scopes is not None and (system_id, event_date) not in missing_scopes:
                continue
            config = ENSEMBLE_SYSTEMS[system_id]
            api_model = str(config["api_model"])
            expected = int(config["expected_member_count"])
            namespaced = [
                "temperature_2m_max_" + api_model
                if member_index == 0
                else f"temperature_2m_max_member{member_index:02d}_{api_model}"
                for member_index in range(expected)
            ]
            member_keys: list[str] | None = None
            if all(key in daily and isinstance(daily[key], list) for key in namespaced):
                member_keys = namespaced
            elif allow_generic:
                for candidate in (
                    [
                        "temperature_2m_max"
                        if member_index == 0
                        else f"temperature_2m_max_member{member_index:02d}"
                        for member_index in range(expected)
                    ],
                    [
                        "temperature_2m_max"
                        if member_index == 0
                        else f"temperature_2m_max_member{member_index}"
                        for member_index in range(expected)
                    ],
                ):
                    if all(key in daily and isinstance(daily[key], list) for key in candidate):
                        member_keys = candidate
                        break
            if member_keys is None:
                unavailable.append({
                    "event_date": event_date,
                    "system_id": system_id,
                    "reason": "member_set_incomplete",
                })
                continue
            members: list[dict[str, Any]] = []
            incomplete = False
            for member_index, key in enumerate(member_keys):
                values = daily.get(key)
                if not isinstance(values, list) or index >= len(values) or values[index] is None:
                    incomplete = True
                    break
                members.append({
                    "member_id": "control" if member_index == 0 else f"perturbed_{member_index:02d}",
                    "member_role": "CONTROL" if member_index == 0 else "PERTURBED",
                    "raw_tmax_c": float(values[index]),
                })
            if incomplete:
                unavailable.append({
                    "event_date": event_date,
                    "system_id": system_id,
                    "reason": "member_set_incomplete",
                })
                continue
            model_counts[system_id] = model_counts.get(system_id, 0) + 1
            for member in members:
                identity = {
                    "ensemble_system_id": system_id,
                    "station_id": station,
                    "target_local_date": event_date,
                    "archive_lead_bucket": "d0",
                    "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                    "member_id": str(member["member_id"]),
                    "member_role": str(member["member_role"]),
                    "raw_tmax_c": float(member["raw_tmax_c"]),
                    "settlement_definition": "official_daily_tmax_c",
                    "extraction_version": EXTRACTION_VERSION,
                    "provider_id": str(config["provider_id"]),
                    "transport_provider_id": "OPEN_METEO",
                    "native_run_init_utc": None,
                    "offline_oos_eligible": 0,
                    "archive_query_at_utc": captured_at_utc,
                    "source_url": response_url,
                    "raw_object_ref": "open_meteo_response_sha256:" + response_sha,
                    "source_object_sha256": response_sha,
                    "qc_status": "ACCEPTED",
                    "authority_scope": "CALIBRATION_TRAIN_ONLY",
                    "holdout_eligible": 0,
                    "historical_oos_contract_id": None,
                    "historical_oos_eligible": 0,
                }
                rows.append({
                    "historical_member_run_id": "om-history-member-" + content_sha256(identity)[:36],
                    **identity,
                    "content_sha256": content_sha256(identity),
                })
    return rows, {
        "station_id": station,
        "response_sha256": response_sha,
        "response_url": response_url,
        "response_bytes": int(response["response_bytes"]),
        "model_day_counts": model_counts,
        "captured_at_utc": captured_at_utc,
        "unavailable": unavailable,
    }


def _write_raw(output_root: Path, response: Mapping[str, Any]) -> str:
    raw_dir = output_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    sha = str(response["response_sha256"])
    path = raw_dir / (sha + ".json")
    if not path.exists():
        path.write_text(
            json.dumps(response["payload"], ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    return str(path)


def _commit_rows(db: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    columns = [
        "historical_member_run_id", "ensemble_system_id", "station_id",
        "target_local_date", "archive_lead_bucket", "run_cycle", "member_id",
        "member_role", "raw_tmax_c", "settlement_definition", "extraction_version",
        "provider_id", "transport_provider_id", "native_run_init_utc",
        "offline_oos_eligible", "archive_query_at_utc", "source_url", "raw_object_ref",
        "source_object_sha256", "qc_status", "authority_scope", "holdout_eligible",
        "content_sha256", "historical_oos_contract_id", "historical_oos_eligible",
    ]
    inserted = 0
    with sqlite3.connect(db, timeout=120.0) as conn:
        conn.execute("PRAGMA busy_timeout=120000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ",".join("?" for _ in columns)
            sql = (
                "INSERT OR IGNORE INTO calibration_republisher_ensemble_historical_member_run ("
                + ",".join(columns) + ") VALUES (" + placeholders + ")"
            )
            for row in rows:
                before = conn.total_changes
                conn.execute(sql, tuple(row.get(column) for column in columns))
                inserted += conn.total_changes - before
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--system-id", action="append", default=[])
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    db = args.db.expanduser().resolve(strict=True)
    if db.is_symlink():
        raise RuntimeError("open_meteo_archive_db_symlink_forbidden")
    start, end = _date(args.start_date), _date(args.end_date)
    if end < start or (end - start).days + 1 > MAX_DAYS:
        raise RuntimeError("open_meteo_archive_date_range_invalid")
    today = datetime.now(timezone.utc).date()
    if end >= today:
        raise RuntimeError("open_meteo_archive_forward_date_forbidden")
    dates = [
        (start + timedelta(days=i)).isoformat()
        for i in range((end - start).days + 1)
    ]
    stations = sorted({str(value).strip().upper() for value in args.station_id if str(value).strip()})
    if not stations:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            stations = [str(row[0]).upper() for row in conn.execute(
                "SELECT station_id FROM calibration_station_registry "
                "ORDER BY station_id LIMIT ?", (MAX_STATIONS,)
            )]
    if not stations or len(stations) > MAX_STATIONS:
        raise RuntimeError("open_meteo_archive_station_scope_invalid")
    for station in stations:
        station_coordinates(station)
    systems = sorted({str(value).strip() for value in args.system_id if str(value).strip()})
    if not systems:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            systems = [str(row[0]) for row in conn.execute(
                "SELECT ensemble_system_id FROM calibration_republisher_ensemble_product "
                "ORDER BY ensemble_system_id"
            )]
    unknown = sorted(set(systems) - set(ENSEMBLE_SYSTEMS))
    if unknown:
        raise RuntimeError("open_meteo_archive_system_unknown:" + ",".join(unknown))
    timezones = _station_timezones(db)
    existing = _existing_days(db, stations=stations, systems=systems)
    # Keep the fetch scope at station×system×day.  A complete ECMWF history
    # must not be re-requested merely because ICON/GEM has a missing day for
    # the same station.  The API request still uses one contiguous date range
    # per station, but only models with an actual gap are included and row
    # materialization filters to the exact missing scopes.
    missing_scopes_by_station = {
        station: {
            (system, day)
            for system in systems
            for day in dates
            if (station, system, day) not in existing
        }
        for station in stations
    }
    missing_dates = {
        station: sorted({day for _system, day in scopes})
        for station, scopes in missing_scopes_by_station.items()
    }
    missing_systems_by_station = {
        station: sorted({system for system, _day in scopes})
        for station, scopes in missing_scopes_by_station.items()
    }
    fetch_stations = [
        station for station in stations if missing_scopes_by_station[station]
    ]
    # The ensemble endpoint emits unsuffixed ``temperature_2m_max`` member
    # keys for a single requested model.  Sending a comma-separated model
    # list can return the same generic family, which must be quarantined
    # rather than copied into every requested system.  Build one bounded
    # request per station×API-model so the response is unambiguous and can be
    # safely merged by the existing station×system×day scope filter.
    request_jobs: list[tuple[str, tuple[str, ...], str]] = []
    for station in fetch_stations:
        for system_id in missing_systems_by_station[station]:
            request_jobs.append((
                station,
                (system_id,),
                str(ENSEMBLE_SYSTEMS[system_id]["api_model"]),
            ))
    captured_at = _utc_now()
    responses: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    def fetch(job: tuple[str, tuple[str, ...], str]) -> dict[str, Any]:
        station, station_systems, api_model = job
        return _bounded_response(
            station=station,
            latitude=float(station_coordinates(station)["latitude"]),
            longitude=float(station_coordinates(station)["longitude"]),
            timezone_name=timezones[station],
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            api_models=api_model,
            system_ids=list(station_systems),
            timeout_seconds=float(args.timeout_seconds),
        )

    worker_count = max(1, min(int(args.workers), MAX_WORKERS, len(request_jobs) or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(fetch, job): job for job in request_jobs}
        for future in as_completed(futures):
            station, system_ids, _api_model = futures[future]
            try:
                responses.append(future.result())
            except Exception as exc:  # retain bounded failure evidence
                failures.append({
                    "station_id": station,
                    "system_ids": list(system_ids),
                    "error": type(exc).__name__ + ":" + str(exc)[:240],
                })
    total_bytes = sum(int(item["response_bytes"]) for item in responses)
    if total_bytes > MAX_TOTAL_RAW_BYTES:
        raise RuntimeError("open_meteo_archive_total_response_bytes_exceeded")
    all_rows: list[dict[str, Any]] = []
    response_receipts: list[dict[str, Any]] = []
    raw_paths: list[str] = []
    for response in sorted(responses, key=lambda item: str(item["station_id"])):
        station = str(response["station_id"])
        response_systems = [
            str(value).strip()
            for value in response.get("system_ids", [])
            if str(value).strip()
        ]
        if not response_systems:
            # Retain compatibility with a manually supplied response fixture;
            # real collector responses always carry an explicit one-model
            # scope.  The old fallback remains safe because the generic-key
            # resolver rejects ambiguous multi-model payloads.
            response_systems = list(missing_systems_by_station[station])
        if not set(response_systems).issubset(
            set(missing_systems_by_station[station])
        ):
            failures.append({
                "station_id": station,
                "system_ids": response_systems,
                "error": "response_system_scope_outside_missing_scope",
            })
            continue
        rows, receipt = _build_rows(
            response=response,
            station=station,
            event_dates=missing_dates[station],
            systems=response_systems,
            captured_at_utc=captured_at,
            missing_scopes=missing_scopes_by_station[station],
        )
        receipt["system_ids"] = response_systems
        # Do not retain an all-null/fully quarantined response as if it were a
        # usable raw object.  The receipt remains in the audit output so the
        # missing-day reason is auditable; only a response that yielded at
        # least one complete member-day is content-addressed on disk.
        if rows:
            raw_paths.append(
                _write_raw(args.output_root.expanduser().resolve(), response)
            )
        all_rows.extend(rows)
        response_receipts.append(receipt)
    inserted = _commit_rows(db, all_rows) if args.commit else 0
    result = {
        "schema": "open_meteo_historical_ensemble_capture_v1",
        "captured_at_utc": captured_at,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "stations": stations,
        "systems": systems,
        "missing_dates_by_station": missing_dates,
        "missing_systems_by_station": missing_systems_by_station,
        "fetch_station_count": len(fetch_stations),
        "response_receipts": response_receipts,
        "raw_paths": raw_paths,
        "failures": failures,
        "candidate_member_row_count": len(all_rows),
        "inserted_member_row_count": inserted,
        "commit": bool(args.commit),
        "production_no_live": True,
        "future_event_count": 0,
        "oos_data_policy": "HISTORICAL_CLOSED_DAY_ONLY",
        "native_arrival_index_written": False,
        "p1_authority_written": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
