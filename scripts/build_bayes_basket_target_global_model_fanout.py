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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402


SCHEMA = "bayes_basket_target_global_model_fanout_v1"
ALLOWED_PROVIDERS = frozenset({
    "ecmwf_ifs025_official",
    "ecmwf_aifs025_official",
    "icon_global_official",
})
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
    supplemental_source_roots: list[Path] | None = None,
    target_date: str,
    target_timezone: str,
    run: str,
    source_station_id: str,
    source_station_ids: list[str] | None = None,
) -> list[tuple[Path, Mapping[str, Any]]]:
    records: list[tuple[Path, Mapping[str, Any]]] = []
    roots = [source_root, *(supplemental_source_roots or [])]
    normalized_roots = sorted({path.expanduser().resolve() for path in roots})
    allowed_source_stations = {
        str(station_id).strip().upper()
        for station_id in (source_station_ids or [source_station_id])
        if str(station_id).strip()
    }
    if not allowed_source_stations:
        raise ValueError("global_fanout_source_station_scope_missing")
    for root in normalized_roots:
        receipt_dir = root / provider / "receipts"
        for path in sorted(receipt_dir.glob("*.json")):
            try:
                value = load_bounded_json(path)
            except Exception:
                continue
            if not isinstance(value, Mapping):
                continue
            if (
                value.get("provider_id") == provider
                and str(value.get("station_id") or "").strip().upper()
                in allowed_source_stations
                and value.get("target_local_date") == target_date
                and value.get("run_initialization_utc") == run
                and isinstance(value.get("raw_receipts"), list)
                and len(value.get("raw_receipts")) == 1
            ):
                raw = value["raw_receipts"][0]
                if not isinstance(raw, Mapping):
                    continue
                raw_ref = Path(str(raw.get("raw_object_ref") or "")).expanduser()
                expected_sha = str(raw.get("raw_sha256") or "").strip().lower()
                try:
                    raw_ref = raw_ref.resolve(strict=True)
                    actual_bytes = int(raw_ref.stat().st_size)
                    response_bytes = int(raw.get("response_bytes") or -1)
                    advertised_raw = (raw.get("response_headers") or {}).get(
                        "content-length"
                    )
                    advertised_bytes = (
                        actual_bytes
                        if advertised_raw in (None, "")
                        else int(advertised_raw)
                    )
                except (OSError, TypeError, ValueError):
                    continue
                if (
                    not raw_ref.is_file()
                    or raw_ref.is_symlink()
                    or len(expected_sha) != 64
                    or _sha256(raw_ref) != expected_sha
                    or response_bytes != actual_bytes
                    or advertised_bytes != actual_bytes
                ):
                    continue
                records.append((path, value))
    by_valid: dict[str, tuple[Path, Mapping[str, Any]]] = {}
    for path, value in records:
        valid = str(value.get("valid_time_utc") or "")
        if valid:
            previous = by_valid.get(valid)
            if previous is not None:
                previous_raw = previous[1]["raw_receipts"][0]
                current_raw = value["raw_receipts"][0]
                if previous_raw.get("raw_sha256") != current_raw.get("raw_sha256"):
                    raise ValueError("global_fanout_duplicate_valid_raw_conflict")
                previous_global = bool(
                    (previous[1].get("global_raw_transport") or {}).get(
                        "global_raw_transport_anchor_only"
                    )
                )
                current_global = bool(
                    (value.get("global_raw_transport") or {}).get(
                        "global_raw_transport_anchor_only"
                    )
                )
                if current_global and not previous_global:
                    by_valid[valid] = (path, value)
                continue
            by_valid[valid] = (path, value)
    return [by_valid[key] for key in sorted(by_valid)]


def fanout(
    *,
    provider: str,
    target_local_date: str,
    run_initialization_utc: str,
    decision_at_utc: str,
    target_timezone: str,
    station_timezones: Mapping[str, str] | None = None,
    station_valid_times: Mapping[str, list[str]] | None = None,
    station_ids: list[str],
    source_station_id: str,
    source_station_ids: list[str] | None = None,
    source_root: Path = DEFAULT_ROOT,
    supplemental_source_roots: list[Path] | None = None,
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
    raw_station_timezones = station_timezones or {
        station_id: timezone_name for station_id in stations
    }
    normalized_timezones = {
        str(station_id or "").strip().upper(): str(value or "").strip()
        for station_id, value in raw_station_timezones.items()
    }
    if set(normalized_timezones) != set(stations) or not all(
        normalized_timezones.values()
    ):
        raise ValueError("global_fanout_station_timezone_scope_invalid")
    try:
        for value in normalized_timezones.values():
            ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("global_fanout_station_timezone_invalid") from exc
    raw_station_valid_times = station_valid_times or {}
    normalized_valid_times = {
        str(station_id or "").strip().upper(): {
            str(value).strip() for value in values if str(value).strip()
        }
        for station_id, values in raw_station_valid_times.items()
    }
    if normalized_valid_times and (
        set(normalized_valid_times) != set(stations)
        or any(not values for values in normalized_valid_times.values())
    ):
        raise ValueError("global_fanout_station_valid_time_scope_invalid")
    for station_id in stations:
        station_coordinates(station_id)
    anchor = str(source_station_id or "").strip().upper()
    if anchor not in stations:
        raise ValueError("global_fanout_source_station_outside_scope")
    source_ids = sorted({
        anchor,
        *(
            str(station_id).strip().upper()
            for station_id in (source_station_ids or [])
            if str(station_id).strip()
        ),
    })
    if any(station_id not in stations for station_id in source_ids):
        raise ValueError("global_fanout_source_station_outside_scope")
    sources = _source_records(
        provider=provider,
        source_root=source_root.expanduser().resolve(),
        supplemental_source_roots=supplemental_source_roots,
        target_date=target_local_date,
        target_timezone=timezone_name,
        run=run_initialization_utc,
        source_station_id=anchor,
        source_station_ids=source_ids,
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
            if (
                normalized_valid_times
                and valid not in normalized_valid_times[station_id]
            ):
                continue
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
                target_timezone=normalized_timezones[station_id],
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
        "station_timezones": dict(sorted(normalized_timezones.items())),
        "station_valid_times": {
            station_id: sorted(values)
            for station_id, values in sorted(normalized_valid_times.items())
        },
        "run_initialization_utc": run_initialization_utc,
        "decision_at_utc": decision_at_utc,
        "source_valid_time_count": len(sources),
        "station_count": len(stations),
        "expected_arrival_count": (
            sum(len(values) for values in normalized_valid_times.values())
            if normalized_valid_times
            else len(sources) * len(stations)
        ),
        "available_arrival_count": len(outputs),
        "captured_count": sum(item["status"] == "CAPTURED" for item in outputs),
        "blocked_count": len(blockers),
        "source_receipts": [
            {"path": str(path.resolve()), "sha256": _sha256(path), "valid_time_utc": str(value.get("valid_time_utc"))}
            for path, value in sources
        ],
        "source_station_ids": source_ids,
        "supplemental_source_roots": [
            str(path.expanduser().resolve())
            for path in (supplemental_source_roots or [])
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
    parser.add_argument(
        "--station-timezone",
        action="append",
        default=[],
        metavar="STATION=IANA_TIMEZONE",
    )
    parser.add_argument(
        "--station-valid-time",
        action="append",
        default=[],
        metavar="STATION=UTC_ISO8601",
    )
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument("--source-station-id", required=True)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    station_timezones: dict[str, str] = {}
    for item in args.station_timezone:
        station_id, separator, timezone_name = str(item).partition("=")
        if not separator or not station_id.strip() or not timezone_name.strip():
            parser.error("--station-timezone must be STATION=IANA_TIMEZONE")
        station_timezones[station_id.strip().upper()] = timezone_name.strip()
    station_valid_times: dict[str, list[str]] = {}
    for item in args.station_valid_time:
        station_id, separator, valid_time = str(item).partition("=")
        if not separator or not station_id.strip() or not valid_time.strip():
            parser.error("--station-valid-time must be STATION=UTC_ISO8601")
        station_valid_times.setdefault(station_id.strip().upper(), []).append(
            valid_time.strip()
        )
    result = fanout(
        provider=args.provider,
        target_local_date=args.target_local_date,
        run_initialization_utc=args.run_initialization_utc,
        decision_at_utc=args.decision_at_utc,
        target_timezone=args.target_timezone,
        station_timezones=station_timezones or None,
        station_valid_times=station_valid_times or None,
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
