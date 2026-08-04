#!/usr/bin/env python3
"""Capture bounded GEM/CMA 45-station curves from Open-Meteo.

These providers are explicit ``aggregator_republisher`` evidence.  They may
feed offline calibration candidates after separate OOS governance, but they
never claim native publication/arrival, enter the native arrival index, hard
NO, or authorize live execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
    station_coordinates,
)


SCHEMA = "open_meteo_aux_nwp_capture_v1"
CONFIG_SCHEMA = "open_meteo_aux_nwp_capture_config_v1"
MAX_RESPONSE_BYTES_HARD = 8 * 1024 * 1024
MAX_CAPTURE_BYTES_HARD = 32 * 1024 * 1024
MIN_FREE_BYTES_HARD = 512 * 1024 * 1024
MAX_FORECAST_DAYS = 10
HOURLY_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "cloud_cover",
    "wind_speed_10m",
)
PROVIDERS: dict[str, dict[str, Any]] = {
    "gem_global_open_meteo": {
        "endpoint": "https://api.open-meteo.com/v1/gem",
        "model": "gem_global",
        "model_role": "GEM",
        "native_resolution_km": 15,
        "native_temporal_resolution_hours": 3,
        "native_update_frequency_hours": 12,
        "maximum_forecast_days": 10,
        "documentation": "https://open-meteo.com/en/docs/gem-api",
    },
    "cma_grapes_global_open_meteo": {
        "endpoint": "https://api.open-meteo.com/v1/cma",
        "model": "cma_grapes_global",
        "model_role": "CMA",
        "native_resolution_km": 15,
        "native_temporal_resolution_hours": 3,
        "native_update_frequency_hours": 6,
        "maximum_forecast_days": 10,
        "documentation": "https://open-meteo.com/en/docs/cma-api",
    },
}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_json_response(
    session: requests.Session,
    *,
    endpoint: str,
    params: Mapping[str, Any],
    timeout_seconds: float,
    max_response_bytes: int,
) -> tuple[Any, str, int, str]:
    with session.get(
        endpoint,
        params=params,
        timeout=timeout_seconds,
        stream=True,
    ) as response:
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) > max_response_bytes:
            raise RuntimeError("open_meteo_aux_content_length_exceeded")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_response_bytes:
                raise RuntimeError("open_meteo_aux_response_bytes_exceeded")
            chunks.append(chunk)
            assert_process_memory_budget(projected_bytes=total * 3)
        raw = b"".join(chunks)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("open_meteo_aux_response_json_invalid") from exc
        return payload, response.url, total, _sha256(raw)


def _batched(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _normalize_batch(
    payload: Any,
    *,
    station_ids: Sequence[str],
    temporal_resolution_hours: int,
) -> list[dict[str, Any]]:
    items = payload if isinstance(payload, list) else [payload]
    if len(items) != len(station_ids):
        raise RuntimeError("open_meteo_aux_station_response_count_mismatch")
    rows: list[dict[str, Any]] = []
    for station_id, item in zip(station_ids, items):
        if not isinstance(item, Mapping):
            raise RuntimeError("open_meteo_aux_station_payload_invalid")
        hourly = item.get("hourly")
        units = item.get("hourly_units")
        if not isinstance(hourly, Mapping) or not isinstance(units, Mapping):
            raise RuntimeError("open_meteo_aux_hourly_payload_missing")
        times = hourly.get("time")
        if (
            not isinstance(times, Sequence)
            or isinstance(times, (str, bytes, bytearray))
        ):
            raise RuntimeError("open_meteo_aux_hourly_time_invalid")
        for field in HOURLY_FIELDS:
            values = hourly.get(field)
            if (
                not isinstance(values, Sequence)
                or isinstance(values, (str, bytes, bytearray))
                or len(values) != len(times)
            ):
                raise RuntimeError(
                    f"open_meteo_aux_hourly_field_invalid:{field}"
                )
        points: list[dict[str, Any]] = []
        native_point_count = 0
        incomplete_point_count = 0
        for index, timestamp in enumerate(times):
            parsed = datetime.fromisoformat(str(timestamp)).replace(
                tzinfo=timezone.utc
            )
            if parsed.hour % temporal_resolution_hours:
                continue
            native_point_count += 1
            values = {
                field: hourly[field][index] for field in HOURLY_FIELDS
            }
            if any(
                value is None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in values.values()
            ):
                incomplete_point_count += 1
                continue
            point = {
                "valid_time_utc": parsed.isoformat().replace("+00:00", "Z"),
                **values,
            }
            points.append(point)
        if not points:
            raise RuntimeError("open_meteo_aux_native_curve_empty")
        rows.append({
            "station_id": station_id,
            "requested_coordinates": {
                key: station_coordinates(station_id)[key]
                for key in ("latitude", "longitude")
            },
            "grid_coordinates": {
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
                "elevation_m": item.get("elevation"),
            },
            "hourly_units": {
                field: units.get(field) for field in HOURLY_FIELDS
            },
            "availability": {
                "native_point_count_requested": native_point_count,
                "complete_point_count": len(points),
                "incomplete_point_count_omitted": incomplete_point_count,
                "first_valid_time_utc": points[0]["valid_time_utc"],
                "last_valid_time_utc": points[-1]["valid_time_utc"],
            },
            "points": points,
        })
    return rows


def _atomic_json(path: Path, value: Any, *, min_free_bytes: int) -> None:
    raw = _canonical_bytes(value) + b"\n"
    if shutil.disk_usage(path.parent).free - len(raw) < min_free_bytes:
        raise RuntimeError("open_meteo_aux_min_free_disk_guard")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _retain_latest(output_dir: Path, provider_id: str, count: int) -> None:
    captures = sorted(
        output_dir.glob(f"{provider_id}_*.json"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for obsolete in captures[count:]:
        obsolete.unlink(missing_ok=True)


def capture_provider(
    *,
    provider_id: str,
    output_dir: Path,
    station_ids: Sequence[str],
    forecast_days: int,
    batch_size: int,
    timeout_seconds: float,
    max_response_bytes: int,
    max_capture_bytes: int,
    min_free_bytes: int,
    retention_count: int,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    config = PROVIDERS.get(provider_id)
    if config is None:
        raise ValueError("open_meteo_aux_provider_invalid")
    if not 1 <= forecast_days <= min(
        MAX_FORECAST_DAYS,
        int(config["maximum_forecast_days"]),
    ):
        raise ValueError("open_meteo_aux_forecast_days_out_of_bounds")
    if not 1 <= batch_size <= 15:
        raise ValueError("open_meteo_aux_batch_size_out_of_bounds")
    manifest = coordinate_registry_manifest()
    canonical = set(manifest["station_ids"])
    requested = tuple(sorted({str(value).strip().upper() for value in station_ids}))
    if not requested or set(requested) - canonical:
        raise ValueError("open_meteo_aux_station_scope_invalid")
    if max_response_bytes <= 0 or max_response_bytes > MAX_RESPONSE_BYTES_HARD:
        raise ValueError("open_meteo_aux_response_budget_invalid")
    if max_capture_bytes < max_response_bytes or max_capture_bytes > MAX_CAPTURE_BYTES_HARD:
        raise ValueError("open_meteo_aux_capture_budget_invalid")
    if min_free_bytes < MIN_FREE_BYTES_HARD:
        raise ValueError("open_meteo_aux_free_disk_floor_invalid")
    if retention_count < 1 or retention_count > 32:
        raise ValueError("open_meteo_aux_retention_invalid")
    output_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output_dir).free < min_free_bytes:
        raise RuntimeError("open_meteo_aux_min_free_disk_guard")
    assert_process_memory_budget()
    client = session or requests.Session()
    client.headers.update({"User-Agent": "BayesBasketTarget-AuxNWP/1.0"})
    rows: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    total_bytes = 0
    for batch in _batched(requested, batch_size):
        coordinates = [station_coordinates(station) for station in batch]
        payload, url, response_bytes, response_sha256 = _bounded_json_response(
            client,
            endpoint=str(config["endpoint"]),
            params={
                "latitude": ",".join(str(row["latitude"]) for row in coordinates),
                "longitude": ",".join(str(row["longitude"]) for row in coordinates),
                "hourly": ",".join(HOURLY_FIELDS),
                "models": config["model"],
                "forecast_days": forecast_days,
                "timezone": "GMT",
                "wind_speed_unit": "ms",
            },
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        total_bytes += response_bytes
        if total_bytes > max_capture_bytes:
            raise RuntimeError("open_meteo_aux_capture_bytes_exceeded")
        rows.extend(
            _normalize_batch(
                payload,
                station_ids=batch,
                temporal_resolution_hours=int(
                    config["native_temporal_resolution_hours"]
                ),
            )
        )
        receipts.append({
            "station_ids": list(batch),
            "request_url": url,
            "response_bytes": response_bytes,
            "response_sha256": response_sha256,
        })
    captured_at = _utc_now()
    identity = {
        "schema": SCHEMA,
        "provider_id": provider_id,
        "model_role": config["model_role"],
        "model": config["model"],
        "source_class": "aggregator_republisher",
        "transport": "Open-Meteo",
        "documentation": config["documentation"],
        "captured_at_utc": captured_at,
        "run_initialization_utc": None,
        "native_publication_at_utc": None,
        "arrival_at_utc": None,
        "run_binding": "CURRENT_BEST_AVAILABLE_NO_NATIVE_RUN_ID",
        "native_resolution_km": config["native_resolution_km"],
        "native_temporal_resolution_hours": (
            config["native_temporal_resolution_hours"]
        ),
        "native_update_frequency_hours": config["native_update_frequency_hours"],
        "forecast_days": forecast_days,
        "station_registry_sha256": manifest["registry_sha256"],
        "station_count": len(rows),
        "stations": rows,
        "receipts": receipts,
        "resource_usage": {
            "downloaded_bytes": total_bytes,
            "max_response_bytes": max_response_bytes,
            "max_capture_bytes": max_capture_bytes,
            "min_free_bytes": min_free_bytes,
        },
        "authority": {
            "source_class": "aggregator_republisher",
            "calibration_candidate_eligible": True,
            "native_arrival_index_eligible": False,
            "native_exact_join_eligible": False,
            "hard_no_eligible": False,
            "formal_calibration_eligible": False,
            "production_activation_eligible": False,
            # Forward/observational packets are never OOS.  Only a separate
            # closed-day AWC historical contract can activate this
            # republisher as a no-live P0/P0-M fallback.
            "requires_separate_historical_oos_artifact": True,
            "forward_evidence_role": "observational_candidate_not_oos",
        },
        "boundary": {
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    artifact = {
        **identity,
        "content_sha256": _sha256(_canonical_bytes(identity)),
    }
    timestamp = captured_at.replace("-", "").replace(":", "")
    target = output_dir / f"{provider_id}_{timestamp}.json"
    _atomic_json(target, artifact, min_free_bytes=min_free_bytes)
    _retain_latest(output_dir, provider_id, retention_count)
    return {
        "schema": "open_meteo_aux_nwp_capture_receipt_v1",
        "state": "CAPTURED",
        "provider_id": provider_id,
        "artifact_path": str(target),
        "artifact_sha256": artifact["content_sha256"],
        "station_count": len(rows),
        "downloaded_bytes": total_bytes,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        action="append",
        choices=sorted(PROVIDERS),
        dest="providers",
    )
    parser.add_argument("--station", action="append", dest="stations")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "data" / "bayes_basket_target" / "aux_nwp",
    )
    parser.add_argument("--forecast-days", type=int, default=7)
    parser.add_argument("--batch-size", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=MAX_RESPONSE_BYTES_HARD,
    )
    parser.add_argument(
        "--max-capture-bytes",
        type=int,
        default=MAX_CAPTURE_BYTES_HARD,
    )
    parser.add_argument(
        "--min-free-bytes",
        type=int,
        default=MIN_FREE_BYTES_HARD,
    )
    parser.add_argument("--retention-count", type=int, default=4)
    args = parser.parse_args()
    providers = args.providers or sorted(PROVIDERS)
    stations = (
        args.stations
        or coordinate_registry_manifest()["station_ids"]
    )
    receipts = [
        capture_provider(
            provider_id=provider,
            output_dir=args.output_dir.expanduser().resolve(),
            station_ids=stations,
            forecast_days=args.forecast_days,
            batch_size=args.batch_size,
            timeout_seconds=args.timeout_seconds,
            max_response_bytes=args.max_response_bytes,
            max_capture_bytes=args.max_capture_bytes,
            min_free_bytes=args.min_free_bytes,
            retention_count=args.retention_count,
        )
        for provider in providers
    ]
    print(json.dumps({
        "schema": "open_meteo_aux_nwp_capture_batch_v1",
        "state": "CAPTURED",
        "receipts": receipts,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
