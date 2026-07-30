#!/usr/bin/env python3
"""Build a non-active release bundle for the identifiable physical composer."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from identifiable_physical_state_bayes.core import (  # noqa: E402
    canonical_sha256,
    validate_observation_artifact,
    validate_state_model_artifact,
)
from identifiable_physical_state_bayes.reporting import (  # noqa: E402
    validate_reporting_operator_artifact,
)
from production_probability_v6_activation_contract import (  # noqa: E402
    CHECKPOINT_SCHEMA,
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    IDENTIFIABLE_PHYSICAL_FORMULA_ID,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION as ACTIVATION_SCHEMA,
    STRONG_UPDATE_SCHEMA,
    activation_sha256,
    validate_v6_activation,
)
from production_probability_v6_live_route_contract import audit_v6_live_routes  # noqa: E402
from production_probability_v6_runtime_manifest import build_v6_runtime_manifest  # noqa: E402
from scripts.identifiable_physical_state_production_readiness import (  # noqa: E402
    build_readiness,
)


SCHEMA_VERSION = "identifiable_physical_state_release_bundle_v1"
CONFIRMATION = "BUILD_IDENTIFIABLE_PHYSICAL_STATE_RELEASE"
FILE_NAMES = {
    "state_model": "identifiable_physical_state_model_v1.json",
    "observation": "identifiable_physical_observation_v1.json",
    "reporting_operator": "amsc_frozen_reporting_operator_v1.json",
    "activation": "production_probability_runtime_activation.json",
}
REQUIRED_BLOCKING_GATES = frozenset(
    {
        "candidate_artifacts_valid",
        "observation_identifiability_boundary_declared",
        "additive_schema_plan_valid",
        "zero_order_transaction_b_accepted",
        "runtime_dependency_closure_complete",
        "current_basket_hard_no_production_accepted",
    }
)


def _canonical_bytes(value: Any, *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            default=str,
        )
        + "\n"
    ).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_bytes(value)
    os.replace(temporary, path)


def _promote(
    artifact: Mapping[str, Any],
    *,
    readiness_sha256: str,
    validator: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    value = dict(artifact)
    value.pop("artifact_sha256", None)
    value.update(
        {
            "state": "approved",
            "production_runtime_eligible": True,
            "shadow_only": False,
            "promotion_forbidden_until_oos_gates": False,
            "promotion_evidence_sha256": readiness_sha256,
            "promotion_scope": "resident_probability_worker_for_yes_basket_only",
        }
    )
    value["artifact_sha256"] = canonical_sha256(value)
    return validator(value)


def build_release(
    *,
    root: Path,
    readiness: Mapping[str, Any],
    state_candidate: Mapping[str, Any],
    observation_candidate: Mapping[str, Any],
    reporting_candidate: Mapping[str, Any],
) -> dict[str, Any]:
    blocking_gates = dict(readiness.get("blocking_gates") or {})
    if set(blocking_gates) != REQUIRED_BLOCKING_GATES or not all(
        value is True for value in blocking_gates.values()
    ):
        raise ValueError("identifiable_physical_release_blocking_gate_not_true")
    policy = dict(readiness.get("promotion_policy") or {})
    if policy.get("nll_crps_excluded_from_blocking_gate_set") is not True:
        raise ValueError("identifiable_physical_release_nll_crps_policy_invalid")
    if policy.get("research_evidence_excluded_from_blocking_gate_set") is not True:
        raise ValueError("identifiable_physical_release_research_evidence_policy_invalid")
    if any("nll" in key.lower() or "crps" in key.lower() for key in blocking_gates):
        raise ValueError("identifiable_physical_release_nll_crps_blocker_forbidden")
    if any(
        key in blocking_gates
        for key in (
            "state_parameter_draws_bound_to_calibration",
            "strict_asof_parameter_calibration_ready",
            "canonical_replay_complete",
            "canonical_replay_blocking_gates_pass",
        )
    ):
        raise ValueError("identifiable_physical_release_research_evidence_blocker_forbidden")
    readiness_sha = str(readiness.get("readiness_sha256") or "")
    if len(readiness_sha) != 64:
        raise ValueError("identifiable_physical_release_readiness_sha_invalid")
    route = audit_v6_live_routes(root)
    if route.get("ok") is not True:
        raise ValueError("identifiable_physical_release_resident_route_not_ready")

    validated_state_candidate = validate_state_model_artifact(state_candidate)
    validated_observation_candidate = validate_observation_artifact(
        observation_candidate
    )
    observation_boundary = dict(
        validated_observation_candidate.get("identifiability_boundary") or {}
    )
    if (
        validated_observation_candidate.get("calibration_state")
        != "bounded_structural_prior_candidate"
        or validated_observation_candidate.get("parameter_source")
        != "domain_bounded_weak_regime_emission_prior_v1"
        or validated_observation_candidate.get("empirical_calibration_claimed")
        is not False
        or observation_boundary.get("joint_weather_parameters_not_claimed_data_fitted")
        is not True
    ):
        raise ValueError(
            "identifiable_physical_release_observation_identifiability_boundary_missing"
        )
    validated_reporting_candidate = validate_reporting_operator_artifact(
        reporting_candidate
    )
    candidates = dict(readiness.get("candidate_artifacts") or {})
    if candidates != {
        "state_model": validated_state_candidate["artifact_sha256"],
        "observation": validated_observation_candidate["artifact_sha256"],
        "reporting_operator": validated_reporting_candidate["artifact_sha256"],
    }:
        raise ValueError("identifiable_physical_release_candidate_binding_mismatch")
    state = _promote(
        validated_state_candidate,
        readiness_sha256=readiness_sha,
        validator=validate_state_model_artifact,
    )
    observation = _promote(
        validated_observation_candidate,
        readiness_sha256=readiness_sha,
        validator=validate_observation_artifact,
    )
    reporting = _promote(
        validated_reporting_candidate,
        readiness_sha256=readiness_sha,
        validator=validate_reporting_operator_artifact,
    )
    current = json.loads(
        (root / "config/production_probability_runtime_activation.json").read_text(
            encoding="utf-8"
        )
    )
    runtime_manifest = build_v6_runtime_manifest(root)
    if readiness.get("runtime_manifest_sha256") != runtime_manifest.get(
        "runtime_manifest_sha256"
    ):
        raise ValueError("identifiable_physical_release_runtime_binding_mismatch")
    activation = {
        "schema": ACTIVATION_SCHEMA,
        "state": "production_enabled",
        "bayes_formula_id": IDENTIFIABLE_PHYSICAL_FORMULA_ID,
        "composer_schema": IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "v5_fallback_forbidden": True,
        "pre_event_predict_does_not_consume_current_observation": True,
        "no_post_emission_state_transition": True,
        "nwp_hourly_curve_not_reapplied_as_independent_likelihood": True,
        "runtime_manifest_sha256": runtime_manifest["runtime_manifest_sha256"],
        "prior_model_artifact_sha256": str(
            current.get("prior_model_artifact_sha256") or ""
        ),
        "weak_likelihood_artifact_sha256": str(
            current.get("weak_likelihood_artifact_sha256") or ""
        ),
        "physical_state_model_artifact_sha256": state["artifact_sha256"],
        "physical_observation_artifact_sha256": observation["artifact_sha256"],
        "physical_reporting_operator_sha256": reporting["artifact_sha256"],
        "physical_state_vector": [
            "latent_temperature",
            "specific_humidity",
            "net_temperature_tendency",
            "surface_wetness_memory",
            "cold_pool_memory",
            "regime_state",
        ],
        "frozen_reporting_operator_enabled": True,
        "current_event_joint_consumption_count": 1,
        "production_transaction_b_authorized": True,
        "places_orders": False,
        "authorizes_clob": False,
        "activation_requires_atomic_install": True,
        "resident_service": "com.weather.reported_trajectory_no",
        "release_readiness_sha256": readiness_sha,
        "nll_crps_activation_role": "non_blocking_diagnostic_only",
        "strict_asof_activation_role": "non_blocking_diagnostic_only",
        "sample_volume_activation_role": "non_blocking_diagnostic_only",
        "calibration_activation_role": "non_blocking_diagnostic_only",
        "canonical_replay_activation_role": "non_blocking_diagnostic_only",
        "research_evidence_excluded_from_blocking_gate_set": True,
        "supersedes_probability_epoch_schema_on_cutover": str(
            current.get("schema") or ""
        ),
    }
    activation["activation_contract_sha256"] = activation_sha256(activation)
    activation = validate_v6_activation(activation)
    values = {
        "state_model": state,
        "observation": observation,
        "reporting_operator": reporting,
        "activation": activation,
    }
    file_bytes = {
        FILE_NAMES[name]: _canonical_bytes(value, pretty=True)
        for name, value in values.items()
    }
    manifest_core = {
        "schema": SCHEMA_VERSION,
        "state": "release_built_not_active",
        "promotion_ready": True,
        "readiness_sha256": readiness_sha,
        "blocking_gates": blocking_gates,
        "non_blocking_diagnostics": dict(
            readiness.get("non_blocking_diagnostics") or {}
        ),
        "promotion_policy": policy,
        "runtime_manifest_sha256": runtime_manifest["runtime_manifest_sha256"],
        "activation_contract_sha256": activation["activation_contract_sha256"],
        "files": {
            name: {"sha256": _sha_bytes(value), "size_bytes": len(value)}
            for name, value in sorted(file_bytes.items())
        },
        "resident_service": "com.weather.reported_trajectory_no",
        "adds_fourth_service": False,
        "places_orders": False,
        "activates_production": False,
    }
    manifest = {
        **manifest_core,
        "release_manifest_sha256": canonical_sha256(manifest_core),
    }
    return {"manifest": manifest, "file_bytes": file_bytes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument(
        "--end-date",
        default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    )
    parser.add_argument("--replay-days", type=int, default=45)
    parser.add_argument("--minimum-training-days", type=int, default=30)
    parser.add_argument("--minimum-holdout-days", type=int, default=10)
    parser.add_argument(
        "--state-candidate",
        type=Path,
        default=ROOT
        / "config/artifacts/identifiable_physical_state_model_v1.candidate.json",
    )
    parser.add_argument(
        "--observation-candidate",
        type=Path,
        default=ROOT
        / "config/artifacts/identifiable_physical_observation_v1.candidate.json",
    )
    parser.add_argument(
        "--reporting-candidate",
        type=Path,
        default=ROOT
        / "config/artifacts/amsc_frozen_reporting_operator_v1.candidate.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "output/releases/identifiable_physical_state_bayes_v1",
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    readiness = build_readiness(
        db_path=args.db.resolve(),
        end_date=str(args.end_date)[:10],
        replay_days=max(1, args.replay_days),
        minimum_training_days=max(1, args.minimum_training_days),
        minimum_holdout_days=max(1, args.minimum_holdout_days),
        state_artifact_path=args.state_candidate.resolve(),
        observation_artifact_path=args.observation_candidate.resolve(),
        reporting_artifact_path=args.reporting_candidate.resolve(),
    )
    try:
        release = build_release(
            root=ROOT,
            readiness=readiness,
            state_candidate=json.loads(args.state_candidate.read_text(encoding="utf-8")),
            observation_candidate=json.loads(
                args.observation_candidate.read_text(encoding="utf-8")
            ),
            reporting_candidate=json.loads(
                args.reporting_candidate.read_text(encoding="utf-8")
            ),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_fail_closed",
            "reason": f"{type(exc).__name__}:{exc}",
            "readiness_sha256": readiness.get("readiness_sha256"),
            "blocking_gates": readiness.get("blocking_gates"),
            "non_blocking_diagnostics": readiness.get("non_blocking_diagnostics"),
            "writes_release": False,
            "activates_production": False,
            "places_orders": False,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    if args.write and args.confirm != CONFIRMATION:
        raise SystemExit("--write requires exact --confirm token")
    output = args.output_dir.expanduser().resolve()
    if args.write:
        for name, value in release["file_bytes"].items():
            _atomic_write(output / name, value)
        _atomic_write(
            output / "release_manifest.json",
            _canonical_bytes(release["manifest"], pretty=True),
        )
    result = {
        **release["manifest"],
        "ok": True,
        "state": "release_written_not_active" if args.write else "release_preview_ready",
        "output_dir": str(output),
        "writes_release": bool(args.write),
        "activates_production": False,
        "places_orders": False,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
