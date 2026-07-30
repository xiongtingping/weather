#!/usr/bin/env python3
"""Build bounded registry-settlement history for the target lane.

The command is an offline evidence step.  It never writes SQLite, changes an
activation pointer, reads market state, or calls an order gateway.  The target
history accepts only complete curves from the native-provider decode receipt;
legacy model CSVs are not read, counted, or used as fallback input.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    assert_timestamp_not_future,
    content_sha256,
)
from bayes_basket_target.et_history import build_et_history_evidence, event_day_window  # noqa: E402
from bayes_basket_target.historical_model_rows import (  # noqa: E402
    load_historical_model_rows,
    select_preferred_model_rows,
)
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402


DEFAULT_CANONICAL = ROOT / "output/audits/bayes_basket_target_canonical_observation_evidence.json"
DEFAULT_TIMEZONE_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_et_history_evidence.json"
MAX_INPUT_BYTES = 64 * 1024 * 1024
HISTORICAL_REBUILD_SCHEMA = "bayes_basket_target_historical_native_rebuild_evidence_v1"
ICON_RETAINED_RAW_REBUILD_SCHEMA = "bayes_basket_target_icon_retained_raw_rebuild_evidence_v1"


def _current_utc() -> str:
    """Return the invocation clock used when no explicit as-of is supplied."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bounded_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    if resolved.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("et_history_input_file_exceeds_bound")
    return resolved


def _station_lane_map(path: Path) -> dict[str, str]:
    value = load_bounded_json(_bounded_path(path))
    if isinstance(value, Mapping) and isinstance(value.get("stations"), Mapping):
        value = value["stations"]
    if not isinstance(value, Mapping):
        raise ValueError("station_lane_map_mapping_required")
    result = {str(key).strip().upper(): str(item).strip().lower() for key, item in value.items()}
    if not result:
        raise ValueError("station_lane_map_empty")
    return result


def _station_timezone_map(path: Path) -> dict[str, str]:
    value = load_bounded_json(_bounded_path(path))
    rows = value.get("rows") if isinstance(value, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("station_timezone_registry_rows_missing")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        if not station or not timezone_name:
            continue
        previous = result.setdefault(station, timezone_name)
        if previous != timezone_name:
            raise ValueError("station_settlement_timezone_conflict:" + station)
    if not result:
        raise ValueError("station_timezone_registry_empty")
    return result


def _normalize_station_scope(
    station_ids: set[str] | None,
    lane_map: Mapping[str, str],
) -> set[str] | None:
    if station_ids is None:
        return None
    normalized = {str(item).strip().upper() for item in station_ids if str(item).strip()}
    if not normalized:
        raise ValueError("station_scope_empty")
    unknown = sorted(normalized - set(lane_map))
    if unknown:
        raise ValueError("station_scope_not_in_lane_map:" + ",".join(unknown))
    return normalized


def _canonical_observations(
    path: Path,
    *,
    station_scope: set[str] | None = None,
) -> list[dict[str, Any]]:
    value = load_bounded_json(_bounded_path(path))
    if not isinstance(value, Mapping):
        raise ValueError("canonical_evidence_mapping_required")
    if value.get("schema") != "bayes_basket_target_canonical_observation_evidence_v1":
        raise ValueError("canonical_evidence_schema_invalid")
    observations = value.get("observations")
    if not isinstance(observations, list):
        raise ValueError("canonical_evidence_observations_missing")
    return [
        dict(item)
        for item in observations
        if isinstance(item, Mapping)
        and (
            station_scope is None
            or str(item.get("station_id") or "").strip().upper() in station_scope
        )
    ]


def _native_complete_rows(
    path: Path,
    *,
    station_scope: set[str] | None = None,
) -> list[dict[str, Any]]:
    value = load_bounded_json(_bounded_path(path))
    if not isinstance(value, Mapping):
        raise ValueError("native_decode_evidence_mapping_required")
    if value.get("schema") != "bayes_basket_target_native_model_arrival_decode_evidence_v1":
        raise ValueError("native_decode_evidence_schema_invalid")
    curves = value.get("curves")
    if not isinstance(curves, Mapping):
        raise ValueError("native_decode_curves_missing")
    eligible = curves.get("eligible_model_rows")
    if not isinstance(eligible, list):
        raise ValueError("native_decode_eligible_model_rows_missing")
    target_timezone = str(value.get("target_timezone") or "").strip()
    rows: list[dict[str, Any]] = []
    for item in eligible:
        if not isinstance(item, Mapping):
            raise ValueError("native_decode_curve_invalid")
        if (
            station_scope is not None
            and str(item.get("station_id") or "").strip().upper() not in station_scope
        ):
            continue
        for point in item.get("temperature_points") or item.get("points") or []:
            if not isinstance(point, Mapping):
                raise ValueError("native_decode_point_invalid")
            rows.append({
                "station_id": item.get("station_id"),
                "event_date": item.get("event_date"),
                "target_timezone": item.get("target_timezone") or target_timezone,
                "provider_id": item.get("provider_id"),
                "model_run_initialization_utc": item.get("model_run_initialization_utc"),
                "valid_time_utc": point.get("valid_time_utc"),
                "temperature_2m_c": point.get("temperature_2m_c"),
                "raw_message_sha256": point.get("raw_message_sha256"),
                "receipt_sha256": point.get("receipt_sha256"),
                "published_at_utc": point.get("published_at_utc"),
                "received_at_utc": point.get("received_at_utc"),
                "capture_mode": "prospective",
                "historical_replay_eligible": False,
                "prospective_strict_asof_eligible": True,
            })
    return rows


def _historical_rebuild_rows(
    path: Path,
    *,
    event_dates: set[str] | None = None,
    as_of_utc: str | None = None,
    excluded_event_dates: set[str] | None = None,
    station_scope: set[str] | None = None,
    station_timezone_map: Mapping[str, str],
) -> list[dict[str, Any]]:
    value = load_bounded_json(_bounded_path(path))
    source_rows: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        raw_source_rows = value.get("model_rows") or value.get("model_points")
        if isinstance(raw_source_rows, list):
            source_rows.extend(item for item in raw_source_rows if isinstance(item, Mapping))
        curves = value.get("curves")
        if isinstance(curves, Mapping) and isinstance(curves.get("eligible_model_rows"), list):
            source_rows.extend(
                item for item in curves["eligible_model_rows"] if isinstance(item, Mapping)
            )
    if as_of_utc and excluded_event_dates is not None:
        for source_row in source_rows:
            event_date = str(source_row.get("event_date") or source_row.get("target_local_date") or "").strip()
            if not event_date:
                continue
            try:
                station = str(source_row.get("station_id") or "").strip().upper()
                if event_day_window(
                    event_date,
                    as_of_utc=as_of_utc,
                    target_timezone=str(station_timezone_map.get(station) or ""),
                )["state"] != "closed":
                    excluded_event_dates.add(event_date)
            except ContractError:
                continue
    try:
        rows = load_historical_model_rows(
            value,
            as_of_utc=as_of_utc or _current_utc(),
            station_timezone_map=station_timezone_map,
            require_reconstructed_at=False,
        )
    except ContractError as exc:
        raise ValueError(str(exc)) from exc
    output: list[dict[str, Any]] = []
    for row in rows:
        if (
            station_scope is not None
            and str(row.get("station_id") or "").strip().upper() not in station_scope
        ):
            continue
        event_date = str(row.get("event_date") or "").strip()
        if event_dates and event_date not in event_dates:
            continue
        if as_of_utc:
            try:
                station = str(row.get("station_id") or "").strip().upper()
                window = event_day_window(
                    event_date,
                    as_of_utc=as_of_utc,
                    target_timezone=str(station_timezone_map.get(station) or ""),
                )
            except ContractError:
                window = None
            if window is not None and window["state"] != "closed":
                if excluded_event_dates is not None:
                    excluded_event_dates.add(event_date)
                continue
        output.append(row)
    return output


def run(
    *,
    canonical_path: Path,
    lane_map_path: Path,
    timezone_registry_path: Path,
    output_path: Path,
    as_of_utc: str,
    native_decode_path: Path | None = None,
    historical_rebuild_paths: list[Path] | None = None,
    historical_event_dates: set[str] | None = None,
    history_only: bool = False,
    station_ids: set[str] | None = None,
) -> dict[str, Any]:
    as_of_utc = assert_timestamp_not_future(as_of_utc)
    lane_map = _station_lane_map(lane_map_path)
    station_timezone_map = _station_timezone_map(timezone_registry_path)
    station_scope = _normalize_station_scope(station_ids, lane_map)
    selected_stations = station_scope or set(lane_map)
    missing_timezones = sorted(selected_stations - set(station_timezone_map))
    if missing_timezones:
        raise ValueError(
            "station_settlement_timezone_missing:" + ",".join(missing_timezones)
        )
    model_rows: list[dict[str, Any]] = []
    excluded_historical_event_dates: set[str] = set()
    if native_decode_path is not None:
        model_rows.extend(
            _native_complete_rows(native_decode_path, station_scope=station_scope)
        )
    for historical_rebuild_path in historical_rebuild_paths or []:
        if not history_only:
            raise ValueError("historical_rebuild_requires_history_only_mode")
        model_rows.extend(
            _historical_rebuild_rows(
                historical_rebuild_path,
                event_dates=historical_event_dates,
                as_of_utc=as_of_utc,
                excluded_event_dates=excluded_historical_event_dates,
                station_scope=station_scope,
                station_timezone_map=station_timezone_map,
            )
        )
    model_rows = select_preferred_model_rows(model_rows)
    observations = _canonical_observations(canonical_path, station_scope=station_scope)
    evidence = build_et_history_evidence(
        model_rows=model_rows,
        observations=observations,
        station_lane_map=lane_map,
        as_of_utc=as_of_utc,
        station_timezone_map={
            station: station_timezone_map[station]
            for station in selected_stations
        },
        canonical_ingress=True,
        history_only=history_only,
    )
    # Bind the source-side date quarantine into the same content-addressed
    # receipt.  This makes it auditable that omitted current/future historical
    # rows were excluded before normalization/grouping, rather than merely
    # ignored after a failed join.
    evidence = {
        **evidence,
        "historical_rebuild_boundary": {
            "closed_settlement_days_only": True,
            "as_of_utc": as_of_utc,
            "excluded_non_closed_event_dates": sorted(excluded_historical_event_dates),
        },
        "scope": {
            "mode": "allowlist" if station_scope is not None else "full_lane_map",
            "included_station_ids": sorted(station_scope or set(lane_map)),
            "excluded_station_ids": sorted(set(lane_map) - (station_scope or set(lane_map))),
            "excluded_source_lanes": sorted(
                {"amsc", "awc"}
                - {
                    lane_map[station]
                    for station in (station_scope or set(lane_map))
                }
            ),
        },
    }
    evidence["receipt_sha256"] = content_sha256({
        key: value for key, value in evidence.items() if key != "receipt_sha256"
    })
    write_bounded_json(output_path.expanduser().resolve(), evidence)
    return {
        "schema": evidence["schema"],
        "state": evidence["state"],
        "output": str(output_path.expanduser().resolve()),
        "receipt_sha256": evidence["receipt_sha256"],
        "input_counts": evidence["input_counts"],
        "blocker_count": len(evidence["blockers"]),
        "pending_event_day_count": len(evidence.get("pending_event_days") or []),
        "excluded_non_closed_event_dates": sorted(excluded_historical_event_dates),
        "promotion_allowed": False,
        "writes_target_db": False,
        "calls_order_gateway": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical-evidence", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--station-lane-map", type=Path, required=True)
    parser.add_argument(
        "--timezone-registry",
        type=Path,
        default=DEFAULT_TIMEZONE_REGISTRY,
    )
    parser.add_argument("--native-decode-evidence", type=Path)
    parser.add_argument("--historical-rebuild-evidence", type=Path, action="append")
    parser.add_argument(
        "--historical-event-date",
        action="append",
        help="Restrict rows loaded from historical rebuild evidence to this ET date; repeatable.",
    )
    parser.add_argument(
        "--history-only",
        action="store_true",
        help="Join closed historical ET days now; report open/current days as pending.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--as-of-utc",
        default="",
        help="Strict decision clock; defaults to the current UTC at invocation time.",
    )
    parser.add_argument(
        "--station-id",
        action="append",
        help="Restrict this isolated ET packet to registered stations; repeatable.",
    )
    args = parser.parse_args()
    result = run(
        canonical_path=args.canonical_evidence,
        lane_map_path=args.station_lane_map,
        timezone_registry_path=args.timezone_registry,
        output_path=args.output,
        as_of_utc=args.as_of_utc or _current_utc(),
        native_decode_path=args.native_decode_evidence,
        historical_rebuild_paths=list(args.historical_rebuild_evidence or []),
        historical_event_dates={
            str(item).strip()
            for item in (args.historical_event_date or [])
            if str(item).strip()
        }
        or None,
        history_only=args.history_only,
        station_ids={
            str(item).strip().upper()
            for item in (args.station_id or [])
            if str(item).strip()
        }
        or None,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "READY_FOR_STRICT_ASOF" else 1


if __name__ == "__main__":
    raise SystemExit(main())
