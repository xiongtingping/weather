#!/usr/bin/env python3
"""Bind one official global model run to an explicit station scope.

Only ECMWF and ICON native global payloads may use this adapter.  The raw
transport receipt and content-addressed body are reused by reference; the
adapter creates one station-bound arrival record per canonical coordinate so
the downstream decoder can apply station-specific lineage.  GFS is
station-filtered and is deliberately rejected here.

This command writes no SQLite, does not copy raw bodies, and cannot authorize
calibration, orders, or a production service.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402


SCHEMA = "bayes_basket_target_global_model_fanout_v1"
ALLOWED_PROVIDERS = frozenset({"ecmwf_ifs025_official", "icon_global_official"})
DEFAULT_ROOT = ROOT / "data" / "research" / "bayes_basket_target" / "model_arrivals"


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_records(
    *,
    provider: str,
    source_root: Path,
    target_date: str,
    target_timezone: str,
    run: str,
    source_station_id: str,
) -> list[tuple[Path, Mapping[str, Any]]]:
    records: list[tuple[Path, Mapping[str, Any]]] = []
    receipt_dir = source_root / provider / "receipts"
    for path in sorted(receipt_dir.glob("*.json")):
        try:
            value = load_bounded_json(path)
        except Exception:
            continue
        if not isinstance(value, Mapping):
            continue
        if (
            value.get("provider_id") == provider
            and value.get("station_id") == source_station_id
            and value.get("target_local_date") == target_date
            and value.get("run_initialization_utc") == run
            and isinstance(value.get("raw_receipts"), list)
            and len(value.get("raw_receipts")) == 1
        ):
            records.append((path, value))
    by_valid: dict[str, tuple[Path, Mapping[str, Any]]] = {}
    for path, value in records:
        valid = str(value.get("valid_time_utc") or "")
        if valid:
            by_valid.setdefault(valid, (path, value))
    return [by_valid[key] for key in sorted(by_valid)]


def fanout(
    *,
    provider: str,
    target_local_date: str,
    run_initialization_utc: str,
    decision_at_utc: str,
    target_timezone: str,
    station_ids: list[str],
    source_station_id: str,
    source_root: Path = DEFAULT_ROOT,
    output_dir: Path,
) -> dict[str, Any]:
    provider = str(provider).strip().lower()
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError("global_fanout_provider_not_allowed")
    _parse_utc(run_initialization_utc)
    _parse_utc(decision_at_utc)
    timezone_name = str(target_timezone or "").strip()
    if not timezone_name:
        raise ValueError("global_fanout_target_timezone_missing")
    stations = sorted({str(value or "").strip().upper() for value in station_ids if str(value or "").strip()})
    if not stations:
        raise ValueError("global_fanout_station_scope_missing")
    for station_id in stations:
        station_coordinates(station_id)
    anchor = str(source_station_id or "").strip().upper()
    if anchor not in stations:
        raise ValueError("global_fanout_source_station_outside_scope")
    sources = _source_records(
        provider=provider,
        source_root=source_root.expanduser().resolve(),
        target_date=target_local_date,
        target_timezone=timezone_name,
        run=run_initialization_utc,
        source_station_id=anchor,
    )
    if not sources:
        raise ValueError("global_fanout_source_receipts_missing")
    destination = output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    record_module = importlib.import_module("scripts.record_bayes_basket_target_model_arrival")
    outputs: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for receipt_path, receipt in sources:
        raw = receipt["raw_receipts"][0]
        published = str(raw.get("provider_published_at_utc") or "")
        received = str(raw.get("response_completed_at") or "")
        source_url = str(raw.get("raw_request_url") or "")
        valid = str(receipt["valid_time_utc"])
        for station_id in stations:
            output_path = destination / f"{station_id}_{valid.replace(':', '').replace('-', '')}.json"
            result = record_module.record(
                receipt_path=receipt_path,
                provider_id=provider,
                station_id=station_id,
                target_local_date=target_local_date,
                run_initialization_utc=run_initialization_utc,
                valid_time_utc=valid,
                published_at_utc=published,
                received_at_utc=received,
                decision_at_utc=decision_at_utc,
                source_url=source_url,
                target_timezone=timezone_name,
            )
            write_bounded_json(output_path, result)
            item = {"path": str(output_path), "status": result.get("status")}
            if result.get("status") != "CAPTURED":
                item["reason_codes"] = result.get("receipt", {}).get("reason_codes", [])
                blockers.append(item)
            outputs.append(item)
    manifest = {
        "schema": SCHEMA,
        "provider_id": provider,
        "target_local_date": target_local_date,
        "target_timezone": timezone_name,
        "run_initialization_utc": run_initialization_utc,
        "decision_at_utc": decision_at_utc,
        "source_valid_time_count": len(sources),
        "station_count": len(stations),
        "expected_arrival_count": len(sources) * len(stations),
        "captured_count": sum(item["status"] == "CAPTURED" for item in outputs),
        "blocked_count": len(blockers),
        "source_receipts": [
            {"path": str(path.resolve()), "sha256": _sha256(path), "valid_time_utc": str(value.get("valid_time_utc"))}
            for path, value in sources
        ],
        "outputs": outputs,
        "blockers": blockers,
        "checks": {
            "global_provider_only": True,
            "explicit_station_scope": True,
            "duplicate_station_valid_keys": len(outputs) == len({str(item["path"]) for item in outputs}),
            "p0_station_lineage_may_precede_event_day": True,
            "p1_authority_created": False,
            "writes_target_database": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "promotion_allowed": False,
            "protected_production_line_touched": False,
        },
        "boundary": {
            "target_only": True,
            "raw_objects_copied": False,
            "p0_station_lineage_only": True,
            "p1_probability_input_authorized": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "live_order_authorized": False,
        },
    }
    write_bounded_json(destination / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=sorted(ALLOWED_PROVIDERS))
    parser.add_argument("--target-local-date", required=True)
    parser.add_argument("--run-initialization-utc", required=True)
    parser.add_argument("--decision-at-utc", required=True)
    parser.add_argument("--target-timezone", required=True)
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument("--source-station-id", required=True)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = fanout(
        provider=args.provider,
        target_local_date=args.target_local_date,
        run_initialization_utc=args.run_initialization_utc,
        decision_at_utc=args.decision_at_utc,
        target_timezone=args.target_timezone,
        station_ids=args.station_id,
        source_station_id=args.source_station_id,
        source_root=args.source_root,
        output_dir=args.output_dir,
    )
    print(json.dumps({
        "schema": result["schema"],
        "provider_id": result["provider_id"],
        "expected_arrival_count": result["expected_arrival_count"],
        "captured_count": result["captured_count"],
        "blocked_count": result["blocked_count"],
        "promotion_allowed": result["checks"]["promotion_allowed"],
        "writes_production_db": result["checks"]["writes_production_db"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not result["blockers"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
