from __future__ import annotations

import os
import subprocess
import sys

import pytest

from bayes_basket_target import ContractError, model_set_hash
from bayes_basket_target.calibration import (
    _formal_model_masks,
    fit_model_calibration_artifact,
    select_newest_compatible_model_run_bundle,
    validate_exact_model_scope,
    validate_formal_ensemble_artifact,
)
from bayes_basket_target.contracts import ResidualObservation
from tests.test_bayes_basket_target import (
    DECISION,
    _ensemble,
    _artifact,
    _rehash_ensemble,
    _runs,
)


def test_five_model_pool_exposes_all_twenty_six_formal_subsets() -> None:
    models = ("ECMWF", "GFS", "ICON", "GEM", "CMA")
    masks = _formal_model_masks(models)

    assert len(masks) == 26
    assert model_set_hash(("ECMWF", "GEM")) in masks
    assert model_set_hash(models) in masks
    assert model_set_hash(("CMA",)) not in masks


def test_gem_and_cma_have_independent_bias_calibration_artifacts() -> None:
    gem = _artifact("GEM", 0.4)
    cma = _artifact("CMA", -0.3)

    assert gem.model_id == "GEM"
    assert cma.model_id == "CMA"
    assert gem.bias_c > 0
    assert cma.bias_c < 0


def test_ecmwf_one_hundred_percent_is_not_a_formal_ensemble() -> None:
    base = _ensemble()
    masks = {key: dict(value) for key, value in base.supported_masks.items()}
    masks[model_set_hash(("ECMWF", "GFS", "ICON"))] = {
        "ECMWF": 1.0,
        "GFS": 0.0,
        "ICON": 0.0,
    }
    with pytest.raises(ContractError):
        _rehash_ensemble(base, supported_masks=masks)


def test_two_model_formal_candidate_passes_with_positive_oos_weights() -> None:
    artifact = _rehash_ensemble(
        _ensemble(),
        candidate_models=["ECMWF", "GFS"],
        supported_masks={model_set_hash(("ECMWF", "GFS")): {"ECMWF": 0.7, "GFS": 0.3}},
    )
    validate_formal_ensemble_artifact(artifact)
    assert artifact.resolve_weights(("ECMWF", "GFS")) == {"ECMWF": 0.7, "GFS": 0.3}


def test_three_model_non_equal_oos_weights_are_preserved() -> None:
    base = _ensemble()
    masks = {key: dict(value) for key, value in base.supported_masks.items()}
    masks[model_set_hash(("ECMWF", "GFS", "ICON"))] = {
        "ECMWF": 0.60,
        "GFS": 0.25,
        "ICON": 0.15,
    }
    artifact = _rehash_ensemble(base, supported_masks=masks)
    validate_formal_ensemble_artifact(artifact)
    assert artifact.resolve_weights(("ECMWF", "GFS", "ICON"))["ECMWF"] == pytest.approx(0.60)


def test_newest_incomplete_run_falls_back_to_previous_compatible_exact_bundle() -> None:
    old = _runs()
    newest_partial = [
        type(row).from_mapping({
            **row.__dict__,
            "model_run_id": f"new-{row.model_id}",
            "run_init_utc": "2026-07-23T00:00:00Z",
            "published_at_utc": "2026-07-23T01:00:00Z",
            "received_at_utc": "2026-07-23T01:01:00Z",
        })
        for row in old[:2]
    ]
    selected = select_newest_compatible_model_run_bundle(
        [*old, *newest_partial],
        decision_at_utc=DECISION,
    )
    assert {row.run_init_utc for row in selected} == {"2026-07-22T00:00:00Z"}
    assert {row.model_id for row in selected} == {"ECMWF", "GFS", "ICON"}


def test_wrong_three_model_mask_is_rejected_even_when_contract_parser_accepts_it() -> None:
    pair = model_set_hash(("ECMWF", "GFS"))
    artifact = _rehash_ensemble(
        _ensemble(),
        supported_masks={pair: {"ECMWF": 0.6, "GFS": 0.4}},
    )
    with pytest.raises(ContractError, match="exact_masks"):
        validate_formal_ensemble_artifact(artifact)


def test_single_model_is_blocked_before_formal_activation() -> None:
    with pytest.raises(ContractError):
        _rehash_ensemble(
            _ensemble(),
            candidate_models=["ECMWF"],
            supported_masks={model_set_hash(("ECMWF",)): {"ECMWF": 1.0}},
        )


def test_scope_cutoff_and_truth_revision_mismatch_fail_closed() -> None:
    raw = [dict(row.__dict__) for row in _runs()[:2]]
    raw[0].update({"cutoff_utc": DECISION, "truth_revision": "r1"})
    raw[1].update({"cutoff_utc": DECISION, "truth_revision": "r2"})
    with pytest.raises(ContractError, match="exact_model_scope_mismatch"):
        validate_exact_model_scope(raw, decision_at_utc=DECISION)


def test_fit_excludes_residual_not_yet_paired_at_training_cutoff() -> None:
    known = ResidualObservation(
        residual_id="known",
        station_id="ZUUU",
        model_id="ECMWF",
        season_bin="summer",
        lead_bucket="d1",
        run_cycle="00Z",
        residual_c=1.0,
        target_local_date="2026-07-20",
        paired_asof_utc="2026-07-21T00:00:00Z",
    )
    future_truth = ResidualObservation(
        residual_id="future-truth",
        station_id="ZUUU",
        model_id="ECMWF",
        season_bin="summer",
        lead_bucket="d1",
        run_cycle="00Z",
        residual_c=100.0,
        target_local_date="2026-07-21",
        paired_asof_utc="2026-07-23T00:00:00Z",
    )
    artifact = fit_model_calibration_artifact(
        [known, future_truth],
        artifact_id="strict-asof-fit",
        trained_through_utc="2026-07-22T00:00:00Z",
        valid_from_utc="2026-07-22T00:00:00Z",
        valid_until_utc="2026-08-22T00:00:00Z",
        parent_bias_c=0.0,
        parent_scale_c=1.0,
        kappa=0.0,
    )

    assert artifact.sample_n == 1
    assert artifact.bias_c == pytest.approx(1.0)


def test_formal_ensemble_rejects_artifact_valid_before_holdout() -> None:
    artifact = _rehash_ensemble(
        _ensemble(),
        valid_from_utc="2026-06-29T00:00:00Z",
    )
    with pytest.raises(
        ContractError,
        match="formal_ensemble_valid_before_holdout",
    ):
        validate_formal_ensemble_artifact(artifact)


def test_low_high_low_rewind_and_hash_seed_are_deterministic() -> None:
    base = _ensemble()
    high = _rehash_ensemble(
        base,
        supported_masks={
            **{key: dict(value) for key, value in base.supported_masks.items()},
            model_set_hash(("ECMWF", "GFS", "ICON")): {
                "ECMWF": 0.60,
                "GFS": 0.25,
                "ICON": 0.15,
            },
        },
    )
    rewind = _rehash_ensemble(
        high,
        supported_masks={key: dict(value) for key, value in base.supported_masks.items()},
    )
    assert rewind.content_sha256 == base.content_sha256

    code = "from bayes_basket_target import content_sha256; print(content_sha256({'b': 2, 'a': 1}))"
    outputs = []
    for seed in ("1", "31337", "20260727"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outputs.append(subprocess.check_output([sys.executable, "-c", code], env=env, text=True).strip())
    assert len(set(outputs)) == 1
