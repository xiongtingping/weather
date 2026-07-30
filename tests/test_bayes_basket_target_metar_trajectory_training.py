from __future__ import annotations

from copy import deepcopy
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.metar_trajectory import (
    MetarTrajectoryLikelihoodArtifact,
)
from bayes_basket_target.metar_trajectory_training import (
    train_metar_trajectory_artifact,
)
from bayes_basket_target.probability_evidence_store import (
    apply_probability_evidence_schema,
)
from scripts.build_bayes_basket_target_metar_trajectory_oos import (
    build_ingress_packet,
)
from scripts.ingest_bayes_basket_probability_evidence import ingest


def _fold(year: int) -> dict[str, object]:
    return {
        "fold_id": f"fold-{year}",
        "target_local_date": f"{year}-07-30",
        "evaluation_truth_id": f"truth-{year}",
        "evaluation_truth_settled_at_utc": (
            f"{year}-07-31T00:00:00Z"
        ),
        "training_cutoff_utc": f"{year}-07-30T12:00:00Z",
        "trajectory_training_truths": [{
            "truth_id": f"prior-{year - 1}",
            "target_local_date": f"{year - 1}-07-30",
            "settled_at_utc": f"{year - 1}-07-31T00:00:00Z",
        }],
        "canonical_observation_sequence_sha256": (
            f"{year % 10}" * 64
        ),
        "remaining_warming_c": 2.0,
        "root_probabilities": {
            "0C": 1 / 3,
            "1C": 1 / 3,
            "2C": 1 / 3,
        },
        "bucket_centers_c": {
            "0C": 0.0,
            "1C": 1.0,
            "2C": 2.0,
        },
        "bucket_upper_bounds_c": {
            "0C": 0.49,
            "1C": 1.49,
            "2C": 2.49,
        },
        "truth_bucket": "2C",
        "running_high_c": 0.0,
        "weight": 1.0,
    }


def _parameters() -> dict[str, object]:
    return {
        "activation_id": "metar-trajectory-activation-rkpk-v1",
        "station_id": "RKPK",
        "source_lane": "awc",
        "season_bin": "JJA",
        "local_time_bucket": "AFTERNOON",
        "maturity_bin": "D0",
        "settlement_definition": "station_local_calendar_day_tmax",
        "settlement_timezone": "Asia/Seoul",
        "feature_definitions": {
            "running_high_c": "maximum canonical dry-bulb as-of",
            "remaining_warming_c": "final tmax minus running high",
        },
        "missing_observation_policy": "DEGRADE_STRENGTH",
        "expected_interval_minutes": 60,
        "long_interval_minutes": 90,
        "max_missing_intervals": 4,
        "missing_interval_strength_factor": 0.8,
        "long_interval_strength_factor": 0.5,
        "hard_exclusion_baseline_distribution": {
            "0": 1 / 3,
            "1": 1 / 3,
            "2": 1 / 3,
        },
        "base_strength": 0.8,
        "maturity_strength": 0.75,
        "likelihood_ratio_floor": 0.25,
        "likelihood_ratio_ceiling": 4.0,
        "trained_through_utc": "2025-12-31T23:59:59Z",
        "valid_from_utc": "2026-01-01T00:00:00Z",
        "valid_until_utc": "2027-01-01T00:00:00Z",
    }


def _train() -> dict[str, object]:
    return train_metar_trajectory_artifact(
        folds=[_fold(year) for year in range(2021, 2026)],
        smoothing_candidates=[0.1, 1.0, 10.0],
        minimum_training_folds=1,
        minimum_effective_sample_n=3.0,
        artifact_parameters=_parameters(),
    )


def test_expanding_window_oos_builds_formal_artifact() -> None:
    result = _train()
    artifact = MetarTrajectoryLikelihoodArtifact.from_mapping(
        result["artifact"]
    )
    evidence = result["training_evidence"]

    assert evidence["selected_by_fold"][0]["prior_fold_count"] == 0
    assert all(
        row["prior_fold_count"] == index
        for index, row in enumerate(evidence["selected_by_fold"])
    )
    assert evidence["stable_oos_gain"] is True
    assert evidence["formal_probability_oos_available"] is True
    assert evidence["oos_gate_passed"] is True
    assert artifact.activated is True
    assert artifact.remaining_warming_distribution[2.0] > 1 / 3


def test_training_rejects_late_or_unsettled_training_truth() -> None:
    late = _fold(2025)
    late["trajectory_training_truths"] = [{
        "truth_id": "late-truth",
        "target_local_date": "2025-07-30",
        "settled_at_utc": "2025-07-30T13:00:00Z",
    }]
    with pytest.raises(
        ContractError,
        match="metar_trajectory_training_truth_lineage_invalid",
    ):
        train_metar_trajectory_artifact(
            folds=[_fold(2024), late],
            smoothing_candidates=[1.0],
            minimum_training_folds=1,
            minimum_effective_sample_n=1.0,
            artifact_parameters=_parameters(),
        )


def test_training_rejects_future_evaluation_truth() -> None:
    future = _fold(2026)
    with pytest.raises(
        ContractError,
        match="metar_trajectory_training_future_truth_leakage",
    ):
        train_metar_trajectory_artifact(
            folds=[_fold(2025), future],
            smoothing_candidates=[1.0],
            minimum_training_folds=1,
            minimum_effective_sample_n=1.0,
            artifact_parameters=_parameters(),
        )


def test_formal_training_packet_ingress_is_atomic_and_idempotent() -> None:
    packet = build_ingress_packet(
        result=_train(),
        generation=1,
        decided_at_utc="2026-01-01T00:00:00Z",
    )
    with sqlite3.connect(":memory:") as conn:
        apply_probability_evidence_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        staged = ingest(
            conn,
            packet,
            commit=True,
            finalize_transaction=False,
        )
        assert staged["state"] == "STAGED_IN_CALLER_TRANSACTION"
        conn.commit()
        replay = ingest(conn, packet, commit=True)
        assert replay["state"] == "COMMITTED"
        assert replay["artifact_receipts"][0]["wrote"] is False
        assert replay["promotion_receipts"][0]["wrote"] is False
        assert replay["activation_receipts"][0]["wrote"] is False


def test_gate_failure_never_emits_activation() -> None:
    parameters = deepcopy(_parameters())
    result = train_metar_trajectory_artifact(
        folds=[_fold(year) for year in range(2021, 2023)],
        smoothing_candidates=[1.0],
        minimum_training_folds=1,
        minimum_effective_sample_n=10.0,
        artifact_parameters=parameters,
    )
    packet = build_ingress_packet(
        result=result,
        generation=1,
        decided_at_utc="2026-01-01T00:00:00Z",
    )
    assert result["training_evidence"]["oos_gate_passed"] is False
    assert packet["activations"] == []
    assert packet["promotions"][0]["decision"] == "REJECTED"


def test_increment_only_oos_can_never_formally_activate() -> None:
    folds = [_fold(year) for year in range(2021, 2026)]
    for fold in folds:
        for key in (
            "root_probabilities",
            "bucket_centers_c",
            "bucket_upper_bounds_c",
            "truth_bucket",
            "running_high_c",
        ):
            fold.pop(key)
    result = train_metar_trajectory_artifact(
        folds=folds,
        smoothing_candidates=[0.1, 1.0, 10.0],
        minimum_training_folds=1,
        minimum_effective_sample_n=3.0,
        artifact_parameters=_parameters(),
    )
    assert result["training_evidence"][
        "increment_distribution_stable_oos_gain"
    ] is True
    assert result["training_evidence"][
        "formal_probability_oos_available"
    ] is False
    assert result["training_evidence"]["oos_gate_passed"] is False
