#!/usr/bin/env python3
"""Rebuild settlement-time ICON replay curves from retained Open-Meteo raw.

This offline adapter re-decodes content-addressed aggregator payloads only.
It performs no network request, does not create native arrival timestamps,
does not write a database, and cannot authorize production or orders.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_PROCESS_RSS_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)
from scripts.capture_bayes_basket_target_open_meteo_icon import (  # noqa: E402
    MAX_RESPONSE_BYTES,
    MODEL_ID,
    PROVIDER_ID,
    RECEIPT_SCHEMA,
    SCHEMA,
    SOURCE_CLASS,
    _extract_station,
    _now,
    _registry_timezone,
)


DEFAULT_SOURCE = (
    ROOT
    / "output/audits/bayes_basket_target_18z_fallback_20260726"
    / "icon_18z_d2_fallback_complete.json"
)
DEFAULT_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_OUTPUT = (
    ROOT
    / "output/audits"
    / "bayes_basket_target_open_meteo_icon_retained_rebuild.json"
)
ALLOWED_RAW_ROOT = (
    ROOT
    / "data/research/bayes_basket_target/model_arrivals"
    / PROVIDER_ID
    / "raw"
).resolve()


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def rebuild(
    *,
    source_evidence: Path,
    timezone_registry: Path,
    event_dates: Sequence[str],
    station_ids: Sequence[str],
    output: Path,
    as_of_utc: str,
    run_cycle: str,
    run_day_offset: int,
) -> dict[str, Any]:
    assert_process_memory_budget(limit=MAX_TARGET_PROCESS_RSS_BYTES)
    dates = sorted({date.fromisoformat(str(item).strip()) for item in event_dates})
    stations = sorted({str(item).strip().upper() for item in station_ids if str(item).strip()})
    if not dates or len(dates) > 14:
        raise ContractError("open_meteo_retained_event_date_count_invalid")
    if not stations or len(stations) > 45:
        raise ContractError("open_meteo_retained_station_count_invalid")
    target_timezone = _registry_timezone(
        timezone_registry,
        stations=stations,
        event_dates=dates,
    )
    source_path = source_evidence.expanduser().resolve()
    source = load_bounded_json(source_path)
    if not isinstance(source, Mapping) or source.get("schema") != SCHEMA:
        raise ContractError("open_meteo_retained_source_schema_invalid")
    source_rows = source.get("model_rows")
    if not isinstance(source_rows, list):
        raise ContractError("open_meteo_retained_source_rows_missing")

    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw in source_rows:
        if not isinstance(raw, Mapping):
            continue
        key = (
            str(raw.get("station_id") or "").strip().upper(),
            str(raw.get("event_date") or "").strip(),
        )
        if key[0] in stations and key[1] in {item.isoformat() for item in dates}:
            by_key.setdefault(key, raw)

    model_rows: list[dict[str, Any]] = []
    curves: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    for station in stations:
        for event_date in dates:
            key = (station, event_date.isoformat())
            source_row = by_key.get(key)
            if source_row is None:
                blockers.append({
                    "station_id": station,
                    "event_date": event_date.isoformat(),
                    "reason": "retained_source_row_missing",
                })
                continue
            raw_path = Path(str(source_row.get("raw_object_ref") or "")).expanduser().resolve()
            if not raw_path.is_file() or not raw_path.is_relative_to(ALLOWED_RAW_ROOT):
                blockers.append({
                    "station_id": station,
                    "event_date": event_date.isoformat(),
                    "reason": "retained_raw_reference_invalid",
                })
                continue
            if raw_path.stat().st_size > MAX_RESPONSE_BYTES:
                raise ContractError("open_meteo_retained_raw_too_large")
            body = raw_path.read_bytes()
            expected_sha = str(source_row.get("raw_message_sha256") or "").strip().lower()
            if _sha256_bytes(body) != expected_sha:
                blockers.append({
                    "station_id": station,
                    "event_date": event_date.isoformat(),
                    "reason": "retained_raw_sha256_mismatch",
                })
                continue
            rows, station_curves, receipt = _extract_station(
                station=station,
                event_dates=[event_date],
                url=str(source_row.get("source_url") or ""),
                body=body,
                http_status="retained",
                raw_path=raw_path,
                reconstructed_at=as_of_utc,
                run_cycle=run_cycle,
                run_day_offset=run_day_offset,
                target_timezone=target_timezone,
            )
            model_rows.extend(rows)
            curves.extend(station_curves)
            blockers.extend(receipt.pop("blockers", []))
            receipts.append(receipt)

    payload = {
        "schema": SCHEMA,
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "target_timezone": target_timezone,
        "event_dates": [item.isoformat() for item in dates],
        "station_ids": stations,
        "station_count": len(stations),
        "source": {
            "source_class": SOURCE_CLASS,
            "aggregator_or_republisher": True,
            "open_meteo": True,
            "retained_source_evidence": str(source_path),
            "retained_source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "network_requests": 0,
        },
        "reconstructed_at_utc": as_of_utc,
        "model_rows": model_rows,
        "curves": {
            "complete_curve_count": len(curves),
            "eligible_model_rows": curves,
            "complete_station_day_keys": sorted(
                f"{item['station_id']}|{item['event_date']}" for item in curves
            ),
        },
        "receipts": receipts,
        "blockers": blockers,
        "checks": {
            "all_curves_complete": len(curves) == len(stations) * len(dates) and not blockers,
            "historical_replay_eligible": bool(curves),
            "prospective_strict_asof_eligible": False,
            "native_arrival_index_eligible": False,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "protected_production_line_touched": False,
        },
        "boundary": {
            "offline_research_only": True,
            "aggregator_cannot_be_native_arrival": True,
            "production_calibration_promotion_allowed": False,
            "live_submit_authorized": False,
        },
    }
    payload["receipt_sha256"] = content_sha256(payload)
    write_bounded_json(output.expanduser().resolve(), payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-evidence", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--timezone-registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--event-date", action="append", required=True)
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument("--as-of-utc", default="")
    parser.add_argument("--run-cycle", default="18Z", choices=["00Z", "06Z", "12Z", "18Z"])
    parser.add_argument("--run-day-offset", type=int, default=2, choices=[1, 2])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = rebuild(
        source_evidence=args.source_evidence,
        timezone_registry=args.timezone_registry,
        event_dates=args.event_date,
        station_ids=args.station_id,
        output=args.output,
        as_of_utc=args.as_of_utc or _now(),
        run_cycle=args.run_cycle,
        run_day_offset=args.run_day_offset,
    )
    print(json.dumps({
        "schema": payload["schema"],
        "target_timezone": payload["target_timezone"],
        "complete_curve_count": payload["curves"]["complete_curve_count"],
        "blocker_count": len(payload["blockers"]),
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if payload["checks"]["all_curves_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
