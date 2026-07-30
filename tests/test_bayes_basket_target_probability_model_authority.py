from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bayes_basket_target import ContractError, ProbabilityEnvelope, content_sha256
from bayes_basket_target.production_io import (
    assert_formal_probability_model_authority,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_bayes_basket_target import _envelope


def _mutate_root(
    *,
    fingerprint_changes: dict[str, object] | None = None,
    lineage_changes: dict[str, str] | None = None,
    recompute_fingerprint: bool = True,
) -> ProbabilityEnvelope:
    raw = _envelope().to_dict()
    lineage = dict(raw["lineage"])
    fingerprint = json.loads(lineage["taf_nwp_live_compatibility_json"])
    fingerprint.update(fingerprint_changes or {})
    if recompute_fingerprint:
        identity = dict(fingerprint)
        identity.pop("fingerprint_sha256", None)
        fingerprint["fingerprint_sha256"] = content_sha256(identity)
        lineage["taf_nwp_live_compatibility_fingerprint_sha256"] = (
            fingerprint["fingerprint_sha256"]
        )
    lineage["taf_nwp_live_compatibility_json"] = json.dumps(
        fingerprint,
        sort_keys=True,
        separators=(",", ":"),
    )
    lineage.update(lineage_changes or {})
    raw["lineage"] = lineage
    return ProbabilityEnvelope.from_mapping(raw)


def _downstream(root: ProbabilityEnvelope, *, stage: str = "P0-M") -> ProbabilityEnvelope:
    raw = root.to_dict()
    raw.update(
        {
            "stage": stage,
            "checkpoint_id": stage.lower().replace("-", "") + "-test",
            "decision_at_utc": "2026-07-23T04:01:00Z",
            "posterior_version": stage.lower() + "-test",
            "lineage": {
                "settlement_timezone": root.settlement_timezone,
                "p0_root_checkpoint_id": root.checkpoint_id,
                "p0_root_content_sha256": root.content_sha256,
            },
        }
    )
    return ProbabilityEnvelope.from_mapping(raw)


def test_formal_probability_authority_accepts_two_model_root_and_downstream() -> None:
    root = _envelope()
    downstream = _downstream(root)
    assert (
        assert_formal_probability_model_authority([root], probability=root)
        is root
    )
    assert (
        assert_formal_probability_model_authority(
            [root, downstream],
            probability=downstream,
        )
        is root
    )


def test_formal_probability_authority_rejects_single_model() -> None:
    calibrations = {
        "ECMWF": {
            "calibration_artifact_id": "cal-ecmwf-test",
            "calibration_artifact_content_sha256": "1" * 64,
        }
    }
    root = _mutate_root(
        fingerprint_changes={
            "available_models": ["ECMWF"],
            "resolved_weights": {"ECMWF": 1.0},
            "model_calibration_artifacts": calibrations,
        }
    )
    with pytest.raises(ContractError, match="model_combination_invalid"):
        assert_formal_probability_model_authority([root], probability=root)


def test_formal_probability_authority_rejects_only_one_positive_weight() -> None:
    root = _mutate_root(
        fingerprint_changes={
            "resolved_weights": {"ECMWF": 1.0, "GFS": 0.0},
        }
    )
    with pytest.raises(ContractError, match="positive_weight_models_too_few"):
        assert_formal_probability_model_authority([root], probability=root)


def test_formal_probability_authority_rejects_weight_sum_drift() -> None:
    root = _mutate_root(
        fingerprint_changes={
            "resolved_weights": {"ECMWF": 0.7, "GFS": 0.4},
        }
    )
    with pytest.raises(ContractError, match="weights_not_normalized"):
        assert_formal_probability_model_authority([root], probability=root)


def test_formal_probability_authority_requires_exact_member_centers() -> None:
    root = _mutate_root(
        lineage_changes={"formal_model_members_json": "[]"},
    )
    with pytest.raises(
        ContractError,
        match="model_members_incomplete",
    ):
        assert_formal_probability_model_authority([root], probability=root)


def test_formal_probability_authority_rejects_model_set_hash_drift() -> None:
    root = _mutate_root(
        lineage_changes={"available_model_set_hash": "0" * 64},
    )
    with pytest.raises(ContractError, match="model_set_hash_mismatch"):
        assert_formal_probability_model_authority([root], probability=root)


def test_formal_probability_authority_rejects_fingerprint_sha_drift() -> None:
    root = _mutate_root(
        fingerprint_changes={"run_cycle": "12Z"},
        recompute_fingerprint=False,
    )
    with pytest.raises(ContractError, match="compatibility_sha_mismatch"):
        assert_formal_probability_model_authority([root], probability=root)


def test_formal_probability_authority_rejects_ensemble_binding_drift() -> None:
    root = _mutate_root(
        lineage_changes={"ensemble_artifact_content_sha256": "9" * 64},
    )
    with pytest.raises(ContractError, match="ensemble_artifact_sha_mismatch"):
        assert_formal_probability_model_authority([root], probability=root)


def test_formal_probability_authority_rejects_missing_or_wrong_downstream_root() -> None:
    root = _envelope()
    downstream = _downstream(root)
    raw = downstream.to_dict()
    raw["lineage"] = {
        **raw["lineage"],
        "p0_root_checkpoint_id": "missing-root",
    }
    wrong = ProbabilityEnvelope.from_mapping(raw)
    with pytest.raises(ContractError, match="p0_root_checkpoint_missing"):
        assert_formal_probability_model_authority(
            [root, wrong],
            probability=wrong,
        )

    raw = downstream.to_dict()
    raw["lineage"] = {
        **raw["lineage"],
        "p0_root_content_sha256": "8" * 64,
    }
    wrong_sha = ProbabilityEnvelope.from_mapping(raw)
    with pytest.raises(ContractError, match="p0_root_content_sha_mismatch"):
        assert_formal_probability_model_authority(
            [root, wrong_sha],
            probability=wrong_sha,
        )
