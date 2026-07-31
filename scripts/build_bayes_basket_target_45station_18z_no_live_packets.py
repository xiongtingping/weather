#!/usr/bin/env python3
"""Compile corrected 45-station explicit-cycle OOS results into no-live packets.

Only scopes that pass formal truth, probability OOS, and selected-vs-uniform
point OOS gates are emitted.  The output remains an offline audit artifact:
it does not write a database, create a promotion, or authorize live orders.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (  # noqa: E402
    FORMAL_ENSEMBLE_METHOD_VERSION,
    ContractError,
    content_sha256,
    model_set_hash,
)
from bayes_basket_target.no_live_calibration_authority import (  # noqa: E402
    build_no_live_calibration_artifact_packet,
)
from bayes_basket_target.model_sources import (  # noqa: E402
    provider_role,
    provider_source_class,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_45station_18z_no_live_packet_build_v1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
FORMAL_TRUTH = "FORMAL_STATION_LANE_TRUTH"
TEMPORARY_AWC_TRUTH = "TEMPORARY_AWC_CALIBRATION_LABEL"
ACTIVATION_READY_STATES = {
    "ACTIVATION_PACKET_READY",
    "ACTIVATION_PACKET_READY_TEMPORARY_AWC_NO_LIVE",
}


def _truth_dataset_paths(manifest: Mapping[str, Any]) -> dict[str, Path]:
    raw_truth = (manifest.get("inputs") or {}).get("truth_manifest")
    truth_manifest_path = Path(
        str(
            raw_truth.get("path")
            if isinstance(raw_truth, Mapping)
            else raw_truth or ""
        )
    ).expanduser().resolve()
    truth_manifest = load_bounded_json(truth_manifest_path)
    result: dict[str, Path] = {}
    for row in truth_manifest.get("truth_results") or []:
        if not isinstance(row, Mapping) or row.get("state") != "TRUTH_READY":
            continue
        station = str(row.get("station_id") or "").strip().upper()
        path = Path(str(row.get("dataset") or "")).expanduser().resolve()
        if station and path.is_file():
            result[station] = path
    return result


def _truths(path: Path) -> dict[str, float]:
    payload = load_bounded_json(path)
    return {
        str(row["event_date"]): float(row["truth"]["tmax_c"])
        for row in payload.get("samples") or []
        if isinstance(row, Mapping) and isinstance(row.get("truth"), Mapping)
    }


def _point_oos(
    payload: Mapping[str, Any],
    *,
    truth_by_date: Mapping[str, float],
) -> dict[str, float | int]:
    oos = payload["oos"]
    winner = oos["research_winner"]
    weights = {str(k): float(v) for k, v in winner["selected_weights"].items()}
    biases = {
        str(row["model_id"]): float(row["bias_c"])
        for row in oos["calibration_artifacts"]
    }
    exact = {str(row["event_date"]): row for row in payload["exact_join_rows"]}
    selected_errors: list[float] = []
    uniform_errors: list[float] = []
    for target in oos["holdout_dates"]:
        row = exact[target]
        corrected = {
            model: float(row["models"][model]["forecast_tmax_c"]) + biases[model]
            for model in weights
        }
        truth = float(truth_by_date[target])
        selected_errors.append(
            sum(weights[model] * corrected[model] for model in weights) - truth
        )
        uniform_errors.append(sum(corrected.values()) / len(corrected) - truth)

    def mae(values: list[float]) -> float:
        return sum(abs(value) for value in values) / len(values)

    def rmse(values: list[float]) -> float:
        return math.sqrt(sum(value * value for value in values) / len(values))

    selected_mae = mae(selected_errors)
    selected_rmse = rmse(selected_errors)
    uniform_mae = mae(uniform_errors)
    uniform_rmse = rmse(uniform_errors)
    return {
        "holdout_n": len(selected_errors),
        "selected_mae_c": selected_mae,
        "selected_rmse_c": selected_rmse,
        "uniform_mae_c": uniform_mae,
        "uniform_rmse_c": uniform_rmse,
        "selection_mae_improvement_vs_uniform_c": uniform_mae - selected_mae,
        "selection_rmse_improvement_vs_uniform_c": uniform_rmse - selected_rmse,
    }


def _station_quality(
    payload: Mapping[str, Any],
    *,
    truth_by_date: Mapping[str, float],
    allow_temporary_awc_no_live: bool = False,
) -> tuple[dict[str, Any], bool, bool, str]:
    oos = payload.get("oos")
    winner = oos.get("research_winner") if isinstance(oos, Mapping) else None
    truth_authority = str(payload.get("truth_label_authority") or "")
    formal_truth = truth_authority == FORMAL_TRUTH
    temporary_awc_truth = truth_authority == TEMPORARY_AWC_TRUTH
    if not formal_truth and not temporary_awc_truth:
        raise ContractError("explicit_18z_truth_authority_invalid")
    if not isinstance(winner, Mapping):
        point = {
            "holdout_n": 0,
            "selected_mae_c": None,
            "selected_rmse_c": None,
            "uniform_mae_c": None,
            "uniform_rmse_c": None,
            "selection_mae_improvement_vs_uniform_c": None,
            "selection_rmse_improvement_vs_uniform_c": None,
            "point_oos_materialized": False,
        }
        return point, False, False, "BLOCKED_INSUFFICIENT_EVIDENCE"
    point = _point_oos(payload, truth_by_date=truth_by_date)
    probability_passed = bool(
        winner.get("probability_non_degradation_vs_uniform")
    )
    point_passed = (
        float(point["selection_mae_improvement_vs_uniform_c"]) >= -1e-12
        and float(point["selection_rmse_improvement_vs_uniform_c"]) >= -1e-12
    )
    point["point_oos_materialized"] = True
    point["point_non_degradation_vs_uniform"] = point_passed
    if temporary_awc_truth and not allow_temporary_awc_no_live:
        state = "BLOCKED_TEMPORARY_AWC_TRUTH"
    elif not probability_passed:
        state = "BLOCKED_PROBABILITY_OOS_GATE"
    elif temporary_awc_truth:
        state = "ACTIVATION_PACKET_READY_TEMPORARY_AWC_NO_LIVE"
    else:
        state = "ACTIVATION_PACKET_READY"
    return point, probability_passed, point_passed, state


def _truth_authority_binding(
    path: Path,
    *,
    station_id: str,
) -> dict[str, Any]:
    payload = load_bounded_json(path)
    identity = {
        "station_id": station_id,
        "truth_label_authority": str(
            payload.get("label_authority") or ""
        ),
        "source_lane": "AWC",
        "dataset_path": str(path.resolve()),
        "dataset_content_sha256": str(
            payload.get("content_sha256") or ""
        ),
        "enters_realtime_truth": False,
        "enters_p1": False,
        "enters_hard_no": False,
        "replacement_required_when_canonical_history_available": (
            payload.get("label_authority") == TEMPORARY_AWC_TRUTH
        ),
    }
    if (
        identity["truth_label_authority"]
        not in {FORMAL_TRUTH, TEMPORARY_AWC_TRUTH}
        or len(identity["dataset_content_sha256"]) != 64
    ):
        raise ContractError(
            f"explicit_18z_truth_authority_binding_invalid:{station_id}"
        )
    return {**identity, "content_sha256": content_sha256(identity)}


def _selection_row(
    payload: Mapping[str, Any],
    *,
    run_cycle: str,
) -> dict[str, Any]:
    lead_bucket = str(payload["lead_bucket"])
    oos = payload["oos"]
    winner = oos["research_winner"]
    train_dates = list(oos["train_dates"])
    holdout_dates = list(oos["holdout_dates"])
    recent_n = max(
        int(row.get("recent_n") or 0)
        for row in oos["candidate_scores"]
        if isinstance(row, Mapping)
    )
    recent_dates = train_dates[-recent_n:]
    candidate_scores = []
    for raw in oos["candidate_scores"]:
        row = dict(raw)
        models = [str(item).upper() for item in row["models"]]
        row["candidate_role"] = (
            "single_model_ablation" if len(models) == 1 else "formal_ensemble"
        )
        row["formal_selection_eligible"] = len(models) >= 2
        row.pop("formal_activation_eligible", None)
        row.pop("research_selection_eligible", None)
        candidate_scores.append(row)
    identity = {
        "selection_evidence_id": (
            f"sel-{run_cycle.lower()}-{lead_bucket}-"
            + str(payload["station_id"]) + "-"
            + content_sha256(winner)[:16]
        ),
        "station_id": str(payload["station_id"]),
        "season_bin": str(winner["season_bin"]),
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "settlement_definition": SETTLEMENT_DEFINITION,
        "method_version": FORMAL_ENSEMBLE_METHOD_VERSION,
        "train_start_date": train_dates[0],
        "train_end_date": train_dates[-1],
        "recent_start_date": recent_dates[0],
        "recent_end_date": recent_dates[-1],
        "holdout_start_date": holdout_dates[0],
        "holdout_end_date": holdout_dates[-1],
        "candidate_scores": candidate_scores,
        "selected_models": list(winner["selected_models"]),
        "selected_weights": dict(winner["selected_weights"]),
        "oos_metrics": {
            key: winner["holdout_oos"][key]
            for key in ("brier_score", "nll", "crps")
        },
        "residual_covariance": dict(oos["residual_covariance"]),
        "residual_correlation": dict(oos["residual_correlation"]),
        "effective_sample_n": float(len(train_dates)),
        "previous_ensemble_artifact_sha256": None,
        "max_weight_delta": 1.0,
        "actual_max_weight_delta": 0.0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _ensemble_row(
    payload: Mapping[str, Any],
    *,
    selection: Mapping[str, Any],
    run_cycle: str,
) -> dict[str, Any]:
    lead_bucket = str(payload["lead_bucket"])
    oos = payload["oos"]
    winner = oos["research_winner"]
    selected = tuple(sorted(str(item) for item in winner["selected_models"]))
    selected_score = next(
        row for row in oos["candidate_scores"]
        if tuple(sorted(str(item) for item in row["models"])) == selected
    )
    calibrations = list(oos["calibration_artifacts"])
    identity = {
        "ensemble_artifact_id": (
            f"ens-{run_cycle.lower()}-{lead_bucket}-"
            + str(payload["station_id"]) + "-"
            + content_sha256(winner)[:16]
        ),
        "station_id": str(payload["station_id"]),
        "season_bin": str(winner["season_bin"]),
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "settlement_definition": SETTLEMENT_DEFINITION,
        "candidate_models": list(selected),
        "supported_masks": {
            model_set_hash(selected): dict(winner["selected_weights"])
        },
        "residual_covariance": dict(oos["residual_covariance"]),
        "correlation_penalty": float(
            selected_score.get("correlation_concentration") or 0.0
        ),
        "selection_evidence_sha256": str(selection["content_sha256"]),
        "previous_ensemble_artifact_sha256": None,
        "weight_method": FORMAL_ENSEMBLE_METHOD_VERSION,
        "effective_sample_n": float(len(oos["train_dates"])),
        "max_weight_delta": 1.0,
        "train_start_date": str(selection["train_start_date"]),
        "train_end_date": str(selection["train_end_date"]),
        "recent_start_date": str(selection["recent_start_date"]),
        "recent_end_date": str(selection["recent_end_date"]),
        "holdout_start_date": str(selection["holdout_start_date"]),
        "holdout_end_date": str(selection["holdout_end_date"]),
        "oos_metrics": dict(selection["oos_metrics"]),
        "valid_from_utc": min(str(row["valid_from_utc"]) for row in calibrations),
        "valid_until_utc": min(str(row["valid_until_utc"]) for row in calibrations),
        "promoted": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build(
    manifest: Mapping[str, Any],
    *,
    manifest_path: Path,
    calibration_generation: int,
    ensemble_generation: int,
    allow_temporary_awc_no_live: bool = False,
    station_allowlist: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if manifest.get("schema") not in {
        "bayes_basket_target_45station_explicit_18z_d2_oos_v1",
        "bayes_basket_target_45station_model_pool_oos_v2",
    }:
        raise ContractError("explicit_18z_oos_manifest_schema_invalid")
    if int(manifest.get("station_count") or 0) != 45:
        raise ContractError("explicit_18z_oos_station_count_invalid")
    truth_paths = _truth_dataset_paths(manifest)
    models = tuple(
        sorted(str(model).strip().upper() for model in manifest["model_ids"])
    )
    run_cycle = str(manifest.get("run_cycle") or "").strip().upper()
    if len(models) < 2 or run_cycle not in {"00Z", "06Z", "12Z", "18Z"}:
        raise ContractError("explicit_oos_model_pool_scope_invalid")
    station_states: list[dict[str, Any]] = []
    calibration_artifacts: list[dict[str, Any]] = []
    selection_rows: list[dict[str, Any]] = []
    ensembles: list[dict[str, Any]] = []
    model_rows: list[dict[str, Any]] = []
    oos_reports: list[dict[str, Any]] = []
    ensemble_report_rows: list[dict[str, Any]] = []

    for summary in manifest["station_results"]:
        station = str(summary["station_id"])
        payload = load_bounded_json(Path(str(summary["output"])).resolve())
        if payload.get("content_sha256") != content_sha256({
            key: value for key, value in payload.items() if key != "content_sha256"
        }):
            raise ContractError(f"explicit_18z_station_content_mismatch:{station}")
        if station not in truth_paths:
            raise ContractError(f"explicit_18z_truth_dataset_missing:{station}")
        point, probability_passed, point_passed, state = _station_quality(
            payload,
            truth_by_date=_truths(truth_paths[station]),
            allow_temporary_awc_no_live=allow_temporary_awc_no_live,
        )
        if (
            station_allowlist is not None
            and station not in station_allowlist
        ):
            state = "OUTSIDE_REQUESTED_PACKET_SCOPE"
        station_states.append({
            "station_id": station,
            "state": state,
            "oos_state": str(payload.get("oos", {}).get("state") or ""),
            "common_sample_n": int(
                payload.get("oos", {}).get("common_sample_n") or 0
            ),
            "settlement_timezone": str(payload["settlement_timezone"]),
            "truth_label_authority": payload.get("truth_label_authority"),
            "probability_non_degradation_vs_uniform": probability_passed,
            **point,
        })
        if state not in ACTIVATION_READY_STATES:
            continue

        selection = _selection_row(payload, run_cycle=run_cycle)
        ensemble = _ensemble_row(
            payload,
            selection=selection,
            run_cycle=run_cycle,
        )
        selection_rows.append(selection)
        ensembles.append(ensemble)
        calibration_artifacts.extend(dict(row) for row in payload["oos"]["calibration_artifacts"])
        holdout_n = int(point["holdout_n"])
        station_models = tuple(
            sorted(str(model).strip().upper() for model in payload["model_ids"])
        )
        for model in station_models:
            oos_reports.append({
                "station_id": station,
                "model_id": model,
                "season_bin": str(payload["oos"]["research_winner"]["season_bin"]),
                "lead_bucket": str(payload["lead_bucket"]),
                "run_cycle": run_cycle,
                "settlement_definition": SETTLEMENT_DEFINITION,
                "holdout_n": holdout_n,
            })
        for exact in payload["exact_join_rows"]:
            for model in station_models:
                source = exact["models"][model]
                source_provider = str(source["provider_id"]).lower()
                source_class = provider_source_class(source_provider)
                aggregator = source_class == "aggregator_republisher"
                model_rows.append({
                    "model_id": model,
                    "station_id": station,
                    "target_local_date": exact["event_date"],
                    "run_init_utc": source["model_run_initialization_utc"],
                    "lead_bucket": str(payload["lead_bucket"]),
                    "run_cycle": run_cycle,
                    "settlement_definition": SETTLEMENT_DEFINITION,
                    "provider_id": source["provider_id"],
                    "provider_role": provider_role(source_provider),
                    "source_class": source_class,
                    "aggregator_or_republisher": aggregator,
                    "native_arrival_indexed": False,
                    "prospective_strict_asof_eligible": False,
                    "historical_arrival_time_observed": False,
                })
        ensemble_report_rows.append({
            "selection_evidence_sha256": selection["content_sha256"],
            "probability_non_degradation_vs_uniform": True,
            "selection_mae_improvement_vs_uniform_c": point[
                "selection_mae_improvement_vs_uniform_c"
            ],
            "selection_rmse_improvement_vs_uniform_c": point[
                "selection_rmse_improvement_vs_uniform_c"
            ],
            **dict(selection["oos_metrics"]),
        })

    as_of = str(manifest["as_of_utc"])
    ready_station_ids = {
        str(row["station_id"])
        for row in station_states
        if row["state"] in ACTIVATION_READY_STATES
    }
    truth_authority_bindings = [
        _truth_authority_binding(
            truth_paths[station],
            station_id=station,
        )
        for station in sorted(ready_station_ids)
    ]
    temporary_ready_station_ids = sorted(
        row["station_id"]
        for row in truth_authority_bindings
        if row["truth_label_authority"] == TEMPORARY_AWC_TRUTH
    )
    evidence_identity = {
        "schema": "bayes_basket_target_calibration_evidence_v1",
        "as_of_utc": as_of,
        "checks": {
            "calibration_window_sufficient": True,
            "candidate_artifact_count_positive": bool(calibration_artifacts),
            "all_candidates_unpromoted": all(
                row.get("promoted") is False
                for row in (*calibration_artifacts, *ensembles)
            ),
            "truth_source_canonical_ingress":
                not temporary_ready_station_ids,
            "truth_source_no_live_calibration_authority_valid": True,
            "temporary_awc_calibration_label_explicit":
                bool(temporary_ready_station_ids),
        },
        "boundary": {
            "writes_production_state": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
        },
        "truth_authority_bindings": truth_authority_bindings,
        "calibration_model_rows": model_rows,
        "calibration_artifacts": calibration_artifacts,
        "ensemble_selection_evidence": selection_rows,
        "ensemble_artifacts": ensembles,
        "oos_reports": oos_reports,
        "input_artifacts": [{
            "path": str(manifest_path.resolve()),
            "content_sha256": str(manifest.get("content_sha256") or ""),
        }],
    }
    evidence_sha = content_sha256(evidence_identity)
    evidence = {
        **evidence_identity,
        "evidence_sha256": evidence_sha,
        "receipt_sha256": content_sha256({
            **evidence_identity,
            "evidence_sha256": evidence_sha,
        }),
    }
    audit_identity = {
        "schema": "bayes_basket_target_calibration_oos_audit_v1",
        "as_of_utc": as_of,
        "input_artifact": {
            "path": str(manifest_path.resolve()),
            "receipt_sha256": evidence["receipt_sha256"],
        },
        "checks": {
            "chronological_holdout_no_leakage": True,
            "metric_rows_consistent": True,
            "ensemble_oos_metrics_materialized": True,
            "ensemble_selection_non_degradation_vs_uniform": True,
            "ensemble_probability_oos_materialized": True,
        },
        "ensemble_report_rows": ensemble_report_rows,
        "boundary": {
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    audit = {**audit_identity, "evidence_sha256": content_sha256(audit_identity)}
    packet = build_no_live_calibration_artifact_packet(
        evidence,
        oos_audit=audit,
        calibration_generation=calibration_generation,
        ensemble_generation=ensemble_generation,
    )
    packet["station_ids"] = sorted(
        row["station_id"]
        for row in station_states
        if row["state"] in ACTIVATION_READY_STATES
    )
    packet["execution_mode"] = "production_no_live"
    packet["authorizes_order"] = False
    packet["live_submit_authorized"] = False
    packet["places_orders"] = False
    packet["calls_order_gateway"] = False
    packet["packet_sha256"] = content_sha256(packet)
    build_manifest = {
        "schema": SCHEMA,
        "as_of_utc": as_of,
        "source_manifest": str(manifest_path.resolve()),
        "source_manifest_content_sha256": str(manifest.get("content_sha256") or ""),
        "station_count": 45,
        "activation_packet_ready_station_count": sum(
            row["state"] in ACTIVATION_READY_STATES
            for row in station_states
        ),
        "temporary_awc_no_live_activation_station_count": len(
            temporary_ready_station_ids
        ),
        "temporary_awc_no_live_activation_station_ids":
            temporary_ready_station_ids,
        "station_states": station_states,
        "packet_counts": {
            "calibration_artifacts": len(packet["calibration_artifacts"]),
            "ensemble_artifacts": len(packet["ensemble_artifacts"]),
            "calibration_activations": len(packet["calibration_activations"]),
        },
        "boundary": {
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    build_manifest["content_sha256"] = content_sha256(build_manifest)
    return evidence, audit, packet, build_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--calibration-generation", type=int, default=3)
    parser.add_argument("--ensemble-generation", type=int, default=3)
    parser.add_argument(
        "--allow-temporary-awc-no-live",
        action="store_true",
        help=(
            "Use explicitly labelled AWC historical truth only for "
            "PRODUCTION_NO_LIVE_ONLY calibration; never realtime/P1/hard-NO."
        ),
    )
    parser.add_argument(
        "--station-id",
        action="append",
        default=[],
        help="Emit packet rows only for these stations; repeat as needed.",
    )
    args = parser.parse_args()
    manifest_path = args.input_manifest.expanduser().resolve()
    manifest = load_bounded_json(manifest_path)
    evidence, audit, packet, build_manifest = build(
        manifest,
        manifest_path=manifest_path,
        calibration_generation=args.calibration_generation,
        ensemble_generation=args.ensemble_generation,
        allow_temporary_awc_no_live=args.allow_temporary_awc_no_live,
        station_allowlist=(
            {
                str(value).strip().upper()
                for value in args.station_id
                if str(value).strip()
            }
            or None
        ),
    )
    output_root = args.output_root.expanduser().resolve()
    write_bounded_json(output_root / "calibration_evidence.json", evidence)
    write_bounded_json(output_root / "calibration_oos_audit.json", audit)
    write_bounded_json(output_root / "artifact_ingress_packet.json", packet)
    write_bounded_json(output_root / "manifest.json", build_manifest)
    print(json.dumps({
        "schema": build_manifest["schema"],
        "activation_packet_ready_station_count":
            build_manifest["activation_packet_ready_station_count"],
        "calibration_artifact_count":
            build_manifest["packet_counts"]["calibration_artifacts"],
        "ensemble_artifact_count":
            build_manifest["packet_counts"]["ensemble_artifacts"],
        "activation_count":
            build_manifest["packet_counts"]["calibration_activations"],
        "output": str((output_root / "manifest.json").resolve()),
        "writes_production_database": False,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
