#!/usr/bin/env python3
"""Materialize the operator-approved three-model D1 calibration artifact.

The output keeps the complete seven-source exact-set template.  Only ECMWF,
ICON and GFS receive D1 bias/residual calibration; untrained sources retain
their prior conservative parameters.  This script never edits activation.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from joint_forecast_weak_likelihood_runtime import canonical_artifact_sha256, validate_joint_forecast_weak_artifact  # noqa: E402


APPROVAL_PHRASE = "APPROVE_THREE_MODEL_D1_SETTLEMENT_CALIBRATION"
CALIBRATED_SOURCES = ("ecmwf", "icon", "gfs")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _read(path: Path) -> dict[str, Any]:
    return _mapping(json.loads(path.read_text(encoding="utf-8")))


def build_artifact(*, baseline: Mapping[str, Any], candidate_report: Mapping[str, Any], approved_at: str) -> dict[str, Any]:
    report = _mapping(candidate_report)
    training = _mapping(report.get("training"))
    candidate = _mapping(training.get("artifact"))
    safety = _mapping(_mapping(report.get("oos")).get("oos_safety"))
    metrics = _mapping(safety.get("metrics"))
    if report.get("state") != "shadow_candidate_built" or not candidate:
        raise ValueError("settlement_interval_candidate_missing_or_invalid")
    for field in ("interval_mae_improvement_c", "bucket_logloss_improvement", "bucket_rps_improvement"):
        if float(metrics.get(field) or 0.0) <= 0.0:
            raise ValueError(f"candidate_metric_not_improved:{field}")
    bias = copy.deepcopy(_mapping(candidate.get("bias_correction")))
    states = _mapping(bias.get("source_global_states"))
    sigmas: dict[str, float] = {}
    for source in CALIBRATED_SOURCES:
        state = _mapping(states.get(f"source_global:{source}|d1"))
        if int(state.get("event_day_count") or 0) < 30 or state.get("fallback") is not None:
            raise ValueError(f"eligible_d1_bias_state_missing:{source}")
        sigma = float(state.get("robust_scale_c") or 0.0)
        if sigma <= 0.0:
            raise ValueError(f"eligible_d1_residual_scale_missing:{source}")
        sigmas[source] = sigma
    artifact = copy.deepcopy(_mapping(baseline))
    artifact.pop("artifact_sha256", None)
    artifact.update({
        "approval_state": "approved",
        "approval_kind": "operator_directed_early_d1_settlement_interval_calibration",
        "approved_at": approved_at,
        "source_data_policy": "forecast_only_asof",
        "training_data_policy": candidate.get("training_data_policy"),
        "replacement_policy": "immutable_operator_approved_artifact_only",
        "bias_correction": bias,
        "source_set_surfaces": {},
        "operator_approval": {
            "scope": "ecmwf_icon_gfs_d1_bias_and_residual_scale",
            "instruction": "three_major_models_d1_corrected_cut_to_production_and_continue_sampling",
            "oos_minimum_event_days": safety.get("minimum_event_days"),
            "oos_observed_event_days": safety.get("sample_count"),
            "early_cutover_waiver": True,
            "metrics": metrics,
        },
    })
    template = _mapping(artifact.get("exact_surface_baseline_template"))
    template.update({
        "source_error_sigma_c_by_horizon": {"d1": sigmas},
        "calibrated_source_ids_by_horizon": {"d1": list(CALIBRATED_SOURCES)},
        "training_event_day_count": int(bias.get("training_event_day_count") or 0),
        "calibration_state": "operator_approved_three_model_d1_interval_calibration",
        "bias_policy": "artifact_hierarchical_d1_for_ecmwf_icon_gfs_zero_for_untrained_sources",
    })
    artifact["exact_surface_baseline_template"] = template
    boundary = _mapping(artifact.get("boundary"))
    boundary.update({
        "settlement_bucket_used_as_interval_not_exact_x": True,
        "three_model_d1_fixed_error_scale_retired": True,
        "untrained_source_fixed_error_scale_retained": True,
        "operator_early_cutover_waiver_recorded": True,
    })
    artifact["boundary"] = boundary
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)
    contract = validate_joint_forecast_weak_artifact(artifact)
    if contract.get("ok") is not True:
        raise ValueError("production_artifact_contract_invalid:" + ",".join(contract.get("issues") or []))
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=ROOT / "data/runtime/joint_forecast_weak_likelihood/approved/joint_weak_operational_baseline_v3.json")
    parser.add_argument("--candidate", type=Path, default=ROOT / "data/research/joint_forecast_weak_likelihood/settlement_interval_candidate_latest.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/runtime/joint_forecast_weak_likelihood/approved/joint_weak_d1_settlement_calibrated_v1.json")
    parser.add_argument("--approve", default="")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    artifact = build_artifact(
        baseline=_read(args.baseline.resolve()), candidate_report=_read(args.candidate.resolve()),
        approved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    if args.write:
        if args.approve != APPROVAL_PHRASE:
            raise SystemExit("operator_approval_phrase_required")
        if args.output.exists():
            existing = _read(args.output)
            if existing.get("artifact_sha256") != artifact.get("artifact_sha256"):
                raise SystemExit("approved_artifact_identity_collision")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "state": "materialized" if args.write else "preview",
        "artifact_sha256": artifact["artifact_sha256"], "output": str(args.output.resolve()),
        "approval_state": artifact["approval_state"], "operator_approval": artifact["operator_approval"],
        "d1_sigma_c": artifact["exact_surface_baseline_template"]["source_error_sigma_c_by_horizon"]["d1"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
