from __future__ import annotations

import itertools
import json
from dataclasses import asdict, replace

import pytest

from bayes_basket_target import (
    CalibrationActivation,
    ContractError,
    ModelEnsembleArtifact,
    ResidualObservation,
    content_sha256,
    fit_model_calibration_artifact,
    model_set_hash,
)
from bayes_basket_target.production_io import ProductionArtifactBundle
from scripts.build_bayes_basket_target_p0_packets import (
    _curve_rows,
    _curve_rows_from_receipts,
    _select_latest_compatible_bundle,
    bucket_intervals_from_registry_rows,
)
from bayes_basket_target.p0_producer import (
    _current_runs,
    compatible_run_candidates,
    market_bucket_bridge_identity,
)


SHA = "a" * 64
MODELS = ("ECMWF", "GFS", "ICON")


def _rows(unit: str):
    values = [
        ("low", None, 20.0),
        ("mid", 21.0, 21.0),
        ("high", 22.0, None),
    ]
    return [
        {
            "station_id": "TEST",
            "event_date": "2026-07-26",
            "settlement_unit": unit,
            "outcome": outcome,
            "registry_json": json.dumps({"lower": lower, "upper": upper}),
        }
        for outcome, lower, upper in values
    ]


def test_celsius_integer_buckets_use_half_degree_edges() -> None:
    intervals = bucket_intervals_from_registry_rows(_rows("C"))
    assert intervals == {
        "low": (None, 20.5),
        "mid": (20.5, 21.5),
        "high": (21.5, None),
    }


def test_fahrenheit_edges_are_converted_to_celsius_after_half_degree_shift() -> None:
    intervals = bucket_intervals_from_registry_rows(_rows("F"))
    assert intervals["low"][0] is None
    assert intervals["low"][1] == pytest.approx((20.5 - 32) * 5 / 9)
    assert intervals["mid"][0] == pytest.approx((20.5 - 32) * 5 / 9)
    assert intervals["mid"][1] == pytest.approx((21.5 - 32) * 5 / 9)
    assert intervals["high"][0] == pytest.approx((21.5 - 32) * 5 / 9)
    assert intervals["high"][1] is None


def test_bucket_gap_fails_closed() -> None:
    rows = _rows("C")
    rows[1]["registry_json"] = json.dumps({"lower": 23.0, "upper": 23.0})
    with pytest.raises(ContractError, match="tail_buckets_missing|bucket_gap"):
        bucket_intervals_from_registry_rows(rows)


def test_multi_integer_fahrenheit_ranges_remain_contiguous() -> None:
    rows = [
        {
            "station_id": "TEST",
            "event_date": "2026-07-26",
            "settlement_unit": "F",
            "outcome": outcome,
            "registry_json": json.dumps({"lower": lower, "upper": upper}),
        }
        for outcome, lower, upper in (
            ("75°F or below", None, 75.0),
            ("76-77°F", 76.0, 77.0),
            ("78-79°F", 78.0, 79.0),
            ("80°F or higher", 80.0, None),
        )
    ]
    intervals = bucket_intervals_from_registry_rows(rows)
    assert intervals["76-77°F"] == pytest.approx((
        (75.5 - 32.0) * 5.0 / 9.0,
        (77.5 - 32.0) * 5.0 / 9.0,
    ))
    assert intervals["78-79°F"][0] == intervals["76-77°F"][1]


def test_station_day_bridge_ignores_unrelated_catalog_generation() -> None:
    rows = []
    for index, row in enumerate(_rows("C"), start=1):
        registry = json.loads(row["registry_json"])
        registry.update({
            "market_id": f"market-{index}",
            "condition_id": f"condition-{index}",
        })
        rows.append({
            **row,
            "settlement_timezone": "UTC",
            "yes_token": f"yes-{index}",
            "no_token": f"no-{index}",
            "rules_sha256": f"{index:x}" * 64,
            "registry_json": json.dumps(registry),
            "refreshed_at": "2026-07-29T00:00:00Z",
        })
    intervals = bucket_intervals_from_registry_rows(rows)
    first = market_bucket_bridge_identity(
        rows,
        bucket_intervals=intervals,
        settlement_timezone="UTC",
    )
    refreshed = [
        {**row, "refreshed_at": "2026-07-30T00:00:00Z"}
        for row in rows
    ]
    second = market_bucket_bridge_identity(
        refreshed,
        bucket_intervals=intervals,
        settlement_timezone="UTC",
    )

    assert first == second
    assert content_sha256(first) == content_sha256(second)


def test_station_day_bridge_changes_when_token_contract_changes() -> None:
    rows = []
    for index, row in enumerate(_rows("C"), start=1):
        registry = json.loads(row["registry_json"])
        registry.update({
            "market_id": f"market-{index}",
            "condition_id": f"condition-{index}",
        })
        rows.append({
            **row,
            "settlement_timezone": "UTC",
            "yes_token": f"yes-{index}",
            "no_token": f"no-{index}",
            "rules_sha256": f"{index:x}" * 64,
            "registry_json": json.dumps(registry),
        })
    intervals = bucket_intervals_from_registry_rows(rows)
    baseline = market_bucket_bridge_identity(
        rows,
        bucket_intervals=intervals,
        settlement_timezone="UTC",
    )
    changed = [{**row} for row in rows]
    changed[1]["yes_token"] = "replacement-token"

    assert content_sha256(baseline) != content_sha256(
        market_bucket_bridge_identity(
            changed,
            bucket_intervals=intervals,
            settlement_timezone="UTC",
        )
    )


def _activation(
    *,
    kind: str,
    artifact_id: str,
    model: str = "*",
    mask: str = "*",
) -> CalibrationActivation:
    return CalibrationActivation.from_mapping({
        "activation_id": f"activation-{kind}-{artifact_id}",
        "artifact_kind": kind,
        "artifact_id": artifact_id,
        "station_id": "CYYZ",
        "model_id_or_star": model,
        "season_bin": "summer",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "settlement_definition": "official_daily_tmax_c",
        "available_model_set_hash_or_star": mask,
        "generation": 1,
        "valid_from_utc": "2026-07-25T00:00:00Z",
        "valid_until_utc": "2026-08-25T00:00:00Z",
        "activated_at_utc": "2026-07-25T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "content_sha256": SHA,
    })


def _calibration(model: str):
    return fit_model_calibration_artifact(
        [
            ResidualObservation(
                residual_id=f"{model}-r-{index}",
                station_id="CYYZ",
                model_id=model,
                season_bin="summer",
                lead_bucket="d2",
                run_cycle="18Z",
                residual_c=0.0,
                target_local_date=f"2026-07-{day:02d}",
                paired_asof_utc=f"2026-07-{day:02d}T18:00:00Z",
            )
            for index, day in enumerate((20, 21), start=1)
        ],
        artifact_id=f"cal-{model}-18z",
        trained_through_utc="2026-07-22T00:00:00Z",
        valid_from_utc="2026-07-25T00:00:00Z",
        valid_until_utc="2026-08-25T00:00:00Z",
        promoted=False,
    )


def _ensemble() -> ModelEnsembleArtifact:
    masks = {
        model_set_hash(subset): {model: 1.0 / len(subset) for model in subset}
        for size in range(2, 4)
        for subset in itertools.combinations(MODELS, size)
    }
    raw = {
        "ensemble_artifact_id": "ens-18z",
        "station_id": "CYYZ",
        "season_bin": "summer",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "settlement_definition": "official_daily_tmax_c",
        "candidate_models": list(MODELS),
        "supported_masks": masks,
        "residual_covariance": {},
        "correlation_penalty": 0.0,
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
        "valid_from_utc": "2026-07-25T00:00:00Z",
        "valid_until_utc": "2026-08-25T00:00:00Z",
        "promoted": False,
    }
    identity = {
        "ensemble_artifact_id": raw["ensemble_artifact_id"],
        "station_id": raw["station_id"],
        "season_bin": raw["season_bin"],
        "lead_bucket": raw["lead_bucket"],
        "run_cycle": raw["run_cycle"],
        "settlement_definition": raw["settlement_definition"],
        "candidate_models": list(sorted(MODELS)),
        "supported_masks": masks,
        "residual_covariance": raw["residual_covariance"],
        "correlation_penalty": raw["correlation_penalty"],
        "selection_evidence_sha256": raw["selection_evidence_sha256"],
        "previous_ensemble_artifact_sha256": raw[
            "previous_ensemble_artifact_sha256"
        ],
        "weight_method": raw["weight_method"],
        "effective_sample_n": raw["effective_sample_n"],
        "max_weight_delta": raw["max_weight_delta"],
        "train_start_date": raw["train_start_date"],
        "train_end_date": raw["train_end_date"],
        "recent_start_date": raw["recent_start_date"],
        "recent_end_date": raw["recent_end_date"],
        "holdout_start_date": raw["holdout_start_date"],
        "holdout_end_date": raw["holdout_end_date"],
        "oos_metrics": raw["oos_metrics"],
        "valid_from_utc": raw["valid_from_utc"],
        "valid_until_utc": raw["valid_until_utc"],
        "promoted": raw["promoted"],
    }
    return ModelEnsembleArtifact.from_mapping({
        **raw,
        "content_sha256": content_sha256(identity),
    })


def _rehash_ensemble(
    artifact: ModelEnsembleArtifact,
    **changes: object,
) -> ModelEnsembleArtifact:
    """Rebuild fixture content hash using the formal consumption identity."""

    value = replace(artifact, **changes)
    raw = asdict(value)
    raw["candidate_models"] = list(value.candidate_models)
    raw["supported_masks"] = {
        key: dict(weights)
        for key, weights in value.supported_masks.items()
    }
    identity = {
        "ensemble_artifact_id": value.ensemble_artifact_id,
        "station_id": value.station_id,
        "season_bin": value.season_bin,
        "lead_bucket": value.lead_bucket,
        "run_cycle": value.run_cycle,
        "settlement_definition": value.settlement_definition,
        "candidate_models": list(value.candidate_models),
        "supported_masks": {
            key: dict(weights)
            for key, weights in value.supported_masks.items()
        },
        "residual_covariance": {
            key: dict(matrix)
            for key, matrix in value.residual_covariance.items()
        },
        "correlation_penalty": value.correlation_penalty,
        "selection_evidence_sha256": value.selection_evidence_sha256,
        "previous_ensemble_artifact_sha256": value.previous_ensemble_artifact_sha256,
        "weight_method": value.weight_method,
        "effective_sample_n": value.effective_sample_n,
        "max_weight_delta": value.max_weight_delta,
        "train_start_date": value.train_start_date,
        "train_end_date": value.train_end_date,
        "recent_start_date": value.recent_start_date,
        "recent_end_date": value.recent_end_date,
        "holdout_start_date": value.holdout_start_date,
        "holdout_end_date": value.holdout_end_date,
        "oos_metrics": dict(value.oos_metrics),
        "valid_from_utc": value.valid_from_utc,
        "valid_until_utc": value.valid_until_utc,
        "promoted": value.promoted,
    }
    raw["content_sha256"] = content_sha256(identity)
    return ModelEnsembleArtifact.from_mapping(raw)


def _curve(model: str, run_init: str) -> dict:
    return {
        "state": "COMPLETE",
        "station_id": "CYYZ",
        "event_date": "2026-07-27",
        "model_id": model,
        "model_run_initialization_utc": run_init,
        "forecast_daily_max_c": 28.0,
        "curve_sha256": SHA,
        "points": [{
            "published_at_utc": "2026-07-25T19:00:00Z",
            "received_at_utc": "2026-07-25T19:01:00Z",
        }],
    }


def test_p0_current_run_preserves_aggregator_availability_without_fake_publication() -> None:
    curve = _curve("CMA", "2026-07-25T18:00:00Z")
    curve["source_class"] = "aggregator_republisher"
    curve["points"][0]["published_at_utc"] = None
    curve["points"][0]["source_class"] = "aggregator_republisher"

    runs = _current_runs(
        {"CMA": curve},
        station="CYYZ",
        event_date="2026-07-27",
        lead_bucket="d2",
        run_cycle="18Z",
        season_bin="summer",
        settlement_timezone="America/New_York",
    )

    assert len(runs) == 1
    assert runs[0].source_class == "aggregator_republisher"
    assert runs[0].published_at_utc is None
    assert runs[0].is_asof("2026-07-25T19:01:00Z") is True


def test_p0_packet_falls_back_when_newer_partial_mask_is_not_activated() -> None:
    curves = [
        _curve(model, "2026-07-26T00:00:00Z")
        for model in ("ECMWF", "GFS")
    ] + [
        _curve(model, "2026-07-25T18:00:00Z")
        for model in MODELS
    ]
    identity = {
        "schema": "bayes_basket_target_native_model_arrival_decode_evidence_v1",
        "target_local_date": "2026-07-27",
        "curves": {"curves": curves},
    }
    grouped = _curve_rows(
        {**identity, "receipt_sha256": content_sha256(identity)},
        event_date="2026-07-27",
    )
    assert len(grouped["CYYZ"]) == 2
    assert set(grouped["CYYZ"][0]) == {"ECMWF", "GFS"}

    calibrations = {model: _calibration(model) for model in MODELS}
    ensemble = _ensemble()
    activations = tuple(
        [
            _activation(
                kind="MODEL_CALIBRATION",
                artifact_id=calibrations[model].calibration_artifact_id,
                model=model,
            )
            for model in MODELS
        ]
        + [
            _activation(
                kind="MODEL_ENSEMBLE",
                artifact_id=ensemble.ensemble_artifact_id,
                mask=model_set_hash(MODELS),
            )
        ]
    )
    artifacts = ProductionArtifactBundle(
        calibration_artifacts=calibrations,
        ensemble_artifacts={ensemble.ensemble_artifact_id: ensemble},
        calibration_activations=activations,
        global_priors={},
        policy_artifacts={},
        policy_activations=(),
        cash_phase_policies=(),
    )
    selected, diagnostic, selected_ensemble = _select_latest_compatible_bundle(
        grouped["CYYZ"],
        artifacts=artifacts,
        station="CYYZ",
        event_date="2026-07-27",
        settlement_timezone="America/New_York",
        decision_at_utc="2026-07-26T03:00:00Z",
    )
    assert {row.model_id for row in selected} == set(MODELS)
    assert {row.model_id for row in diagnostic} == set(MODELS)
    assert {row.run_init_utc for row in selected} == {"2026-07-25T18:00:00Z"}
    assert {row.lead_bucket for row in selected} == {"d2"}
    assert {row.run_cycle for row in selected} == {"18Z"}
    assert selected_ensemble.ensemble_artifact_id == "ens-18z"
    compatible = compatible_run_candidates(
        artifacts=artifacts,
        station="CYYZ",
        event_date="2026-07-27",
        settlement_timezone="America/New_York",
        decision_at_utc="2026-07-26T03:00:00Z",
        run_initializations=[
            "2026-07-26T00:00:00Z",
            "2026-07-25T18:00:00Z",
        ],
    )
    assert [row["run_initialization_utc"] for row in compatible] == [
        "2026-07-25T18:00:00Z"
    ]
    assert compatible[0]["activation_binding"]["selected_models"] == list(
        MODELS
    )
    assert compatible[0]["activation_binding"][
        "ensemble_artifact_id"
    ] == "ens-18z"
    assert compatible[0]["activation_binding"][
        "activation_generation"
    ] == 1


def test_p0_packet_uses_newer_partial_mask_when_exact_scope_is_activated() -> None:
    curves = [
        _curve(model, "2026-07-26T00:00:00Z")
        for model in ("GFS", "ICON")
    ] + [
        _curve(model, "2026-07-25T18:00:00Z")
        for model in MODELS
    ]
    identity = {
        "schema": "bayes_basket_target_native_model_arrival_decode_evidence_v1",
        "target_local_date": "2026-07-27",
        "curves": {"curves": curves},
    }
    grouped = _curve_rows(
        {**identity, "receipt_sha256": content_sha256(identity)},
        event_date="2026-07-27",
    )
    calibrations = {
        model: replace(
            _calibration(model),
            calibration_artifact_id=f"cal-{model}-00z",
            lead_bucket="d2",
            run_cycle="00Z",
        )
        for model in ("GFS", "ICON")
    }
    ensemble = _rehash_ensemble(
        _ensemble(),
        ensemble_artifact_id="ens-00z",
        lead_bucket="d2",
        run_cycle="00Z",
    )
    activations = tuple(
        [
            replace(
                _activation(
                    kind="MODEL_CALIBRATION",
                    artifact_id=calibrations[model].calibration_artifact_id,
                    model=model,
                ),
                lead_bucket="d2",
                run_cycle="00Z",
            )
            for model in calibrations
        ]
        + [
            replace(
                _activation(
                    kind="MODEL_ENSEMBLE",
                    artifact_id=ensemble.ensemble_artifact_id,
                    mask=model_set_hash(("GFS", "ICON")),
                ),
                lead_bucket="d2",
                run_cycle="00Z",
            )
        ]
    )
    selected, diagnostic, selected_ensemble = _select_latest_compatible_bundle(
        grouped["CYYZ"],
        artifacts=ProductionArtifactBundle(
            calibration_artifacts=calibrations,
            ensemble_artifacts={ensemble.ensemble_artifact_id: ensemble},
            calibration_activations=activations,
            global_priors={},
            policy_artifacts={},
            policy_activations=(),
            cash_phase_policies=(),
        ),
        station="CYYZ",
        event_date="2026-07-27",
        settlement_timezone="America/New_York",
        decision_at_utc="2026-07-26T03:00:00Z",
    )
    assert {row.model_id for row in selected} == {"GFS", "ICON"}
    assert {row.model_id for row in diagnostic} == {"GFS", "ICON"}
    assert {row.run_init_utc for row in selected} == {"2026-07-26T00:00:00Z"}
    assert {row.lead_bucket for row in selected} == {"d2"}
    assert selected_ensemble.ensemble_artifact_id == "ens-00z"


def test_probability_exact_mask_is_separate_from_full_diagnostic_model_set() -> None:
    curves = [_curve(model, "2026-07-25T18:00:00Z") for model in MODELS]
    identity = {
        "schema": "bayes_basket_target_native_model_arrival_decode_evidence_v1",
        "target_local_date": "2026-07-27",
        "curves": {"curves": curves},
    }
    grouped = _curve_rows(
        {**identity, "receipt_sha256": content_sha256(identity)},
        event_date="2026-07-27",
    )
    calibrations = {model: _calibration(model) for model in MODELS}
    ensemble = _ensemble()
    activations = tuple(
        [
            _activation(
                kind="MODEL_CALIBRATION",
                artifact_id=calibrations[model].calibration_artifact_id,
                model=model,
            )
            for model in MODELS
        ]
        + [
            _activation(
                kind="MODEL_ENSEMBLE",
                artifact_id=ensemble.ensemble_artifact_id,
                mask=model_set_hash(("ECMWF", "GFS")),
            )
        ]
    )
    selected, diagnostic, selected_ensemble = _select_latest_compatible_bundle(
        grouped["CYYZ"],
        artifacts=ProductionArtifactBundle(
            calibration_artifacts=calibrations,
            ensemble_artifacts={ensemble.ensemble_artifact_id: ensemble},
            calibration_activations=activations,
            global_priors={},
            policy_artifacts={},
            policy_activations=(),
            cash_phase_policies=(),
        ),
        station="CYYZ",
        event_date="2026-07-27",
        settlement_timezone="America/New_York",
        decision_at_utc="2026-07-26T03:00:00Z",
    )
    assert {row.model_id for row in selected} == {"ECMWF", "GFS"}
    assert {row.model_id for row in diagnostic} == set(MODELS)
    assert selected_ensemble.ensemble_artifact_id == ensemble.ensemble_artifact_id


def test_prefetch_uses_oos_selected_two_model_artifact_not_three_model_arrival() -> None:
    selected_models = ("GFS", "ICON")
    mask = model_set_hash(selected_models)
    ensemble = _rehash_ensemble(
        _ensemble(),
        ensemble_artifact_id="ens-selected-gfs-icon",
        candidate_models=list(selected_models),
        supported_masks={
            mask: {"GFS": 0.7, "ICON": 0.3},
        },
    )
    calibrations = {
        model: _calibration(model)
        for model in MODELS
    }
    activations = tuple(
        [
            _activation(
                kind="MODEL_CALIBRATION",
                artifact_id=calibrations[model].calibration_artifact_id,
                model=model,
            )
            for model in MODELS
        ]
        + [
            _activation(
                kind="MODEL_ENSEMBLE",
                artifact_id=ensemble.ensemble_artifact_id,
                mask=mask,
            )
        ]
    )
    candidates = compatible_run_candidates(
        artifacts=ProductionArtifactBundle(
            calibration_artifacts=calibrations,
            ensemble_artifacts={
                ensemble.ensemble_artifact_id: ensemble,
            },
            calibration_activations=activations,
            global_priors={},
            policy_artifacts={},
            policy_activations=(),
            cash_phase_policies=(),
        ),
        station="CYYZ",
        event_date="2026-07-27",
        settlement_timezone="America/New_York",
        decision_at_utc="2026-07-26T03:00:00Z",
        run_initializations=["2026-07-25T18:00:00Z"],
    )

    assert len(candidates) == 1
    assert candidates[0]["activation_binding"]["selected_models"] == [
        "GFS",
        "ICON",
    ]
    assert candidates[0]["activation_binding"][
        "ensemble_artifact_id"
    ] == "ens-selected-gfs-icon"


def test_p0_packet_falls_back_when_newer_complete_scope_has_no_calibration() -> None:
    curves = [
        _curve(model, run_init)
        for run_init in ("2026-07-26T00:00:00Z", "2026-07-25T18:00:00Z")
        for model in MODELS
    ]
    identity = {
        "schema": "bayes_basket_target_native_model_arrival_decode_evidence_v1",
        "target_local_date": "2026-07-27",
        "curves": {"curves": curves},
    }
    grouped = _curve_rows(
        {**identity, "receipt_sha256": content_sha256(identity)},
        event_date="2026-07-27",
    )
    assert len(grouped["CYYZ"]) == 2

    calibrations = {model: _calibration(model) for model in MODELS}
    ensemble = _ensemble()
    artifacts = ProductionArtifactBundle(
        calibration_artifacts=calibrations,
        ensemble_artifacts={ensemble.ensemble_artifact_id: ensemble},
        calibration_activations=tuple(
            [
                _activation(
                    kind="MODEL_CALIBRATION",
                    artifact_id=calibrations[model].calibration_artifact_id,
                    model=model,
                )
                for model in MODELS
            ]
            + [
                _activation(
                    kind="MODEL_ENSEMBLE",
                    artifact_id=ensemble.ensemble_artifact_id,
                    mask=model_set_hash(MODELS),
                )
            ]
        ),
        global_priors={},
        policy_artifacts={},
        policy_activations=(),
        cash_phase_policies=(),
    )
    selected, diagnostic, _selected_ensemble = _select_latest_compatible_bundle(
        grouped["CYYZ"],
        artifacts=artifacts,
        station="CYYZ",
        event_date="2026-07-27",
        settlement_timezone="America/New_York",
        decision_at_utc="2026-07-26T03:00:00Z",
    )
    assert {row.run_init_utc for row in selected} == {"2026-07-25T18:00:00Z"}
    assert {row.model_id for row in diagnostic} == set(MODELS)
    assert {row.lead_bucket for row in selected} == {"d2"}


def test_three_independent_decode_receipts_join_without_legacy_top_level_date() -> None:
    receipts = []
    for model in MODELS:
        identity = {
            "schema": "bayes_basket_target_native_model_arrival_decode_evidence_v1",
            "curves": {"curves": [_curve(model, "2026-07-25T18:00:00Z")]},
        }
        receipts.append({**identity, "receipt_sha256": content_sha256(identity)})
    grouped = _curve_rows_from_receipts(receipts, event_date="2026-07-27")
    assert len(grouped["CYYZ"]) == 1
    assert set(grouped["CYYZ"][0]) == set(MODELS)
