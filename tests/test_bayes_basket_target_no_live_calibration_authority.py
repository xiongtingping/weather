from __future__ import annotations

from dataclasses import asdict, replace
import itertools

import pytest

from bayes_basket_target import (
    ContractError,
    ModelEnsembleSelectionEvidence,
    content_sha256,
    materialize_p0,
    model_set_hash,
)
from bayes_basket_target.no_live_calibration_authority import (
    _validate_candidate_score_partition,
    _validate_aggregator_lineage,
    build_no_live_calibration_artifact_packet,
)
from tests.test_bayes_basket_target import (
    DECISION,
    _activation,
    _artifact,
    _ensemble,
    _rehash_ensemble,
    _runs,
)


def _selection_for(ensemble):
    identity = {
        "selection_evidence_id": "selection-1",
        "station_id": "ZUUU",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "method_version": "rolling_origin_probability_stack_v4_five_model_pool_min2",
        "train_start_date": "2026-06-01",
        "train_end_date": "2026-06-20",
        "recent_start_date": "2026-06-14",
        "recent_end_date": "2026-06-20",
        "holdout_start_date": "2026-06-21",
        "holdout_end_date": "2026-06-30",
        "candidate_scores": [
            {
                "models": list(subset),
                "formal_selection_eligible": len(subset) >= 2,
                "candidate_role": "formal_ensemble" if len(subset) >= 2 else "single_model_ablation",
            }
            for size in (1, 2, 3)
            for subset in itertools.combinations(("ECMWF", "GFS", "ICON"), size)
        ],
        "selected_models": ["ECMWF", "GFS", "ICON"],
        "selected_weights": ensemble.supported_masks[
            model_set_hash(("ECMWF", "GFS", "ICON"))
        ],
        "oos_metrics": {"brier_score": 0.2, "nll": 0.5, "crps": 0.3},
        "residual_covariance": {
            model: dict(values)
            for model, values in ensemble.residual_covariance.items()
        },
        "residual_correlation": {},
        "effective_sample_n": 20.0,
        "previous_ensemble_artifact_sha256": None,
        "max_weight_delta": 0.2,
        "actual_max_weight_delta": 0.0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_open_meteo_icon_keeps_model_role_separate_from_transport_class() -> None:
    assert _validate_aggregator_lineage([
        {
            "model_id": "ECMWF",
            "provider_id": "ecmwf_ifs025_official",
            "provider_role": "ecmwf_ifs025_official",
            "source_class": "native_official",
            "aggregator_or_republisher": False,
        },
        {
            "model_id": "ICON",
            "provider_id": "icon_global_open_meteo",
            "provider_role": "icon_global_official",
            "source_class": "aggregator_republisher",
            "aggregator_or_republisher": True,
            "native_arrival_indexed": False,
            "prospective_strict_asof_eligible": False,
        },
    ]) == {"ECMWF", "ICON"}


def test_open_meteo_gem_and_cma_enter_candidate_pool_without_native_arrival() -> None:
    rows = [{
        "model_id": "ECMWF",
        "provider_id": "ecmwf_ifs025_official",
        "provider_role": "ecmwf_ifs025_official",
        "source_class": "native_or_official_cloud_replica",
        "aggregator_or_republisher": False,
    }]
    for model, provider, role in (
        ("GEM", "gem_global_open_meteo", "gem_global_open_meteo"),
        ("CMA", "cma_grapes_global_open_meteo", "cma_grapes_global_open_meteo"),
    ):
        rows.append({
            "model_id": model,
            "provider_id": provider,
            "provider_role": role,
            "source_class": "aggregator_republisher",
            "aggregator_or_republisher": True,
            "native_arrival_indexed": False,
            "prospective_strict_asof_eligible": False,
        })

    assert _validate_aggregator_lineage(rows) == {"ECMWF", "GEM", "CMA"}


def test_open_meteo_gem_cannot_forge_native_arrival() -> None:
    with pytest.raises(
        ContractError,
        match="aggregator_native_arrival_forbidden",
    ):
        _validate_aggregator_lineage([
            {
                "model_id": "ECMWF",
                "provider_id": "ecmwf_ifs025_official",
                "provider_role": "ecmwf_ifs025_official",
                "source_class": "native_or_official_cloud_replica",
                "aggregator_or_republisher": False,
            },
            {
                "model_id": "GEM",
                "provider_id": "gem_global_open_meteo",
                "provider_role": "gem_global_open_meteo",
                "source_class": "aggregator_republisher",
                "aggregator_or_republisher": True,
                "native_arrival_indexed": True,
                "prospective_strict_asof_eligible": False,
            },
        ])


def test_two_model_winner_accepts_complete_three_model_candidate_partition() -> None:
    raw = _selection_for(_ensemble())
    identity = {
        **{key: value for key, value in raw.items() if key != "content_sha256"},
        "selected_models": ["GFS", "ICON"],
        "selected_weights": {"GFS": 0.25, "ICON": 0.75},
    }
    selection = ModelEnsembleSelectionEvidence.from_mapping({
        **identity,
        "content_sha256": content_sha256(identity),
    })

    _validate_candidate_score_partition(selection)


def test_two_model_winner_accepts_complete_five_model_candidate_partition() -> None:
    models = ("ECMWF", "GFS", "ICON", "GEM", "CMA")
    candidate_scores = [
        {
            "models": list(subset),
            "formal_selection_eligible": len(subset) >= 2,
            "candidate_role": (
                "formal_ensemble"
                if len(subset) >= 2
                else "single_model_ablation"
            ),
        }
        for size in range(1, len(models) + 1)
        for subset in itertools.combinations(models, size)
    ]
    identity = {
        "selection_evidence_id": "selection-five-pool",
        "station_id": "ZUUU",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "method_version": "rolling_origin_probability_stack_v4_five_model_pool_min2",
        "train_start_date": "2026-06-01",
        "train_end_date": "2026-06-20",
        "recent_start_date": "2026-06-14",
        "recent_end_date": "2026-06-20",
        "holdout_start_date": "2026-06-21",
        "holdout_end_date": "2026-06-30",
        "candidate_scores": candidate_scores,
        "selected_models": ["ECMWF", "GEM"],
        "selected_weights": {"ECMWF": 0.65, "GEM": 0.35},
        "oos_metrics": {"brier_score": 0.2, "nll": 0.5, "crps": 0.3},
        "residual_covariance": {},
        "residual_correlation": {},
        "effective_sample_n": 20.0,
        "previous_ensemble_artifact_sha256": None,
        "max_weight_delta": 0.2,
        "actual_max_weight_delta": 0.0,
    }
    selection = ModelEnsembleSelectionEvidence.from_mapping({
        **identity,
        "content_sha256": content_sha256(identity),
    })

    assert len(candidate_scores) == 31
    assert sum(
        row["formal_selection_eligible"] is True
        for row in candidate_scores
    ) == 26
    _validate_candidate_score_partition(selection)


def test_no_live_activation_cannot_satisfy_formal_gate_but_can_materialize_no_live() -> None:
    artifacts = {
        model: replace(_artifact(model, 0.0), promoted=False)
        for model in ("ECMWF", "GFS", "ICON")
    }
    activations = [
        replace(
            _activation(
                kind="MODEL_CALIBRATION",
                artifact_id=artifacts[model].calibration_artifact_id,
                model=model,
            ),
            authority_scope="PRODUCTION_NO_LIVE_ONLY",
        )
        for model in artifacts
    ]
    selection_row = _selection_for(_ensemble())
    ensemble = _rehash_ensemble(
        _ensemble(),
        promoted=False,
        selection_evidence_sha256=selection_row["content_sha256"],
    )
    ensemble_activation = replace(
        _activation(
            kind="MODEL_ENSEMBLE",
            artifact_id=ensemble.ensemble_artifact_id,
            mask=model_set_hash(("ECMWF", "GFS", "ICON")),
        ),
        authority_scope="PRODUCTION_NO_LIVE_ONLY",
    )
    kwargs = {
        "current_runs": _runs(),
        "diagnostic_runs": _runs(),
        "calibration_artifacts": artifacts,
        "diagnostic_calibration_artifacts": artifacts,
        "ensemble_artifact": ensemble,
        "calibration_activations": activations,
        "ensemble_activation": ensemble_activation,
        "bucket_intervals": {"37": (36.5, 37.5), "38": (37.5, 38.5), "39": (38.5, 39.5)},
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "America/New_York",
        "decision_at_utc": DECISION,
        "fresh_until_utc": "2026-07-24T00:00:00Z",
        "bridge_sha256": "b" * 64,
    }
    with pytest.raises(ContractError, match="ensemble_artifact_not_active"):
        materialize_p0(**kwargs)

    envelope = materialize_p0(
        **kwargs,
        required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
    )
    assert envelope.stage == "P0"
    assert sum(envelope.probabilities.values()) == pytest.approx(1.0)


def test_promoted_artifact_is_forbidden_under_no_live_only_authority() -> None:
    artifacts = {model: _artifact(model, 0.0) for model in ("ECMWF", "GFS", "ICON")}
    activations = [
        replace(
            _activation(
                kind="MODEL_CALIBRATION",
                artifact_id=artifacts[model].calibration_artifact_id,
                model=model,
            ),
            authority_scope="PRODUCTION_NO_LIVE_ONLY",
        )
        for model in artifacts
    ]
    ensemble_activation = replace(
        _activation(
            kind="MODEL_ENSEMBLE", artifact_id="ens-1",
            mask=model_set_hash(("ECMWF", "GFS", "ICON")),
        ),
        authority_scope="PRODUCTION_NO_LIVE_ONLY",
    )
    with pytest.raises(ContractError, match="must_remain_unpromoted"):
        materialize_p0(
            _runs(),
            diagnostic_runs=_runs(),
            calibration_artifacts=artifacts,
            diagnostic_calibration_artifacts=artifacts,
            ensemble_artifact=_ensemble(),
            calibration_activations=activations,
            ensemble_activation=ensemble_activation,
            bucket_intervals={"38": (37.5, 38.5)},
            station_id="ZUUU", event_date="2026-07-23",
            settlement_timezone="America/New_York",
            decision_at_utc=DECISION, fresh_until_utc="2026-07-24T00:00:00Z",
            bridge_sha256="b" * 64,
            required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
        )


def test_no_live_exact_two_model_mask_materializes_without_waiting_for_third() -> None:
    selected_models = ("ECMWF", "GFS")
    runs = [row for row in _runs() if row.model_id in selected_models]
    artifacts = {
        model: replace(_artifact(model, 0.0), promoted=False)
        for model in selected_models
    }
    activations = [
        replace(
            _activation(
                kind="MODEL_CALIBRATION",
                artifact_id=artifacts[model].calibration_artifact_id,
                model=model,
            ),
            authority_scope="PRODUCTION_NO_LIVE_ONLY",
        )
        for model in selected_models
    ]
    ensemble = _rehash_ensemble(_ensemble(), promoted=False)
    ensemble_activation = replace(
        _activation(
            kind="MODEL_ENSEMBLE",
            artifact_id=ensemble.ensemble_artifact_id,
            mask=model_set_hash(selected_models),
        ),
        authority_scope="PRODUCTION_NO_LIVE_ONLY",
    )
    envelope = materialize_p0(
        runs,
        diagnostic_runs=runs,
        calibration_artifacts=artifacts,
        diagnostic_calibration_artifacts=artifacts,
        ensemble_artifact=ensemble,
        calibration_activations=activations,
        ensemble_activation=ensemble_activation,
        bucket_intervals={"37": (36.5, 37.5), "38": (37.5, 38.5), "39": (38.5, 39.5)},
        station_id="ZUUU",
        event_date="2026-07-23",
        settlement_timezone="America/New_York",
        decision_at_utc=DECISION,
        fresh_until_utc="2026-07-24T00:00:00Z",
        bridge_sha256="b" * 64,
        required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
    )
    assert envelope.lineage["available_model_set_hash"] == model_set_hash(selected_models)
    assert len(envelope.lineage["model_run_ids"].split(",")) == 2


def test_no_live_authority_activates_each_optimized_supported_mask() -> None:
    artifacts = [
        replace(_artifact(model, 0.0), promoted=False)
        for model in ("ECMWF", "GFS", "ICON")
    ]
    selection_row = _selection_for(_ensemble())
    ensemble = _rehash_ensemble(
        _ensemble(),
        promoted=False,
        selection_evidence_sha256=selection_row["content_sha256"],
    )
    evidence = {
        "schema": "bayes_basket_target_calibration_evidence_v1",
        "as_of_utc": DECISION,
        "evidence_sha256": "e" * 64,
        "receipt_sha256": "f" * 64,
        "checks": {
            "calibration_window_sufficient": True,
            "candidate_artifact_count_positive": True,
            "all_candidates_unpromoted": True,
            "truth_source_canonical_ingress": True,
            "three_model_available": True,
            "model_provider_set_complete": True,
        },
        "boundary": {
            "writes_production_state": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
        "calibration_model_rows": [
            {"model_id": model, "aggregator_or_republisher": False}
            for model in ("ECMWF", "GFS", "ICON")
        ],
        "calibration_artifacts": [asdict(item) for item in artifacts],
        "ensemble_selection_evidence": [selection_row],
        "ensemble_artifacts": [asdict(ensemble)],
        "oos_reports": [
            {
                "station_id": "ZUUU",
                "model_id": model,
                "season_bin": "summer",
                "holdout_n": 1,
            }
            for model in ("ECMWF", "GFS", "ICON")
        ],
    }
    formal_binding_identity = {
        "station_id": "ZUUU",
        "truth_label_authority": "FORMAL_STATION_LANE_TRUTH",
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "America/New_York",
        "reference_only": False,
        "dataset_path": "/bounded/ZUUU/calibration_truth_dataset.json",
        "dataset_content_sha256": "d" * 64,
        "enters_realtime_truth": False,
        "enters_p1": False,
        "enters_hard_no": False,
        "replacement_required_when_canonical_history_available": False,
    }
    evidence["truth_authority_bindings"] = [{
        **formal_binding_identity,
        "content_sha256": content_sha256(formal_binding_identity),
    }]
    oos_audit = {
        "schema": "bayes_basket_target_calibration_oos_audit_v1",
        "evidence_sha256": "a" * 64,
        "input_artifact": {"receipt_sha256": evidence["receipt_sha256"]},
        "checks": {
            "chronological_holdout_no_leakage": True,
            "metric_rows_consistent": True,
            "ensemble_oos_metrics_materialized": True,
            "ensemble_selection_non_degradation_vs_uniform": True,
            "ensemble_probability_oos_materialized": True,
        },
        "ensemble_report_rows": [{
            "selection_evidence_sha256": selection_row["content_sha256"],
            "probability_non_degradation_vs_uniform": True,
            "selection_mae_improvement_vs_uniform_c": 0.1,
            "selection_rmse_improvement_vs_uniform_c": 0.1,
        }],
        "boundary": {
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    packet = build_no_live_calibration_artifact_packet(
        evidence,
        oos_audit=oos_audit,
        calibration_generation=1,
        ensemble_generation=2,
    )
    ensemble_activations = [
        row
        for row in packet["calibration_activations"]
        if row["artifact_kind"] == "MODEL_ENSEMBLE"
    ]
    assert len(ensemble_activations) == 4
    assert {row["generation"] for row in ensemble_activations} == {2}
    assert {
        row["available_model_set_hash_or_star"]
        for row in ensemble_activations
    } == set(ensemble.supported_masks)

    binding_identity = {
        "station_id": "ZUUU",
        "truth_label_authority": "TEMPORARY_AWC_CALIBRATION_LABEL",
        "source_lane": "AWC",
        "dataset_path": "/bounded/ZUUU/calibration_truth_dataset.json",
        "dataset_content_sha256": "d" * 64,
        "enters_realtime_truth": False,
        "enters_p1": False,
        "enters_hard_no": False,
        "replacement_required_when_canonical_history_available": True,
    }
    temporary_evidence = {
        **evidence,
        "checks": {
            **evidence["checks"],
            "truth_source_canonical_ingress": False,
            "truth_source_no_live_calibration_authority_valid": True,
            "temporary_awc_calibration_label_explicit": True,
        },
        "boundary": {
            **evidence["boundary"],
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
        },
        "truth_authority_bindings": [{
            **binding_identity,
            "content_sha256": content_sha256(binding_identity),
        }],
    }
    with pytest.raises(
        ContractError, match="no_live_calibration_temporary_awc_retired"
    ):
        build_no_live_calibration_artifact_packet(
            temporary_evidence,
            oos_audit=oos_audit,
            calibration_generation=3,
            ensemble_generation=4,
        )

    # Point MAE/RMSE remain diagnostics.  The formal artifact optimizes bucket
    # probabilities and is governed by Brier/NLL/CRPS non-degradation.
    oos_audit["ensemble_report_rows"][0][
        "selection_mae_improvement_vs_uniform_c"
    ] = -0.01
    oos_audit["ensemble_report_rows"][0][
        "selection_rmse_improvement_vs_uniform_c"
    ] = -0.02
    diagnostic_packet = build_no_live_calibration_artifact_packet(
        evidence,
        oos_audit=oos_audit,
        calibration_generation=1,
        ensemble_generation=2,
    )
    assert any(
        row["artifact_kind"] == "MODEL_ENSEMBLE"
        for row in diagnostic_packet["calibration_activations"]
    )
