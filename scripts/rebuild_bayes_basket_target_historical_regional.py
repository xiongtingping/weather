#!/usr/bin/env python3
"""Rebuild one station-scoped regional model's closed historical d2 curves.

Only explicit Open-Meteo single runs are accepted.  The resulting artifact is
offline OOS evidence: it never claims native publication/arrival, writes a
database, calls the order gateway, or uses an unclosed/future target day.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time as _time
from typing import Any, Mapping, Sequence

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import event_day_window  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402


SCHEMA = "bayes_basket_target_historical_regional_exact_run_evidence_v1"
ENDPOINT = "https://single-runs-api.open-meteo.com/v1/forecast"
DEFAULT_POOL = (
    ROOT / "config/bayes_basket_target/station_regional_model_pool_v1.json"
)
DEFAULT_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_RAW_ROOT = (
    ROOT / "data/research/bayes_basket_target/regional_exact_run_history"
)
MAX_DAYS = 32
MAX_STATIONS = 45
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024


def _parse_utc(value: str, *, reason: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(reason)
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _dates(first: str, last: str) -> list[str]:
    start = date.fromisoformat(first)
    end = date.fromisoformat(last)
    if start > end or (end - start).days + 1 > MAX_DAYS:
        raise ContractError("regional_history_date_range_invalid")
    result: list[str] = []
    cursor = start
    while cursor <= end:
        result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def _load_scope(
    *,
    pool_path: Path,
    registry_path: Path,
    model_id: str,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    pool = load_bounded_json(pool_path.resolve())
    if pool.get("schema") != "bayes_basket_target_station_regional_model_pool_v1":
        raise ContractError("regional_history_pool_schema_invalid")
    models = pool.get("regional_models")
    global_capture_models = pool.get("global_aggregator_capture_models")
    station_pool = pool.get("station_regional_pool")
    if (
        not isinstance(models, Mapping)
        or not isinstance(global_capture_models, Mapping)
        or not isinstance(station_pool, Mapping)
    ):
        raise ContractError("regional_history_pool_shape_invalid")
    profile = models.get(model_id)
    pool_class = "regional"
    if not isinstance(profile, Mapping):
        profile = global_capture_models.get(model_id)
        pool_class = "global"
    if not isinstance(profile, Mapping):
        raise ContractError("regional_history_model_not_registered")
    if (
        profile.get("source_class") != "aggregator_republisher"
        or profile.get("run_cycle") != "12Z"
        or int(profile.get("run_day_offset") or 0) not in {1, 2}
    ):
        raise ContractError("regional_history_model_profile_invalid")
    stations = (
        sorted(str(station).strip().upper() for station in station_pool)
        if pool_class == "global"
        else sorted(
            str(station).strip().upper()
            for station, candidates in station_pool.items()
            if isinstance(candidates, list) and model_id in candidates
        )
    )
    if not stations or len(stations) > MAX_STATIONS:
        raise ContractError("regional_history_station_scope_invalid")
    registry = load_bounded_json(registry_path.resolve())
    values: dict[str, set[str]] = {station: set() for station in stations}
    for row in registry.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        if station in values and timezone_name:
            values[station].add(timezone_name)
    timezones: dict[str, str] = {}
    for station, station_timezones in values.items():
        if len(station_timezones) != 1:
            raise ContractError(
                f"regional_history_registry_timezone_invalid:{station}"
            )
        timezones[station] = next(iter(station_timezones))
    return {**dict(profile), "model_pool_class": pool_class}, stations, timezones


def _bounded_response(
    session: requests.Session,
    *,
    stations: Sequence[str],
    model_parameter: str,
    run_initialization: datetime,
    timeout_seconds: float,
) -> tuple[list[Mapping[str, Any]], bytes, str]:
    params = {
        "latitude": ",".join(
            str(station_coordinates(station)["latitude"])
            for station in stations
        ),
        "longitude": ",".join(
            str(station_coordinates(station)["longitude"])
            for station in stations
        ),
        "hourly": "temperature_2m",
        "models": model_parameter,
        "timezone": "UTC",
        "run": run_initialization.strftime("%Y-%m-%dT%H:%M"),
        "forecast_days": 4,
    }
    with session.get(
        ENDPOINT,
        params=params,
        timeout=timeout_seconds,
        stream=True,
    ) as response:
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_RESPONSE_BYTES:
            raise ContractError("regional_history_content_length_exceeded")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ContractError("regional_history_response_bytes_exceeded")
            assert_process_memory_budget(projected_bytes=total * 3)
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("regional_history_response_json_invalid") from exc
        rows = payload if isinstance(payload, list) else [payload]
        if (
            len(rows) != len(stations)
            or not all(isinstance(row, Mapping) for row in rows)
        ):
            raise ContractError("regional_history_response_shape_invalid")
        return rows, raw, str(response.url)


def _persist_raw(raw_root: Path, raw: bytes) -> tuple[Path, str]:
    digest = hashlib.sha256(raw).hexdigest()
    destination = raw_root / "raw" / f"{digest}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ContractError("regional_history_raw_hash_collision")
        return destination, digest
    if shutil.disk_usage(destination.parent).free < MIN_FREE_BYTES + len(raw):
        raise ContractError("regional_history_minimum_free_disk_guard")
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination, digest


def _curve(
    payload: Mapping[str, Any],
    *,
    station: str,
    event_date: str,
    target_timezone: str,
    as_of_utc: str,
) -> tuple[list[dict[str, Any]], float]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise ContractError(f"regional_history_hourly_missing:{station}:{event_date}")
    times = hourly.get("time")
    values = hourly.get("temperature_2m")
    if (
        not isinstance(times, list)
        or not isinstance(values, list)
        or len(times) != len(values)
    ):
        raise ContractError(f"regional_history_hourly_shape_invalid:{station}:{event_date}")
    window = event_day_window(
        event_date,
        as_of_utc=as_of_utc,
        target_timezone=target_timezone,
    )
    start = _parse_utc(
        str(window["target_day_start_utc"]),
        reason="regional_history_window_invalid",
    )
    end = _parse_utc(
        str(window["target_day_end_utc"]),
        reason="regional_history_window_invalid",
    )
    points: list[dict[str, Any]] = []
    for raw_time, raw_value in zip(times, values):
        if raw_value is None:
            continue
        valid = _parse_utc(
            f"{raw_time}:00Z" if len(str(raw_time)) == 16 else f"{raw_time}Z",
            reason="regional_history_valid_time_invalid",
        )
        if start <= valid < end:
            points.append({
                "valid_time_utc": _utc_text(valid),
                "temperature_2m_c": float(raw_value),
            })
    expected = int((end - start).total_seconds() // 3600)
    if (
        len(points) != expected
        or len({row["valid_time_utc"] for row in points}) != expected
    ):
        raise ContractError(
            f"regional_history_curve_incomplete:{station}:{event_date}:"
            f"{len(points)}:{expected}"
        )
    return points, max(float(row["temperature_2m_c"]) for row in points)


def rebuild(
    *,
    model_id: str,
    first_date: str,
    last_date: str,
    as_of_utc: str,
    pool_path: Path,
    registry_path: Path,
    raw_root: Path,
    output_path: Path,
    session: requests.Session,
    timeout_seconds: float = 45.0,
    request_interval_seconds: float = 0.0,
) -> dict[str, Any]:
    model = str(model_id or "").strip().upper()
    days = _dates(first_date, last_date)
    as_of = _parse_utc(as_of_utc, reason="regional_history_asof_invalid")
    profile, stations, timezones = _load_scope(
        pool_path=pool_path,
        registry_path=registry_path,
        model_id=model,
    )
    run_day_offset = int(profile["run_day_offset"])
    model_rows: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    last_request_started = 0.0
    for event_date in days:
        run = datetime.combine(
            date.fromisoformat(event_date) - timedelta(days=run_day_offset),
            time(hour=12),
            tzinfo=timezone.utc,
        )
        if run > as_of:
            raise ContractError("regional_history_future_run_forbidden")
        if any(
            _parse_utc(
                str(event_day_window(
                    event_date,
                    as_of_utc=as_of_utc,
                    target_timezone=timezones[station],
                )["target_day_end_utc"]),
                reason="regional_history_window_invalid",
            ) > as_of
            for station in stations
        ):
            raise ContractError("regional_history_unclosed_target_forbidden")
        try:
            interval = max(0.0, min(10.0, float(request_interval_seconds)))
            remaining = interval - (_time.monotonic() - last_request_started)
            if remaining > 0:
                _time.sleep(remaining)
            last_request_started = _time.monotonic()
            last_fetch_error: Exception | None = None
            for _attempt in range(3):
                try:
                    payloads, raw, request_url = _bounded_response(
                        session,
                        stations=stations,
                        model_parameter=str(profile["model_parameter"]),
                        run_initialization=run,
                        timeout_seconds=timeout_seconds,
                    )
                    break
                except (OSError, requests.RequestException) as exc:
                    last_fetch_error = exc
                    if _attempt < 2 and interval > 0:
                        _time.sleep(interval)
            else:
                raise ContractError(
                    "regional_history_fetch_retries_exhausted:"
                    + str(last_fetch_error)[:160]
                )
            raw_path, raw_sha = _persist_raw(raw_root, raw)
            inputs.append({
                "event_date": event_date,
                "run_initialization_utc": _utc_text(run),
                "request_url": request_url,
                "raw_object_ref": str(raw_path.resolve()),
                "raw_message_sha256": raw_sha,
            })
            day_rows: list[dict[str, Any]] = []
            for station, payload in zip(stations, payloads):
                points, forecast_max = _curve(
                    payload,
                    station=station,
                    event_date=event_date,
                    target_timezone=timezones[station],
                    as_of_utc=as_of_utc,
                )
                identity = {
                    "station_id": station,
                    "event_date": event_date,
                    "target_timezone": timezones[station],
                    "model_id": model,
                    "provider_id": str(profile["provider_id"]),
                    "source_class": "aggregator_republisher",
                    "model_pool_class": str(profile["model_pool_class"]),
                    "model_run_initialization_utc": _utc_text(run),
                    "sample_count": len(points),
                    "daily_curve_complete": True,
                    "forecast_daily_max_c": forecast_max,
                    "points": points,
                    "raw_object_ref": str(raw_path.resolve()),
                    "raw_message_sha256": raw_sha,
                    "native_publication_claimed": False,
                    "native_arrival_claimed": False,
                    "future_data_used": False,
                }
                day_rows.append({
                    **identity,
                    "receipt_sha256": content_sha256(identity),
                })
            model_rows.extend(day_rows)
        except (ContractError, OSError, requests.RequestException, ValueError) as exc:
            blockers.append({
                "event_date": event_date,
                "reason": str(exc)[:256],
            })
    identity = {
        "schema": (
            SCHEMA
            if profile["model_pool_class"] == "regional"
            else "bayes_basket_target_historical_aggregator_rebuild_evidence_v1"
        ),
        "model_id": model,
        "provider_id": str(profile["provider_id"]),
        "source": {
            "source_class": "aggregator_republisher",
            "source_url": ENDPOINT,
            "model_parameter": str(profile["model_parameter"]),
            "run_cycle": "12Z",
            "run_day_offset": run_day_offset,
            "single_run_parameter_explicit": True,
            "native_publication_time_available": False,
        },
        "start_date": days[0],
        "end_date": days[-1],
        "as_of_utc": _utc_text(as_of),
        "station_ids": stations,
        "model_rows": model_rows,
        "inputs": inputs,
        "blockers": blockers,
        "checks": {
            "historical_closed_targets_only": True,
            "future_data_used": False,
            "online_weight_update": False,
            "all_expected_station_dates_complete":
                len(model_rows) == len(stations) * len(days)
                and not blockers,
            "native_publication_not_claimed": True,
            "native_arrival_not_claimed": True,
        },
        "boundary": {
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(output_path, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--proxy-url", default="")
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=0.0,
    )
    args = parser.parse_args(argv)
    session = requests.Session()
    session.headers.update({"User-Agent": "bayes-basket-regional-history/1"})
    if args.proxy_url:
        session.proxies.update({
            "http": args.proxy_url,
            "https": args.proxy_url,
        })
    result = rebuild(
        model_id=args.model_id,
        first_date=args.start_date,
        last_date=args.end_date,
        as_of_utc=args.as_of_utc,
        pool_path=args.pool,
        registry_path=args.registry,
        raw_root=args.raw_root / args.model_id.lower(),
        output_path=args.output,
        session=session,
        timeout_seconds=args.timeout_seconds,
        request_interval_seconds=args.request_interval_seconds,
    )
    print(json.dumps({
        "schema": result["schema"],
        "model_id": result["model_id"],
        "station_count": len(result["station_ids"]),
        "model_row_count": len(result["model_rows"]),
        "blocker_count": len(result["blockers"]),
        "future_data_used": False,
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not result["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
