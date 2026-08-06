#!/usr/bin/env python3
"""Fetch only missing PR10 Open-Meteo Previous-Runs raw objects.

This source ingress deliberately does not calculate OOS metrics, cohorts,
selection, or P0.  One logical request is station x model x lead x date range;
an existing receipt is verified before any network request is attempted.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta, datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import assert_process_memory_budget  # noqa: E402
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
    station_coordinates,
)


ENDPOINT = "https://previous-runs-api.open-meteo.com/v1/forecast"
MODELS = {
    "ECMWF": ("ecmwf_ifs", "ecmwf_ifs_open_meteo_previous_runs"),
    "GFS": ("gfs_global", "gfs_global_open_meteo_previous_runs"),
    "ICON": ("icon_global", "icon_global_open_meteo"),
    "GEM": ("gem_global", "gem_global_open_meteo"),
    "CMA": ("cma_grapes_global", "cma_grapes_global_open_meteo"),
}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    data = _canonical(value) + b"\n"
    if len(data) > MAX_RESPONSE_BYTES:
        raise ContractError("pr10_raw_receipt_size_exceeded")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _verified_existing(receipt_path: Path, raw_root: Path, request_key: str) -> dict[str, Any] | None:
    if not receipt_path.is_file():
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("pr10_raw_existing_receipt_invalid") from exc
    if receipt.get("request_key_sha256") != request_key:
        raise ContractError("pr10_raw_existing_request_key_mismatch")
    raw_sha = str(receipt.get("raw_object_sha256") or "")
    raw_path = raw_root / f"{raw_sha}.json"
    if (
        len(raw_sha) != 64
        or not raw_path.is_file()
        or hashlib.sha256(raw_path.read_bytes()).hexdigest() != raw_sha
    ):
        raise ContractError("pr10_raw_existing_object_hash_mismatch")
    return receipt


def _fetch_one(
    *,
    station_id: str,
    model_id: str,
    lead: int,
    event_start: str,
    event_end: str,
    proxy_url: str,
    raw_root: Path,
    receipt_root: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    coordinates = station_coordinates(station_id)
    api_model, provider_id = MODELS[model_id]
    query_end = (date.fromisoformat(event_end) + timedelta(days=1)).isoformat()
    params = {
        "latitude": coordinates["latitude"],
        "longitude": coordinates["longitude"],
        "start_date": event_start,
        "end_date": query_end,
        "hourly": f"temperature_2m_previous_day{lead}",
        "models": api_model,
        "timezone": "GMT",
    }
    request_identity = {
        "endpoint": ENDPOINT,
        "params": params,
        "station_id": station_id,
        "model_id": model_id,
        "provider_id": provider_id,
        "lead_bucket": f"d{lead}",
        "lead_bucket_scope": f"PREVIOUS_DAY{lead}",
        "event_start_date": event_start,
        "event_end_date": event_end,
        "settlement_timezone": "America/New_York",
        "source_class": "aggregator_republisher",
    }
    request_key = content_sha256(request_identity)
    receipt_path = receipt_root / f"{request_key}.json"
    existing = _verified_existing(receipt_path, raw_root, request_key)
    if existing is not None:
        return {**existing, "fetch_state": "VERIFIED_REUSED"}
    assert_process_memory_budget(projected_bytes=MAX_RESPONSE_BYTES)
    response = requests.get(
        ENDPOINT,
        params=params,
        proxies={"http": proxy_url, "https": proxy_url},
        timeout=(10.0, timeout_seconds),
        stream=True,
        headers={"User-Agent": "weather-pr10-canonical-raw-ingress/1"},
    )
    response.raise_for_status()
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ContractError("pr10_raw_response_size_exceeded")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ContractError("pr10_raw_response_json_invalid") from exc
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    variable = params["hourly"]
    if (
        not isinstance(hourly, dict)
        or not isinstance(hourly.get("time"), list)
        or not isinstance(hourly.get(variable), list)
        or len(hourly["time"]) != len(hourly[variable])
    ):
        raise ContractError("pr10_raw_hourly_vector_invalid")
    raw_sha = hashlib.sha256(raw).hexdigest()
    raw_path = raw_root / f"{raw_sha}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if raw_path.exists():
        if hashlib.sha256(raw_path.read_bytes()).hexdigest() != raw_sha:
            raise ContractError("pr10_raw_content_address_collision")
    else:
        temporary = raw_path.with_suffix(".json.tmp")
        temporary.write_bytes(raw)
        temporary.replace(raw_path)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    receipt_identity = {
        "schema": "bayes_basket_target_pr10_open_meteo_raw_receipt_v1",
        **request_identity,
        "request_key_sha256": request_key,
        "source_url": response.url,
        "fetched_at_utc": fetched_at,
        "raw_object_sha256": raw_sha,
        "raw_object_path": str(raw_path.resolve()),
        "response_bytes": len(raw),
        "hourly_row_count": len(hourly["time"]),
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "state": "MATERIALIZED_UNPUBLISHED",
    }
    receipt = {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }
    _write_json(receipt_path, receipt)
    assert_process_memory_budget()
    return {**receipt, "fetch_state": "FETCHED"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-start", required=True)
    parser.add_argument("--event-end", required=True)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:1082")
    parser.add_argument("--workers", type=int, default=2, choices=(1, 2, 3, 4))
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    start = date.fromisoformat(args.event_start)
    end = date.fromisoformat(args.event_end)
    day_count = (end - start).days + 1
    if not 1 <= day_count <= 32 or end >= date.today():
        raise ContractError("pr10_raw_closed_historical_range_invalid")
    stations = coordinate_registry_manifest()["station_ids"]
    jobs = [
        (station, model, lead)
        for station in stations
        for lead in (1, 2)
        for model in sorted(MODELS)
    ]
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _fetch_one,
                station_id=station,
                model_id=model,
                lead=lead,
                event_start=args.event_start,
                event_end=args.event_end,
                proxy_url=args.proxy_url,
                raw_root=args.raw_root.resolve(),
                receipt_root=args.receipt_root.resolve(),
                timeout_seconds=args.timeout_seconds,
            ): (station, model, lead)
            for station, model, lead in jobs
        }
        for future in as_completed(futures):
            station, model, lead = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                failures.append({
                    "station_id": station,
                    "model_id": model,
                    "lead_bucket": f"d{lead}",
                    "error": f"{type(exc).__name__}:{exc}",
                })
    results.sort(key=lambda item: (
        item["station_id"], item["lead_bucket"], item["model_id"]
    ))
    failures.sort(key=lambda item: (
        item["station_id"], item["lead_bucket"], item["model_id"]
    ))
    identity = {
        "schema": "bayes_basket_target_pr10_open_meteo_raw_bundle_v1",
        "event_start_date": args.event_start,
        "event_end_date": args.event_end,
        "settlement_timezone": "America/New_York",
        "source_class": "aggregator_republisher",
        "expected_request_count": len(jobs),
        "materialized_request_count": len(results),
        "failure_count": len(failures),
        "fetched_count": sum(item["fetch_state"] == "FETCHED" for item in results),
        "reused_count": sum(item["fetch_state"] == "VERIFIED_REUSED" for item in results),
        "receipts": results,
        "failures": failures,
        "state": "MATERIALIZED_UNPUBLISHED" if not failures else "BLOCKED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "consumable": False,
    }
    bundle = {**identity, "content_sha256": content_sha256(identity)}
    _write_json(args.output.resolve(), bundle)
    print(json.dumps({
        key: bundle[key] for key in (
            "state", "expected_request_count", "materialized_request_count",
            "failure_count", "fetched_count", "reused_count", "content_sha256",
        )
    }, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
