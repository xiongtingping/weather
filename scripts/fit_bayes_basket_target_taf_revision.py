#!/usr/bin/env python3
"""Fit one strict-time OOS TAF revision artifact from a bounded AWC dataset."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.taf_calibration import fit_taf_revision_artifact  # noqa: E402
from bayes_basket_target.taf_native_ensemble_oos import (  # noqa: E402
    EXACT_JOIN_SCHEMA as TAF_NATIVE_EXACT_JOIN_SCHEMA,
)
from bayes_basket_target.taf_exact_join import TAF_EXACT_JOIN_SCHEMA  # noqa: E402
from scripts.build_historical_taf_p0m_exact_join import (  # noqa: E402
    SCHEMA as HISTORICAL_TAF_P0M_PIPELINE_SCHEMA,
)


FORMAL_STATION_LANE_TRUTH = "FORMAL_STATION_LANE_TRUTH"


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid")
    return result


def _verify_content_hash(raw: Mapping[str, Any], *, name: str) -> str:
    supplied = _sha(f"{name}_content_sha256", raw.get("content_sha256"))
    identity = {
        str(key): value
        for key, value in raw.items()
        if key != "content_sha256"
    }
    if content_sha256(identity) != supplied:
        raise ContractError(f"{name}_content_sha_mismatch")
    return supplied


def _expected_native_tmax(native: Mapping[str, Any]) -> float:
    probabilities_raw = native.get("probabilities")
    centers_raw = native.get("bucket_centers_c")
    if not isinstance(probabilities_raw, Mapping) or not isinstance(centers_raw, Mapping):
        raise ContractError("taf_native_exact_join_probability_fields_missing")
    probabilities = {str(key): _finite("taf_native_probability", value)
                     for key, value in probabilities_raw.items()}
    centers = {str(key): _finite("taf_native_bucket_center", value)
               for key, value in centers_raw.items()}
    if not probabilities or set(probabilities) != set(centers):
        raise ContractError("taf_native_exact_join_probability_support_mismatch")
    if any(value < 0.0 for value in probabilities.values()):
        raise ContractError("taf_native_exact_join_probability_negative")
    if abs(sum(probabilities.values()) - 1.0) > 1e-9:
        raise ContractError("taf_native_exact_join_probability_not_normalized")
    return sum(probabilities[bucket] * centers[bucket] for bucket in probabilities)


def _validate_taf_lineage(sample: Mapping[str, Any], taf: Mapping[str, Any]) -> None:
    """Require formal AWC lineage before fitting executable P0-M evidence."""

    label = str(taf.get("truth_label_authority") or "").strip()
    if label != FORMAL_STATION_LANE_TRUTH:
        raise ContractError("taf_native_formal_awc_label_required")
    if taf.get("reference_only") is not False:
        raise ContractError("taf_native_formal_awc_reference_only_forbidden")
    for field in ("enters_realtime_truth", "enters_p1", "enters_hard_no"):
        if field in taf and taf.get(field) is not False:
            raise ContractError(f"taf_native_formal_awc_{field}_forbidden")
    lineage = sample.get("taf_lineage")
    if not isinstance(lineage, Mapping):
        raise ContractError("taf_native_formal_awc_lineage_missing")
    if str(lineage.get("truth_label_authority") or "").strip() != label:
        raise ContractError("taf_native_formal_awc_lineage_label_mismatch")
    if lineage.get("reference_only") is not False:
        raise ContractError("taf_native_formal_awc_lineage_reference_only_forbidden")
    for field in ("enters_realtime_truth", "enters_p1", "enters_hard_no"):
        if lineage.get(field) is not False:
            raise ContractError(f"taf_native_formal_awc_lineage_{field}_forbidden")


def _native_samples(dataset: Mapping[str, Any], *, station_id: str | None = None) -> list[dict[str, Any]]:
    if dataset.get("schema") != TAF_NATIVE_EXACT_JOIN_SCHEMA:
        raise ContractError("TAF native exact-join dataset schema invalid")
    if dataset.get("source_class") != "strict_oos_native_p0_taf_truth_exact_join":
        raise ContractError("TAF native exact-join source class invalid")
    _verify_content_hash(dataset, name="taf_native_exact_join")
    crossfit_sha = _sha(
        "nwp_crossfit_prediction_artifact_sha256",
        dataset.get("nwp_crossfit_prediction_artifact_sha256"),
    )
    rows_raw = dataset.get("samples")
    if not isinstance(rows_raw, list):
        raise ContractError("taf_native_exact_join_samples_invalid")
    if int(dataset.get("sample_count", -1)) != len(rows_raw):
        raise ContractError("taf_native_exact_join_sample_count_invalid")
    if not rows_raw:
        raise ContractError("taf_native_exact_join_samples_empty")
    selected_station = str(station_id or "").strip().upper() or None
    rows: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for item in rows_raw:
        if not isinstance(item, Mapping):
            raise ContractError("taf_native_exact_join_sample_invalid")
        station = str(item.get("station_id") or "").strip().upper()
        if not station or (selected_station is not None and station != selected_station):
            raise ContractError("taf_native_exact_join_station_scope_mismatch")
        event_date = str(item.get("event_date") or "").strip()
        if not event_date or event_date in seen_dates:
            raise ContractError("taf_native_exact_join_event_date_duplicate_or_missing")
        seen_dates.add(event_date)
        try:
            event_day = date.fromisoformat(event_date)
        except ValueError as exc:
            raise ContractError("taf_native_exact_join_event_date_invalid") from exc
        scope = item.get("calibration_scope")
        native = item.get("native_p0")
        taf = item.get("taf")
        truth = item.get("truth")
        window = item.get("settlement_window")
        if not all(isinstance(value, Mapping) for value in (scope, native, taf, truth, window)):
            raise ContractError("taf_native_exact_join_fields_missing")
        if native.get("prediction_oos") is not True:
            raise ContractError("taf_native_exact_join_prediction_oos_required")
        _sha("taf_native_prediction_content_sha256", native.get("prediction_content_sha256"))
        expected_tmax = _expected_native_tmax(native)
        decision_at = str(native.get("decision_at_utc") or "").strip()
        available_at = str(taf.get("available_at_utc") or "").strip()
        if not decision_at or not available_at:
            raise ContractError("taf_native_exact_join_asof_timestamps_missing")
        trained_through = str(native.get("trained_through_event_date") or "").strip()
        try:
            if date.fromisoformat(trained_through) >= event_day:
                raise ContractError("taf_native_training_leakage")
        except ValueError as exc:
            raise ContractError("taf_native_training_through_date_invalid") from exc
        truth_id = str(truth.get("canonical_truth_id") or "").strip()
        truth_authority = str(truth.get("authority_scope") or "").strip()
        if (
            not truth_id
            or not truth_authority
            or str(truth.get("source_lane") or "").strip().upper() != "AWC"
        ):
            raise ContractError("taf_native_canonical_truth_lineage_required")
        _validate_taf_lineage(item, taf)
        issue_at = str(
            taf.get("issue_time_utc")
            or taf.get("issued_at_utc")
            or available_at
        ).strip()
        sample_id = str(item.get("sample_id") or f"native-taf-{station}-{event_date}").strip()
        rows.append({
            "sample_id": sample_id,
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": str(item.get("settlement_timezone") or "").strip(),
            "season_bin": str(scope.get("season_bin") or "").strip(),
            "lead_bucket": str(scope.get("lead_bucket") or "").strip(),
            "run_cycle": str(scope.get("run_cycle") or "").strip(),
            "settlement_definition": str(scope.get("settlement_definition") or "").strip(),
            "nwp_combination_artifact_sha256": crossfit_sha,
            "nwp_combination_decision_at_utc": decision_at,
            "nwp_combination_prediction_oos": True,
            "taf_issued_at_utc": issue_at,
            "taf_available_at_utc": available_at,
            "truth_closed_at_utc": str(window.get("end_utc") or "").strip(),
            "tx_c": taf.get("tx_c"),
            "truth_tmax_c": truth.get("tmax_c"),
            "cloud_cover_fraction": taf.get("cloud_cover_fraction"),
            "precipitation_probability": taf.get("precipitation_probability"),
            "wind_speed_mps": taf.get("wind_speed_mps"),
            "nwp_combined_tmax_c": expected_tmax,
        })
    return rows


def _pipeline_exact_join(
    dataset: Mapping[str, Any],
    *,
    station_id: str | None,
) -> Mapping[str, Any]:
    if dataset.get("schema") != HISTORICAL_TAF_P0M_PIPELINE_SCHEMA:
        raise ContractError("historical_taf_p0m_pipeline_schema_invalid")
    _verify_content_hash(dataset, name="historical_taf_p0m_pipeline")
    results = dataset.get("results")
    if not isinstance(results, list) or not results:
        raise ContractError("historical_taf_p0m_pipeline_results_missing")
    requested = str(station_id or "").strip().upper() or None
    if requested is None and len(results) != 1:
        raise ContractError("historical_taf_p0m_pipeline_station_selection_ambiguous")
    matches = []
    for result in results:
        if not isinstance(result, Mapping):
            raise ContractError("historical_taf_p0m_pipeline_result_invalid")
        result_station = str(result.get("station_id") or "").strip().upper()
        if requested is None or result_station == requested:
            matches.append(result)
    if len(matches) != 1:
        raise ContractError("historical_taf_p0m_pipeline_station_missing_or_ambiguous")
    selected_result_station = str(matches[0].get("station_id") or "").strip().upper()
    if not selected_result_station:
        raise ContractError("historical_taf_p0m_pipeline_result_station_missing")
    exact_join = matches[0].get("exact_join")
    if not isinstance(exact_join, Mapping):
        raise ContractError("historical_taf_p0m_pipeline_exact_join_missing")
    sample_stations = {
        str(item.get("station_id") or "").strip().upper()
        for item in exact_join.get("samples", [])
        if isinstance(item, Mapping)
    }
    if sample_stations != {selected_result_station}:
        raise ContractError("historical_taf_p0m_pipeline_station_scope_mismatch")
    return exact_join


def _resolve_samples(dataset: Mapping[str, Any], *, station_id: str | None) -> list[dict[str, Any]]:
    schema = dataset.get("schema")
    if schema == TAF_NATIVE_EXACT_JOIN_SCHEMA:
        return _native_samples(dataset, station_id=station_id)
    if schema == HISTORICAL_TAF_P0M_PIPELINE_SCHEMA:
        return _native_samples(_pipeline_exact_join(dataset, station_id=station_id), station_id=station_id)
    return _samples(dataset, station_id=station_id)


def _samples(dataset: Mapping[str, Any], *, station_id: str | None = None) -> list[dict]:
    if dataset.get("schema") != TAF_EXACT_JOIN_SCHEMA:
        raise ValueError("TAF calibration dataset schema invalid")
    if dataset.get("source_class") != "strict_oos_crossfit_exact_join":
        raise ValueError("TAF calibration dataset source class invalid")
    if dataset.get("blockers"):
        raise ValueError("TAF calibration dataset has blockers")
    selected_station = str(station_id or "").strip().upper() or None
    rows: list[dict] = []
    for item in dataset.get("samples", []):
        if selected_station is not None and str(item.get("station_id") or "").strip().upper() != selected_station:
            raise ValueError("TAF calibration station scope mismatch")
        taf = item["taf"]
        truth = item["truth"]
        window = item["settlement_window"]
        scope = item.get("calibration_scope")
        nwp = item.get("nwp_combination")
        if not isinstance(scope, dict):
            raise ValueError(
                "TAF sample missing calibration_scope; raw TAF+truth capture "
                "must first be exact-joined to the P0-M NWP scope"
            )
        if not isinstance(nwp, dict):
            raise ValueError(
                "TAF sample missing nwp_combination; conditional TAF increment "
                "cannot be fitted without a strict OOS P0-M baseline"
            )
        if truth.get("source_lane") != "AWC":
            raise ValueError("TAF calibration truth lane must be AWC")
        rows.append({
            "sample_id": item["sample_id"],
            "station_id": item["station_id"],
            "event_date": item["event_date"],
            "settlement_timezone": item["settlement_timezone"],
            "season_bin": scope["season_bin"],
            "lead_bucket": scope["lead_bucket"],
            "run_cycle": scope["run_cycle"],
            "settlement_definition": scope["settlement_definition"],
            "nwp_combination_artifact_sha256": (
                nwp["artifact_sha256"]
            ),
            "nwp_combination_decision_at_utc": nwp["decision_at_utc"],
            "nwp_combination_prediction_oos": nwp["prediction_oos"],
            "taf_issued_at_utc": taf["issue_time_utc"],
            "taf_available_at_utc": taf["available_time_utc"],
            "truth_closed_at_utc": window["end_utc"],
            "tx_c": taf["tx_c"],
            "truth_tmax_c": truth["tmax_c"],
            "cloud_cover_fraction": taf["cloud_cover_fraction"],
            "precipitation_probability": taf["precipitation_probability"],
            "wind_speed_mps": taf["wind_speed_mps"],
            "nwp_combined_tmax_c": nwp["combined_tmax_c"],
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--dataset", type=Path)
    input_group.add_argument(
        "--pipeline-output",
        type=Path,
        help="Optional historical_taf_native_p0m_exact_join_manifest_v1 output.",
    )
    parser.add_argument("--artifact-output", type=Path, required=True)
    parser.add_argument("--scorecard-output", type=Path, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--activation-id", required=True)
    # No default on purpose.  Resident P0-M selects a TAF artifact by exact
    # equality against the live P0 root's lineage, and P0 stamps month-trigram
    # seasons (DJF/MAM/JJA/SON).  The old default of "summer" produced an
    # artifact no root would ever match once P0 moved off that vocabulary --
    # the stage ran silent rather than failing loudly at fit time.
    parser.add_argument("--season-bin", required=True)
    parser.add_argument("--lead-bucket", default="d0")
    parser.add_argument("--run-cycle", default="18Z")
    parser.add_argument("--settlement-definition", default="official_daily_tmax_c")
    parser.add_argument(
        "--station-id",
        action="append",
        default=[],
        help="Exact single station selection for a pipeline wrapper.",
    )
    parser.add_argument("--valid-from-utc", required=True)
    parser.add_argument("--valid-until-utc", required=True)
    args = parser.parse_args()

    if len(args.station_id) > 1:
        raise SystemExit("--station-id must be supplied at most once")
    station_id = args.station_id[0] if args.station_id else None
    input_path = args.dataset if args.dataset is not None else args.pipeline_output
    dataset = load_bounded_json(input_path.expanduser().resolve())
    if not isinstance(dataset, dict):
        raise SystemExit("dataset must be a JSON object")
    artifact, scorecard = fit_taf_revision_artifact(
        _resolve_samples(dataset, station_id=station_id),
        artifact_id=args.artifact_id,
        activation_id=args.activation_id,
        season_bin=args.season_bin,
        lead_bucket=args.lead_bucket,
        run_cycle=args.run_cycle,
        settlement_definition=args.settlement_definition,
        valid_from_utc=args.valid_from_utc,
        valid_until_utc=args.valid_until_utc,
    )
    write_bounded_json(args.artifact_output.expanduser().resolve(), artifact.to_dict())
    write_bounded_json(args.scorecard_output.expanduser().resolve(), scorecard)
    print(json.dumps({
        "artifact_id": artifact.artifact_id,
        "artifact_content_sha256": artifact.content_sha256,
        "sample_n": artifact.sample_n,
        "effective_sample_n": artifact.effective_sample_n,
        "oos_metrics": dict(artifact.oos_metrics),
        "artifact_output": str(args.artifact_output.expanduser().resolve()),
        "scorecard_output": str(args.scorecard_output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
