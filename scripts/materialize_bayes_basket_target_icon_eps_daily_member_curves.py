#!/usr/bin/env python3
"""Materialize candidate-only ICON Global/EPS local-day member curves.

The DWD ICON Global/EPS T_2M product is retained one native 40-member raw
object per valid hour.  This offline assembler consumes only the already
verified 45-station member cohorts produced by
``decode_bayes_basket_target_icon_eps_station_members.py`` and an explicit
45-station settlement-timezone mapping.  It computes a local-day maximum for
each station/member only after the normal native daily-coverage checks pass.

No network, database, P0/P0-M/P1 materialization, arrival-index promotion, or
order path is reachable here.  In particular, a single successful lead is
useful forward evidence but remains a blocked day curve, never a probability.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.native_model_decode import (  # noqa: E402
    materialize_native_daily_curves_by_station_timezone,
)
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json_if_changed  # noqa: E402
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


SCHEMA = "bayes_basket_target_icon_global_eps_daily_member_curve_v1"
COHORT_SCHEMA = "bayes_basket_target_icon_global_eps_station_member_cohort_v1"
PROVIDER_ID = "icon_global_eps_official"
NATIVE_CURVE_PROVIDER_ID = "icon_global_official"
PRODUCT_ID = "DWD_ICON_GLOBAL_EPS"
FAMILY_ID = "DWD_ICON_EPS"
EXPECTED_STATIONS = 45
EXPECTED_MEMBERS = tuple(range(1, 41))
EXPECTED_MEMBER_RECORDS = EXPECTED_STATIONS * len(EXPECTED_MEMBERS)
MAX_COHORTS = 32
MAX_COHORT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_COHORT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_POINTS = EXPECTED_MEMBER_RECORDS * 26
MAX_RAW_OBJECTS = 32
DEFAULT_OUTPUT = ROOT / "output/audits/icon_global_eps_daily_member_curves_20260801.json"


class IconEpsDailyCurveError(ContractError):
    """A lineage or coverage failure that must never be repaired by fallback."""


def _text(field: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise IconEpsDailyCurveError(f"icon_eps_daily_{field}_missing")
    return result


def _timestamp(field: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(field, value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise IconEpsDailyCurveError(f"icon_eps_daily_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IconEpsDailyCurveError(f"icon_eps_daily_{field}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(value: Any) -> str:
    return content_sha256(value)


def _valid_sha(value: Any, *, field: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise IconEpsDailyCurveError(f"icon_eps_daily_{field}_invalid")
    return result


def _canonical_uuid(value: Any, *, field: str) -> str:
    result = str(value or "").strip().lower().replace("-", "")
    if len(result) != 32 or any(char not in "0123456789abcdef" for char in result):
        raise IconEpsDailyCurveError(f"icon_eps_daily_{field}_invalid")
    return result


def _official_dwd_url(value: Any) -> str:
    url = _text("source_url", value)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not (host == "dwd.de" or host.endswith(".dwd.de")):
        raise IconEpsDailyCurveError("icon_eps_daily_source_not_official_dwd")
    return url


def _load_mapping(path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    try:
        document = load_bounded_json(path.expanduser().resolve(strict=True), limit=MAX_COHORT_BYTES)
    except (OSError, ValueError) as exc:
        raise IconEpsDailyCurveError("icon_eps_daily_timezone_mapping_missing") from exc
    if not isinstance(document, Mapping):
        raise IconEpsDailyCurveError("icon_eps_daily_timezone_mapping_invalid")
    mapping = document.get("station_timezones")
    if not isinstance(mapping, Mapping):
        raise IconEpsDailyCurveError("icon_eps_daily_timezone_mapping_invalid")
    expected = {str(item) for item in coordinate_registry_manifest()["station_ids"]}
    normalized = {
        str(station).strip().upper(): str(zone).strip()
        for station, zone in mapping.items()
        if str(station).strip() and str(zone).strip()
    }
    if len(expected) != EXPECTED_STATIONS or set(normalized) != expected:
        raise IconEpsDailyCurveError("icon_eps_daily_timezone_station_scope_invalid")
    try:
        for timezone_name in normalized.values():
            ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise IconEpsDailyCurveError("icon_eps_daily_timezone_invalid") from exc
    supplied_hash = document.get("content_sha256")
    if supplied_hash is not None:
        identity = {key: value for key, value in document.items() if key != "content_sha256"}
        if _valid_sha(supplied_hash, field="timezone_mapping_content_sha") != _sha256(identity):
            raise IconEpsDailyCurveError("icon_eps_daily_timezone_mapping_content_sha_mismatch")
    lineage = {
        "path": str(path.expanduser().resolve()),
        "content_sha256": str(supplied_hash or _sha256(document)),
        "station_count": len(normalized),
        "mapping_schema": str(document.get("schema") or "unversioned_explicit_mapping"),
    }
    return dict(sorted(normalized.items())), lineage


def _load_cohort(path: Path) -> dict[str, Any]:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise IconEpsDailyCurveError("icon_eps_daily_cohort_missing") from exc
    if not resolved.is_file() or resolved.is_symlink() or resolved.stat().st_size > MAX_COHORT_BYTES:
        raise IconEpsDailyCurveError("icon_eps_daily_cohort_bound_invalid")
    document = load_bounded_json(resolved, limit=MAX_COHORT_BYTES)
    if not isinstance(document, Mapping):
        raise IconEpsDailyCurveError("icon_eps_daily_cohort_mapping_required")
    identity = {key: value for key, value in document.items() if key != "content_sha256"}
    if _valid_sha(document.get("content_sha256"), field="cohort_content_sha") != _sha256(identity):
        raise IconEpsDailyCurveError("icon_eps_daily_cohort_content_sha_mismatch")
    if (
        document.get("schema") != COHORT_SCHEMA
        or document.get("state") != "OFFICIAL_40_MEMBER_45_STATION_DECODED_CANDIDATE_ONLY"
        or document.get("provider_id") != PROVIDER_ID
        or document.get("product_id") != PRODUCT_ID
        or document.get("independence_family_id") != FAMILY_ID
        or document.get("station_count") != EXPECTED_STATIONS
        or document.get("member_count") != len(EXPECTED_MEMBERS)
        or document.get("member_record_count") != EXPECTED_MEMBER_RECORDS
        or document.get("formal_p0_eligible") is not False
        or document.get("native_arrival_index_eligible") is not False
        or document.get("hard_no_eligible") is not False
    ):
        raise IconEpsDailyCurveError("icon_eps_daily_cohort_contract_invalid")
    raw_sha = _valid_sha(document.get("raw_sha256"), field="cohort_raw_sha")
    raw_ref = Path(_text("cohort_raw_object_ref", document.get("raw_object_ref"))).expanduser()
    try:
        raw_path = raw_ref.resolve(strict=True)
    except OSError as exc:
        raise IconEpsDailyCurveError("icon_eps_daily_cohort_raw_missing") from exc
    if not raw_path.is_file() or raw_path.is_symlink():
        raise IconEpsDailyCurveError("icon_eps_daily_cohort_raw_invalid")
    grid = ((document.get("lineage") or {}).get("official_grid") if isinstance(document.get("lineage"), Mapping) else None)
    if not isinstance(grid, Mapping):
        raise IconEpsDailyCurveError("icon_eps_daily_cohort_grid_missing")
    return {
        "path": str(resolved),
        "content_sha256": str(document["content_sha256"]),
        "raw_sha256": raw_sha,
        "raw_path": str(raw_path),
        "source_url": _official_dwd_url(((document.get("lineage") or {}).get("source_url") if isinstance(document.get("lineage"), Mapping) else "")),
        "run_initialization_utc": _timestamp_text(_timestamp("cohort_run", document.get("run_initialization_utc"))),
        "valid_time_utc": _timestamp_text(_timestamp("cohort_valid", document.get("valid_time_utc"))),
        "published_at_utc": _timestamp_text(_timestamp("cohort_published", document.get("provider_published_at_utc"))),
        "received_at_utc": _timestamp_text(_timestamp("cohort_received", document.get("response_completed_at_utc"))),
        "grid": {
            "source_grid_sha256": _valid_sha(grid.get("source_grid_sha256"), field="grid_sha"),
            "source_grid_uuid": _canonical_uuid(grid.get("source_grid_uuid"), field="grid_uuid"),
            "source_grid_cell_count": int(grid.get("source_grid_cell_count") or 0),
            "source_grid_receipt_sha256": _valid_sha(grid.get("source_grid_receipt_sha256"), field="grid_receipt_sha"),
        },
        "records": document.get("member_station_records"),
    }


def _local_day_bounds(event_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    try:
        target = date.fromisoformat(event_date)
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise IconEpsDailyCurveError("icon_eps_daily_target_timezone_invalid") from exc
    start = datetime.combine(target, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _cohort_points(
    cohorts: Iterable[Mapping[str, Any]],
    *,
    station_timezones: Mapping[str, str],
    target_local_date: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected_stations = set(station_timezones)
    seen_valid: set[str] = set()
    grid_signatures: set[tuple[str, str, int, str]] = set()
    points: list[dict[str, Any]] = []
    raw_objects: dict[str, dict[str, Any]] = {}
    common_run = ""
    for cohort in cohorts:
        valid_time = str(cohort["valid_time_utc"])
        if valid_time in seen_valid:
            raise IconEpsDailyCurveError("icon_eps_daily_duplicate_valid_time")
        seen_valid.add(valid_time)
        run = str(cohort["run_initialization_utc"])
        if common_run and run != common_run:
            raise IconEpsDailyCurveError("icon_eps_daily_mixed_run_initializations")
        common_run = run
        grid = cohort["grid"]
        assert isinstance(grid, Mapping)
        grid_signatures.add((
            str(grid["source_grid_sha256"]), str(grid["source_grid_uuid"]),
            int(grid["source_grid_cell_count"]), str(grid["source_grid_receipt_sha256"]),
        ))
        raw_objects[str(cohort["raw_sha256"])] = {
            "raw_sha256": str(cohort["raw_sha256"]),
            "raw_object_ref": str(cohort["raw_path"]),
            "cohort_content_sha256": str(cohort["content_sha256"]),
            "valid_time_utc": valid_time,
            "published_at_utc": str(cohort["published_at_utc"]),
            "received_at_utc": str(cohort["received_at_utc"]),
        }
        records = cohort["records"]
        if not isinstance(records, list) or len(records) != EXPECTED_MEMBER_RECORDS:
            raise IconEpsDailyCurveError("icon_eps_daily_cohort_record_count_invalid")
        expected_pairs = {(station, member) for station in expected_stations for member in EXPECTED_MEMBERS}
        seen_pairs: set[tuple[str, int]] = set()
        for raw in records:
            if not isinstance(raw, Mapping):
                raise IconEpsDailyCurveError("icon_eps_daily_record_mapping_invalid")
            station = _text("record_station", raw.get("station_id")).upper()
            try:
                member = int(raw.get("member_id"))
                temperature = float(raw.get("temperature_2m_c"))
            except (TypeError, ValueError) as exc:
                raise IconEpsDailyCurveError("icon_eps_daily_record_value_invalid") from exc
            if station not in expected_stations or member not in EXPECTED_MEMBERS or not math.isfinite(temperature):
                raise IconEpsDailyCurveError("icon_eps_daily_record_scope_invalid")
            if _valid_sha(raw.get("raw_sha256"), field="record_raw_sha") != cohort["raw_sha256"]:
                raise IconEpsDailyCurveError("icon_eps_daily_record_raw_sha_mismatch")
            pair = (station, member)
            if pair in seen_pairs:
                raise IconEpsDailyCurveError("icon_eps_daily_cohort_pair_duplicate")
            seen_pairs.add(pair)
            start, end = _local_day_bounds(target_local_date, station_timezones[station])
            parsed_valid = _timestamp("point_valid", valid_time)
            if not start <= parsed_valid < end:
                continue
            points.append({
                # Reuse the proven daily-coverage contract one member at a
                # time; provider is restored to ICON-EPS in the derived rows.
                "provider_id": NATIVE_CURVE_PROVIDER_ID,
                "station_id": station,
                "event_date": target_local_date,
                "model_run_initialization_utc": run,
                "valid_time_utc": valid_time,
                "temperature_2m_c": round(temperature, 6),
                "raw_message_sha256": str(cohort["raw_sha256"]),
                "receipt_sha256": str(cohort["content_sha256"]),
                "published_at_utc": str(cohort["published_at_utc"]),
                "received_at_utc": str(cohort["received_at_utc"]),
                "member_id": member,
            })
        if seen_pairs != expected_pairs:
            raise IconEpsDailyCurveError("icon_eps_daily_cohort_pair_scope_invalid")
    if len(grid_signatures) != 1:
        raise IconEpsDailyCurveError("icon_eps_daily_grid_signature_mixed")
    if len(raw_objects) > MAX_RAW_OBJECTS or len(points) > MAX_POINTS:
        raise IconEpsDailyCurveError("icon_eps_daily_resource_bound_exceeded")
    grid_sha, grid_uuid, cell_count, grid_receipt_sha = next(iter(grid_signatures))
    return points, {
        "model_run_initialization_utc": common_run,
        "raw_objects": [raw_objects[key] for key in sorted(raw_objects)],
        "grid": {
            "source_grid_sha256": grid_sha,
            "source_grid_uuid": grid_uuid,
            "source_grid_cell_count": cell_count,
            "source_grid_receipt_sha256": grid_receipt_sha,
        },
    }


def build_daily_member_curves(
    cohort_paths: Iterable[Path],
    *,
    station_timezones_path: Path,
    target_local_date: str,
) -> dict[str, Any]:
    paths = [Path(path) for path in cohort_paths]
    if not paths or len(paths) > MAX_COHORTS or len({str(path.expanduser().resolve()) for path in paths}) != len(paths):
        raise IconEpsDailyCurveError("icon_eps_daily_cohort_path_scope_invalid")
    station_timezones, timezone_lineage = _load_mapping(station_timezones_path)
    cohorts = [_load_cohort(path) for path in paths]
    points, lineage = _cohort_points(
        cohorts, station_timezones=station_timezones, target_local_date=target_local_date
    )
    curves: list[dict[str, Any]] = []
    eligible_rows: list[dict[str, Any]] = []
    expected_pairs = {(station, member) for station in station_timezones for member in EXPECTED_MEMBERS}
    for member in EXPECTED_MEMBERS:
        member_points = [point for point in points if point["member_id"] == member]
        materialized = materialize_native_daily_curves_by_station_timezone(
            member_points, station_timezones=station_timezones
        ) if member_points else {"curves": [], "eligible_model_rows": []}
        by_station = {
            str(row.get("station_id")): row
            for row in materialized.get("curves", [])
            if isinstance(row, Mapping)
        }
        eligible_by_station = {
            str(row.get("station_id")): row
            for row in materialized.get("eligible_model_rows", [])
            if isinstance(row, Mapping)
        }
        for station in sorted(station_timezones):
            base = by_station.get(station)
            if base is None:
                base = {
                    "station_id": station,
                    "event_date": target_local_date,
                    "model_run_initialization_utc": lineage["model_run_initialization_utc"],
                    "points": [],
                    "coverage": {"point_count": 0},
                    "state": "BLOCKED_INCOMPLETE_CURVE",
                    "blockers": ["native_daily_curve_empty"],
                    "forecast_daily_max_c": None,
                }
            curve = {
                **dict(base),
                "schema": SCHEMA,
                "provider_id": PROVIDER_ID,
                "product_id": PRODUCT_ID,
                "independence_family_id": FAMILY_ID,
                "member_id": member,
                "target_timezone": station_timezones[station],
                "candidate_only": True,
                "formal_p0_eligible": False,
                "native_arrival_index_eligible": False,
                "p1_or_hard_no_eligible": False,
            }
            curve_identity = {key: value for key, value in curve.items() if key != "curve_sha256"}
            curve["curve_sha256"] = _sha256(curve_identity)
            curves.append(curve)
            eligible = eligible_by_station.get(station)
            if eligible is not None:
                row = {
                    **dict(eligible),
                    "provider_id": PROVIDER_ID,
                    "product_id": PRODUCT_ID,
                    "independence_family_id": FAMILY_ID,
                    "member_id": member,
                    "candidate_only": True,
                    "formal_p0_eligible": False,
                    "native_arrival_index_eligible": False,
                    "p1_or_hard_no_eligible": False,
                }
                row["member_daily_curve_sha256"] = curve["curve_sha256"]
                eligible_rows.append(row)
    actual_pairs = {(str(curve["station_id"]), int(curve["member_id"])) for curve in curves}
    if actual_pairs != expected_pairs or len(curves) != EXPECTED_MEMBER_RECORDS:
        raise IconEpsDailyCurveError("icon_eps_daily_curve_scope_invalid")
    complete = len(eligible_rows) == EXPECTED_MEMBER_RECORDS
    identity = {
        "schema": SCHEMA,
        "state": (
            "OFFICIAL_ICON_EPS_DAILY_MEMBER_CURVES_COMPLETE_CANDIDATE_ONLY"
            if complete else "BLOCKED_INCOMPLETE_DAILY_COVERAGE"
        ),
        "provider_id": PROVIDER_ID,
        "product_id": PRODUCT_ID,
        "independence_family_id": FAMILY_ID,
        "target_local_date": target_local_date,
        "model_run_initialization_utc": lineage["model_run_initialization_utc"],
        "station_coordinate_registry": coordinate_registry_manifest(),
        "station_timezones": station_timezones,
        "timezone_lineage": timezone_lineage,
        "source_cohorts": [
            {"path": cohort["path"], "content_sha256": cohort["content_sha256"], "valid_time_utc": cohort["valid_time_utc"]}
            for cohort in sorted(cohorts, key=lambda item: str(item["valid_time_utc"]))
        ],
        "raw_objects": lineage["raw_objects"],
        "official_grid": lineage["grid"],
        "daily_member_curves": curves,
        "eligible_member_daily_max_rows": eligible_rows,
        "checks": {
            "source_cohort_count": len(cohorts),
            "hourly_member_point_count_in_target_days": len(points),
            "daily_member_curve_count": len(curves),
            "complete_daily_member_curve_count": len(eligible_rows),
            "all_45_station_40_member_curves_complete": complete,
            "single_native_run": True,
            "single_grid_signature": True,
            "writes_target_database": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "authority": {
            "source_role": "official_native",
            "candidate_only": True,
            "formal_p0_eligible": False,
            "native_arrival_index_eligible": False,
            "p1_or_hard_no_eligible": False,
            "strict_oos_activation_present": False,
        },
        "boundary": {
            "network_requests": 0,
            "writes_target_database": False,
            "writes_production_database": False,
            "activates_p0": False,
            "activates_p0m": False,
            "materializes_p1": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": _sha256(identity)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", action="append", type=Path, required=True)
    parser.add_argument("--station-timezones", type=Path, required=True)
    parser.add_argument("--target-local-date", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = build_daily_member_curves(
        args.cohort,
        station_timezones_path=args.station_timezones,
        target_local_date=str(args.target_local_date),
    )
    write_bounded_json_if_changed(args.output.expanduser().resolve(), result, limit=MAX_OUTPUT_BYTES)
    print(json.dumps({
        "state": result["state"],
        "daily_member_curve_count": result["checks"]["daily_member_curve_count"],
        "complete_daily_member_curve_count": result["checks"]["complete_daily_member_curve_count"],
        "formal_p0_eligible": result["authority"]["formal_p0_eligible"],
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "OFFICIAL_ICON_EPS_DAILY_MEMBER_CURVES_COMPLETE_CANDIDATE_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
