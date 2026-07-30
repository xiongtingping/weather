#!/usr/bin/env python3
"""Read-only production readiness for the identifiable physical composer.

This command never promotes artifacts, edits activation, writes a database or
places orders.  It binds the candidate artifacts, strict-as-of calibration,
canonical replay, zero-order Transaction-B drill, runtime closure and current
basket+hard-NO production acceptance into one fail-closed receipt.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import time
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from identifiable_physical_state_bayes.core import (  # noqa: E402
    validate_observation_artifact,
    validate_state_model_artifact,
)
from identifiable_physical_state_bayes.reporting import (  # noqa: E402
    validate_reporting_operator_artifact,
)
from production_probability_v6_runtime_manifest import (  # noqa: E402
    audit_v6_runtime_manifest_dependency_closure,
    build_v6_runtime_manifest,
)
from physical_state_bayes_v6.schema_contract import build_forward_plan  # noqa: E402
from scripts.calibrate_identifiable_physical_state_artifact import (  # noqa: E402
    DEFAULT_MINIMUM_HOLDOUT_DAYS,
    DEFAULT_MINIMUM_TRAINING_DAYS,
    collect_strict_asof_samples,
    fit_bounded_parameter_draws,
)
from scripts.replay_identifiable_physical_state_bayes_candidate import replay  # noqa: E402


SCHEMA_VERSION = "identifiable_physical_state_production_readiness_v1"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _run_json(command: list[str], *, timeout: float) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    try:
        payload = json.loads(completed.stdout.strip())
    except (TypeError, ValueError):
        payload = {
            "ok": False,
            "state": "subprocess_json_unavailable",
            "returncode": completed.returncode,
            "stderr": completed.stderr[-1000:],
        }
    if not isinstance(payload, Mapping):
        return {"ok": False, "state": "subprocess_payload_not_mapping"}
    return dict(payload)


def _run_json_until_ok(
    command: list[str], *, timeout: float, attempts: int = 5, interval_sec: float = 1.0
) -> dict[str, Any]:
    snapshots = []
    for attempt in range(1, max(1, attempts) + 1):
        payload = _run_json(command, timeout=timeout)
        snapshots.append(
            {
                "attempt": attempt,
                "ok": payload.get("ok") is True,
                "state": payload.get("state") or payload.get("schema"),
            }
        )
        if payload.get("ok") is True:
            return {
                **payload,
                "readiness_sample_attempt_count": attempt,
                "readiness_sample_trace": snapshots,
            }
        if attempt < attempts:
            time.sleep(max(0.0, interval_sec))
    return {
        **payload,
        "readiness_sample_attempt_count": len(snapshots),
        "readiness_sample_trace": snapshots,
    }


def evaluate_release_readiness(
    *,
    candidate_artifacts_valid: bool,
    observation_identifiability_boundary_declared: bool,
    state_parameter_draws_bound_to_calibration: bool,
    additive_schema_plan_valid: bool,
    calibration: Mapping[str, Any],
    replay_report: Mapping[str, Any],
    zero_order: Mapping[str, Any],
    runtime_closure: Mapping[str, Any],
    current_production: Mapping[str, Any],
) -> dict[str, Any]:
    replay_gates = dict(replay_report.get("promotion_gates") or {})
    diagnostics = dict(replay_report.get("non_blocking_diagnostics") or {})
    research_evidence = {
        "state_parameter_draws_bound_to_calibration": (
            state_parameter_draws_bound_to_calibration
        ),
        "strict_asof_parameter_calibration_ready": calibration.get("ok") is True,
        "canonical_replay_complete": replay_report.get("ok") is True,
        "canonical_replay_blocking_gates_pass": bool(replay_gates)
        and all(value is True for value in replay_gates.values()),
    }
    blocking_gates = {
        "candidate_artifacts_valid": candidate_artifacts_valid,
        "observation_identifiability_boundary_declared": (
            observation_identifiability_boundary_declared
        ),
        "additive_schema_plan_valid": additive_schema_plan_valid,
        "zero_order_transaction_b_accepted": zero_order.get("ok") is True,
        "runtime_dependency_closure_complete": runtime_closure.get("ok") is True,
        "current_basket_hard_no_production_accepted": current_production.get("ok") is True,
    }
    diagnostics["research_evidence_gates"] = research_evidence
    policy = {
        "nll_activation_role": "non_blocking_diagnostic_only",
        "crps_activation_role": "non_blocking_diagnostic_only",
        "strict_asof_activation_role": "non_blocking_diagnostic_only",
        "sample_volume_activation_role": "non_blocking_diagnostic_only",
        "calibration_activation_role": "non_blocking_diagnostic_only",
        "canonical_replay_activation_role": "non_blocking_diagnostic_only",
        "strict_oos_sample_volume": "14_training_days_4_strict_oos_days",
        "strict_oos_sample_volume_production_gate": False,
        "production_unblocked_by_strict_oos_sample_volume": True,
        "nll_crps_excluded_from_blocking_gate_set": not any(
            "nll" in key.lower() or "crps" in key.lower()
            for key in blocking_gates
        ),
        "research_evidence_excluded_from_blocking_gate_set": not any(
            key in blocking_gates for key in research_evidence
        ),
    }
    return {
        "blocking_gates": blocking_gates,
        "non_blocking_diagnostics": diagnostics,
        "research_evidence": research_evidence,
        "promotion_policy": policy,
        "promotion_ready": all(blocking_gates.values())
        and policy["nll_crps_excluded_from_blocking_gate_set"]
        and policy["research_evidence_excluded_from_blocking_gate_set"],
    }


def build_readiness(
    *,
    db_path: Path,
    end_date: str,
    replay_days: int,
    minimum_training_days: int,
    minimum_holdout_days: int,
    state_artifact_path: Path,
    observation_artifact_path: Path,
    reporting_artifact_path: Path,
) -> dict[str, Any]:
    state_artifact = validate_state_model_artifact(
        json.loads(state_artifact_path.read_text(encoding="utf-8"))
    )
    observation_artifact = validate_observation_artifact(
        json.loads(observation_artifact_path.read_text(encoding="utf-8"))
    )
    reporting_artifact = validate_reporting_operator_artifact(
        json.loads(reporting_artifact_path.read_text(encoding="utf-8"))
    )
    candidate_artifacts_valid = all(
        artifact.get("state") == "candidate"
        and artifact.get("production_runtime_eligible") is False
        and artifact.get("shadow_only") is True
        for artifact in (state_artifact, observation_artifact, reporting_artifact)
    )
    observation_boundary = dict(
        observation_artifact.get("identifiability_boundary") or {}
    )
    observation_identifiability_boundary_declared = bool(
        observation_artifact.get("calibration_state")
        == "bounded_structural_prior_candidate"
        and observation_artifact.get("parameter_source")
        == "domain_bounded_weak_regime_emission_prior_v1"
        and observation_artifact.get("empirical_calibration_claimed") is False
        and observation_boundary.get(
            "joint_weather_parameters_not_claimed_data_fitted"
        )
        is True
    )
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        samples = collect_strict_asof_samples(conn)
    calibration = fit_bounded_parameter_draws(
        samples,
        minimum_training_days=minimum_training_days,
        minimum_holdout_days=minimum_holdout_days,
    )
    calibration["strict_asof_samples"] = samples
    state_calibration = dict(state_artifact.get("parameter_draw_calibration") or {})
    state_parameter_draws_bound = bool(
        calibration.get("ok") is True
        and state_artifact.get("calibration_state")
        in {
            "strict_asof_candidate",
            "strict_oos_metar_transition_candidate",
        }
        and state_calibration.get("sample_lineage_sha256")
        == calibration.get("sample_lineage_sha256")
        and state_artifact.get("bounded_parameter_draws")
        == calibration.get("bounded_parameter_draws")
    )
    replay_report = replay(
        db_path=db_path,
        days=replay_days,
        end_date=end_date,
        state_artifact_path=state_artifact_path,
        observation_artifact_path=observation_artifact_path,
        reporting_artifact_path=reporting_artifact_path,
    )
    zero_order = _run_json(
        [sys.executable, "scripts/identifiable_physical_state_bayes_zero_order_drill.py"],
        timeout=180.0,
    )
    current_production = _run_json_until_ok(
        [sys.executable, "scripts/reported_trajectory_no_production_acceptance.py"],
        timeout=120.0,
        attempts=5,
        interval_sec=1.0,
    )
    runtime_closure = audit_v6_runtime_manifest_dependency_closure(ROOT)
    manifest = build_v6_runtime_manifest(ROOT)
    schema_plan = build_forward_plan(db_path)
    assessment = evaluate_release_readiness(
        candidate_artifacts_valid=candidate_artifacts_valid,
        observation_identifiability_boundary_declared=(
            observation_identifiability_boundary_declared
        ),
        state_parameter_draws_bound_to_calibration=state_parameter_draws_bound,
        additive_schema_plan_valid=schema_plan.get("ok") is True,
        calibration=calibration,
        replay_report=replay_report,
        zero_order=zero_order,
        runtime_closure=runtime_closure,
        current_production=current_production,
    )
    core = {
        "schema": SCHEMA_VERSION,
        "state": (
            "ready_for_explicit_release_build"
            if assessment["promotion_ready"]
            else "blocked_fail_closed"
        ),
        "promotion_ready": assessment["promotion_ready"],
        "read_only": True,
        "writes_artifacts": False,
        "writes_activation": False,
        "writes_database": False,
        "places_orders": False,
        "candidate_artifacts": {
            "state_model": state_artifact["artifact_sha256"],
            "observation": observation_artifact["artifact_sha256"],
            "reporting_operator": reporting_artifact["artifact_sha256"],
        },
        "runtime_manifest_sha256": manifest["runtime_manifest_sha256"],
        "blocking_gates": assessment["blocking_gates"],
        "non_blocking_diagnostics": assessment["non_blocking_diagnostics"],
        "promotion_policy": assessment["promotion_policy"],
        "calibration": calibration,
        "canonical_replay": replay_report,
        "zero_order": zero_order,
        "runtime_closure": runtime_closure,
        "additive_schema_plan": schema_plan,
        "current_production": current_production,
    }
    return {**core, "readiness_sha256": _sha(core)}


def _summary(value: Mapping[str, Any]) -> dict[str, Any]:
    calibration = dict(value.get("calibration") or {})
    replay_report = dict(value.get("canonical_replay") or {})
    return {
        key: value.get(key)
        for key in (
            "schema",
            "state",
            "promotion_ready",
            "read_only",
            "writes_artifacts",
            "writes_activation",
            "writes_database",
            "places_orders",
            "candidate_artifacts",
            "runtime_manifest_sha256",
            "blocking_gates",
            "non_blocking_diagnostics",
            "promotion_policy",
            "readiness_sha256",
        )
    } | {
        "calibration_summary": {
            key: calibration.get(key)
            for key in (
                "ok",
                "state",
                "available_unique_days",
                "minimum_training_days",
                "minimum_holdout_days",
                "minimum_total_days",
            )
        },
        "replay_summary": {
            "ok": replay_report.get("ok"),
            "replayed_day_count": replay_report.get("replayed_day_count"),
            "aligned_event_count": (
                replay_report.get("aligned_summary") or {}
            ).get("aligned_event_count"),
            "promotion_gates": replay_report.get("promotion_gates"),
            "latency_summary_ms": replay_report.get("latency_summary_ms"),
        },
        "zero_order_ok": (value.get("zero_order") or {}).get("ok"),
        "runtime_closure_ok": (value.get("runtime_closure") or {}).get("ok"),
        "additive_schema_summary": {
            "ok": (value.get("additive_schema_plan") or {}).get("ok"),
            "state": (value.get("additive_schema_plan") or {}).get("state"),
            "action_count": len(
                (value.get("additive_schema_plan") or {}).get("actions") or []
            ),
        },
        "current_production_ok": (value.get("current_production") or {}).get("ok"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument(
        "--end-date",
        default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    )
    parser.add_argument("--replay-days", type=int, default=45)
    parser.add_argument(
        "--minimum-training-days",
        type=int,
        default=DEFAULT_MINIMUM_TRAINING_DAYS,
    )
    parser.add_argument(
        "--minimum-holdout-days",
        type=int,
        default=DEFAULT_MINIMUM_HOLDOUT_DAYS,
    )
    parser.add_argument(
        "--state-artifact",
        type=Path,
        default=ROOT / "config/artifacts/identifiable_physical_state_model_v1.candidate.json",
    )
    parser.add_argument(
        "--observation-artifact",
        type=Path,
        default=ROOT / "config/artifacts/identifiable_physical_observation_v1.candidate.json",
    )
    parser.add_argument(
        "--reporting-artifact",
        type=Path,
        default=ROOT / "config/artifacts/amsc_frozen_reporting_operator_v1.candidate.json",
    )
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = build_readiness(
        db_path=args.db,
        end_date=args.end_date,
        replay_days=max(1, args.replay_days),
        minimum_training_days=max(1, args.minimum_training_days),
        minimum_holdout_days=max(1, args.minimum_holdout_days),
        state_artifact_path=args.state_artifact,
        observation_artifact_path=args.observation_artifact,
        reporting_artifact_path=args.reporting_artifact,
    )
    print(json.dumps(_summary(result) if args.summary else result, ensure_ascii=False, sort_keys=True))
    return 0 if result["promotion_ready"] is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
