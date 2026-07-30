from __future__ import annotations

from copy import deepcopy
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.p0_historical_meta import (
    P0HistoricalMetaCombinationArtifact,
)
from bayes_basket_target.p0_historical_meta_training import (
    train_p0_historical_meta_artifact,
)
from bayes_basket_target.probability_evidence_store import (
    apply_probability_evidence_schema,
)
from bayes_basket_target.same_day_climatology import (
    build_same_day_climatology_artifact,
)
from scripts.build_bayes_basket_target_p0_historical_meta_oos import (
    build_ingress_packet,
    materialize_training_result,
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
        "model_training_truths": [{
            "truth_id": f"model-train-{year - 1}",
            "target_local_date": f"{year - 1}-07-30",
            "settled_at_utc": f"{year - 1}-07-31T00:00:00Z",
        }],
        "climatology_training_truths": [{
            "truth_id": f"climate-train-{year - 1}",
            "target_local_date": f"{year - 1}-07-30",
            "settled_at_utc": f"{year - 1}-07-31T00:00:00Z",
        }],
        "climatology_artifact_sha256": f"{year % 10}" * 64,
        "model_probabilities": {
            "34": 0.60,
            "35": 0.30,
            "36": 0.10,
        },
        "climatology_probabilities": {
            "34": 0.05,
            "35": 0.15,
            "36": 0.80,
        },
        "bucket_centers_c": {"34": 34.0, "35": 35.0, "36": 36.0},
        "truth_bucket": "36",
        "weight": 1.0,
    }


def _parameters() -> dict[str, object]:
    return {
        "activation_id": "historical-meta-activation-1",
        "activation_content_sha256": "a" * 64,
        "station_id": "RKPK",
        "season_bin": "JJA",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "daily_high_c",
        "settlement_timezone": "Asia/Seoul",
        "trained_through_utc": "2025-12-31T23:59:59Z",
        "base_ensemble_artifact_sha256": "b" * 64,
        "base_ensemble_selection_evidence_sha256": "c" * 64,
        "minimum_effective_sample_n": 3.0,
        "max_abs_theta": 8.0,
        "max_center_displacement_buckets": 2,
        "max_kl_divergence": 2.0,
        "max_total_probability_mass_transfer": 0.8,
        "max_single_bucket_amplification": 20.0,
        "strong_direction_threshold": 0.05,
        "valid_from_utc": "2026-01-01T00:00:00Z",
        "valid_until_utc": "2027-01-01T00:00:00Z",
    }


def test_walk_forward_oos_uses_only_prior_folds_and_builds_artifact() -> None:
    result = train_p0_historical_meta_artifact(
        folds=[_fold(year) for year in range(2021, 2026)],
        coefficient_candidates=[0.0, 1.0, 2.0, 4.0],
        minimum_meta_training_folds=1,
        artifact_parameters=_parameters(),
    )
    artifact = P0HistoricalMetaCombinationArtifact.from_mapping(
        result["artifact"]
    )
    evidence = result["training_evidence"]

    assert evidence["selected_by_fold"][0]["selected_coefficient"] == 0.0
    assert all(
        row["prior_fold_count"] == index
        for index, row in enumerate(evidence["selected_by_fold"])
    )
    assert evidence["final_coefficient"] > 0.0
    assert artifact.stable_oos_gain
    assert artifact.center_delta_to_theta > 0.0
    assert artifact.oos_training_evidence_sha256


def test_walk_forward_training_rejects_same_fold_truth_leakage() -> None:
    leaking = _fold(2025)
    leaking["model_training_truths"] = [{
        "truth_id": "truth-2025",
        "target_local_date": "2024-07-30",
        "settled_at_utc": "2024-07-31T00:00:00Z",
    }]
    with pytest.raises(
        ContractError,
        match="historical_meta_training_truth_lineage_invalid",
    ):
        train_p0_historical_meta_artifact(
            folds=[_fold(2024), leaking],
            coefficient_candidates=[0.0, 1.0],
            minimum_meta_training_folds=1,
            artifact_parameters=_parameters(),
        )


def test_walk_forward_training_rejects_late_training_truth() -> None:
    late = _fold(2025)
    late["model_training_truths"] = [{
        "truth_id": "late-training-truth",
        "target_local_date": "2025-07-30",
        "settled_at_utc": "2025-07-30T13:00:00Z",
    }]
    with pytest.raises(
        ContractError,
        match="historical_meta_training_truth_lineage_invalid",
    ):
        train_p0_historical_meta_artifact(
            folds=[_fold(2024), late],
            coefficient_candidates=[0.0, 1.0],
            minimum_meta_training_folds=1,
            artifact_parameters=_parameters(),
        )


def test_walk_forward_training_rejects_future_truth() -> None:
    future = _fold(2026)
    parameters = deepcopy(_parameters())
    parameters["trained_through_utc"] = "2025-12-31T23:59:59Z"
    with pytest.raises(
        ContractError,
        match="historical_meta_training_future_truth_leakage",
    ):
        train_p0_historical_meta_artifact(
            folds=[_fold(2025), future],
            coefficient_candidates=[0.0, 1.0],
            minimum_meta_training_folds=1,
            artifact_parameters=parameters,
        )


def test_oos_result_materializes_formal_artifact_atomically_no_live() -> None:
    result = train_p0_historical_meta_artifact(
        folds=[_fold(year) for year in range(2021, 2026)],
        coefficient_candidates=[0.0, 1.0, 2.0, 4.0],
        minimum_meta_training_folds=1,
        artifact_parameters=_parameters(),
    )
    with sqlite3.connect(":memory:") as conn:
        apply_probability_evidence_schema(conn)
        receipt = materialize_training_result(
            conn,
            result=result,
            generation=1,
            activated_at_utc="2026-01-01T00:00:00Z",
        )

        assert receipt["oos_gate_passed"]
        assert receipt["authority_scope"] == "FORMAL_PROMOTED"
        assert receipt["ingress"]["state"] == "COMMITTED"
        assert receipt["ingress"]["row_count"] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_evidence_artifact"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_evidence_activation"
        ).fetchone()[0] == 1
        assert not receipt["calls_order_gateway"]
        assert receipt["orders_submitted"] == 0


def test_formal_packet_co_promotes_same_day_climatology_pair() -> None:
    result = train_p0_historical_meta_artifact(
        folds=[_fold(year) for year in range(2021, 2026)],
        coefficient_candidates=[0.0, 1.0, 2.0, 4.0],
        minimum_meta_training_folds=1,
        artifact_parameters=_parameters(),
    )
    climate = build_same_day_climatology_artifact(
        station_id="RKPK",
        settlement_timezone="Asia/Seoul",
        settlement_definition="daily_high_c",
        target_year=2026,
        calendar_day="07-30",
        truth_samples=[
            {
                "truth_id": "climate-truth-2024",
                "station_id": "RKPK",
                "local_date": "2024-07-30",
                "official_tmax_c": 35.0,
                "settled_at_utc": "2024-07-31T00:00:00Z",
                "settlement_closed": True,
                "source_lane": "AWC",
                "settlement_definition": "daily_high_c",
                "settlement_timezone": "Asia/Seoul",
                "sample_role": "SAME_DAY_CORE",
                "trend_adjustment_c": 0.0,
                "station_regime_adjustment_c": 0.0,
                "weight": 1.0,
                "truth_content_sha256": "d" * 64,
            }
        ],
        bucket_centers_c={"34": 34.0, "35": 35.0, "36": 36.0},
        kde_bandwidth_c=1.0,
        activation_id="same-day-rkpk-2026-07-30",
        built_at_utc="2025-12-31T23:59:59Z",
        valid_from_utc="2026-01-01T00:00:00Z",
        valid_until_utc="2027-01-01T00:00:00Z",
    )
    packet = build_ingress_packet(
        result=result,
        generation=1,
        activated_at_utc="2026-01-01T00:00:00Z",
        climatology_artifact=climate.to_dict(),
    )
    assert len(packet["artifacts"]) == 2
    assert len(packet["promotions"]) == 2
    assert len(packet["activations"]) == 2
    with sqlite3.connect(":memory:") as conn:
        apply_probability_evidence_schema(conn)
        receipt = ingest(conn, packet, commit=True)
        assert receipt["row_count"] == 6
