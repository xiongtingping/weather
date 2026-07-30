from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.p0_historical_meta import (
    P0HistoricalMetaCombinationArtifact,
    P0HistoricalTiltProof,
    build_p0_historical_meta_combination_artifact,
    compile_p0_historical_tilt_proof,
)
from bayes_basket_target.same_day_climatology import (
    build_same_day_climatology_artifact,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
CENTERS = {"33": 33.0, "34": 34.0, "35": 35.0, "36": 36.0, "37": 37.0}
BASE = {"33": 0.10, "34": 0.35, "35": 0.35, "36": 0.15, "37": 0.05}


def _truth(
    truth_id: str,
    local_date: str,
    temperature: float,
    *,
    truth_sha: str,
) -> dict[str, object]:
    return {
        "truth_id": truth_id,
        "station_id": "EDDM",
        "local_date": local_date,
        "official_tmax_c": temperature,
        "settled_at_utc": f"{local_date}T22:00:00Z",
        "settlement_closed": True,
        "source_lane": "AWC",
        "settlement_definition": "daily_high_c",
        "settlement_timezone": "Europe/Berlin",
        "sample_role": "SAME_DAY_CORE",
        "trend_adjustment_c": 0.0,
        "station_regime_adjustment_c": 0.0,
        "weight": 1.0,
        "truth_content_sha256": truth_sha,
    }


def _climate(*, temperatures=(36.0, 37.0)):
    return build_same_day_climatology_artifact(
        station_id="EDDM",
        settlement_timezone="Europe/Berlin",
        settlement_definition="daily_high_c",
        target_year=2026,
        calendar_day="07-30",
        truth_samples=[
            _truth("clim-2023", "2023-07-30", temperatures[0], truth_sha=SHA_A),
            _truth("clim-2024", "2024-07-30", temperatures[1], truth_sha=SHA_B),
        ],
        bucket_centers_c=CENTERS,
        kde_bandwidth_c=0.8,
        activation_id="climate-activation",
        built_at_utc="2026-01-01T00:00:00Z",
        valid_from_utc="2026-01-01T00:00:00Z",
        valid_until_utc="2027-01-01T00:00:00Z",
    )


def _fold(
    fold_id: str,
    target_date: str,
    evaluation_truth_id: str,
    *,
    climate_sha: str = SHA_C,
) -> dict[str, object]:
    return {
        "fold_id": fold_id,
        "target_local_date": target_date,
        "evaluation_truth_id": evaluation_truth_id,
        "model_probability_sha256": SHA_A,
        "model_training_truth_ids": [f"model-train-{fold_id}"],
        "climatology_training_truth_ids": [f"climate-train-{fold_id}"],
        "climatology_artifact_sha256": climate_sha,
    }


def _meta(
    *,
    baseline=None,
    candidate=None,
    center_delta_to_theta: float = 1.0,
    max_abs_theta: float = 2.0,
    max_center_displacement_buckets: int = 2,
    max_kl_divergence: float = 0.5,
    max_mass: float = 0.4,
    max_amplification: float = 3.0,
    reversal_policy: str = "FORBID_STRONG_REVERSAL",
    reversal_validated: bool = False,
    folds=None,
):
    return build_p0_historical_meta_combination_artifact(
        activation_id="meta-activation",
        activation_content_sha256=SHA_D,
        station_id="EDDM",
        season_bin="JJA",
        lead_bucket="d1",
        run_cycle="00Z",
        settlement_definition="daily_high_c",
        settlement_timezone="Europe/Berlin",
        trained_through_utc="2025-12-31T23:59:59Z",
        base_ensemble_artifact_sha256=SHA_A,
        base_ensemble_selection_evidence_sha256=SHA_B,
        oof_folds=folds
        or [
            _fold("fold-2024", "2024-07-30", "eval-2024"),
            _fold("fold-2025", "2025-07-30", "eval-2025"),
        ],
        effective_sample_n=2.0,
        minimum_effective_sample_n=2.0,
        baseline_oos_metrics=baseline
        or {"nll": 0.9, "brier_score": 0.25, "crps": 0.8},
        candidate_oos_metrics=candidate
        or {"nll": 0.8, "brier_score": 0.24, "crps": 0.7},
        center_delta_to_theta=center_delta_to_theta,
        max_abs_theta=max_abs_theta,
        max_center_displacement_buckets=max_center_displacement_buckets,
        max_kl_divergence=max_kl_divergence,
        max_total_probability_mass_transfer=max_mass,
        max_single_bucket_amplification=max_amplification,
        strong_direction_threshold=0.05,
        reversal_policy=reversal_policy,
        oos_reversal_validated=reversal_validated,
        valid_from_utc="2026-01-01T00:00:00Z",
        valid_until_utc="2027-01-01T00:00:00Z",
    )


def _proof(meta=None, climate=None, probabilities=None):
    probabilities = probabilities or BASE
    resolved_meta = meta or _meta()
    probability_sha = content_sha256(
        {"bucket_centers_c": CENTERS, "probabilities": probabilities}
    )
    return compile_p0_historical_tilt_proof(
        artifact=resolved_meta,
        climatology=climate or _climate(),
        station_id="EDDM",
        target_local_date="2026-07-30",
        settlement_timezone="Europe/Berlin",
        settlement_definition="daily_high_c",
        season_bin="JJA",
        lead_bucket="d1",
        run_cycle="00Z",
        as_of_utc="2026-07-29T00:00:00Z",
        model_probabilities=probabilities,
        bucket_centers_c=CENTERS,
        model_probability_sha256=probability_sha,
        ensemble_artifact_sha256=SHA_A,
        ensemble_selection_evidence_sha256=SHA_B,
        ensemble_activation_sha256=SHA_B,
        climatology_activation_sha256=SHA_C,
        meta_activation_id=resolved_meta.activation_id,
        meta_activation_sha256=SHA_D,
    )


def test_builds_oof_meta_artifact_and_deterministic_bounded_proof() -> None:
    meta = _meta()
    proof = _proof(meta=meta)
    duplicate = _proof(meta=meta)

    assert meta.theta_enabled
    assert proof.applied_theta > 0.0
    assert proof.expected_center_c_after > proof.expected_center_c_before
    assert proof.total_probability_mass_transfer <= (
        meta.max_total_probability_mass_transfer
    )
    assert proof.kl_divergence <= meta.max_kl_divergence
    assert proof.max_single_bucket_amplification <= (
        meta.max_single_bucket_amplification
    )
    assert duplicate.content_sha256 == proof.content_sha256
    assert duplicate.proof_id == proof.proof_id
    assert P0HistoricalTiltProof.from_mapping(
        proof.to_dict()
    ).content_sha256 == proof.content_sha256
    assert P0HistoricalMetaCombinationArtifact.from_mapping(
        meta.to_dict()
    ).content_sha256 == meta.content_sha256


def test_no_oos_gain_forces_theta_zero_without_blocking_p0() -> None:
    no_gain = _meta(
        candidate={"nll": 0.91, "brier_score": 0.25, "crps": 0.8},
        center_delta_to_theta=4.0,
        max_abs_theta=4.0,
    )
    proof = _proof(meta=no_gain)

    assert not no_gain.stable_oos_gain
    assert no_gain.center_delta_to_theta == 0.0
    assert no_gain.max_abs_theta == 0.0
    assert proof.theta_zero_fallback
    assert dict(proof.probabilities_after) == BASE


def test_rejects_same_fold_truth_leakage_and_duplicate_station_day_fold() -> None:
    leaking = _fold("fold-leak", "2024-07-30", "eval-leak")
    leaking["model_training_truth_ids"] = ["eval-leak"]
    with pytest.raises(
        ContractError, match="p0_historical_same_fold_truth_leakage"
    ):
        _meta(folds=[leaking])

    with pytest.raises(
        ContractError, match="p0_historical_duplicate_station_day_fold"
    ):
        _meta(
            folds=[
                _fold("fold-a", "2024-07-30", "eval-a"),
                _fold("fold-b", "2024-07-30", "eval-b"),
            ]
        )


@pytest.mark.parametrize(
    ("meta", "error"),
    [
        (
            lambda: _meta(max_center_displacement_buckets=0),
            "p0_historical_center_displacement_limit_exceeded",
        ),
        (
            lambda: _meta(max_kl_divergence=0.00001),
            "p0_historical_kl_limit_exceeded",
        ),
        (
            lambda: _meta(max_mass=0.00001),
            "p0_historical_mass_transfer_limit_exceeded",
        ),
        (
            lambda: _meta(max_amplification=1.00001),
            "p0_historical_single_bucket_amplification_limit_exceeded",
        ),
    ],
)
def test_center_kl_mass_and_single_bucket_amplification_limits_fail_closed(
    meta, error: str
) -> None:
    with pytest.raises(ContractError, match=error):
        _proof(meta=meta())


def test_low_high_low_replay_rewinds_from_model_root() -> None:
    meta = _meta()
    low_climate = _climate(temperatures=(33.0, 33.0))
    high_climate = _climate(temperatures=(37.0, 37.0))

    low_first = _proof(meta=meta, climate=low_climate)
    high = _proof(meta=meta, climate=high_climate)
    low_replay = _proof(meta=meta, climate=low_climate)

    assert low_first.applied_theta < 0.0 < high.applied_theta
    assert low_first.content_sha256 == low_replay.content_sha256
    assert dict(low_first.probabilities_after) == dict(
        low_replay.probabilities_after
    )
    assert low_first.content_sha256 != high.content_sha256


def test_strong_direction_reversal_requires_explicit_oos_policy() -> None:
    strong_up = {
        "33": 0.05,
        "34": 0.10,
        "35": 0.35,
        "36": 0.25,
        "37": 0.25,
    }
    cold_climate = _climate(temperatures=(33.0, 33.0))
    restrictive = _meta(
        center_delta_to_theta=1.5,
        max_abs_theta=20.0,
        max_center_displacement_buckets=4,
        max_kl_divergence=10.0,
        max_mass=1.0,
        max_amplification=100.0,
    )
    with pytest.raises(
        ContractError, match="p0_historical_unproven_direction_reversal"
    ):
        _proof(
            meta=restrictive,
            climate=cold_climate,
            probabilities=strong_up,
        )

    permissive = _meta(
        center_delta_to_theta=1.5,
        max_abs_theta=20.0,
        max_center_displacement_buckets=4,
        max_kl_divergence=10.0,
        max_mass=1.0,
        max_amplification=100.0,
        reversal_policy="ALLOW_OOS_PROVEN_REVERSAL",
        reversal_validated=True,
    )
    proof = _proof(
        meta=permissive,
        climate=cold_climate,
        probabilities=strong_up,
    )
    assert proof.reversal_applied
    assert proof.reversal_oos_validated


def test_scope_hash_and_proof_tampering_fail_closed() -> None:
    with pytest.raises(
        ContractError, match="p0_historical_model_probability_sha256_mismatch"
    ):
        compile_p0_historical_tilt_proof(
            artifact=_meta(),
            climatology=_climate(),
            station_id="EDDM",
            target_local_date="2026-07-30",
            settlement_timezone="Europe/Berlin",
            settlement_definition="daily_high_c",
            season_bin="JJA",
            lead_bucket="d1",
            run_cycle="00Z",
            as_of_utc="2026-07-29T00:00:00Z",
            model_probabilities=BASE,
            bucket_centers_c=CENTERS,
            model_probability_sha256=SHA_D,
            ensemble_artifact_sha256=SHA_A,
            ensemble_selection_evidence_sha256=SHA_B,
            ensemble_activation_sha256=SHA_B,
            climatology_activation_sha256=SHA_C,
            meta_activation_id=_meta().activation_id,
            meta_activation_sha256=SHA_D,
        )

    with pytest.raises(
        ContractError,
        match="base_ensemble_selection_evidence_sha256_mismatch",
    ):
        compile_p0_historical_tilt_proof(
            artifact=_meta(),
            climatology=_climate(),
            station_id="EDDM",
            target_local_date="2026-07-30",
            settlement_timezone="Europe/Berlin",
            settlement_definition="daily_high_c",
            season_bin="JJA",
            lead_bucket="d1",
            run_cycle="00Z",
            as_of_utc="2026-07-29T00:00:00Z",
            model_probabilities=BASE,
            bucket_centers_c=CENTERS,
            model_probability_sha256=content_sha256(
                {
                    "bucket_centers_c": CENTERS,
                    "probabilities": BASE,
                }
            ),
            ensemble_artifact_sha256=SHA_A,
            ensemble_selection_evidence_sha256=SHA_D,
            ensemble_activation_sha256=SHA_B,
            climatology_activation_sha256=SHA_C,
            meta_activation_id=_meta().activation_id,
            meta_activation_sha256=SHA_D,
        )

    raw = deepcopy(_proof().to_dict())
    raw["expected_center_c_after"] += 0.25
    raw["content_sha256"] = content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )
    with pytest.raises(
        ContractError, match="p0_historical_proof_expected_center_after_mismatch"
    ):
        P0HistoricalTiltProof.from_mapping(raw)
