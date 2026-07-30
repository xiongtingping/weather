#!/usr/bin/env python3
"""Capture one explicit aggregator model run as bounded P0-compatible evidence.

The default model remains CMA for backward compatibility.  Explicit ICON
capture uses the same exact-run contract and remains an
``aggregator_republisher`` source.  The adapter records the observed receive
time, never invents a native publication/arrival time, never enters hard-NO,
and never writes SQLite or an order gateway.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Mapping, Sequence

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import event_day_window  # noqa: E402
from bayes_basket_target.market_registry import load_market_registry_snapshot  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    write_bounded_json,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
    station_coordinates,
)
from official_nwp_https_transport import official_nwp_proxy_url  # noqa: E402


SCHEMA = "bayes_basket_target_native_model_arrival_decode_evidence_v1"
CURVE_SCHEMA = "bayes_basket_target_aggregator_model_daily_curve_v1"
PROVIDER_ID = "cma_grapes_global_open_meteo"
MODEL_ID = "CMA"
SOURCE_CLASS = "aggregator_republisher"
ENDPOINT = "https://single-runs-api.open-meteo.com/v1/forecast"
MODEL_PARAM = "cma_grapes_global"
MODEL_PROFILES = {
    "CMA": {
        "provider_id": PROVIDER_ID,
        "model_param": MODEL_PARAM,
    },
    "ICON": {
        "provider_id": "icon_global_open_meteo",
        "model_param": "dwd_icon_global",
    },
    "HRRR": {
        "provider_id": "ncep_hrrr_conus_open_meteo",
        "model_param": "ncep_hrrr_conus",
    },
    "NBM": {
        "provider_id": "ncep_nbm_conus_open_meteo",
        "model_param": "ncep_nbm_conus",
    },
    "NAM": {
        "provider_id": "ncep_nam_conus_open_meteo",
        "model_param": "ncep_nam_conus",
    },
    "ICON-D2": {
        "provider_id": "icon_d2_open_meteo",
        "model_param": "icon_d2",
    },
    "AROME-FR": {
        "provider_id": "meteofrance_arome_france0025_open_meteo",
        "model_param": "meteofrance_arome_france0025",
    },
    "UKMO-2KM": {
        "provider_id": "ukmo_uk_deterministic_2km_open_meteo",
        "model_param": "ukmo_uk_deterministic_2km",
    },
    "HARMONIE-NL": {
        "provider_id": "knmi_harmonie_arome_netherlands_open_meteo",
        "model_param": "knmi_harmonie_arome_netherlands",
    },
    "HARMONIE-EU": {
        "provider_id": "knmi_harmonie_arome_europe_open_meteo",
        "model_param": "knmi_harmonie_arome_europe",
    },
    "JMA-MSM": {
        "provider_id": "jma_msm_open_meteo",
        "model_param": "jma_msm",
    },
}
DEFAULT_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_RAW_ROOT = ROOT / "data/research/bayes_basket_target/cma_exact_run"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_cma_exact_run_decode.json"
MAX_STATIONS = 15
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_EXACT_RUN_FREE_BYTES = 10 * 1024 * 1024 * 1024


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _now_utc() -> str:
    return _utc_text(datetime.now(timezone.utc))


def _parse_utc(value: str, *, reason: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(reason)
    return parsed.astimezone(timezone.utc)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _station_timezones(
    registry: Mapping[str, Any],
    *,
    stations: Sequence[str],
) -> dict[str, str]:
    values: dict[str, set[str]] = {station: set() for station in stations}
    for row in registry.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        if station in values and timezone_name:
            values[station].add(timezone_name)
    result: dict[str, str] = {}
    for station in stations:
        if len(values[station]) != 1:
            raise ContractError(f"cma_exact_run_timezone_invalid:{station}")
        result[station] = next(iter(values[station]))
    if len(set(result.values())) != 1:
        raise ContractError("cma_exact_run_mixed_timezone_batch")
    return result


def _bounded_response(
    session: requests.Session,
    *,
    station: str,
    run_initialization: datetime,
    timeout_seconds: float,
    model_param: str = MODEL_PARAM,
) -> tuple[dict[str, Any], bytes, str]:
    coordinates = station_coordinates(station)
    params = {
        "latitude": coordinates["latitude"],
        "longitude": coordinates["longitude"],
        "hourly": "temperature_2m",
        "models": model_param,
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
            raise ContractError("cma_exact_run_content_length_exceeded")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ContractError("cma_exact_run_response_bytes_exceeded")
            assert_process_memory_budget(projected_bytes=total * 3)
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("cma_exact_run_response_json_invalid") from exc
        if not isinstance(payload, Mapping):
            raise ContractError("cma_exact_run_response_shape_invalid")
        return dict(payload), raw, str(response.url)


def _persist_raw(raw_root: Path, body: bytes) -> tuple[Path, str]:
    digest = _sha256(body)
    destination = raw_root / "raw" / f"{digest}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if _sha256(destination.read_bytes()) != digest:
            raise ContractError("cma_exact_run_raw_hash_collision")
        return destination, digest
    if shutil.disk_usage(raw_root).free < max(
        MIN_TARGET_FREE_BYTES,
        MIN_EXACT_RUN_FREE_BYTES,
    ) + len(body):
        raise ContractError("cma_exact_run_min_free_disk_guard")
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination, digest


def _hourly(
    payload: Mapping[str, Any],
    *,
    event_date: str,
    target_timezone: str,
    as_of_utc: str,
) -> list[tuple[str, float]]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise ContractError("cma_exact_run_hourly_missing")
    times = hourly.get("time")
    values = hourly.get("temperature_2m")
    if (
        not isinstance(times, list)
        or not isinstance(values, list)
        or len(times) != len(values)
    ):
        raise ContractError("cma_exact_run_hourly_shape_invalid")
    window = event_day_window(
        event_date,
        as_of_utc=as_of_utc,
        target_timezone=target_timezone,
    )
    start = _parse_utc(
        str(window["target_day_start_utc"]),
        reason="cma_exact_run_window_invalid",
    )
    end = _parse_utc(
        str(window["target_day_end_utc"]),
        reason="cma_exact_run_window_invalid",
    )
    points: list[tuple[str, float]] = []
    for raw_time, raw_value in zip(times, values):
        if raw_value is None:
            continue
        stamp = _parse_utc(
            str(raw_time) + (
                "Z"
                if "T" in str(raw_time)
                and "+" not in str(raw_time)
                and not str(raw_time).endswith("Z")
                else ""
            ),
            reason="cma_exact_run_valid_time_invalid",
        )
        value = float(raw_value)
        if start <= stamp < end:
            points.append((_utc_text(stamp), value))
    expected = int((end - start).total_seconds() // 3600)
    if len(points) != expected:
        raise ContractError(
            f"cma_exact_run_curve_incomplete:{len(points)}:{expected}"
        )
    if any(
        _parse_utc(points[index][0], reason="cma_exact_run_valid_time_invalid")
        - _parse_utc(
            points[index - 1][0],
            reason="cma_exact_run_valid_time_invalid",
        )
        != timedelta(hours=1)
        for index in range(1, len(points))
    ):
        raise ContractError("cma_exact_run_curve_gap")
    return points


def capture(
    *,
    stations: Sequence[str],
    event_date: str,
    run_initialization_utc: str,
    as_of_utc: str,
    registry_path: Path = DEFAULT_REGISTRY,
    raw_root: Path = DEFAULT_RAW_ROOT,
    output_path: Path = DEFAULT_OUTPUT,
    timeout_seconds: float = 30.0,
    session: requests.Session | None = None,
    model_id: str = MODEL_ID,
) -> dict[str, Any]:
    normalized_model_id = str(model_id or "").strip().upper()
    profile = MODEL_PROFILES.get(normalized_model_id)
    if profile is None:
        raise ContractError("aggregator_exact_run_model_unsupported")
    provider_id = str(profile["provider_id"])
    model_param = str(profile["model_param"])
    normalized = tuple(sorted({
        str(station).strip().upper()
        for station in stations
        if str(station).strip()
    }))
    canonical = set(coordinate_registry_manifest()["station_ids"])
    if (
        not normalized
        or len(normalized) > MAX_STATIONS
        or set(normalized) - canonical
    ):
        raise ContractError("cma_exact_run_station_scope_invalid")
    run_initialization = _parse_utc(
        run_initialization_utc,
        reason="cma_exact_run_initialization_invalid",
    )
    if run_initialization.hour not in {0, 6, 12, 18}:
        raise ContractError("cma_exact_run_cycle_invalid")
    as_of = _parse_utc(as_of_utc, reason="cma_exact_run_asof_invalid")
    if run_initialization > as_of:
        raise ContractError("cma_exact_run_initialization_after_asof")
    registry = load_market_registry_snapshot(registry_path.resolve())
    timezones = _station_timezones(registry, stations=normalized)
    target_timezone = next(iter(timezones.values()))
    client = session or requests.Session()
    client.headers.update({
        "User-Agent": f"BayesBasketTarget-{normalized_model_id}ExactRun/1.0"
    })
    proxy = official_nwp_proxy_url()
    if proxy:
        client.proxies.update({"http": proxy, "https": proxy})
    curves: list[dict[str, Any]] = []
    decoded_points: list[dict[str, Any]] = []
    input_artifacts: list[dict[str, Any]] = []
    for station in normalized:
        payload, raw, source_url = _bounded_response(
            client,
            station=station,
            run_initialization=run_initialization,
            timeout_seconds=timeout_seconds,
            model_param=model_param,
        )
        received_at = _now_utc()
        canonical_raw = _canonical_bytes(payload)
        raw_path, raw_sha = _persist_raw(raw_root.resolve(), canonical_raw)
        samples = _hourly(
            payload,
            event_date=event_date,
            target_timezone=target_timezone,
            as_of_utc=as_of_utc,
        )
        receipt_identity = {
            "provider_id": provider_id,
            "source_class": SOURCE_CLASS,
            "station_id": station,
            "event_date": event_date,
            "run_initialization_utc": _utc_text(run_initialization),
            "source_url": source_url,
            "raw_object_ref": str(raw_path),
            "raw_sha256": raw_sha,
            "received_at_utc": received_at,
            "native_publication_at_utc": None,
            "native_arrival_at_utc": None,
        }
        receipt_sha = content_sha256(receipt_identity)
        points = [
            {
                "station_id": station,
                "event_date": event_date,
                "provider_id": provider_id,
                "model_id": normalized_model_id,
                "source_class": SOURCE_CLASS,
                "model_run_initialization_utc": _utc_text(
                    run_initialization
                ),
                "valid_time_utc": valid_time,
                "temperature_2m_c": temperature,
                "raw_message_sha256": raw_sha,
                "receipt_sha256": receipt_sha,
                "published_at_utc": None,
                "received_at_utc": received_at,
                "coordinate_lineage": station_coordinates(station),
            }
            for valid_time, temperature in samples
        ]
        curve_identity = {
            "schema": CURVE_SCHEMA,
            "station_id": station,
            "event_date": event_date,
            "target_timezone": target_timezone,
            "provider_id": provider_id,
            "model_id": normalized_model_id,
            "source_class": SOURCE_CLASS,
            "model_run_initialization_utc": _utc_text(run_initialization),
            "forecast_daily_max_c": max(
                point["temperature_2m_c"] for point in points
            ),
            "points": points,
            "state": "COMPLETE",
            "blockers": [],
        }
        curves.append({
            **curve_identity,
            "curve_sha256": content_sha256(curve_identity),
        })
        decoded_points.extend(points)
        input_artifacts.append({
            **receipt_identity,
            "receipt_sha256": receipt_sha,
        })
    identity = {
        "schema": SCHEMA,
        "as_of_utc": _utc_text(as_of),
        "target_local_date": event_date,
        "target_timezone": target_timezone,
        "market_registry_snapshot_sha256": registry["snapshot_sha256"],
        "station_coordinate_registry": coordinate_registry_manifest(),
        "input_artifacts": input_artifacts,
        "decoded_point_count": len(decoded_points),
        "decoded_points": decoded_points,
        "blocked_captures": [],
        "curves": {
            "curve_count": len(curves),
            "complete_curve_count": len(curves),
            "all_complete": bool(curves),
            "curves": curves,
        },
        "checks": {
            "explicit_single_run": True,
            "source_is_aggregator_republisher": True,
            "native_publication_not_claimed": True,
            "native_arrival_not_claimed": True,
            "hard_no_eligible": False,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
        "boundary": {
            "production_no_live_only": True,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    result = {
        **identity,
        "receipt_sha256": content_sha256(identity),
    }
    write_bounded_json(output_path.resolve(), result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", action="append", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--run-initialization-utc", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_PROFILES),
        default=MODEL_ID,
        help="explicit Open-Meteo single-run model profile",
    )
    args = parser.parse_args()
    result = capture(
        stations=args.station,
        event_date=args.event_date,
        run_initialization_utc=args.run_initialization_utc,
        as_of_utc=args.as_of_utc,
        registry_path=args.registry,
        raw_root=args.raw_root,
        output_path=args.output,
        timeout_seconds=args.timeout_seconds,
        model_id=args.model,
    )
    print(json.dumps({
        "schema": result["schema"],
        "output": str(args.output.resolve()),
        "station_count": result["curves"]["complete_curve_count"],
        "decoded_point_count": result["decoded_point_count"],
        "model_id": args.model,
        "provider_id": MODEL_PROFILES[args.model]["provider_id"],
        "source_class": SOURCE_CLASS,
        "native_publication_claimed": False,
        "native_arrival_claimed": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
