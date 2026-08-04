#!/usr/bin/env python3
"""Build target-only calibration evidence and rejected promotion decisions.

This command reads frozen files and writes an audit JSON only.  It never opens
the target or shared-live SQLite database and never imports an order gateway.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration_evidence import (
    build_blocked_calibration_evidence,
    build_calibration_evidence,
    load_historical_rebuild_model_rows,
)
from bayes_basket_target.contracts import (
    ContractError,
    assert_timestamp_not_future,
    content_sha256,
)
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json
from bayes_basket_target.source_policy import normalize_station_lane_map
from bayes_basket_target.strict_asof_replay import maturity_key_for_model_run
from bayes_basket_target.historical_model_rows import select_preferred_model_rows


DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_calibration_evidence.json"
DEFAULT_CANONICAL = ROOT / "output/audits/bayes_basket_target_canonical_observation_evidence.json"
DEFAULT_ET_HISTORY = ROOT / "output/audits/bayes_basket_target_et_history_evidence.json"
DEFAULT_TIMEZONE_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_STATION_LANE_MAP = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"


def _station_lane_map(path: Path) -> dict[str, str]:
    payload = load_bounded_json(path.expanduser().resolve())
    stations = payload.get("stations") if isinstance(payload, Mapping) else payload
    if not isinstance(stations, Mapping):
        raise ContractError("calibration_station_lane_map_mapping_required")
    return normalize_station_lane_map(stations)


def _settlement_timezone(
    registry_path: Path,
    station_scope: set[str] | None,
) -> tuple[str, dict[str, str]]:
    payload = load_bounded_json(registry_path.expanduser().resolve())
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ContractError("calibration_timezone_registry_rows_missing")
    by_station: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        timezone_name = str(row.get("settlement_timezone") or "").strip()
        if not station or not timezone_name:
            continue
        previous = by_station.setdefault(station, timezone_name)
        if previous != timezone_name:
            raise ContractError(
                "calibration_registry_settlement_timezone_not_unique"
            )
    selected = station_scope or set(by_station)
    missing = sorted(selected - set(by_station))
    if missing:
        raise ContractError(
            "calibration_registry_settlement_timezone_missing:" + ",".join(missing)
        )
    timezones = {by_station[station] for station in selected}
    # A full 45-station packet is intentionally multi-timezone.  Keep a valid
    # scalar compatibility value for legacy builders; the explicit map is the
    # authority passed to any station-aware stage.
    scalar_timezone = next(iter(timezones)) if len(timezones) == 1 else "UTC"
    return scalar_timezone, {
        station: by_station[station] for station in selected
    }


def _current_utc() -> str:
    """Return the invocation clock for an omitted strict-as-of argument."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_as_of(as_of_utc: str, et_history_evidence_path: Path | None) -> str:
    """Use the settlement-history receipt clock when the caller omits a clock.

    Settlement history and calibration are one strict-as-of boundary. Re-reading a
    READY receipt with a newly generated wall-clock timestamp would create a
    false mismatch between the two evidence objects.
    """

    explicit = str(as_of_utc or "").strip()
    if explicit:
        return explicit
    if et_history_evidence_path is not None:
        try:
            payload = load_bounded_json(Path(et_history_evidence_path).expanduser().resolve())
            if isinstance(payload, dict) and payload.get("schema") == "bayes_basket_target_et_history_evidence_v1":
                receipt_as_of = str(payload.get("as_of_utc") or "").strip()
                if receipt_as_of:
                    return receipt_as_of
        except (OSError, ValueError, TypeError):
            pass
    return _current_utc()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _native_complete_model_rows(
    payload: object,
    *,
    station_timezone_map: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    """Flatten only complete native daily curves into strict-as-of rows.

    The decoder evidence deliberately exposes incomplete curves as blocked;
    they never enter this list.  This keeps a transport point from silently
    becoming a daily maximum in calibration.
    """

    if not isinstance(payload, dict):
        raise ContractError("native_model_decode_evidence_mapping_required")
    if payload.get("schema") != "bayes_basket_target_native_model_arrival_decode_evidence_v1":
        raise ContractError("native_model_decode_evidence_schema_invalid")
    checks = payload.get("checks")
    if not isinstance(checks, dict) or checks.get("writes_production_db") is not False or checks.get("calls_order_gateway") is not False:
        raise ContractError("native_model_decode_evidence_boundary_invalid")
    curves = payload.get("curves")
    if not isinstance(curves, dict):
        raise ContractError("native_model_decode_curves_missing")
    eligible = curves.get("eligible_model_rows") or []
    if not isinstance(eligible, list):
        raise ContractError("native_model_decode_eligible_rows_invalid")
    complete_count = int(curves.get("complete_curve_count") or 0)
    if complete_count != len(eligible):
        raise ContractError("native_model_decode_complete_curve_count_mismatch")
    rows: list[dict[str, object]] = []
    for item in eligible:
        if not isinstance(item, dict):
            raise ContractError("native_model_decode_model_row_invalid")
        for point in item.get("temperature_points") or []:
            if not isinstance(point, dict):
                raise ContractError("native_model_decode_temperature_point_invalid")
            station = str(item.get("station_id") or "").strip().upper()
            expected_timezone = str((station_timezone_map or {}).get(station) or item.get("target_timezone") or payload.get("target_timezone") or "").strip()
            event_date = str(item.get("event_date") or "").strip()
            valid_time = str(point.get("valid_time_utc") or "").strip()
            if station_timezone_map and expected_timezone and valid_time:
                try:
                    parsed = datetime.fromisoformat(valid_time.replace("Z", "+00:00"))
                    derived = parsed.astimezone(ZoneInfo(expected_timezone)).date().isoformat()
                    event_date = derived
                except (TypeError, ValueError):
                    pass
            rows.append({
                "station_id": station,
                "event_date": event_date,
                "target_timezone": expected_timezone,
                "provider_id": item.get("provider_id"),
                "model_run_initialization_utc": item.get("model_run_initialization_utc"),
                "valid_time_utc": point.get("valid_time_utc"),
                "temperature_2m_c": point.get("temperature_2m_c"),
                "raw_message_sha256": point.get("raw_message_sha256"),
                "receipt_sha256": point.get("receipt_sha256"),
                "published_at_utc": point.get("published_at_utc"),
                "received_at_utc": point.get("received_at_utc"),
                "capture_mode": "prospective",
                "production_calibration_eligible": True,
            })
    return rows


def _bind_native_source_quality(evidence: dict[str, Any], native_payload: object) -> None:
    """Make the receipt name the native decode evidence actually used."""

    quality = evidence.get("source_quality")
    if not isinstance(quality, dict):
        return
    native = isinstance(native_payload, dict)
    if not native:
        return
    quality["model_source"] = "NATIVE_MODEL_DECODE_EVIDENCE"
    quality["model_arrival_source"] = "NATIVE_MODEL_DECODE_EVIDENCE"
    quality["model_arrival_audit"] = {
        "decoded_point_count": int(native_payload.get("decoded_point_count") or 0),
        "complete_curve_count": int((native_payload.get("curves") or {}).get("complete_curve_count") or 0),
        "promotion_allowed": False,
    }


def _normalize_station_scope(station_ids: set[str] | None) -> set[str] | None:
    if station_ids is None:
        return None
    normalized = {str(item).strip().upper() for item in station_ids if str(item).strip()}
    if not normalized:
        raise ValueError("station_scope_empty")
    return normalized


def _scope_canonical_evidence(
    payload: object,
    *,
    station_scope: set[str] | None,
    source_sha256: str = "",
) -> dict[str, Any] | None:
    if payload is None or station_scope is None:
        return payload if isinstance(payload, dict) else None
    if not isinstance(payload, dict):
        raise ContractError("canonical_observation_evidence_mapping_required")
    scoped = dict(payload)
    scoped["observations"] = [
        dict(item)
        for item in payload.get("observations") or []
        if isinstance(item, dict)
        and str(item.get("station_id") or "").strip().upper() in station_scope
    ]
    scoped["truth_rows"] = [
        dict(item)
        for item in payload.get("truth_rows") or []
        if isinstance(item, dict)
        and str(item.get("station_id") or "").strip().upper() in station_scope
    ]
    included_source_lanes = {
        str(item.get("source_lane") or "").strip().lower()
        for item in (
            list(scoped["observations"]) + list(scoped["truth_rows"])
        )
        if str(item.get("source_lane") or "").strip().lower()
        in {"amsc", "awc"}
    }
    scoped["scope"] = {
        "mode": "allowlist",
        "included_station_ids": sorted(station_scope),
        "excluded_source_lanes": sorted(
            {"amsc", "awc"} - included_source_lanes
        ),
        "source_evidence_sha256": source_sha256,
    }
    scoped["receipt_sha256"] = content_sha256(
        {key: value for key, value in scoped.items() if key != "receipt_sha256"}
    )
    return scoped


def _filter_model_rows(
    rows: list[dict[str, Any]],
    station_scope: set[str] | None,
) -> list[dict[str, Any]]:
    if station_scope is None:
        return rows
    return [
        row for row in rows
        if str(row.get("station_id") or "").strip().upper() in station_scope
    ]


def _output_scope(
    *,
    station_scope: set[str] | None,
    canonical_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    canonical_scope = (
        canonical_evidence.get("scope")
        if isinstance(canonical_evidence, Mapping)
        and isinstance(canonical_evidence.get("scope"), Mapping)
        else {}
    )
    excluded = canonical_scope.get("excluded_source_lanes")
    return {
        "mode": "allowlist" if station_scope is not None else "full_input",
        "included_station_ids": sorted(station_scope or set()),
        "excluded_source_lanes": sorted({
            str(item).strip().lower()
            for item in (excluded if isinstance(excluded, list) else [])
            if str(item).strip().lower() in {"amsc", "awc"}
        }),
    }


def _partition_model_rows_by_registry_scope(
    native_rows: list[dict[str, object]],
    historical_rows: list[dict[str, Any]],
    *,
    station_timezone_map: Mapping[str, str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Partition rows by the immutable registry timezone and maturity key.

    ``evaluate_strict_asof_replay`` intentionally accepts one maturity scope
    per packet.  A full 45-station refresh can contain both a D-1 packet and a
    same-UTC-date 00Z packet (and, independently, multiple settlement zones).
    Keeping those rows in one replay would turn a valid packet into the
    misleading ``strict_asof_maturity_not_uniform`` blocker.  This helper only
    partitions already-loaded rows; it never drops or rewrites a row.
    """

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for source_name, rows in (
        ("native_model_rows", native_rows),
        ("historical_model_rows", historical_rows),
    ):
        for row in rows:
            station = str(row.get("station_id") or "").strip().upper()
            timezone_name = str(station_timezone_map.get(station) or "").strip()
            event_date = str(
                row.get("event_date") or row.get("target_local_date") or ""
            ).strip()
            run_initialization = str(
                row.get("model_run_initialization_utc")
                or row.get("run_initialization_utc")
                or ""
            ).strip()
            if not station or not timezone_name or not event_date or not run_initialization:
                raise ContractError("calibration_model_scope_identity_missing")
            maturity = maturity_key_for_model_run(
                run_initialization,
                event_date,
                target_timezone=timezone_name,
            )
            key = (timezone_name, maturity)
            packet = groups.setdefault(
                key,
                {
                    "native_model_rows": [],
                    "historical_model_rows": [],
                    "station_ids": set(),
                },
            )
            packet[source_name].append(row)
            packet["station_ids"].add(station)
    return groups


def _aggregate_multi_timezone_evidence(
    grouped: list[Mapping[str, Any]],
    *,
    as_of_utc: str,
    station_timezone_map: Mapping[str, str],
    station_scope: set[str] | None,
    input_artifacts: Mapping[str, Mapping[str, Any]],
    canonical_evidence: Mapping[str, Any] | None,
    et_history_summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine station-timezone calibration packets without mixed-clock joins."""

    list_keys = (
        "station_registry", "truth_evidence", "calibration_model_rows",
        "model_arrival_evidence", "residual_evidence", "calibration_artifacts",
        "ensemble_artifacts", "ensemble_selection_evidence",
        "ensemble_selection_reports", "oos_reports", "promotion_decisions",
        "calibration_activations",
    )
    merged_lists = {
        key: [
            item
            for packet in grouped
            for item in (packet.get(key) or [])
            if isinstance(item, Mapping)
        ]
        for key in list_keys
    }
    windows = [packet.get("calibration_window") or {} for packet in grouped]
    scope_counts: dict[str, int] = {}
    for window in windows:
        raw_counts = window.get("scope_sample_counts")
        if isinstance(raw_counts, Mapping):
            scope_counts.update({str(key): int(value or 0) for key, value in raw_counts.items()})
    states = [str(window.get("state") or "BLOCKED_EVIDENCE") for window in windows]
    if any(state == "CANDIDATE_SAMPLE_READY" for state in states):
        window_state = "CANDIDATE_SAMPLE_READY"
    elif any(state in {"PARTIAL_SCOPE_CANDIDATE_READY", "INSUFFICIENT_SCOPE_SAMPLES"} for state in states):
        window_state = "PARTIAL_SCOPE_CANDIDATE_READY"
    else:
        window_state = "INSUFFICIENT_CLOSED_SETTLEMENT_HISTORY"
    checks_by_group = [packet.get("checks") or {} for packet in grouped]
    checks = {
        "strict_asof_replay_pass": bool(checks_by_group) and all(item.get("strict_asof_replay_pass") is True for item in checks_by_group),
        "model_lineage_complete": bool(checks_by_group) and all(item.get("model_lineage_complete") is True for item in checks_by_group),
        "truth_lineage_complete": bool(checks_by_group) and all(item.get("truth_lineage_complete") is True for item in checks_by_group),
        "residual_count_positive": bool(merged_lists["residual_evidence"]),
        "calibration_window_sufficient": any(item.get("calibration_window_sufficient") is True for item in checks_by_group),
        "candidate_artifact_count_positive": bool(merged_lists["calibration_artifacts"]),
        "all_candidates_unpromoted": all(item.get("all_candidates_unpromoted", True) for item in checks_by_group),
        "published_receive_timestamps_complete": bool(merged_lists["calibration_model_rows"]) and all(item.get("published_receive_timestamps_complete") is True for item in checks_by_group),
        "truth_source_canonical_ingress": canonical_evidence is not None,
        "multi_model_available": any(item.get("multi_model_available") is True for item in checks_by_group),
        "candidate_model_pool": sorted({str(model) for item in checks_by_group for model in (item.get("candidate_model_pool") or [])}),
        "model_provider_set_complete": all(item.get("model_provider_set_complete") is True for item in checks_by_group) if checks_by_group else False,
        "oos_promotion_gate_complete": False,
        "promotion_allowed": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": sum(int(item.get("orders_submitted") or 0) for item in checks_by_group),
    }
    failure_reasons = sorted({
        str((packet.get("source_quality") or {}).get("failure_reason") or "")
        for packet in grouped
        if str((packet.get("source_quality") or {}).get("failure_reason") or "")
    })
    source_quality_rows = [
        packet.get("source_quality") or {}
        for packet in grouped
        if isinstance(packet.get("source_quality") or {}, Mapping)
    ]
    model_arrival_sources = {
        str(item.get("model_arrival_source") or "").strip()
        for item in source_quality_rows
        if str(item.get("model_arrival_source") or "").strip()
    }
    scope = _output_scope(
        station_scope=station_scope,
        canonical_evidence=canonical_evidence,
    )
    if station_scope is None:
        scope["mode"] = "full_lane_map"
        scope["included_station_ids"] = sorted(station_timezone_map)
    group_receipts = [
        dict(packet.get("_packet_scope") or {})
        for packet in grouped
        if isinstance(packet.get("_packet_scope"), Mapping)
    ]
    identity = {
        "schema": "bayes_basket_target_calibration_evidence_v1",
        "as_of_utc": as_of_utc,
        "target_timezone": "REGISTRY_BOUND_PER_STATION",
        "station_timezones": dict(sorted(station_timezone_map.items())),
        "settlement_definition": "official_daily_tmax_c",
        "strict_asof_replay_sha256": content_sha256({
            "group_replay_sha256": [str(packet.get("strict_asof_replay_sha256") or "") for packet in grouped],
        }),
        "input_artifacts": dict(input_artifacts),
        "packet_groups": group_receipts,
        **merged_lists,
        "calibration_window": {
            "closed_settlement_event_day_count": sum(int(item.get("closed_settlement_event_day_count") or 0) for item in windows),
            "closed_settlement_event_dates": sorted({str(date) for item in windows for date in (item.get("closed_settlement_event_dates") or [])}),
            "joined_station_day_count": sum(int(item.get("joined_station_day_count") or 0) for item in windows),
            "residual_count": len(merged_lists["residual_evidence"]),
            "scope_count": len(scope_counts),
            "scope_sample_counts": dict(sorted(scope_counts.items())),
            "minimum_scope_sample_n": 4,
            "minimum_bias_correction_sample_n": 20,
            "eligible_scope_count": sum(1 for count in scope_counts.values() if count >= 4),
            "bias_correction_eligible_scope_count": sum(1 for count in scope_counts.values() if count >= 20),
            "insufficient_scope_count": sum(1 for count in scope_counts.values() if count < 4),
            "insufficient_scope_keys": sorted(key for key, count in scope_counts.items() if count < 4),
            "state": window_state,
            "promotion_allowed": False,
        },
        "et_history_evidence": dict(et_history_summary),
        "scope": scope,
        "oos_reports": merged_lists["oos_reports"],
        "checks": checks,
        "source_quality": {
            "observation_source": "CANONICAL_AMSC_AWC_INGRESS" if canonical_evidence is not None else "NOT_EVALUATED",
            "model_source": "REGISTRY_SCOPED_MULTI_TIMEZONE_PACKETS",
            "model_arrival_source": (
                "NATIVE_MODEL_DECODE_EVIDENCE"
                if "NATIVE_MODEL_DECODE_EVIDENCE" in model_arrival_sources
                else "PROSPECTIVE_NATIVE_PROVIDER_RECEIPTS"
                if merged_lists["model_arrival_evidence"]
                else "NOT_CAPTURED"
            ),
            "model_arrival_audit": {
                # Every packet references the same native decode receipt; do
                # not multiply its source counts by the number of timezone /
                # maturity packets.
                "decoded_point_count": max(
                    [
                    int((item.get("model_arrival_audit") or {}).get("decoded_point_count") or 0)
                    for item in source_quality_rows
                    ]
                    or [0]
                ),
                "complete_curve_count": max(
                    [
                    int((item.get("model_arrival_audit") or {}).get("complete_curve_count") or 0)
                    for item in source_quality_rows
                    ]
                    or [0]
                ),
                "promotion_allowed": False,
            },
            "model_provider_set_blockers": [
                blocker
                for packet in grouped
                for blocker in ((packet.get("source_quality") or {}).get("model_provider_set_blockers") or [])
            ][:500],
            "status": "CANONICAL_TRUTH_WITH_PROMOTION_CLOSED",
            "failure_reasons": failure_reasons,
            "canonical_observation_evidence_sha256": canonical_evidence.get("receipt_sha256") if isinstance(canonical_evidence, Mapping) else None,
        },
        "boundary": {
            "offline_research_only": True,
            "uses_production_database": False,
            "writes_production_state": False,
            "reads_probability_state": False,
            "reads_market_state": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    identity["evidence_sha256"] = content_sha256(identity)
    return {**identity, "receipt_sha256": content_sha256(identity)}


def run(*, output_path: Path = DEFAULT_OUTPUT, as_of_utc: str = "", canonical_evidence_path: Path | None = DEFAULT_CANONICAL, model_arrival_evidence_path: Path | None = None, native_model_decode_evidence_path: Path | None = None, et_history_evidence_path: Path | None = DEFAULT_ET_HISTORY, historical_rebuild_evidence_paths: list[Path] | None = None, station_ids: set[str] | None = None, timezone_registry_path: Path = DEFAULT_TIMEZONE_REGISTRY, station_lane_map_path: Path = DEFAULT_STATION_LANE_MAP) -> dict[str, object]:
    station_scope = _normalize_station_scope(station_ids)
    station_lane_map = _station_lane_map(station_lane_map_path)
    if station_scope is not None:
        missing_lane_stations = sorted(station_scope - set(station_lane_map))
        if missing_lane_stations:
            raise ContractError(
                "calibration_station_lane_scope_missing:" + ",".join(missing_lane_stations)
            )
    target_timezone, _timezone_map = _settlement_timezone(
        timezone_registry_path,
        station_scope,
    )
    # Historical rebuild packets are retained at full 45-station scope.  A
    # station-scoped calibration must decode them with the complete registry
    # map first and only then apply the allowlist; otherwise a row for an
    # excluded station is mistaken for a missing timezone authority.
    _, _all_timezone_map = _settlement_timezone(timezone_registry_path, None)
    as_of = _resolve_as_of(as_of_utc, et_history_evidence_path)
    as_of = assert_timestamp_not_future(as_of)
    output = Path(output_path).expanduser().resolve()
    canonical_evidence = None
    model_arrival_evidence = None
    native_model_decode_evidence = None
    canonical_input_artifact: dict[str, str] = {}
    if canonical_evidence_path is not None:
        canonical_path = Path(canonical_evidence_path).expanduser().resolve()
        if not canonical_path.is_file():
            raise FileNotFoundError(str(canonical_path))
        source_canonical = load_bounded_json(canonical_path)
        canonical_evidence = _scope_canonical_evidence(
            source_canonical,
            station_scope=station_scope,
            source_sha256=_sha256(canonical_path),
        )
        canonical_input_artifact = {
            "path": str(canonical_path),
            "sha256": _sha256(canonical_path),
        }
    model_arrival_input_artifact: dict[str, str] = {}
    if model_arrival_evidence_path is not None:
        arrival_path = Path(model_arrival_evidence_path).expanduser().resolve()
        if not arrival_path.is_file():
            raise FileNotFoundError(str(arrival_path))
        model_arrival_evidence = load_bounded_json(arrival_path)
        model_arrival_input_artifact = {
            "path": str(arrival_path),
            "sha256": _sha256(arrival_path),
        }
    native_model_decode_input_artifact: dict[str, str] = {}
    native_model_rows: list[dict[str, object]] = []
    if native_model_decode_evidence_path is not None:
        native_path = Path(native_model_decode_evidence_path).expanduser().resolve()
        if not native_path.is_file():
            raise FileNotFoundError(str(native_path))
        native_model_decode_evidence = load_bounded_json(native_path)
        native_model_rows = _filter_model_rows(
            _native_complete_model_rows(
                native_model_decode_evidence,
                station_timezone_map=_timezone_map,
            ),
            station_scope,
        )
        native_model_decode_input_artifact = {
            "path": str(native_path),
            "sha256": _sha256(native_path),
        }
    model_rows = native_model_rows
    historical_model_rows: list[dict[str, Any]] = []
    historical_input_artifacts: dict[str, dict[str, str]] = {}
    for index, raw_path in enumerate(historical_rebuild_evidence_paths or []):
        historical_path = Path(raw_path).expanduser().resolve()
        if not historical_path.is_file():
            raise FileNotFoundError(str(historical_path))
        historical_payload = load_bounded_json(historical_path)
        historical_model_rows.extend(
            _filter_model_rows(
                load_historical_rebuild_model_rows(
                    historical_payload,
                    as_of_utc=as_of,
                    target_timezone=target_timezone,
                    station_timezone_map=_all_timezone_map,
                ),
                station_scope,
            )
        )
        historical_input_artifacts[f"historical_rebuild_evidence_{index}"] = {
            "path": str(historical_path),
            "sha256": _sha256(historical_path),
        }
    canonical_observation_rows = []
    if isinstance(canonical_evidence, dict):
        canonical_observation_rows = [
            dict(item)
            for item in canonical_evidence.get("observations") or []
            if isinstance(item, dict)
        ]
    input_artifacts = {
        **({"canonical_observation_evidence": canonical_input_artifact} if canonical_input_artifact else {}),
        **({"model_arrival_evidence": model_arrival_input_artifact} if model_arrival_input_artifact else {}),
        **({"native_model_decode_evidence": native_model_decode_input_artifact} if native_model_decode_input_artifact else {}),
        **historical_input_artifacts,
    }
    et_history_summary: dict[str, object] = {}
    if et_history_evidence_path is None:
        et_history_summary = {"state": "MISSING", "promotion_allowed": False}
    else:
        et_history_path = Path(et_history_evidence_path).expanduser().resolve()
        if not et_history_path.is_file():
            et_history_summary = {"state": "MISSING", "path": str(et_history_path), "promotion_allowed": False}
        else:
            et_history_payload = load_bounded_json(et_history_path)
            if not isinstance(et_history_payload, dict) or et_history_payload.get("schema") != "bayes_basket_target_et_history_evidence_v1":
                et_history_summary = {"state": "INVALID", "path": str(et_history_path), "promotion_allowed": False}
            else:
                et_history_summary = {
                    "state": str(et_history_payload.get("state") or "UNKNOWN"),
                    "as_of_utc": str(et_history_payload.get("as_of_utc") or ""),
                    "path": str(et_history_path),
                    "receipt_sha256": str(et_history_payload.get("receipt_sha256") or ""),
                    "joined_station_days": int((et_history_payload.get("input_counts") or {}).get("joined_station_days") or 0),
                    "promotion_allowed": False,
                }
                expected_scope = {
                    "mode": "allowlist" if station_scope is not None else "full_lane_map",
                    "included_station_ids": sorted(station_scope or set()),
                }
                actual_scope = et_history_payload.get("scope")
                if station_scope is not None and (
                    not isinstance(actual_scope, dict)
                    or actual_scope.get("mode") != "allowlist"
                    or sorted(actual_scope.get("included_station_ids") or [])
                    != expected_scope["included_station_ids"]
                ):
                    et_history_summary["state"] = "SCOPE_MISMATCH"
                    et_history_summary["promotion_allowed"] = False
                et_history_summary["scope"] = actual_scope or expected_scope
                if et_history_summary["state"] == "READY_FOR_STRICT_ASOF" and et_history_summary["as_of_utc"] != as_of:
                    et_history_summary["state"] = "AS_OF_MISMATCH"
                    et_history_summary["promotion_allowed"] = False
            input_artifacts["et_history_evidence"] = {
                "path": str(et_history_path),
                "sha256": _sha256(et_history_path),
            }
    if et_history_summary.get("state") != "READY_FOR_STRICT_ASOF":
        evidence = build_blocked_calibration_evidence(
            as_of_utc=as_of,
            target_timezone=target_timezone,
            input_artifacts=input_artifacts,
            canonical_observation_evidence=canonical_evidence,
            model_arrival_evidence=model_arrival_evidence,
            reason_code="et_history_evidence_" + str(et_history_summary.get("state") or "UNKNOWN").lower(),
        )
        evidence["et_history_evidence"] = et_history_summary
        evidence["scope"] = _output_scope(
            station_scope=station_scope,
            canonical_evidence=canonical_evidence,
        )
        _bind_native_source_quality(evidence, native_model_decode_evidence)
        # Bind the summary and its source hash into the durable receipt after
        # constructing the standard blocked shape.
        evidence["receipt_sha256"] = content_sha256(
            {key: value for key, value in evidence.items() if key != "receipt_sha256"}
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        write_bounded_json(output, evidence)
        return {"schema": evidence["schema"], "output": str(output), "receipt_sha256": evidence["receipt_sha256"], "calibration_artifacts": 0, "ensemble_artifacts": 0, "promotion_allowed": False}
    def _build_packet(
        packet_native_rows: list[dict[str, object]],
        packet_historical_rows: list[dict[str, Any]],
        *,
        packet_timezone: str,
        packet_maturity: str,
        packet_stations: set[str],
    ) -> dict[str, Any]:
        """Build one immutable timezone/maturity calibration packet."""

        packet_canonical = _scope_canonical_evidence(
            canonical_evidence,
            station_scope=packet_stations,
            source_sha256=(canonical_input_artifact or {}).get("sha256", ""),
        )
        packet = build_calibration_evidence(
            packet_native_rows,
            [],
            as_of_utc=as_of,
            target_timezone=packet_timezone,
            input_artifacts=input_artifacts,
            canonical_observation_evidence=packet_canonical,
            model_arrival_evidence=model_arrival_evidence,
            historical_model_rows=packet_historical_rows,
            station_lane_map=station_lane_map,
            station_timezone_map={station: packet_timezone for station in sorted(packet_stations)},
        )
        packet["et_history_evidence"] = et_history_summary
        packet["scope"] = _output_scope(
            station_scope=packet_stations,
            canonical_evidence=packet_canonical,
        )
        packet["_packet_scope"] = {
            "target_timezone": packet_timezone,
            "maturity_key": packet_maturity,
            "station_ids": sorted(packet_stations),
            "calibration_model_row_count": len(packet.get("calibration_model_rows") or []),
            "residual_count": len(packet.get("residual_evidence") or []),
            "state": str((packet.get("calibration_window") or {}).get("state") or "BLOCKED_EVIDENCE"),
        }
        _bind_native_source_quality(packet, native_model_decode_evidence)
        packet["receipt_sha256"] = content_sha256(
            {key: value for key, value in packet.items() if key != "receipt_sha256"}
        )
        return packet

    partition_error: ContractError | None = None
    grouped_rows: dict[tuple[str, str], dict[str, Any]] = {}
    if model_rows or historical_model_rows:
        try:
            # The source packets intentionally retain every raw lineage, but
            # strict replay must first apply the same deterministic preferred
            # run selection used by the calibration builder.  Older retained
            # runs can carry a maturity outside the current packet; passing
            # those duplicates into the partitioner would create a false
            # ``model_run_maturity_drift`` blocker before selection occurs.
            preferred_rows = select_preferred_model_rows(
                [*model_rows, *historical_model_rows]
            )
            grouped_rows = _partition_model_rows_by_registry_scope(
                preferred_rows,
                [],
                station_timezone_map=_timezone_map,
            )
        except ContractError as exc:
            # Do not drop malformed rows in order to make a packet appear
            # uniform.  Fall through to the normal blocked receipt instead.
            partition_error = exc

    try:
        if grouped_rows and partition_error is None:
            packet_list: list[dict[str, Any]] = []
            selected_stations = set(_timezone_map)
            for (packet_timezone, packet_maturity), rows in sorted(grouped_rows.items()):
                packet_stations = {
                    station
                    for station in selected_stations
                    if _timezone_map.get(station) == packet_timezone
                }
                packet_stations.update(rows["station_ids"])
                packet = _build_packet(
                    list(rows["native_model_rows"]),
                    list(rows["historical_model_rows"]),
                    packet_timezone=packet_timezone,
                    packet_maturity=packet_maturity,
                    packet_stations=packet_stations,
                )
                packet_list.append(packet)
            evidence = _aggregate_multi_timezone_evidence(
                packet_list,
                as_of_utc=as_of,
                station_timezone_map=_timezone_map,
                station_scope=station_scope,
                input_artifacts=input_artifacts,
                canonical_evidence=canonical_evidence,
                et_history_summary=et_history_summary,
            )
        else:
            # Canonical observations are parsed by the builder from the
            # canonical evidence packet.  Do not pass those raw
            # ``event_date_local`` rows again as legacy cross-check rows,
            # whose contract requires ``event_date``; that would create a
            # false event_date_missing blocker and double-count the source.
            if partition_error is not None:
                raise partition_error
            evidence = build_calibration_evidence(
                model_rows,
                [],
                as_of_utc=as_of,
                target_timezone=target_timezone,
                input_artifacts=input_artifacts,
                canonical_observation_evidence=canonical_evidence,
                model_arrival_evidence=model_arrival_evidence,
                historical_model_rows=historical_model_rows,
                station_lane_map=station_lane_map,
                station_timezone_map=_timezone_map,
            )
            evidence["et_history_evidence"] = et_history_summary
            evidence["scope"] = _output_scope(
                station_scope=station_scope,
                canonical_evidence=canonical_evidence,
            )
            _bind_native_source_quality(evidence, native_model_decode_evidence)
            evidence["receipt_sha256"] = content_sha256(
                {key: value for key, value in evidence.items() if key != "receipt_sha256"}
            )
    except ContractError as exc:
        evidence = build_blocked_calibration_evidence(
            as_of_utc=as_of,
            target_timezone=target_timezone,
            input_artifacts=input_artifacts,
            canonical_observation_evidence=canonical_evidence,
            model_arrival_evidence=model_arrival_evidence,
            reason_code=f"{type(exc).__name__}:{exc}",
        )
        evidence["et_history_evidence"] = et_history_summary
        evidence["scope"] = _output_scope(
            station_scope=station_scope,
            canonical_evidence=canonical_evidence,
        )
        _bind_native_source_quality(evidence, native_model_decode_evidence)
        evidence["receipt_sha256"] = content_sha256(
            {key: value for key, value in evidence.items() if key != "receipt_sha256"}
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    write_bounded_json(output, evidence)
    return {"schema": evidence["schema"], "output": str(output), "receipt_sha256": evidence["receipt_sha256"], "calibration_artifacts": len(evidence["calibration_artifacts"]), "ensemble_artifacts": len(evidence["ensemble_artifacts"]), "promotion_allowed": evidence["checks"]["promotion_allowed"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--canonical-evidence", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--without-canonical-evidence", action="store_true")
    parser.add_argument("--model-arrival-evidence", type=Path)
    parser.add_argument("--native-model-decode-evidence", type=Path)
    parser.add_argument("--et-history-evidence", type=Path, default=DEFAULT_ET_HISTORY)
    parser.add_argument(
        "--timezone-registry",
        type=Path,
        default=DEFAULT_TIMEZONE_REGISTRY,
    )
    parser.add_argument(
        "--station-lane-map",
        type=Path,
        default=DEFAULT_STATION_LANE_MAP,
    )
    parser.add_argument(
        "--historical-rebuild-evidence",
        type=Path,
        action="append",
        help="Official closed-ET-day rebuild packet; repeat once per provider packet.",
    )
    parser.add_argument(
        "--as-of-utc",
        default="",
        help="Strict decision clock; defaults to the current UTC at invocation time.",
    )
    parser.add_argument(
        "--station-id",
        action="append",
        help="Restrict this isolated calibration candidate to registered stations; repeatable.",
    )
    args = parser.parse_args()
    print(json.dumps(run(
        output_path=args.output,
        as_of_utc=args.as_of_utc,
        canonical_evidence_path=None if args.without_canonical_evidence else args.canonical_evidence,
        model_arrival_evidence_path=args.model_arrival_evidence,
        native_model_decode_evidence_path=args.native_model_decode_evidence,
        et_history_evidence_path=args.et_history_evidence,
        historical_rebuild_evidence_paths=list(args.historical_rebuild_evidence or []),
        station_ids={
            str(item).strip().upper()
            for item in (args.station_id or [])
            if str(item).strip()
        }
        or None,
        timezone_registry_path=args.timezone_registry,
        station_lane_map_path=args.station_lane_map,
    ), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
