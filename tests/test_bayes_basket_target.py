from __future__ import annotations

import itertools
import json
import sqlite3
from dataclasses import asdict, replace

import pytest

from bayes_basket_target.basket_kernel import (
    ContractError as BasketContractError,
    Direction,
    TargetBasketPolicy,
    expected_direction_from_probability_vector,
)
from bayes_basket_target import (
    BasketCashPhaseInput,
    BasketCashPhasePolicyArtifact,
    CalibrationActivation,
    ContractError,
    CurrentAsOfModelRun,
    GlobalBasketPriorArtifact,
    ModelEnsembleArtifact,
    PolicyArtifact,
    ProbabilityEnvelope,
    ResidualObservation,
    StationBasketPolicyActivation,
    compile_target_plan,
    content_sha256,
    fit_model_calibration_artifact,
    materialize_p0,
    model_set_hash,
    resolve_station_policy,
    select_latest_complete_model_run_bundle,
    select_current_stage,
)
from bayes_basket_target.ssot import (
    CALIBRATION_TABLES,
    MAKER_GRID_OOS_AUTHORITY_TABLES,
    POLICY_TABLES,
    PROBABILITY_EVIDENCE_TABLES,
    apply_target_ssot_schema,
)
from bayes_basket_target.regime_materialization import build_regime_artifact
from bayes_basket_target.delta_v2_artifact import ARTIFACT_SCHEMA


DECISION = "2026-07-23T04:00:00Z"
VALID_FROM = "2026-07-01T00:00:00Z"
VALID_UNTIL = "2026-08-01T00:00:00Z"
SHA = "a" * 64
SETTLEMENT_TIMEZONE = "America/New_York"


def _activation(*, kind: str, artifact_id: str, model: str = "*", mask: str = "*", generation: int = 1) -> CalibrationActivation:
    return CalibrationActivation.from_mapping({
        "activation_id": f"activation-{kind}-{artifact_id}-{generation}",
        "artifact_kind": kind,
        "artifact_id": artifact_id,
        "station_id": "ZUUU",
        "model_id_or_star": model,
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "available_model_set_hash_or_star": mask,
        "generation": generation,
        "valid_from_utc": VALID_FROM,
        "valid_until_utc": VALID_UNTIL,
        "activated_at_utc": "2026-06-30T00:00:00Z",
        "content_sha256": SHA,
    })


def _artifact(model: str, bias: float) -> object:
    return fit_model_calibration_artifact(
        [
            ResidualObservation(
                residual_id=f"{model}-r1",
                station_id="ZUUU",
                model_id=model,
                season_bin="summer",
                lead_bucket="d1",
                run_cycle="00Z",
                residual_c=bias,
                target_local_date="2026-07-20",
                paired_asof_utc="2026-07-20T01:00:00Z",
            ),
            ResidualObservation(
                residual_id=f"{model}-r2",
                station_id="ZUUU",
                model_id=model,
                season_bin="summer",
                lead_bucket="d1",
                run_cycle="00Z",
                residual_c=bias,
                target_local_date="2026-07-21",
                paired_asof_utc="2026-07-21T01:00:00Z",
            ),
        ],
        artifact_id=f"cal-{model}",
        trained_through_utc="2026-07-22T00:00:00Z",
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
        parent_scale_c=1.0,
        kappa=8.0,
    )


def test_short_history_can_retain_scale_without_enabling_mean_bias_correction():
    artifact = fit_model_calibration_artifact(
        [
            ResidualObservation(
                residual_id="short-r1",
                station_id="ZUUU",
                model_id="ECMWF",
                season_bin="summer",
                lead_bucket="d1",
                run_cycle="00Z",
                residual_c=4.0,
                target_local_date="2026-07-20",
                paired_asof_utc="2026-07-20T01:00:00Z",
            ),
            ResidualObservation(
                residual_id="short-r2",
                station_id="ZUUU",
                model_id="ECMWF",
                season_bin="summer",
                lead_bucket="d1",
                run_cycle="00Z",
                residual_c=4.0,
                target_local_date="2026-07-21",
                paired_asof_utc="2026-07-21T01:00:00Z",
            ),
        ],
        artifact_id="cal-short-no-bias",
        trained_through_utc="2026-07-22T00:00:00Z",
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
        parent_scale_c=1.0,
        kappa=8.0,
        bias_enabled=False,
    )
    assert artifact.bias_c == 0.0
    assert artifact.residual_scale_c > 0.0


def _ensemble() -> ModelEnsembleArtifact:
    masks = {
        model_set_hash(subset): {model: 1.0 / len(subset) for model in subset}
        for size in range(2, 4)
        for subset in itertools.combinations(("ECMWF", "GFS", "ICON"), size)
    }
    identity = {
        "ensemble_artifact_id": "ens-1",
        "station_id": "ZUUU",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "candidate_models": ["ECMWF", "GFS", "ICON"],
        "supported_masks": masks,
        "residual_covariance": {"ECMWF": {"ECMWF": 1.0}},
        "correlation_penalty": 0.2,
        "selection_evidence_sha256": "b" * 64,
        "previous_ensemble_artifact_sha256": None,
        "weight_method": "rolling_origin_probability_stack_v4_five_model_pool_min2",
        "effective_sample_n": 20.0,
        "max_weight_delta": 0.2,
        "train_start_date": "2026-06-01",
        "train_end_date": "2026-06-20",
        "recent_start_date": "2026-06-14",
        "recent_end_date": "2026-06-20",
        "holdout_start_date": "2026-06-21",
        "holdout_end_date": "2026-06-30",
        "oos_metrics": {"brier_score": 0.2, "nll": 0.5, "crps": 0.3},
        "valid_from_utc": VALID_FROM,
        "valid_until_utc": VALID_UNTIL,
        "promoted": True,
    }
    return ModelEnsembleArtifact.from_mapping({
        **identity,
        "content_sha256": content_sha256(identity),
    })


def _rehash_ensemble(ensemble: ModelEnsembleArtifact, **changes: object) -> ModelEnsembleArtifact:
    """Test-only constructor: every immutable identity change gets rehashed."""

    raw = asdict(ensemble)
    raw.update(changes)
    raw.pop("content_sha256", None)
    return ModelEnsembleArtifact.from_mapping({
        **raw,
        "content_sha256": content_sha256(raw),
    })


def _runs() -> list[CurrentAsOfModelRun]:
    return [
        CurrentAsOfModelRun.from_mapping({
            "model_run_id": f"run-{model}",
            "model_id": model,
            "station_id": "ZUUU",
            "target_local_date": "2026-07-23",
            "run_init_utc": "2026-07-22T00:00:00Z",
            "published_at_utc": "2026-07-22T01:00:00Z",
            "received_at_utc": "2026-07-22T01:01:00Z",
            "season_bin": "summer",
            "lead_bucket": "d1",
            "run_cycle": "00Z",
            "raw_tmax_c": 38.0,
            "settlement_definition": "official_daily_tmax_c",
            "source_object_sha256": SHA,
        })
        for model in ("ECMWF", "GFS", "ICON")
    ]


def test_aggregator_model_run_uses_observed_receive_time_without_fake_publication() -> None:
    run = CurrentAsOfModelRun.from_mapping({
        "model_run_id": "cma-run",
        "model_id": "CMA",
        "station_id": "MPMG",
        "target_local_date": "2026-07-29",
        "run_init_utc": "2026-07-27T18:00:00Z",
        "published_at_utc": None,
        "received_at_utc": "2026-07-28T00:01:00Z",
        "season_bin": "summer",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "raw_tmax_c": 31.0,
        "settlement_definition": "official_daily_tmax_c",
        "source_object_sha256": "a" * 64,
        "source_class": "aggregator_republisher",
    })

    assert run.published_at_utc is None
    assert run.is_asof("2026-07-28T00:00:59Z") is False
    assert run.is_asof("2026-07-28T00:01:00Z") is True


def test_native_model_run_still_requires_publication_time() -> None:
    with pytest.raises(ContractError, match="published_at_utc"):
        CurrentAsOfModelRun.from_mapping({
            "model_run_id": "native-run",
            "model_id": "GFS",
            "station_id": "MPMG",
            "target_local_date": "2026-07-29",
            "run_init_utc": "2026-07-27T18:00:00Z",
            "published_at_utc": None,
            "received_at_utc": "2026-07-28T00:01:00Z",
            "season_bin": "summer",
            "lead_bucket": "d2",
            "run_cycle": "18Z",
            "raw_tmax_c": 31.0,
            "settlement_definition": "official_daily_tmax_c",
            "source_object_sha256": "a" * 64,
            "source_class": "native_official",
        })


def _envelope() -> ProbabilityEnvelope:
    models = ("ECMWF", "GFS")
    available_hash = model_set_hash(models)
    compatibility_identity = {
        "schema": "bayes_basket_taf_nwp_live_compatibility_fingerprint_v1",
        "station_id": "ZUUU",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "available_model_set_hash": available_hash,
        "available_models": list(models),
        "resolved_weights": {"ECMWF": 0.6, "GFS": 0.4},
        "ensemble_artifact_id": "ensemble-test",
        "ensemble_artifact_content_sha256": "e" * 64,
        "ensemble_selection_evidence_sha256": "f" * 64,
        "ensemble_weight_method": (
            "rolling_origin_probability_stack_v4_five_model_pool_min2"
        ),
        "model_calibration_artifacts": {
            "ECMWF": {
                "calibration_artifact_id": "cal-ecmwf-test",
                "calibration_artifact_content_sha256": "1" * 64,
            },
            "GFS": {
                "calibration_artifact_id": "cal-gfs-test",
                "calibration_artifact_content_sha256": "2" * 64,
            },
        },
    }
    fingerprint = {
        **compatibility_identity,
        "fingerprint_sha256": content_sha256(compatibility_identity),
    }
    return ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0m-test",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "decision_at_utc": DECISION,
        "fresh_until_utc": "2026-07-24T00:00:00Z",
        "bucket_ordinals": {"37": 37, "38": 38, "39": 39},
        "probabilities": {"37": 0.2, "38": 0.6, "39": 0.2},
        "posterior_version": "p1",
        "posterior_sha256": SHA,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "source": "test",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "season_bin": "summer",
            "lead_bucket": "d1",
            "run_cycle": "00Z",
            "settlement_definition": "official_daily_tmax_c",
            "model_run_ids": "run-ecmwf-test,run-gfs-test",
            "calibration_artifact_ids": "cal-ecmwf-test,cal-gfs-test",
            "activation_ids": "act-ecmwf-test,act-gfs-test,act-ensemble-test",
            "ensemble_artifact_id": "ensemble-test",
            "available_model_set_hash": available_hash,
            "selection_evidence_sha256": "f" * 64,
            "ensemble_artifact_content_sha256": "e" * 64,
            "taf_nwp_live_compatibility_fingerprint_sha256": (
                fingerprint["fingerprint_sha256"]
            ),
            "taf_nwp_live_compatibility_json": json.dumps(
                fingerprint,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "model_envelope_lower_bucket": "38",
            "model_envelope_upper_bucket": "38",
            "model_envelope_span_c": "0",
            "formal_model_members_json": json.dumps(
                [
                    {
                        "model_id": "ECMWF",
                        "weight": 0.6,
                        "calibrated_center_c": 38.1,
                        "calibrated_center_bucket": "38",
                    },
                    {
                        "model_id": "GFS",
                        "weight": 0.4,
                        "calibrated_center_c": 37.9,
                        "calibrated_center_bucket": "38",
                    },
                ],
                sort_keys=True,
                separators=(",", ":"),
            ),
            "formal_model_anchor_lower_bucket": "38",
            "formal_model_anchor_upper_bucket": "38",
            "formal_model_anchor_lower_ordinal": "38",
            "formal_model_anchor_upper_ordinal": "38",
        },
    })


def _policy_inputs():
    default_policy = {
        "total_budget_usdc": 12.0,
        "base_coverage": 0.5,
        "maximum_coverage": 0.7,
        "entropy_coverage_gain": 0.0,
        "clip_min_notional_usdc": 0.5,
        "minimum_expected_pnl_after_usdc": -12.0,
        "protected_floor_usdc": -12.0,
        "max_outside_loss_usdc": 12.0,
    }
    prior = GlobalBasketPriorArtifact.create(**{
        "global_prior_id": "prior-1",
        "strategy_version": "target-v1",
        "family_parameters": {
            "target": {},
            "reserve": {"price_margin": 0.0},
            "cadence": {},
        },
        "default_policy": default_policy,
        "valid_from_utc": VALID_FROM,
        "valid_until_utc": VALID_UNTIL,
    })
    artifacts = [
        PolicyArtifact.create(**{
            "artifact_id": f"policy-{family}",
            "family": family,
            "station_id": "ZUUU",
            "stage": "P0",
            "season_bin": "summer",
            "maturity_bin": "d1",
            "liquidity_bin": "liquid",
            "strategy_version": "target-v1",
            "theta_local": {"price_margin": 0.0} if family == "reserve" else {},
            "effective_sample_n": 10.0,
            "kappa": 10.0,
            "trained_through_utc": "2026-07-22T00:00:00Z",
            "valid_from_utc": VALID_FROM,
            "valid_until_utc": VALID_UNTIL,
        })
        for family in ("target", "reserve", "cadence")
    ]
    activation = StationBasketPolicyActivation.create(**{
        "activation_id": "policy-activation-1",
        "station_id": "ZUUU",
        "stage": "P0",
        "season_bin": "summer",
        "maturity_bin": "d1",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v1",
        "global_prior_id": "prior-1",
        "target_artifact_id": "policy-target",
        "reserve_artifact_id": "policy-reserve",
        "cadence_artifact_id": "policy-cadence",
        "fallback_families": [],
        "generation": 1,
        "valid_from_utc": VALID_FROM,
        "valid_until_utc": VALID_UNTIL,
        "activated_at_utc": "2026-06-30T00:00:00Z",
    })
    return prior, artifacts, [activation]


def _cash_phase_inputs() -> tuple[BasketCashPhasePolicyArtifact, BasketCashPhaseInput]:
    policy = BasketCashPhasePolicyArtifact.create(
        policy_version="production-no-live-test-cash-v1",
        station_id="ZUUU",
        station_timezone="Asia/Shanghai",
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase={"HEAVY": 6.0, "STANDARD": 4.0, "TAIL": 1.0},
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
        evidence_status="DESIGN_CANDIDATE",
    )
    phase_input = BasketCashPhaseInput.from_mapping({
        "policy_version": policy.policy_version,
        "station_id": "ZUUU",
        "current_phase": "HEAVY",
        "event_date_local": "2026-07-23",
        "observed_at_utc": DECISION,
        "station_timezone": "Asia/Shanghai",
        "checkpoint_id": "cash-checkpoint-test",
    })
    return policy, phase_input


def _delta_v2_policy() -> dict[str, object]:
    raw: dict[str, object] = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": "delta-v2-test",
        "station_id": "ZUUU",
        "stage": "P0",
        "season_bin": "summer",
        "maturity_bin": "d1",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v1",
        "value_weights": {
            "lambda_profit_floor": 0.0,
            "lambda_cvar": 0.0,
            "lambda_shortfall": 0.0,
            "lambda_breakthrough": 0.0,
            "lambda_overfill": 0.0,
            "lambda_excess_hhi": 0.0,
            "cvar_alpha": 0.1,
            "hhi_max": 1.0,
            "expected_pnl_scale": 1.0,
            "profit_floor_scale": 1.0,
            "cvar_scale": 1.0,
            "coverage_scale": 1.0,
            "overfill_scale": 1.0,
            "excess_hhi_scale": 1.0,
        },
        "execution_weights": {
            "epsilon_score": 0.000001,
            "lambda_wait": 0.0,
            "lambda_queue": 0.0,
            "lambda_adverse_selection": 0.0,
            "lambda_reprice": 0.0,
            "lambda_capital_lock": 0.0,
            "wait_scale": 1.0,
            "queue_scale": 1.0,
            "adverse_selection_scale": 1.0,
            "reprice_scale": 1.0,
            "capital_lock_scale": 1.0,
            "maximum_candidates": 64,
        },
        "cash_clip_grid": [0.5],
        "price_grid": [0.05],
        "timeout_grid_seconds": [30.0],
        "fill_distribution_artifact_sha256": "1" * 64,
        "training_evidence_sha256": "2" * 64,
        "train_start_date": "2026-05-01",
        "train_end_date": "2026-05-31",
        "holdout_start_date": "2026-06-01",
        "holdout_end_date": "2026-06-30",
        "sample_n": 200,
        "holdout_n": 80,
        "consecutive_forward_windows": 2,
        "compared_baselines": ["EV_ONLY", "HOLD", "ZERO_WEIGHT", "v1"],
        "oos_metrics": {
            "delta_score_lift_vs_v1": 0.1,
            "profit_floor_lift": 0.0,
            "resource_failures": 0.0,
        },
        "oos_gate_version": "delta-v2-test-oos",
        "oos_gate_passed": True,
        "trained_through_utc": "2026-06-30T00:00:00Z",
        "valid_from_utc": VALID_FROM,
        "valid_until_utc": VALID_UNTIL,
        "authority_scope": "PRODUCTION_NO_LIVE",
    }
    raw["content_sha256"] = content_sha256(raw)
    return raw


def _delta_v2_fill(**_kwargs: object) -> dict[str, object]:
    return {
        "fill_distribution": [
            {"probability": 0.1, "fill_fraction": 0.0},
            {"probability": 0.2, "fill_fraction": 0.5},
            {"probability": 0.7, "fill_fraction": 1.0},
        ],
        "execution_costs": {
            "wait": 0.0,
            "queue": 0.0,
            "adverse_selection": 0.0,
            "reprice": 0.0,
            "capital_lock": 0.0,
        },
        "fill_artifact_content_sha256": "3" * 64,
    }


def test_target_ssot_is_unified_and_append_only() -> None:
    with sqlite3.connect(":memory:") as conn:
        result = apply_target_ssot_schema(conn)
        assert result["ok"] is True
        assert set(result["tables"]) == set(
            CALIBRATION_TABLES
            + POLICY_TABLES
            + MAKER_GRID_OOS_AUTHORITY_TABLES
            + PROBABILITY_EVIDENCE_TABLES
        )
        triggers = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]
        assert triggers == (
            len(CALIBRATION_TABLES)
            + len(POLICY_TABLES)
            + len(MAKER_GRID_OOS_AUTHORITY_TABLES)
            + len(PROBABILITY_EVIDENCE_TABLES)
        ) * 2


def test_p0m_compiler_applies_bias_and_requires_exact_active_masks() -> None:
    artifacts = {model: _artifact(model, bias) for model, bias in (("ECMWF", 1.0), ("GFS", 0.0), ("ICON", -1.0))}
    activations = [
        _activation(kind="MODEL_CALIBRATION", artifact_id=artifacts[model].calibration_artifact_id, model=model)
        for model in artifacts
    ]
    activation = _activation(kind="MODEL_ENSEMBLE", artifact_id="ens-1", mask=model_set_hash(("ECMWF", "GFS", "ICON")))
    envelope = materialize_p0(
        _runs(),
        diagnostic_runs=_runs(),
        calibration_artifacts=artifacts,
        diagnostic_calibration_artifacts=artifacts,
        ensemble_artifact=_ensemble(),
        calibration_activations=activations,
        ensemble_activation=activation,
        bucket_intervals={"37": (36.5, 37.5), "38": (37.5, 38.5), "39": (38.5, 39.5)},
        station_id="ZUUU",
        event_date="2026-07-23",
        settlement_timezone=SETTLEMENT_TIMEZONE,
        decision_at_utc=DECISION,
        fresh_until_utc="2026-07-24T00:00:00Z",
        bridge_sha256="b" * 64,
    )
    assert envelope.stage == "P0"
    assert sum(envelope.probabilities.values()) == pytest.approx(1.0)
    assert envelope.center_bucket == "38"
    assert envelope.lineage["available_model_set_hash"] == model_set_hash(("ECMWF", "GFS", "ICON"))
    assert envelope.lineage["model_run_initialization_utc"] == "2026-07-22T00:00:00Z"
    assert envelope.lineage["model_bundle_ready_at_utc"] == "2026-07-22T01:01:00Z"
    with pytest.raises(ContractError, match="future_leakage"):
        materialize_p0(
            [CurrentAsOfModelRun.from_mapping({
                **row.__dict__,
                "published_at_utc": "2026-07-23T05:01:00Z" if row.model_id == "ECMWF" else row.published_at_utc,
                "received_at_utc": "2026-07-23T05:02:00Z" if row.model_id == "ECMWF" else row.received_at_utc,
            }) for row in _runs()],
            diagnostic_runs=_runs(),
            calibration_artifacts=artifacts,
            diagnostic_calibration_artifacts=artifacts,
            ensemble_artifact=_ensemble(),
            calibration_activations=activations,
            ensemble_activation=activation,
            bucket_intervals={"38": (37.5, 38.5)},
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            decision_at_utc=DECISION,
            fresh_until_utc="2026-07-24T00:00:00Z",
            bridge_sha256="b" * 64,
        )


def test_p0_bucket_ordinals_follow_numeric_intervals_not_json_key_order() -> None:
    artifacts = {
        model: _artifact(model, 0.0)
        for model in ("ECMWF", "GFS", "ICON")
    }
    activations = [
        _activation(
            kind="MODEL_CALIBRATION",
            artifact_id=artifact.calibration_artifact_id,
            model=model,
        )
        for model, artifact in artifacts.items()
    ]
    ensemble = _ensemble()
    envelope = materialize_p0(
        _runs(),
        diagnostic_runs=_runs(),
        calibration_artifacts=artifacts,
        diagnostic_calibration_artifacts=artifacts,
        ensemble_artifact=ensemble,
        calibration_activations=activations,
        ensemble_activation=_activation(
            kind="MODEL_ENSEMBLE",
            artifact_id=ensemble.ensemble_artifact_id,
            mask=model_set_hash(("ECMWF", "GFS", "ICON")),
        ),
        bucket_intervals={
            "10°C": (9.5, 10.5),
            "11°C or higher": (10.5, None),
            "5°C or below": (None, 5.5),
            "6°C": (5.5, 6.5),
            "7°C": (6.5, 7.5),
            "8°C": (7.5, 8.5),
            "9°C": (8.5, 9.5),
        },
        station_id="ZUUU",
        event_date="2026-07-23",
        settlement_timezone=SETTLEMENT_TIMEZONE,
        decision_at_utc=DECISION,
        fresh_until_utc="2026-07-24T00:00:00Z",
        bridge_sha256="b" * 64,
    )
    assert envelope.bucket_ordinals == {
        "5°C or below": 0,
        "6°C": 1,
        "7°C": 2,
        "8°C": 3,
        "9°C": 4,
        "10°C": 5,
        "11°C or higher": 6,
    }


def test_p0m_kernel_accepts_partial_model_bundle_when_exact_mask_is_activated() -> None:
    artifacts = {model: _artifact(model, 0.0) for model in ("ECMWF", "GFS")}
    activations = [
        _activation(
            kind="MODEL_CALIBRATION",
            artifact_id=artifacts[model].calibration_artifact_id,
            model=model,
        )
        for model in artifacts
    ]
    ensemble = _ensemble()
    ensemble_activation = _activation(
        kind="MODEL_ENSEMBLE",
        artifact_id=ensemble.ensemble_artifact_id,
        mask=model_set_hash(("ECMWF", "GFS")),
    )
    envelope = materialize_p0(
        [row for row in _runs() if row.model_id != "ICON"],
        diagnostic_runs=[row for row in _runs() if row.model_id != "ICON"],
        calibration_artifacts=artifacts,
        diagnostic_calibration_artifacts=artifacts,
        ensemble_artifact=ensemble,
        calibration_activations=activations,
        ensemble_activation=ensemble_activation,
        bucket_intervals={"38": (37.5, 38.5)},
        station_id="ZUUU",
        event_date="2026-07-23",
        settlement_timezone=SETTLEMENT_TIMEZONE,
        decision_at_utc=DECISION,
        fresh_until_utc="2026-07-24T00:00:00Z",
        bridge_sha256="b" * 64,
    )
    assert envelope.lineage["available_model_set_hash"] == model_set_hash(("ECMWF", "GFS"))


def test_formal_ensemble_contract_rejects_single_model_artifact() -> None:
    with pytest.raises(ContractError, match="ensemble_candidate_models_invalid"):
        ModelEnsembleArtifact.from_mapping({
            "ensemble_artifact_id": "ens-single-invalid",
            "station_id": "ZUUU",
            "season_bin": "summer",
            "lead_bucket": "d1",
            "run_cycle": "00Z",
            "settlement_definition": "official_daily_tmax_c",
            "candidate_models": ["ECMWF"],
            "supported_masks": {
                model_set_hash(("ECMWF",)): {"ECMWF": 1.0},
            },
            "residual_covariance": {},
            "correlation_penalty": 0.0,
            "valid_from_utc": VALID_FROM,
            "valid_until_utc": VALID_UNTIL,
            "content_sha256": SHA,
        })


def test_unselected_model_extreme_changes_only_diagnostic_envelope_and_rewinds_deterministically() -> None:
    all_models = ("ECMWF", "GFS", "ICON")
    selected_models = ("ECMWF", "GFS")
    selected_runs = [row for row in _runs() if row.model_id in selected_models]
    artifacts = {model: _artifact(model, 0.0) for model in all_models}
    activations = [
        _activation(
            kind="MODEL_CALIBRATION",
            artifact_id=artifacts[model].calibration_artifact_id,
            model=model,
        )
        for model in all_models
    ]
    ensemble = _ensemble()
    ensemble_activation = _activation(
        kind="MODEL_ENSEMBLE",
        artifact_id=ensemble.ensemble_artifact_id,
        mask=model_set_hash(selected_models),
    )
    intervals = {
        "37-": (None, 37.5),
        "38": (37.5, 38.5),
        "39": (38.5, 39.5),
        "40+": (39.5, None),
    }

    def build(diagnostic_icon_tmax_c: float) -> ProbabilityEnvelope:
        diagnostic = [
            replace(
                run,
                raw_tmax_c=diagnostic_icon_tmax_c,
                model_run_id=f"run-ICON-{diagnostic_icon_tmax_c}",
            )
            if run.model_id == "ICON"
            else run
            for run in _runs()
        ]
        return materialize_p0(
            selected_runs,
            diagnostic_runs=diagnostic,
            calibration_artifacts={
                model: artifacts[model] for model in selected_models
            },
            diagnostic_calibration_artifacts=artifacts,
            ensemble_artifact=ensemble,
            calibration_activations=activations,
            ensemble_activation=ensemble_activation,
            bucket_intervals=intervals,
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            decision_at_utc=DECISION,
            fresh_until_utc="2026-07-24T00:00:00Z",
            bridge_sha256="b" * 64,
        )

    compact = build(38.0)
    expanded = build(41.0)
    compact_again = build(38.0)

    assert compact.probabilities == expanded.probabilities
    assert compact.center_bucket == expanded.center_bucket == "38"
    assert compact.lineage["available_model_set_hash"] == model_set_hash(selected_models)
    assert compact.lineage["diagnostic_model_set_hash"] == model_set_hash(all_models)
    assert len(compact.lineage["model_run_ids"].split(",")) == 2
    assert len(compact.lineage["diagnostic_model_run_ids"].split(",")) == 3
    assert compact.lineage["model_envelope_span_c"] == "0"
    assert expanded.lineage["model_envelope_span_c"] == "3"
    assert compact.lineage["model_envelope_upper_bucket"] == "38"
    assert expanded.lineage["model_envelope_upper_bucket"] == "40+"
    compact_reliability = json.loads(
        compact.lineage["peripheral_model_reliability_by_bucket_json"]
    )
    expanded_reliability = json.loads(
        expanded.lineage["peripheral_model_reliability_by_bucket_json"]
    )
    assert compact_reliability == expanded_reliability
    assert expanded_reliability == {
        "37-": 0.0,
        "38": 1.0,
        "39": 0.0,
        "40+": 0.0,
    }
    assert compact.posterior_sha256 != expanded.posterior_sha256
    assert compact.posterior_sha256 == compact_again.posterior_sha256


def test_diagnostic_model_generation_must_match_probability_generation() -> None:
    artifacts = {model: _artifact(model, 0.0) for model in ("ECMWF", "GFS", "ICON")}
    activations = [
        _activation(
            kind="MODEL_CALIBRATION",
            artifact_id=artifact.calibration_artifact_id,
            model=model,
        )
        for model, artifact in artifacts.items()
    ]
    ensemble = _ensemble()
    ensemble_activation = _activation(
        kind="MODEL_ENSEMBLE",
        artifact_id=ensemble.ensemble_artifact_id,
        mask=model_set_hash(("ECMWF", "GFS")),
    )
    diagnostic = [
        replace(run, run_init_utc="2026-07-21T00:00:00Z")
        if run.model_id == "GFS"
        else run
        for run in _runs()
    ]
    with pytest.raises(
        ContractError,
        match="no_complete_shared_model_run_asof|diagnostic_model_run_generation_mismatch",
    ):
        materialize_p0(
            [row for row in _runs() if row.model_id in {"ECMWF", "GFS"}],
            diagnostic_runs=diagnostic,
            calibration_artifacts={
                model: artifacts[model] for model in ("ECMWF", "GFS")
            },
            diagnostic_calibration_artifacts=artifacts,
            ensemble_artifact=ensemble,
            calibration_activations=activations,
            ensemble_activation=ensemble_activation,
            bucket_intervals={"37-": (None, 37.5), "38+": (37.5, None)},
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            decision_at_utc=DECISION,
            fresh_until_utc="2026-07-24T00:00:00Z",
            bridge_sha256="b" * 64,
        )


def test_combination_side_mass_can_change_while_diagnostic_extrema_stay_fixed() -> None:
    all_models = ("ECMWF", "GFS", "ICON")
    artifacts = {model: _artifact(model, 0.0) for model in all_models}
    activations = [
        _activation(
            kind="MODEL_CALIBRATION",
            artifact_id=artifact.calibration_artifact_id,
            model=model,
        )
        for model, artifact in artifacts.items()
    ]
    diagnostic = [
        replace(
            run,
            raw_tmax_c={"ECMWF": 37.2, "GFS": 38.8, "ICON": 38.0}[run.model_id],
        )
        for run in _runs()
    ]
    selected = [run for run in diagnostic if run.model_id in {"ECMWF", "GFS"}]
    mask = model_set_hash(("ECMWF", "GFS"))
    intervals = {
        "36-": (None, 36.5),
        "37": (36.5, 37.5),
        "38": (37.5, 38.5),
        "39": (38.5, 39.5),
        "40+": (39.5, None),
    }

    def build(name: str, weights: dict[str, float]) -> ProbabilityEnvelope:
        ensemble = _rehash_ensemble(
            _ensemble(),
            ensemble_artifact_id=f"ens-{name}",
            candidate_models=["ECMWF", "GFS"],
            supported_masks={mask: weights},
        )
        return materialize_p0(
            selected,
            diagnostic_runs=diagnostic,
            calibration_artifacts={
                model: artifacts[model] for model in ("ECMWF", "GFS")
            },
            diagnostic_calibration_artifacts=artifacts,
            ensemble_artifact=ensemble,
            calibration_activations=activations,
            ensemble_activation=_activation(
                kind="MODEL_ENSEMBLE",
                artifact_id=ensemble.ensemble_artifact_id,
                mask=mask,
            ),
            bucket_intervals=intervals,
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            decision_at_utc=DECISION,
            fresh_until_utc="2026-07-24T00:00:00Z",
            bridge_sha256="b" * 64,
        )

    cold_weighted = build("cold-weighted", {"ECMWF": 0.8, "GFS": 0.2})
    warm_weighted = build("warm-weighted", {"ECMWF": 0.2, "GFS": 0.8})
    assert cold_weighted.probabilities != warm_weighted.probabilities
    assert cold_weighted.direction_skew() > 0.0
    assert warm_weighted.direction_skew() < 0.0
    for key in (
        "diagnostic_model_set_hash",
        "model_envelope_lower_c",
        "model_envelope_upper_c",
        "model_envelope_span_c",
        "model_envelope_lower_bucket",
        "model_envelope_upper_bucket",
    ):
        assert cold_weighted.lineage[key] == warm_weighted.lineage[key]


def test_p0m_compiler_is_ready_before_target_et_day_opens() -> None:
    artifacts = {
        model: _artifact(model, bias)
        for model, bias in (("ECMWF", 1.0), ("GFS", 0.0), ("ICON", -1.0))
    }
    activations = [
        _activation(
            kind="MODEL_CALIBRATION",
            artifact_id=artifacts[model].calibration_artifact_id,
            model=model,
        )
        for model in artifacts
    ]
    ensemble_activation = _activation(
        kind="MODEL_ENSEMBLE",
        artifact_id="ens-1",
        mask=model_set_hash(("ECMWF", "GFS", "ICON")),
    )
    envelope = materialize_p0(
        _runs(),
        diagnostic_runs=_runs(),
        calibration_artifacts=artifacts,
        diagnostic_calibration_artifacts=artifacts,
        ensemble_artifact=_ensemble(),
        calibration_activations=activations,
        ensemble_activation=ensemble_activation,
        bucket_intervals={"37": (36.5, 37.5), "38": (37.5, 38.5), "39": (38.5, 39.5)},
        station_id="ZUUU",
        event_date="2026-07-23",
        settlement_timezone=SETTLEMENT_TIMEZONE,
        decision_at_utc="2026-07-22T18:00:00Z",
        fresh_until_utc="2026-07-23T05:00:00Z",
        bridge_sha256="b" * 64,
    )
    assert envelope.stage == "P0"
    assert envelope.event_date == "2026-07-23"
    assert envelope.decision_at_utc == "2026-07-22T18:00:00Z"
    assert sum(envelope.probabilities.values()) == pytest.approx(1.0)


def test_explicit_ensemble_activation_cannot_bypass_newer_generation() -> None:
    selected_models = ("ECMWF", "GFS")
    artifacts = {
        model: _artifact(model, 0.0)
        for model in selected_models
    }
    ensemble = _ensemble()
    stale = _activation(
        kind="MODEL_ENSEMBLE",
        artifact_id=ensemble.ensemble_artifact_id,
        mask=model_set_hash(selected_models),
        generation=1,
    )
    current = _activation(
        kind="MODEL_ENSEMBLE",
        artifact_id=ensemble.ensemble_artifact_id,
        mask=model_set_hash(selected_models),
        generation=2,
    )
    activations = [
        _activation(
            kind="MODEL_CALIBRATION",
            artifact_id=artifacts[model].calibration_artifact_id,
            model=model,
        )
        for model in selected_models
    ] + [current]

    with pytest.raises(
        ContractError,
        match="ensemble_activation_not_current_exact_winner",
    ):
        materialize_p0(
            [
                row
                for row in _runs()
                if row.model_id in selected_models
            ],
            diagnostic_runs=[
                row
                for row in _runs()
                if row.model_id in selected_models
            ],
            calibration_artifacts=artifacts,
            diagnostic_calibration_artifacts=artifacts,
            ensemble_artifact=ensemble,
            calibration_activations=activations,
            ensemble_activation=stale,
            bucket_intervals={"38": (37.5, 38.5)},
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            decision_at_utc=DECISION,
            fresh_until_utc="2026-07-24T00:00:00Z",
            bridge_sha256="b" * 64,
        )


def test_p0_rejects_d1_d2_same_cycle_mixing() -> None:
    artifacts = {model: _artifact(model, 0.0) for model in ("ECMWF", "GFS", "ICON")}
    activations = [
        _activation(kind="MODEL_CALIBRATION", artifact_id=artifacts[model].calibration_artifact_id, model=model)
        for model in artifacts
    ]
    activation = _activation(kind="MODEL_ENSEMBLE", artifact_id="ens-1", mask=model_set_hash(("ECMWF", "GFS", "ICON")))
    mixed = [
        CurrentAsOfModelRun.from_mapping({
            **run.__dict__,
            "run_init_utc": "2026-07-21T00:00:00Z" if run.model_id == "ICON" else run.run_init_utc,
        })
        for run in _runs()
    ]
    with pytest.raises(ContractError, match="no_complete_shared_model_run_asof"):
        materialize_p0(
            mixed,
            diagnostic_runs=mixed,
            calibration_artifacts=artifacts,
            diagnostic_calibration_artifacts=artifacts,
            ensemble_artifact=_ensemble(),
            calibration_activations=activations,
            ensemble_activation=activation,
            bucket_intervals={"38": (37.5, 38.5)},
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            decision_at_utc=DECISION,
            fresh_until_utc="2026-07-24T00:00:00Z",
            bridge_sha256="b" * 64,
        )


def test_p0_selects_latest_complete_shared_run_not_latest_individual_model() -> None:
    older = [
        CurrentAsOfModelRun.from_mapping({
            **run.__dict__,
            "model_run_id": f"older-{run.model_id}",
            "run_init_utc": "2026-07-21T00:00:00Z",
            "published_at_utc": "2026-07-21T01:00:00Z",
            "received_at_utc": "2026-07-21T01:01:00Z",
        })
        for run in _runs()
    ]
    selected = select_latest_complete_model_run_bundle(
        older + _runs(),
        decision_at_utc=DECISION,
    )
    assert [row.model_run_id for row in selected] == ["run-ECMWF", "run-GFS", "run-ICON"]
    assert len({row.run_init_utc for row in selected}) == 1


def test_p0_selection_stays_in_active_d1_bucket_when_d2_is_newer() -> None:
    d2_newer = [
        CurrentAsOfModelRun.from_mapping({
            **run.__dict__,
            "model_run_id": f"d2-{run.model_id}",
            "run_init_utc": "2026-07-22T06:00:00Z",
            "published_at_utc": "2026-07-22T07:00:00Z",
            "received_at_utc": "2026-07-22T07:01:00Z",
            "lead_bucket": "d2",
            "run_cycle": "06Z",
        })
        for run in _runs()
    ]
    selected = select_latest_complete_model_run_bundle(
        _runs() + d2_newer,
        decision_at_utc=DECISION,
        required_lead_bucket="d1",
        required_run_cycle="00Z",
        required_settlement_definition="official_daily_tmax_c",
    )
    assert {row.lead_bucket for row in selected} == {"d1"}
    assert {row.run_cycle for row in selected} == {"00Z"}
    assert {row.model_run_id for row in selected} == {"run-ECMWF", "run-GFS", "run-ICON"}


def test_policy_resolution_shrinks_and_fails_closed_without_activation() -> None:
    prior, artifacts, activations = _policy_inputs()
    resolved = resolve_station_policy(
        activations=activations,
        global_priors={prior.global_prior_id: prior},
        artifacts=artifacts,
        station_id="ZUUU",
        stage="P0",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        decision_at_utc=DECISION,
    )
    assert resolved.fallback_reason == "exact_station_artifacts"
    assert resolved.reserve_theta["price_margin"] == pytest.approx(0.0)
    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        resolve_station_policy(
            activations=[],
            global_priors={prior.global_prior_id: prior},
            artifacts=artifacts,
            station_id="ZUUU",
            stage="P0",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            decision_at_utc=DECISION,
        )


def test_p0m_reuses_the_single_pre_event_policy_family() -> None:
    prior, artifacts, activations = _policy_inputs()
    resolved = resolve_station_policy(
        activations=activations,
        global_priors={prior.global_prior_id: prior},
        artifacts=artifacts,
        station_id="ZUUU",
        stage="P0-M",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        decision_at_utc=DECISION,
    )
    assert resolved.stage == "P0-M"
    assert resolved.activation_id == activations[0].activation_id


def test_p1_does_not_reuse_the_pre_event_p0_policy_family() -> None:
    prior, artifacts, activations = _policy_inputs()
    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        resolve_station_policy(
            activations=activations,
            global_priors={prior.global_prior_id: prior},
            artifacts=artifacts,
            station_id="ZUUU",
            stage="P1",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            decision_at_utc=DECISION,
        )


def test_stage_router_does_not_downgrade_from_stale_p1() -> None:
    p0 = _envelope()
    p1 = ProbabilityEnvelope.from_mapping({
        **p0.to_dict(),
        "stage": "P1",
        "checkpoint_id": "p1-test",
        "decision_at_utc": "2026-07-22T00:00:00Z",
        "fresh_until_utc": "2026-07-22T12:00:00Z",
    })
    with pytest.raises(ContractError, match="latest_stage_stale"):
        select_current_stage([p0, p1], at_utc=DECISION)


def test_stage_precedence_is_only_same_decision_conflict_resolution() -> None:
    p0 = _envelope()
    p0m = ProbabilityEnvelope.from_mapping({**p0.to_dict(), "stage": "P0-M", "checkpoint_id": "p0m-test"})
    p1 = ProbabilityEnvelope.from_mapping({**p0.to_dict(), "stage": "P1", "checkpoint_id": "p1-test"})
    selected = select_current_stage([p0, p0m, p1], at_utc=DECISION)
    assert selected.stage == "P1"


def test_stage_router_preserves_highest_existing_authority_across_new_updates() -> None:
    p0 = _envelope()
    p0m_old = ProbabilityEnvelope.from_mapping(
        {
            **p0.to_dict(),
            "stage": "P0-M",
            "checkpoint_id": "p0m-old",
            "decision_at_utc": "2026-07-23T04:10:00Z",
            "fresh_until_utc": "2026-07-23T08:00:00Z",
        }
    )
    p0_new = ProbabilityEnvelope.from_mapping(
        {
            **p0.to_dict(),
            "checkpoint_id": "p0-new",
            "decision_at_utc": "2026-07-23T04:20:00Z",
            "fresh_until_utc": "2026-07-23T08:00:00Z",
        }
    )
    assert select_current_stage(
        [p0, p0m_old, p0_new],
        at_utc="2026-07-23T04:20:00Z",
    ).checkpoint_id == "p0m-old"

    p0m_new = ProbabilityEnvelope.from_mapping(
        {
            **p0m_old.to_dict(),
            "checkpoint_id": "p0m-new",
            "decision_at_utc": "2026-07-23T04:30:00Z",
        }
    )
    assert select_current_stage(
        [p0, p0m_old, p0_new, p0m_new],
        at_utc="2026-07-23T04:30:00Z",
    ).checkpoint_id == "p0m-new"

    p1 = ProbabilityEnvelope.from_mapping(
        {
            **p0.to_dict(),
            "stage": "P1",
            "checkpoint_id": "p1-authority",
            "decision_at_utc": "2026-07-23T04:15:00Z",
            "fresh_until_utc": "2026-07-23T08:00:00Z",
        }
    )
    assert select_current_stage(
        [p0, p0m_old, p0_new, p0m_new, p1],
        at_utc="2026-07-23T04:30:00Z",
    ).checkpoint_id == "p1-authority"


def test_b06_direction_gate_uses_probability_only_and_fails_closed() -> None:
    policy = TargetBasketPolicy.from_mapping({
        "clip_max_notional_usdc": 30.0,
        "direction_entropy_max": 0.90,
        "direction_min_side_mass": 0.10,
    })
    ordinals = {"37": 37, "38": 38, "39": 39}
    assert expected_direction_from_probability_vector(
        {"37": 0.10, "38": 0.50, "39": 0.40}, ordinals, policy
    ) is Direction.UP
    assert expected_direction_from_probability_vector(
        {"37": 0.33, "38": 0.34, "39": 0.33}, ordinals, policy
    ) is Direction.NEUTRAL
    assert expected_direction_from_probability_vector(
        {"35": 0.125, "36": 0.125, "37": 0.30, "38": 0.225, "39": 0.225},
        {"35": 35, "36": 36, "37": 37, "38": 38, "39": 39},
        policy,
    ) is Direction.NEUTRAL
    assert expected_direction_from_probability_vector(
        {"37": 0.01, "38": 0.90, "39": 0.09}, ordinals, policy
    ) is Direction.NEUTRAL
    assert expected_direction_from_probability_vector(
        {"37": 0.10, "38": 0.50, "39": 0.40}, ordinals, policy, evidence_fresh=False
    ) is Direction.NEUTRAL


@pytest.mark.parametrize("layers", (-1, 3, 9))
def test_target_policy_disagreement_expansion_is_hard_capped_at_two(
    layers: int,
) -> None:
    with pytest.raises(
        BasketContractError,
        match="disagreement_expansion_cap_out_of_range",
    ):
        TargetBasketPolicy.from_mapping(
            {
                "clip_max_notional_usdc": 30.0,
                "max_disagreement_expansion_layers": layers,
            }
        )


@pytest.mark.parametrize(
    ("probabilities", "error"),
    [
        (
            {"37": float("inf"), "38": 0.0, "39": 0.0},
            "direction_probability_vector_invalid",
        ),
        (
            {"37": float("nan"), "38": 0.5, "39": 0.5},
            "direction_probability_vector_invalid",
        ),
        (
            {"37": -0.1, "38": 0.6, "39": 0.5},
            "direction_probability_vector_invalid",
        ),
        (
            {"37": 0.1, "38": 0.2, "39": 0.3},
            "direction_probability_vector_not_normalized",
        ),
    ],
)
def test_direction_probability_entry_rejects_non_finite_negative_or_unscaled(
    probabilities: dict[str, float],
    error: str,
) -> None:
    policy = TargetBasketPolicy.from_mapping(
        {"clip_max_notional_usdc": 30.0}
    )
    with pytest.raises(BasketContractError, match=error):
        expected_direction_from_probability_vector(
            probabilities,
            {"37": 37, "38": 38, "39": 39},
            policy,
        )


def test_target_compiler_direction_ignores_quote_anchor() -> None:
    prior, artifacts, activations = _policy_inputs()
    cash_policy, cash_input = _cash_phase_inputs()
    envelope = ProbabilityEnvelope.from_mapping({
        **_envelope().to_dict(),
        "probabilities": {"37": 0.10, "38": 0.50, "39": 0.40},
    })

    def market_snapshot(*, snapshot_id: str, quote_version: str, prices: tuple[tuple[float, float], ...]) -> dict[str, object]:
        return {
            "snapshot_id": snapshot_id,
            "sequence": 1,
            "observed_at": DECISION,
                "market_id": "market-zuuu-2026-07-23",
                "station_id": "ZUUU",
                "event_date": "2026-07-23",
                "settlement_timezone": SETTLEMENT_TIMEZONE,
            "quote_version": quote_version,
            "quote_observed_at": DECISION,
            "quote_source": "OFFICIAL_CLOB_WS",
            "inventory_version": "inventory-1",
            "order_state_version": "orders-1",
            "remaining_budget_usdc": 12.0,
            "buckets": [
                {
                    "bucket": bucket,
                    "bid_price": bid,
                    "ask_levels": [{"price": ask, "shares": 100.0}],
                    "maker_queue_ahead_shares": 1.0,
                    "maker_queue_known": True,
                }
                for bucket, (bid, ask) in zip(("37", "38", "39"), prices)
            ],
            "checkpoint_changed": True,
        }

    first = compile_target_plan(
        probability_envelopes=[envelope],
        market_snapshot=market_snapshot(
            snapshot_id="quote-anchor-a",
            quote_version="quote-a",
            prices=((0.10, 0.12), (0.20, 0.22), (0.10, 0.12)),
        ),
        policy_activations=activations,
        global_priors={prior.global_prior_id: prior},
        policy_artifacts=artifacts,
        evaluation_at_utc="2026-07-23T04:30:00Z",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={"clip_max_notional_usdc": 3.0},
        cash_phase_policy=cash_policy,
        cash_phase_input=cash_input,
        delta_v2_policy=_delta_v2_policy(),
        delta_v2_fill_resolver=_delta_v2_fill,
        resource_probe_ok=True,
        regime_artifact=build_regime_artifact(envelope),
    )
    second = compile_target_plan(
        probability_envelopes=[envelope],
        market_snapshot=market_snapshot(
            snapshot_id="quote-anchor-b",
            quote_version="quote-b",
            prices=((0.40, 0.45), (0.05, 0.06), (0.60, 0.65)),
        ),
        policy_activations=activations,
        global_priors={prior.global_prior_id: prior},
        policy_artifacts=artifacts,
        evaluation_at_utc="2026-07-23T04:30:00Z",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={"clip_max_notional_usdc": 3.0},
        cash_phase_policy=cash_policy,
        cash_phase_input=cash_input,
        delta_v2_policy=_delta_v2_policy(),
        delta_v2_fill_resolver=_delta_v2_fill,
        resource_probe_ok=True,
        regime_artifact=build_regime_artifact(envelope),
    )
    assert first["plan"]["center_bucket"] == second["plan"]["center_bucket"] == "38"
    assert first["plan"]["direction"] == second["plan"]["direction"] == Direction.UP.value


def test_unobserved_cash_phase_preserves_preselection_but_blocks_execution() -> None:
    prior, artifacts, activations = _policy_inputs()
    observed_only_policy = BasketCashPhasePolicyArtifact.create(
        policy_version="production-no-live-missing-tail-v1",
        station_id="ZUUU",
        station_timezone="Asia/Shanghai",
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase={"STANDARD": 4.0},
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
        evidence_status="DESIGN_CANDIDATE",
    )
    cash_input = BasketCashPhaseInput.from_mapping({
        "policy_version": observed_only_policy.policy_version,
        "station_id": "ZUUU",
        "current_phase": "HEAVY",
        "event_date_local": "2026-07-23",
        "observed_at_utc": DECISION,
        "station_timezone": "Asia/Shanghai",
        "checkpoint_id": "cash-missing-tail",
    })
    result = compile_target_plan(
        probability_envelopes=[_envelope()],
        market_snapshot={
            "snapshot_id": "cash-missing-tail-snapshot",
            "sequence": 1,
            "observed_at": DECISION,
            "market_id": "market-zuuu-2026-07-23",
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "quote_version": "cash-missing-tail-quote",
            "quote_observed_at": DECISION,
            "quote_source": "OFFICIAL_CLOB_WS",
            "inventory_version": "inventory-1",
            "order_state_version": "orders-1",
            "remaining_budget_usdc": 12.0,
            "buckets": [
                {
                    "bucket": bucket,
                    "bid_price": 0.10,
                    "ask_levels": [{"price": 0.12, "shares": 100.0}],
                    "maker_queue_ahead_shares": 1.0,
                    "maker_queue_known": True,
                }
                for bucket in ("37", "38", "39")
            ],
        },
        policy_activations=activations,
        global_priors={prior.global_prior_id: prior},
        policy_artifacts=artifacts,
        evaluation_at_utc="2026-07-23T04:30:00Z",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={"clip_max_notional_usdc": 3.0},
        cash_phase_policy=observed_only_policy,
        cash_phase_input=cash_input,
        delta_v2_policy=_delta_v2_policy(),
        delta_v2_fill_resolver=_delta_v2_fill,
        resource_probe_ok=True,
        regime_artifact=build_regime_artifact(_envelope()),
    )

    assert result["status"] == "blocked"
    assert result["reason_codes"] == [
        "cash_phase_unobserved_phase_fail_closed"
    ]
    assert result["resolved_station_policy"]["activation_id"] == (
        "policy-activation-1"
    )
    assert result["plan"]["center_bucket"] == "38"
    assert result["plan"]["cash_phase_blocked"] is True
    assert result["cash_phase_decision"] is None
    assert result["order_sizing_decisions"] == []
    assert result["boundary"]["orders_submitted"] == 0
    assert result["boundary"]["calls_order_gateway"] is False


def test_missing_exact_model_mask_fails_closed() -> None:
    artifacts = {model: _artifact(model, 0.0) for model in ("ECMWF", "GFS")}
    activations = [
        _activation(kind="MODEL_CALIBRATION", artifact_id=artifacts[model].calibration_artifact_id, model=model)
        for model in artifacts
    ]
    two_model_ensemble = _rehash_ensemble(
        _ensemble(),
        ensemble_artifact_id="ens-two",
        candidate_models=["ECMWF", "GFS"],
        supported_masks={model_set_hash(("ECMWF", "GFS")): {"ECMWF": 0.5, "GFS": 0.5}},
        selection_evidence_sha256="b" * 64,
    )
    with pytest.raises(ContractError, match="no_active_ensemble_artifact"):
        materialize_p0(
            _runs()[:2],
            diagnostic_runs=_runs()[:2],
            calibration_artifacts=artifacts,
            diagnostic_calibration_artifacts=artifacts,
            ensemble_artifact=two_model_ensemble,
            calibration_activations=activations,
            bucket_intervals={"38": (37.5, 38.5)},
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            decision_at_utc=DECISION,
            fresh_until_utc="2026-07-24T00:00:00Z",
            bridge_sha256="b" * 64,
        )


def test_new_ensemble_generation_cannot_fall_back_to_old_mask() -> None:
    artifacts = {
        model: _artifact(model, 0.0)
        for model in ("ECMWF", "GFS", "ICON")
    }
    calibration_activations = [
        _activation(
            kind="MODEL_CALIBRATION",
            artifact_id=artifact.calibration_artifact_id,
            model=model,
        )
        for model, artifact in artifacts.items()
    ]
    ensemble = _ensemble()
    old_three = _activation(
        kind="MODEL_ENSEMBLE",
        artifact_id=ensemble.ensemble_artifact_id,
        mask=model_set_hash(("ECMWF", "GFS", "ICON")),
        generation=1,
    )
    new_pair = _activation(
        kind="MODEL_ENSEMBLE",
        artifact_id=ensemble.ensemble_artifact_id,
        mask=model_set_hash(("ECMWF", "GFS")),
        generation=2,
    )
    with pytest.raises(ContractError, match="no_active_ensemble_artifact"):
        materialize_p0(
            _runs(),
            diagnostic_runs=_runs(),
            calibration_artifacts=artifacts,
            diagnostic_calibration_artifacts=artifacts,
            ensemble_artifact=ensemble,
            calibration_activations=[*calibration_activations, old_three, new_pair],
            bucket_intervals={"38": (37.5, 38.5)},
            station_id="ZUUU",
            event_date="2026-07-23",
            settlement_timezone=SETTLEMENT_TIMEZONE,
            decision_at_utc=DECISION,
            fresh_until_utc="2026-07-24T00:00:00Z",
            bridge_sha256="b" * 64,
        )


def test_global_policy_fallback_requires_explicit_family_mask() -> None:
    prior, artifacts, activations = _policy_inputs()
    fallback_activation = StationBasketPolicyActivation.create(**{
        "activation_id": "policy-fallback-1",
        "station_id": "ZUUU",
        "stage": "P0",
        "season_bin": "summer",
        "maturity_bin": "d1",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v1",
        "global_prior_id": "prior-1",
        "target_artifact_id": None,
        "reserve_artifact_id": None,
        "cadence_artifact_id": None,
        "fallback_families": ["target", "reserve", "cadence"],
        "generation": 2,
        "valid_from_utc": VALID_FROM,
        "valid_until_utc": VALID_UNTIL,
        "activated_at_utc": "2026-06-30T00:00:00Z",
    })
    resolved = resolve_station_policy(
        activations=[fallback_activation],
        global_priors={prior.global_prior_id: prior},
        artifacts=artifacts,
        station_id="ZUUU",
        stage="P0",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        decision_at_utc=DECISION,
    )
    assert resolved.fallback_reason == "target;reserve;cadence"


def test_station_reserve_does_not_rewrite_probability_or_bypass_v2() -> None:
    prior, artifacts, activations = _policy_inputs()
    cash_policy, cash_input = _cash_phase_inputs()
    reserve = next(item for item in artifacts if item.family == "reserve")
    tightened_reserve = PolicyArtifact.create(
        **{
            **{key: value for key, value in reserve.to_dict().items() if key != "content_sha256"},
            "theta_local": {"price_margin": 0.80},
        },
    )
    artifacts = [tightened_reserve if item.family == "reserve" else item for item in artifacts]
    result = compile_target_plan(
        probability_envelopes=[_envelope()],
        market_snapshot={
            "snapshot_id": "market-snapshot-reserve",
            "sequence": 1,
            "observed_at": DECISION,
                "market_id": "market-zuuu-2026-07-23",
                "station_id": "ZUUU",
                "event_date": "2026-07-23",
                "settlement_timezone": SETTLEMENT_TIMEZONE,
            "quote_version": "quote-1",
            "quote_observed_at": DECISION,
            "quote_source": "OFFICIAL_CLOB_WS",
            "inventory_version": "inventory-1",
            "order_state_version": "orders-1",
            "remaining_budget_usdc": 12.0,
            "buckets": [
                {"bucket": "37", "bid_price": 0.10, "ask_levels": [{"price": 0.12, "shares": 100.0}], "maker_queue_ahead_shares": 1.0, "maker_queue_known": True},
                {"bucket": "38", "bid_price": 0.20, "ask_levels": [{"price": 0.22, "shares": 100.0}], "maker_queue_ahead_shares": 1.0, "maker_queue_known": True},
                {"bucket": "39", "bid_price": 0.10, "ask_levels": [{"price": 0.12, "shares": 100.0}], "maker_queue_ahead_shares": 1.0, "maker_queue_known": True},
            ],
        },
        policy_activations=activations,
        global_priors={prior.global_prior_id: prior},
        policy_artifacts=artifacts,
        evaluation_at_utc="2026-07-23T04:30:00Z",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={"clip_max_notional_usdc": 3.0},
        cash_phase_policy=cash_policy,
        cash_phase_input=cash_input,
        delta_v2_policy=_delta_v2_policy(),
        delta_v2_fill_resolver=_delta_v2_fill,
        resource_probe_ok=True,
        regime_artifact=build_regime_artifact(_envelope()),
    )
    assert result["plan"]["center_bucket"] == "38"
    assert result["plan"]["direction"] == Direction.NEUTRAL.value
    assert result["station_reserve_violations"] == []
    assert result["plan"]["delta_v2_audit"]["policy_artifact_id"] == (
        "delta-v2-test"
    )
    assert result["boundary"]["orders_submitted"] == 0
    assert result["boundary"]["calls_order_gateway"] is False


def test_full_bridge_reaches_target_plan_without_orders() -> None:
    prior, artifacts, activations = _policy_inputs()
    cash_policy, cash_input = _cash_phase_inputs()
    market_snapshot = {
        "snapshot_id": "market-snapshot-1",
        "sequence": 1,
        "observed_at": DECISION,
            "market_id": "market-zuuu-2026-07-23",
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
        "quote_version": "quote-1",
        "quote_observed_at": DECISION,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-1",
        "order_state_version": "orders-1",
        "remaining_budget_usdc": 12.0,
        "buckets": [
            {"bucket": "37", "bid_price": 0.10, "ask_levels": [{"price": 0.12, "shares": 100.0}], "maker_queue_ahead_shares": 1.0, "maker_queue_known": True},
            {"bucket": "38", "bid_price": 0.20, "ask_levels": [{"price": 0.22, "shares": 100.0}], "maker_queue_ahead_shares": 1.0, "maker_queue_known": True},
            {"bucket": "39", "bid_price": 0.10, "ask_levels": [{"price": 0.12, "shares": 100.0}], "maker_queue_ahead_shares": 1.0, "maker_queue_known": True},
        ],
        "checkpoint_changed": True,
    }
    result = compile_target_plan(
        probability_envelopes=[_envelope()],
        market_snapshot=market_snapshot,
        policy_activations=activations,
        global_priors={prior.global_prior_id: prior},
        policy_artifacts=artifacts,
        evaluation_at_utc="2026-07-23T04:30:00Z",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={"clip_max_notional_usdc": 3.0},
        cash_phase_policy=cash_policy,
        cash_phase_input=cash_input,
        delta_v2_policy=_delta_v2_policy(),
        delta_v2_fill_resolver=_delta_v2_fill,
        resource_probe_ok=True,
        regime_artifact=build_regime_artifact(_envelope()),
    )
    assert result["boundary"]["orders_submitted"] == 0
    assert result["boundary"]["calls_order_gateway"] is False
    assert result["optimizer"]["optimizer_id"] == "DeltaBasketScoreV2Optimizer"
    assert result["boundary"]["economic_decision_point"] == (
        "DeltaBasketScoreV2Optimizer"
    )
    assert result["plan"]["center_bucket"] == "38"
    assert result["plan"]["direction"] == Direction.NEUTRAL.value
    assert result["cash_phase_decision"]["next_phase"] == "TAIL"
    assert result["plan"]["lineage"]["cash_phase"] == "TAIL"
    assert result["order_sizing_decisions"]
    assert all(
        row["c_order"] == pytest.approx(row["q_order"] * row["r_limit"])
        for row in result["order_sizing_decisions"]
    )

    larger_policy = BasketCashPhasePolicyArtifact.create(
        policy_version="production-no-live-test-cash-v2",
        station_id="ZUUU",
        station_timezone="Asia/Shanghai",
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase={"HEAVY": 6.0, "STANDARD": 8.0, "TAIL": 1.0},
        valid_from_utc=VALID_FROM,
        valid_until_utc=VALID_UNTIL,
        evidence_status="DESIGN_CANDIDATE",
    )
    larger_input = BasketCashPhaseInput.from_mapping({
        **cash_input.to_dict(),
        "policy_version": larger_policy.policy_version,
    })
    larger = compile_target_plan(
        probability_envelopes=[_envelope()],
        market_snapshot=market_snapshot,
        policy_activations=activations,
        global_priors={prior.global_prior_id: prior},
        policy_artifacts=artifacts,
        evaluation_at_utc="2026-07-23T04:30:00Z",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        base_policy={"clip_max_notional_usdc": 3.0},
        cash_phase_policy=larger_policy,
        cash_phase_input=larger_input,
        delta_v2_policy=_delta_v2_policy(),
        delta_v2_fill_resolver=_delta_v2_fill,
        resource_probe_ok=True,
        regime_artifact=build_regime_artifact(_envelope()),
    )
    assert larger["plan"]["center_bucket"] == result["plan"]["center_bucket"]
    assert larger["plan"]["direction"] == result["plan"]["direction"]
    assert (
        larger["plan"]["candidate_support"]
        == result["plan"]["candidate_support"]
    )
    assert (
        larger["plan"]["final_support"]
        == result["plan"]["final_support"]
    )
    assert larger["plan"]["total_target_shares"] == pytest.approx(result["plan"]["total_target_shares"])
    assert [row["target_shares"] for row in larger["plan"]["targets"]] == pytest.approx(
        [row["target_shares"] for row in result["plan"]["targets"]]
    )
    assert larger["cash_phase_decision"]["cash_template_usdc"] == 1.0
    assert all(
        high["c_order"] + 1e-9 >= low["c_order"]
        for low, high in zip(result["order_sizing_decisions"], larger["order_sizing_decisions"])
    )


def test_model_disagreement_expands_candidate_boundary_while_extrema_remain_diagnostic() -> None:
    prior, artifacts, activations = _policy_inputs()
    cash_policy, cash_input = _cash_phase_inputs()
    base = _envelope()
    expanded = ProbabilityEnvelope.from_mapping({
        **base.to_dict(),
        "checkpoint_id": "p0m-expanded-diagnostic-boundary",
        "lineage": {
            **base.lineage,
            "model_envelope_span_c": "2",
            "model_envelope_lower_bucket": "37",
            "model_envelope_upper_bucket": "39",
                "model_envelope_lower_ordinal": "37",
                "model_envelope_upper_ordinal": "39",
                "formal_model_members_json": json.dumps(
                    [
                        {
                            "model_id": "ECMWF",
                            "weight": 0.6,
                            "calibrated_center_c": 39.0,
                            "calibrated_center_bucket": "39",
                        },
                        {
                            "model_id": "GFS",
                            "weight": 0.4,
                            "calibrated_center_c": 37.0,
                            "calibrated_center_bucket": "37",
                        },
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "formal_model_anchor_lower_bucket": "37",
                "formal_model_anchor_upper_bucket": "39",
                "formal_model_anchor_lower_ordinal": "37",
                "formal_model_anchor_upper_ordinal": "39",
            },
        })
    market_snapshot = {
        "snapshot_id": "market-snapshot-envelope-boundary",
        "sequence": 1,
        "observed_at": DECISION,
            "market_id": "market-zuuu-2026-07-23",
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
        "quote_version": "quote-envelope-boundary",
        "quote_observed_at": DECISION,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-envelope-boundary",
        "order_state_version": "orders-envelope-boundary",
        "remaining_budget_usdc": 12.0,
        "buckets": [
            {"bucket": bucket, "bid_price": 0.10, "ask_levels": [{"price": 0.12, "shares": 100.0}], "maker_queue_ahead_shares": 1.0, "maker_queue_known": True}
            for bucket in ("37", "38", "39")
        ],
        "checkpoint_changed": True,
    }

    def compile_for(envelope: ProbabilityEnvelope) -> dict:
        return compile_target_plan(
            probability_envelopes=[envelope],
            market_snapshot=market_snapshot,
            policy_activations=activations,
            global_priors={prior.global_prior_id: prior},
            policy_artifacts=artifacts,
            evaluation_at_utc="2026-07-23T04:30:00Z",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            base_policy={"clip_max_notional_usdc": 3.0},
            cash_phase_policy=cash_policy,
            cash_phase_input=cash_input,
            delta_v2_policy=_delta_v2_policy(),
        delta_v2_fill_resolver=_delta_v2_fill,
        resource_probe_ok=True,
            regime_artifact=build_regime_artifact(envelope),
        )

    compact_plan = compile_for(base)["plan"]
    expanded_plan = compile_for(expanded)["plan"]
    assert compact_plan["center_bucket"] == expanded_plan["center_bucket"] == "38"
    assert compact_plan["direction"] == expanded_plan["direction"] == Direction.NEUTRAL.value
    assert compact_plan["candidate_support"] == ["38"]
    assert expanded_plan["candidate_support"] == ["38"]
    assert compact_plan["diagnostic_lower_ordinal"] is None
    assert compact_plan["diagnostic_upper_ordinal"] is None
    assert (
        compact_plan["candidate_lower_ordinal"],
        compact_plan["candidate_upper_ordinal"],
    ) == (38, 38)
    assert (
        expanded_plan["diagnostic_lower_ordinal"],
        expanded_plan["diagnostic_upper_ordinal"],
    ) == (37, 39)
    assert (
        expanded_plan["candidate_lower_ordinal"],
        expanded_plan["candidate_upper_ordinal"],
    ) == (37, 39)
