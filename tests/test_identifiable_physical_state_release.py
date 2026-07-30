from __future__ import annotations

import json
from pathlib import Path

import pytest

from identifiable_physical_state_bayes.core import canonical_sha256
from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256
from scripts.build_identifiable_physical_state_release import build_release
from scripts.cutover_identifiable_physical_state_production import verify_release


ROOT = Path(__file__).resolve().parents[1]


def _artifact(name: str) -> dict:
    return json.loads((ROOT / "config/artifacts" / name).read_text(encoding="utf-8"))


def _calibrated_state() -> dict:
    value = _artifact("identifiable_physical_state_model_v1.candidate.json")
    value.pop("artifact_sha256", None)
    value["calibration_state"] = "strict_asof_candidate"
    value["parameter_draw_calibration"] = {
        "state": "candidate_parameter_draws_fitted",
        "sample_lineage_sha256": "b" * 64,
    }
    value["artifact_sha256"] = canonical_sha256(value)
    return value


def _ready(state: dict, observation: dict, reporting: dict) -> dict:
    return {
        "promotion_ready": True,
        "readiness_sha256": "a" * 64,
        "runtime_manifest_sha256": v6_runtime_manifest_sha256(),
        "candidate_artifacts": {
            "state_model": state["artifact_sha256"],
            "observation": observation["artifact_sha256"],
            "reporting_operator": reporting["artifact_sha256"],
        },
        "blocking_gates": {
            "candidate_artifacts_valid": True,
            "observation_identifiability_boundary_declared": True,
            "additive_schema_plan_valid": True,
            "zero_order_transaction_b_accepted": True,
            "runtime_dependency_closure_complete": True,
            "current_basket_hard_no_production_accepted": True,
        },
        "non_blocking_diagnostics": {
            "no_worse_mean_nll_than_legacy": False,
            "no_worse_mean_crps_than_legacy": False,
        },
        "promotion_policy": {
            "nll_crps_excluded_from_blocking_gate_set": True,
            "nll_activation_role": "non_blocking_diagnostic_only",
            "crps_activation_role": "non_blocking_diagnostic_only",
            "research_evidence_excluded_from_blocking_gate_set": True,
            "strict_asof_activation_role": "non_blocking_diagnostic_only",
            "sample_volume_activation_role": "non_blocking_diagnostic_only",
            "calibration_activation_role": "non_blocking_diagnostic_only",
            "canonical_replay_activation_role": "non_blocking_diagnostic_only",
        },
    }


def test_release_build_allows_false_nll_crps_diagnostics() -> None:
    state = _calibrated_state()
    observation = _artifact("identifiable_physical_observation_v1.candidate.json")
    reporting = _artifact("amsc_frozen_reporting_operator_v1.candidate.json")
    release = build_release(
        root=ROOT,
        readiness=_ready(state, observation, reporting),
        state_candidate=state,
        observation_candidate=observation,
        reporting_candidate=reporting,
    )

    manifest = release["manifest"]
    assert manifest["promotion_ready"] is True
    assert manifest["non_blocking_diagnostics"][
        "no_worse_mean_nll_than_legacy"
    ] is False
    assert manifest["non_blocking_diagnostics"][
        "no_worse_mean_crps_than_legacy"
    ] is False
    assert manifest["activates_production"] is False
    assert manifest["adds_fourth_service"] is False


def test_release_build_does_not_require_strict_asof_calibration() -> None:
    state = _artifact("identifiable_physical_state_model_v1.candidate.json")
    observation = _artifact("identifiable_physical_observation_v1.candidate.json")
    reporting = _artifact("amsc_frozen_reporting_operator_v1.candidate.json")
    release = build_release(
        root=ROOT,
        readiness=_ready(state, observation, reporting),
        state_candidate=state,
        observation_candidate=observation,
        reporting_candidate=reporting,
    )
    assert release["manifest"]["promotion_ready"] is True
    assert release["manifest"]["promotion_policy"][
        "research_evidence_excluded_from_blocking_gate_set"
    ] is True


def test_release_rejects_nll_or_crps_in_blocking_gate_set() -> None:
    state = _calibrated_state()
    observation = _artifact("identifiable_physical_observation_v1.candidate.json")
    reporting = _artifact("amsc_frozen_reporting_operator_v1.candidate.json")
    readiness = _ready(state, observation, reporting)
    readiness["blocking_gates"]["mean_nll"] = True
    with pytest.raises(ValueError, match="blocking_gate_not_true"):
        build_release(
            root=ROOT,
            readiness=readiness,
            state_candidate=state,
            observation_candidate=observation,
            reporting_candidate=reporting,
        )


def test_cutover_verifier_accepts_nonblocking_diagnostic_failures_and_binds_files(
    tmp_path: Path,
) -> None:
    state = _calibrated_state()
    observation = _artifact("identifiable_physical_observation_v1.candidate.json")
    reporting = _artifact("amsc_frozen_reporting_operator_v1.candidate.json")
    release = build_release(
        root=ROOT,
        readiness=_ready(state, observation, reporting),
        state_candidate=state,
        observation_candidate=observation,
        reporting_candidate=reporting,
    )
    for name, value in release["file_bytes"].items():
        (tmp_path / name).write_bytes(value)
    (tmp_path / "release_manifest.json").write_text(
        json.dumps(release["manifest"], ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )

    verified = verify_release(tmp_path)

    assert verified["manifest"]["non_blocking_diagnostics"][
        "no_worse_mean_nll_than_legacy"
    ] is False
    assert verified["activation"]["nll_crps_activation_role"] == (
        "non_blocking_diagnostic_only"
    )

    activation_path = tmp_path / "production_probability_runtime_activation.json"
    activation_path.write_bytes(activation_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="release_file_sha_mismatch"):
        verify_release(tmp_path)
