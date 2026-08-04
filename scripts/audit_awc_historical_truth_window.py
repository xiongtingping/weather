#!/usr/bin/env python3
"""Inventory retained official-AWC truth for a bounded historical window.

This is a research-only audit.  It reads the station registry and the
content-addressed research raw archive, but never opens or writes a SQLite
database, never mutates the raw archive, and never submits an order.  It is
intentionally separate from ``rebuild_bayes_basket_target_historical_truth``:
the latter accepts one settlement timezone per invocation, while this audit
must report the complete 45-station scope without accidentally treating mixed
timezones as one timezone.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_awc_historical_truth_window_audit_v1"
DEFAULT_LANE_MAP = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
DEFAULT_TIMEZONE_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_RAW_ROOT = ROOT / "data/research/bayes_basket_target/historical_truth_raw"
DEFAULT_OUTPUT = ROOT / "output/audits/awc_historical_truth_window_audit_20260803.json"


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value).strip())


def _parse_utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _station_lanes(path: Path) -> dict[str, str]:
    payload = load_bounded_json(path.expanduser().resolve(), limit=MAX_JSON_INPUT_BYTES)
    rows = payload.get("stations") if isinstance(payload, Mapping) else None
    if not isinstance(rows, Mapping) or not rows:
        raise ValueError("station_lane_map_invalid")
    result = {str(k).strip().upper(): str(v).strip().lower() for k, v in rows.items()}
    if any(value != "awc" for value in result.values()):
        raise ValueError("station_lane_map_not_global_awc")
    return result


def _station_timezones(path: Path, station_ids: set[str]) -> dict[str, str]:
    payload = load_bounded_json(path.expanduser().resolve(), limit=MAX_JSON_INPUT_BYTES)
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("market_registry_rows_missing")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        tz_name = str(row.get("settlement_timezone") or "").strip()
        if station not in station_ids or not tz_name:
            continue
        try:
            ZoneInfo(tz_name)
        except Exception as exc:
            raise ValueError(f"station_timezone_invalid:{station}") from exc
        previous = result.setdefault(station, tz_name)
        if previous != tz_name:
            raise ValueError(f"station_timezone_conflict:{station}")
    missing = station_ids - set(result)
    if missing:
        raise ValueError(f"station_timezone_missing:{','.join(sorted(missing))}")
    return result


def _raw_object_is_valid(path: Path, payload: Mapping[str, Any]) -> tuple[bool, str]:
    """Validate only identity fields; do not reinterpret a raw payload."""

    object_sha = path.name.removesuffix(".json.gz").lower()
    declared_sha = str(payload.get("raw_sha256") or "").strip().lower()
    if len(object_sha) != 64 or object_sha != declared_sha:
        return False, "object_path_raw_sha_mismatch"
    raw_text = str(payload.get("raw_text") or "")
    if not raw_text:
        return False, "raw_text_missing"
    if _sha256_bytes(raw_text.encode("utf-8")) != declared_sha:
        return False, "raw_text_sha_mismatch"
    return True, ""


def audit(
    *,
    start_date: date,
    end_date: date,
    lane_map_path: Path,
    timezone_registry_path: Path,
    raw_root: Path,
    as_of_utc: str,
    api_probe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("date_window_invalid")
    lanes = _station_lanes(lane_map_path)
    station_ids = set(lanes)
    timezones = _station_timezones(timezone_registry_path, station_ids)
    dates = [d.isoformat() for d in (start_date, end_date)]
    station_day_counts: Counter[tuple[str, str]] = Counter()
    station_day_sha: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    station_row_counts: Counter[str] = Counter()
    formal_candidate_row_counts: Counter[str] = Counter()
    excluded_row_counts: Counter[str] = Counter()
    retained_awc_rows_total = 0
    invalid_counts: Counter[str] = Counter()
    retained_object_count = 0
    raw_root = raw_root.expanduser().resolve()
    object_root = raw_root / "objects"
    if object_root.exists():
        for path in object_root.rglob("*.json.gz"):
            assert_process_memory_budget()
            retained_object_count += 1
            try:
                with gzip.open(path, "rb") as handle:
                    encoded = handle.read(MAX_JSON_INPUT_BYTES + 1)
                if len(encoded) > MAX_JSON_INPUT_BYTES:
                    invalid_counts["object_too_large"] += 1
                    continue
                payload = json.loads(encoded.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                invalid_counts["object_json_invalid"] += 1
                continue
            if not isinstance(payload, Mapping):
                invalid_counts["object_not_mapping"] += 1
                continue
            if str(payload.get("source_lane") or "").strip().upper() != "AWC":
                continue
            station = str(payload.get("station_id") or "").strip().upper()
            if station not in station_ids:
                invalid_counts["station_outside_lane_map"] += 1
                continue
            valid, reason = _raw_object_is_valid(path, payload)
            if not valid:
                invalid_counts[reason] += 1
                continue
            try:
                valid_at = _parse_utc(payload.get("verified_valid_time_utc") or payload.get("valid_at_utc"))
                local_day = valid_at.astimezone(ZoneInfo(timezones[station])).date()
            except (TypeError, ValueError, OverflowError):
                invalid_counts["valid_time_invalid"] += 1
                continue
            if not (start_date <= local_day <= end_date):
                continue
            retained_awc_rows_total += 1
            report_type = str(payload.get("report_type") or "METAR").strip().upper()
            if report_type not in {"METAR", "SPECI"}:
                excluded_row_counts["report_type_invalid"] += 1
                continue
            if report_type == "METAR" and (valid_at.minute != 0 or valid_at.second != 0):
                # The formal global-AWC lane only admits routine METAR at the
                # official :00 slot.  Keep the object in the archive audit,
                # but never count it as canonical-truth coverage.
                excluded_row_counts["awc_metar_not_official_hour_slot"] += 1
                continue
            day = local_day.isoformat()
            station_day_counts[(station, day)] += 1
            station_day_sha[(station, day)].add(str(payload["raw_sha256"]).lower())
            station_row_counts[station] += 1
            formal_candidate_row_counts[station] += 1

    covered_station_days = {
        f"{station}|{day}": {
            "row_count": station_day_counts[(station, day)],
            "distinct_raw_sha256_count": len(station_day_sha[(station, day)]),
        }
        for station, day in sorted(station_day_counts)
    }
    expected_station_days = {
        f"{station}|{day}"
        for station in sorted(station_ids)
        for day in ((start_date.fromordinal(start_date.toordinal() + offset)).isoformat()
                    for offset in range((end_date - start_date).days + 1))
    }
    missing_station_days = sorted(expected_station_days - set(covered_station_days))
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at_utc": as_of_utc,
        "window": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        "station_count": len(station_ids),
        "station_ids": sorted(station_ids),
        "station_timezones": {station: timezones[station] for station in sorted(timezones)},
        "raw_root": str(raw_root),
        "retained_object_count": retained_object_count,
        "retained_awc_object_station_day_count": len(covered_station_days),
        "retained_awc_object_row_count": retained_awc_rows_total,
        "retained_awc_formal_candidate_row_count": sum(formal_candidate_row_counts.values()),
        "covered_station_days": covered_station_days,
        "missing_station_day_count": len(missing_station_days),
        "missing_station_days": missing_station_days,
        "invalid_object_counts": dict(sorted(invalid_counts.items())),
        "excluded_awc_row_counts": dict(sorted(excluded_row_counts.items())),
        "official_api_probe": dict(api_probe or {
            "performed": False,
            "reason": "not_requested",
        }),
        "decision": {
            "state": "BLOCKED_EARLY_AWC_HISTORY_MISSING" if missing_station_days else "COVERAGE_PRESENT",
            "formal_truth_source": "AWC",
            "canonical_ingress_allowed": False,
            "reason": (
                "Do not synthesize missing station-days or promote a non-AWC source. "
                "Acquire a retained official-AWC historical receipt through a separately "
                "reviewed archival path before canonical ingress."
                if missing_station_days else "retained official-AWC raw coverage is present"
            ),
        },
        "boundary": {
            "reads_target_database": False,
            "writes_target_database": False,
            "writes_research_raw_archive": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "uses_future_data_as_oos": False,
        },
    }
    result["content_sha256"] = content_sha256(result)
    result["receipt_sha256"] = content_sha256(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--station-lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument("--timezone-registry", type=Path, default=DEFAULT_TIMEZONE_REGISTRY)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument(
        "--api-probe-receipt",
        type=Path,
        default=None,
        help="Optional bounded, already-captured official-AWC API probe receipt; no network is performed by this audit.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    api_probe = None
    if args.api_probe_receipt is not None:
        api_probe = load_bounded_json(
            args.api_probe_receipt.expanduser().resolve(),
            limit=MAX_JSON_INPUT_BYTES,
        )
    result = audit(
        start_date=_parse_date(args.start_date),
        end_date=_parse_date(args.end_date),
        as_of_utc=str(args.as_of_utc),
        lane_map_path=args.station_lane_map,
        timezone_registry_path=args.timezone_registry,
        raw_root=args.raw_root,
        api_probe=api_probe,
    )
    write_bounded_json(args.output.expanduser().resolve(), result, limit=MAX_JSON_OUTPUT_BYTES)
    print(json.dumps({
        "schema": result["schema"],
        "state": result["decision"]["state"],
        "station_count": result["station_count"],
        "retained_awc_object_row_count": result["retained_awc_object_row_count"],
        "retained_awc_formal_candidate_row_count": result["retained_awc_formal_candidate_row_count"],
        "excluded_awc_row_counts": result["excluded_awc_row_counts"],
        "missing_station_day_count": result["missing_station_day_count"],
        "output": str(args.output.expanduser().resolve()),
        "content_sha256": result["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["decision"]["state"] == "COVERAGE_PRESENT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
