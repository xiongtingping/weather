#!/usr/bin/env python3
"""Refresh the target evidence chain under one strict-as-of boundary.

This is an offline evidence coordinator for the ``production_no_live`` lane.
It reads the shared live database only through the canonical read-only
exporter, writes bounded audit receipts, and never opens the target database,
launchd, a WebSocket, or an order gateway.  Every stage receives the same
explicit UTC decision clock so a later calibration step cannot accidentally
join a different evidence generation.
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

from bayes_basket_target.canonical_observation_export import (  # noqa: E402
    export_canonical_observation_evidence,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from scripts.bayes_basket_target_production_readiness import TARGET_WS_MANIFEST, audit  # noqa: E402
from scripts.build_bayes_basket_target_calibration_evidence import run as build_calibration  # noqa: E402
from scripts.build_bayes_basket_target_et_history import run as build_et_history  # noqa: E402
from scripts.build_bayes_basket_target_release_manifest import build_manifest  # noqa: E402
from scripts.merge_bayes_basket_target_historical_truth_observations import (  # noqa: E402
    merge as materialize_historical_canonical,
)


DEFAULT_SOURCE_DB = ROOT / "data" / "archive.db"
DEFAULT_TIMEZONE_REGISTRY = ROOT / "data" / "bayes_basket_target_market_registry_snapshot.json"
DEFAULT_LANE_MAP = ROOT / "config" / "trading" / "bayes_basket_target_station_lane_map_v1.json"
DEFAULT_NATIVE_DECODE = ROOT / "output" / "audits" / "bayes_basket_target_native_model_arrival_decode.json"
DEFAULT_CANONICAL = ROOT / "output" / "audits" / "bayes_basket_target_canonical_observation_evidence.json"
DEFAULT_HISTORICAL_CANONICAL = ROOT / "output" / "audits" / "bayes_basket_target_historical_canonical_observation_evidence.json"
DEFAULT_ET_HISTORY = ROOT / "output" / "audits" / "bayes_basket_target_et_history_evidence.json"
DEFAULT_CALIBRATION = ROOT / "output" / "audits" / "bayes_basket_target_calibration_evidence.json"
DEFAULT_READINESS = ROOT / "output" / "audits" / "bayes_basket_target_production_readiness.json"
DEFAULT_RELEASE = ROOT / "output" / "audits" / "bayes_basket_target_release_manifest.json"
DEFAULT_RECEIPT = ROOT / "output" / "audits" / "bayes_basket_target_evidence_refresh.json"
AWC_ONLY_TARGET_WS_MANIFEST = ROOT / "data" / "bayes_basket_target_awc_only_official_ws_manifest.json"
DEFAULT_HISTORICAL_REBUILDS = (
    ROOT / "output" / "audits" / "bayes_basket_target_historical_native_rebuild_evidence.json",
    ROOT / "output" / "audits" / "bayes_basket_target_historical_ecmwf_20260714_23_s3_fallback_v2.json",
    ROOT / "output" / "audits" / "bayes_basket_target_icon_retained_raw_rebuild_evidence.json",
)


def _current_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timezone_map(path: Path) -> dict[str, str]:
    payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("station_timezone_registry_rows_missing")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        if station and timezone_name:
            previous = result.setdefault(station, timezone_name)
            if previous != timezone_name:
                raise ValueError(f"station_timezone_conflict:{station}")
    if not result:
        raise ValueError("station_timezone_registry_empty")
    return result


def _lane_map(path: Path) -> dict[str, str]:
    payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    stations = payload.get("stations") if isinstance(payload, Mapping) else None
    if not isinstance(stations, Mapping) or not stations:
        raise ValueError("station_lane_map_empty")
    return {str(key).strip().upper(): str(value).strip().lower() for key, value in stations.items()}


def _existing_unique_paths(paths: list[Path] | tuple[Path, ...]) -> list[Path]:
    """Resolve only existing inputs, once, without creating fallback copies."""

    result: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        resolved = raw.expanduser().resolve()
        key = str(resolved)
        if key in seen or not resolved.is_file():
            continue
        seen.add(key)
        result.append(resolved)
    return result


def _excluded_source_lanes(
    station_scope: set[str] | None,
    lane_map_values: Mapping[str, str],
) -> list[str]:
    if not station_scope:
        return []
    scoped_lanes = {str(lane_map_values.get(station_id) or "") for station_id in station_scope}
    if "" in scoped_lanes or not scoped_lanes.issubset({"amsc", "awc"}):
        raise ValueError("evidence_refresh_station_lane_missing")
    return sorted({"amsc", "awc"} - scoped_lanes)


def run(
    *,
    as_of_utc: str = "",
    station_ids: set[str] | None = None,
    source_db: Path = DEFAULT_SOURCE_DB,
    timezone_registry: Path = DEFAULT_TIMEZONE_REGISTRY,
    lane_map: Path = DEFAULT_LANE_MAP,
    native_decode: Path | None = DEFAULT_NATIVE_DECODE,
    model_arrival_evidence: Path | None = None,
    historical_rebuild_paths: list[Path] | None = None,
    historical_canonical_paths: list[Path] | None = None,
    historical_reference_paths: list[Path] | None = None,
    canonical_output: Path = DEFAULT_CANONICAL,
    historical_canonical_output: Path = DEFAULT_HISTORICAL_CANONICAL,
    et_history_output: Path = DEFAULT_ET_HISTORY,
    calibration_output: Path = DEFAULT_CALIBRATION,
    readiness_output: Path = DEFAULT_READINESS,
    release_output: Path = DEFAULT_RELEASE,
    receipt_output: Path = DEFAULT_RECEIPT,
) -> dict[str, Any]:
    decision_clock = str(as_of_utc or _current_utc()).strip()
    station_scope = {
        str(item).strip().upper()
        for item in (station_ids or set())
        if str(item).strip()
    } or None
    lane_map_values = _lane_map(lane_map)
    excluded_source_lanes = _excluded_source_lanes(station_scope, lane_map_values)
    timezone_values = _timezone_map(timezone_registry)
    selected_stations = station_scope or set(lane_map_values)
    missing_timezones = sorted(set(selected_stations) - set(timezone_values))
    if missing_timezones:
        raise ValueError("evidence_refresh_station_timezone_missing:" + ",".join(missing_timezones))
    scoped_timezone_values = {
        timezone_values[station_id]
        for station_id in selected_stations
    }
    # Downstream builders already carry a station→registry-timezone map.  Keep
    # a valid compatibility fallback for legacy scalar fields while allowing
    # the 45-station artifact to span its real settlement zones.
    target_timezone = next(iter(scoped_timezone_values)) if len(scoped_timezone_values) == 1 else "UTC"
    awc_only_scope = bool(station_scope) and all(
        lane_map_values.get(station_id, "") == "awc"
        for station_id in station_scope
    )
    boundary = {
        "reads_shared_live_database": True,
        "shared_live_read_mode": "sqlite_mode_ro_query_only",
        "writes_shared_live_database": False,
        "writes_target_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "restarts_services": False,
        "live_submit_authorized": False,
    }
    canonical = export_canonical_observation_evidence(
        source_db=source_db,
        output_path=canonical_output,
        station_ids=sorted(station_scope or set()),
        source_lanes=["awc"] if awc_only_scope else [],
        station_timezones=_timezone_map(timezone_registry),
        station_lane_map=lane_map_values,
        as_of_utc=decision_clock,
    )
    canonical_input_path = canonical_output
    historical_canonical = None
    use_default_historical_inputs = (
        not historical_rebuild_paths
        and canonical_output.expanduser().resolve() == DEFAULT_CANONICAL.resolve()
    )
    historical_rebuild_inputs = _existing_unique_paths(
        list(historical_rebuild_paths)
        if historical_rebuild_paths
        else list(DEFAULT_HISTORICAL_REBUILDS) if use_default_historical_inputs else []
    )
    use_default_historical_packet = (
        not historical_canonical_paths
        and canonical_output.expanduser().resolve() == DEFAULT_CANONICAL.resolve()
        and historical_canonical_output.expanduser().resolve() == DEFAULT_HISTORICAL_CANONICAL.resolve()
    )
    historical_canonical_inputs = _existing_unique_paths(
        list(historical_canonical_paths)
        if historical_canonical_paths
        else [historical_canonical_output] if use_default_historical_packet else []
    )
    # A prior merged packet is a frozen historical source, not a new live
    # observation.  Re-merge it with the current read-only export so refresh
    # remains repeatable while preserving the canonical historical lineage.
    if historical_canonical_inputs:
        historical_canonical = materialize_historical_canonical(
            base=canonical_output,
            additional=historical_canonical_inputs,
            event_dates=[],
            output=historical_canonical_output,
            station_lane_map=_lane_map(lane_map),
            station_ids=station_scope,
            reference_paths=[path.expanduser().resolve() for path in (historical_reference_paths or [])],
            as_of_utc=decision_clock,
            target_timezone=target_timezone,
            station_timezone_map={
                station: timezone_values[station]
                for station in sorted(selected_stations)
            },
        )
        canonical_input_path = historical_canonical_output
    et_history = build_et_history(
        canonical_path=canonical_input_path,
        lane_map_path=lane_map,
        timezone_registry_path=timezone_registry,
        output_path=et_history_output,
        as_of_utc=decision_clock,
        native_decode_path=native_decode,
        historical_rebuild_paths=historical_rebuild_inputs,
        # Historical backfill does not wait for the currently open settlement
        # day. That day remains pending until its registry boundary closes.
        # exact model/truth join in the same refresh.
        history_only=True,
        station_ids=station_scope,
    )
    model_arrival_path: Path | None = None
    if model_arrival_evidence is not None:
        model_arrival_path = model_arrival_evidence.expanduser().resolve()
        arrival_payload = load_bounded_json(model_arrival_path)
        if not isinstance(arrival_payload, Mapping):
            raise ValueError("model_arrival_evidence_mapping_required")
        arrival_as_of = str(arrival_payload.get("as_of_utc") or "").strip()
        if arrival_as_of and arrival_as_of != decision_clock:
            raise ValueError("model_arrival_evidence_as_of_mismatch")
    calibration = build_calibration(
        as_of_utc=decision_clock,
        canonical_evidence_path=canonical_input_path,
        native_model_decode_evidence_path=native_decode,
        model_arrival_evidence_path=model_arrival_path,
        et_history_evidence_path=et_history_output,
        historical_rebuild_evidence_paths=historical_rebuild_inputs,
        output_path=calibration_output,
        station_ids=station_scope,
    )
    readiness = audit(
        db=ROOT / "data/bayes_basket_target_ssot.db",
        activation_path=ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json",
        manifest_path=AWC_ONLY_TARGET_WS_MANIFEST if awc_only_scope else TARGET_WS_MANIFEST,
        calibration_evidence_path=calibration_output,
        native_decode_evidence_path=native_decode or DEFAULT_NATIVE_DECODE,
        probe_services=False,
        station_ids=station_scope,
    )
    write_bounded_json(readiness_output, readiness)
    release = build_manifest(
        root=ROOT,
        generated_at_utc=decision_clock,
        probe_services=False,
    )
    write_bounded_json(release_output, release)
    core = {
        "schema": "bayes_basket_target_evidence_refresh_v1",
        "state": "READY_FOR_STRICT_ASOF" if et_history.get("state") == "READY_FOR_STRICT_ASOF" else "BLOCKED_EVIDENCE",
        "as_of_utc": decision_clock,
        "scope": {
            "mode": "allowlist" if station_scope is not None else "full_lane_map",
            "included_station_ids": sorted(station_scope or set()),
            "excluded_source_lanes": excluded_source_lanes,
            "settlement_timezone_mode": "registry_bound_per_station" if len(scoped_timezone_values) > 1 else "single_registry_timezone",
            "settlement_timezones": {
                station: timezone_values[station]
                for station in sorted(selected_stations)
            },
        },
        "stages": {
            "canonical": {
                **{key: canonical.get(key) for key in ("state", "observation_count", "accepted_truth_count", "quarantined_truth_count", "receipt_sha256")},
                "used_path": str(canonical_input_path.expanduser().resolve()),
                "historical_materialization": historical_canonical,
            },
            "et_history": et_history,
            "calibration": calibration,
            "readiness": {"state": readiness.get("state"), "blockers": readiness.get("blockers", [])},
            "release": {"release_state": release.get("release_state"), "manifest_sha256": release.get("manifest_sha256")},
        },
        "boundary": boundary,
    }
    result = {**core, "receipt_sha256": content_sha256(core)}
    write_bounded_json(receipt_output, result)
    return {"schema": result["schema"], "state": result["state"], "as_of_utc": decision_clock, "receipt_sha256": result["receipt_sha256"], "output": str(receipt_output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of-utc", default="", help="One explicit UTC decision clock for every stage.")
    parser.add_argument(
        "--station-id",
        action="append",
        help="Restrict ET history and calibration to an explicit station allowlist; repeatable.",
    )
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--timezone-registry", type=Path, default=DEFAULT_TIMEZONE_REGISTRY)
    parser.add_argument("--lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument("--native-decode", type=Path, default=DEFAULT_NATIVE_DECODE)
    parser.add_argument(
        "--model-arrival-evidence",
        type=Path,
        help="Prospective native-provider arrival receipt set for the same strict-as-of clock.",
    )
    parser.add_argument(
        "--historical-rebuild-evidence",
        type=Path,
        action="append",
        help="Official historical native rebuild receipt; repeat for each available provider.",
    )
    parser.add_argument(
        "--historical-canonical-evidence",
        type=Path,
        action="append",
        help="Official historical canonical observation packet; repeat for each bounded packet.",
    )
    parser.add_argument("--historical-canonical-output", type=Path, default=DEFAULT_HISTORICAL_CANONICAL)
    parser.add_argument(
        "--historical-reference-evidence",
        type=Path,
        action="append",
        help="Historical half-hour/AWC reference packet; bodies remain outside formal evidence.",
    )
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    args = parser.parse_args()
    try:
        result = run(
            as_of_utc=args.as_of_utc,
            station_ids={
                str(item).strip().upper()
                for item in (args.station_id or [])
                if str(item).strip()
            } or None,
            source_db=args.source_db,
            timezone_registry=args.timezone_registry,
            lane_map=args.lane_map,
            native_decode=args.native_decode,
            model_arrival_evidence=args.model_arrival_evidence,
            historical_rebuild_paths=list(args.historical_rebuild_evidence or []) or None,
            historical_canonical_paths=list(args.historical_canonical_evidence or []) or None,
            historical_reference_paths=list(args.historical_reference_evidence or []),
            historical_canonical_output=args.historical_canonical_output,
            receipt_output=args.receipt,
        )
    except Exception as exc:
        result = {
            "schema": "bayes_basket_target_evidence_refresh_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "boundary": {
                "writes_shared_live_database": False,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
            },
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") == "READY_FOR_STRICT_ASOF" else 2


if __name__ == "__main__":
    raise SystemExit(main())
