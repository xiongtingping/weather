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
    if len(timezones) != 1:
        raise ContractError(
            "calibration_packet_mixed_settlement_timezones_forbidden"
        )
    return next(iter(timezones)), {
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


def _native_complete_model_rows(payload: object) -> list[dict[str, object]]:
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
            rows.append({
                "station_id": item.get("station_id"),
                "event_date": item.get("event_date"),
                "target_timezone": item.get("target_timezone"),
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
            _native_complete_model_rows(native_model_decode_evidence),
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
    try:
        # Canonical observations are parsed by the builder from the
        # canonical evidence packet.  Do not pass those raw
        # ``event_date_local`` rows again as legacy cross-check rows, whose
        # contract requires ``event_date``; that would create a false
        # event_date_missing blocker and double-count the same source.
        cross_check_rows = []
        evidence = build_calibration_evidence(
            model_rows,
            cross_check_rows,
            as_of_utc=as_of,
            target_timezone=target_timezone,
            input_artifacts=input_artifacts,
            canonical_observation_evidence=canonical_evidence,
            model_arrival_evidence=model_arrival_evidence,
            historical_model_rows=historical_model_rows,
            station_lane_map=station_lane_map,
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
