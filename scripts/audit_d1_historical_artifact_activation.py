#!/usr/bin/env python3
"""Read-only audit of the d1 historical no-live activation root."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration import validate_formal_ensemble_artifact  # noqa: E402
from bayes_basket_target.contracts import (  # noqa: E402
    CalibrationActivation,
    ModelEnsembleArtifact,
    ModelEnsembleSelectionEvidence,
    content_sha256,
    model_set_hash,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


TARGET = tuple(coordinate_registry_manifest()["station_ids"])
MODELS = ("CMA", "ECMWF", "GEM", "GFS", "ICON")
SETTLEMENT = "official_daily_tmax_c"


def _row_to_selection(row: sqlite3.Row) -> dict[str, Any]:
    keys = (
        "selection_evidence_id", "station_id", "season_bin", "lead_bucket", "run_cycle",
        "settlement_definition", "method_version", "train_start_date", "train_end_date",
        "recent_start_date", "recent_end_date", "holdout_start_date", "holdout_end_date",
        "candidate_scores_json", "selected_models_json", "selected_weights_json",
        "oos_metrics_json", "residual_covariance_json", "residual_correlation_json",
        "effective_sample_n", "previous_ensemble_artifact_sha256", "max_weight_delta",
        "actual_max_weight_delta", "content_sha256",
    )
    result = dict(zip(keys, row))
    for key in (
        "candidate_scores_json", "selected_models_json", "selected_weights_json",
        "oos_metrics_json", "residual_covariance_json", "residual_correlation_json",
    ):
        result[key.removesuffix("_json")] = json.loads(result.pop(key))
    result["content_sha256"] = str(result["content_sha256"])
    return result


def _row_to_ensemble(row: sqlite3.Row) -> dict[str, Any]:
    keys = (
        "ensemble_artifact_id", "station_id", "season_bin", "lead_bucket", "run_cycle",
        "settlement_definition", "candidate_models_json", "supported_masks_json",
        "residual_covariance_json", "correlation_penalty", "selection_evidence_sha256",
        "previous_ensemble_artifact_sha256", "weight_method", "effective_sample_n",
        "max_weight_delta", "train_start_date", "train_end_date", "recent_start_date",
        "recent_end_date", "holdout_start_date", "holdout_end_date", "oos_metrics_json",
        "valid_from_utc", "valid_until_utc", "content_sha256",
    )
    result = dict(zip(keys, row))
    for key in ("candidate_models_json", "supported_masks_json", "residual_covariance_json", "oos_metrics_json"):
        result[key.removesuffix("_json")] = json.loads(result.pop(key)) if result[key] else {}
    result["promoted"] = False
    return result


def audit(db: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        for station in sorted(TARGET):
            scope = (station, "summer", "d1", "PREVIOUS_DAY1", SETTLEMENT)
            artifacts = conn.execute(
                "SELECT calibration_artifact_id,model_id,content_sha256 FROM calibration_model_artifact "
                "WHERE station_id=? AND season_bin=? AND lead_bucket=? AND run_cycle=? AND settlement_definition=? "
                "ORDER BY model_id", scope
            ).fetchall()
            selections_raw = conn.execute(
                "SELECT selection_evidence_id,station_id,season_bin,lead_bucket,run_cycle,settlement_definition,method_version,"
                "train_start_date,train_end_date,recent_start_date,recent_end_date,holdout_start_date,holdout_end_date,"
                "candidate_scores_json,selected_models_json,selected_weights_json,oos_metrics_json,residual_covariance_json,"
                "residual_correlation_json,effective_sample_n,previous_ensemble_artifact_sha256,max_weight_delta,"
                "actual_max_weight_delta,content_sha256 FROM calibration_ensemble_selection_evidence "
                "WHERE station_id=? AND season_bin=? AND lead_bucket=? AND run_cycle=? AND settlement_definition=?", scope
            ).fetchall()
            ensembles_raw = conn.execute(
                "SELECT ensemble_artifact_id,station_id,season_bin,lead_bucket,run_cycle,settlement_definition,"
                "candidate_models_json,supported_masks_json,residual_covariance_json,correlation_penalty,"
                "selection_evidence_sha256,previous_ensemble_artifact_sha256,weight_method,effective_sample_n,"
                "max_weight_delta,train_start_date,train_end_date,recent_start_date,recent_end_date,holdout_start_date,"
                "holdout_end_date,oos_metrics_json,valid_from_utc,valid_until_utc,content_sha256 "
                "FROM calibration_ensemble_artifact WHERE station_id=? AND season_bin=? AND lead_bucket=? "
                "AND run_cycle=? AND settlement_definition=?", scope
            ).fetchall()
            activations = conn.execute(
                "SELECT activation_id,artifact_kind,artifact_id,station_id,model_id_or_star,season_bin,lead_bucket,run_cycle,"
                "settlement_definition,available_model_set_hash_or_star,generation,valid_from_utc,valid_until_utc,"
                "activated_at_utc,authority_scope,content_sha256 FROM calibration_activation "
                "WHERE station_id=? AND season_bin=? AND lead_bucket=? AND run_cycle=? AND settlement_definition=? "
                "AND authority_scope='PRODUCTION_NO_LIVE_ONLY'", (station, "summer", "d1", "PREVIOUS_DAY1", SETTLEMENT)
            ).fetchall()
            reasons: list[str] = []
            if len(artifacts) < 5 or {str(row[1]) for row in artifacts} != set(MODELS):
                reasons.append("MODEL_ARTIFACT_SET_INVALID")
            if len(selections_raw) < 1:
                reasons.append("SELECTION_EVIDENCE_COUNT_INVALID")
            if len(ensembles_raw) < 1:
                reasons.append("ENSEMBLE_ARTIFACT_COUNT_INVALID")
            latest_generation = max((int(row[10]) for row in activations), default=0)
            latest_activations = [
                row for row in activations if int(row[10]) == latest_generation
            ]
            if len(latest_activations) != 31:
                reasons.append("ACTIVATION_ROOT_COUNT_INVALID")
            if not reasons:
                latest_model_ids = {
                    str(row[2]) for row in latest_activations
                    if str(row[1]) == "MODEL_CALIBRATION"
                }
                latest_ensemble_ids = {
                    str(row[2]) for row in latest_activations
                    if str(row[1]) == "MODEL_ENSEMBLE"
                }
                artifact_ids = {str(row[0]) for row in artifacts}
                ensemble_ids = {str(row[0]) for row in ensembles_raw}
                if latest_model_ids - artifact_ids:
                    reasons.append("LATEST_MODEL_ARTIFACT_POINTER_MISSING")
                if len(latest_ensemble_ids) != 1 or latest_ensemble_ids - ensemble_ids:
                    reasons.append("LATEST_ENSEMBLE_ARTIFACT_POINTER_MISSING")
                selected_ensemble_raw = next(
                    (row for row in ensembles_raw if str(row[0]) in latest_ensemble_ids),
                    None,
                )
                if selected_ensemble_raw is None:
                    reasons.append("LATEST_ENSEMBLE_ARTIFACT_MISSING")
                selection = _row_to_selection(
                    next(
                        (
                            row for row in selections_raw
                            if selected_ensemble_raw is not None
                            and str(row[23]) == str(selected_ensemble_raw[10])
                        ),
                        selections_raw[-1],
                    )
                )
                ensemble = _row_to_ensemble(selected_ensemble_raw or ensembles_raw[-1])
                try:
                    parsed_selection = ModelEnsembleSelectionEvidence.from_mapping(selection)
                    parsed_ensemble = ModelEnsembleArtifact.from_mapping(ensemble)
                    validate_formal_ensemble_artifact(parsed_ensemble)
                    if parsed_selection.selected_models != tuple(sorted(MODELS)):
                        reasons.append("SELECTION_MODEL_SET_INVALID")
                    if parsed_ensemble.candidate_models != tuple(sorted(MODELS)):
                        reasons.append("ENSEMBLE_MODEL_SET_INVALID")
                    if parsed_ensemble.selection_evidence_sha256 != parsed_selection.content_sha256:
                        reasons.append("SELECTION_ENSEMBLE_HASH_MISMATCH")
                    if set(parsed_ensemble.supported_masks) != {
                        model_set_hash(subset)
                        for size in range(2, 6)
                        for subset in __import__("itertools").combinations(MODELS, size)
                    }:
                        reasons.append("EXACT_MASK_SET_INVALID")
                except Exception as exc:
                    reasons.append(f"CONTRACT_INVALID:{type(exc).__name__}:{exc}")
                for activation in latest_activations:
                    item = CalibrationActivation.from_mapping(dict(activation))
                    identity = dict(activation)
                    supplied = identity.pop("content_sha256")
                    if content_sha256(identity) != supplied:
                        reasons.append("ACTIVATION_CONTENT_HASH_INVALID")
                        break
                    if item.authority_scope != "PRODUCTION_NO_LIVE_ONLY":
                        reasons.append("ACTIVATION_AUTHORITY_INVALID")
                        break
            rows.append({
                "station_id": station,
                "state": "BOUND_NO_LIVE_CANDIDATE" if not reasons else "BLOCKED",
                "reasons": sorted(set(reasons)),
                "model_artifact_count": len(artifacts),
                "selection_evidence_count": len(selections_raw),
                "ensemble_artifact_count": len(ensembles_raw),
                "activation_count": len(activations),
                "latest_activation_generation": latest_generation or None,
                "latest_activation_count": len(latest_activations),
                "native_arrival_index_eligible": False,
                "p1_eligible": False,
                "formal_live_authority": False,
            })
    identity = {
        "schema": "bayes_basket_target_d1_historical_artifact_activation_audit_v1",
        "database": str(db),
        "station_count": len(rows),
        "bound_station_count": sum(row["state"] == "BOUND_NO_LIVE_CANDIDATE" for row in rows),
        "blocked_station_count": sum(row["state"] != "BOUND_NO_LIVE_CANDIDATE" for row in rows),
        "rows": rows,
        "boundary": {
            "read_only": True,
            "production_no_live_only": True,
            "native_arrival_index_written": False,
            "p1_authority_written": False,
            "formal_live_authority_written": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    db = args.db.expanduser().resolve(strict=True)
    report = audit(db)
    write_bounded_json(args.output.expanduser().resolve(), report)
    print(json.dumps({
        "schema": report["schema"],
        "bound_station_count": report["bound_station_count"],
        "blocked_station_count": report["blocked_station_count"],
        "output": str(args.output.expanduser().resolve()),
        "content_sha256": report["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["blocked_station_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
