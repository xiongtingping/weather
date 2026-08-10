"""Strict-as-of calibration and P0-M materialization.

This module only compiles already available facts and promoted artifacts.  It
does not train in the basket hot path, query ``latest``, or access a gateway.
"""

from __future__ import annotations

import json
import itertools
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    EPS,
    MIN_FORMAL_ENSEMBLE_MODELS,
    MODEL_IDS,
    CalibrationActivation,
    ContractError,
    CurrentAsOfModelRun,
    ModelCalibrationArtifact,
    ModelEnsembleArtifact,
    ProbabilityEnvelope,
    ResidualObservation,
    content_sha256,
    model_set_hash,
    strict_asof,
    timestamp_in_window,
    timestamp_lte,
)
from .model_arrival import validate_prospective_capture_window
from .p0_historical_meta import (
    P0HistoricalTiltProof,
    compile_p0_historical_zero_tilt_proof,
)


FORMAL_MODEL_MASK_SIZES = tuple(
    range(MIN_FORMAL_ENSEMBLE_MODELS, len(MODEL_IDS) + 1)
)
TAF_NWP_LIVE_FINGERPRINT_SCHEMA = (
    "bayes_basket_taf_nwp_live_compatibility_fingerprint_v1"
)
TAF_NWP_TRANSPORT_EVIDENCE_SCHEMA = (
    "bayes_basket_taf_nwp_transport_oos_evidence_v1"
)
MIN_FORMAL_ENSEMBLE_TRAIN_N = 20
MIN_FORMAL_ENSEMBLE_HOLDOUT_DAYS = 8


def _formal_model_masks(candidate_models: Iterable[str]) -> set[str]:
    """Return the only model masks eligible for a formal ensemble.

    A single model can still be calibrated for diagnostics/ablation, but it
    is never a formal probability ensemble.  Keeping this rule here avoids
    allowing a caller to smuggle a one-model winner through a generic
    artifact's ``supported_masks`` map.
    """

    members = tuple(sorted({str(model).strip().upper() for model in candidate_models}))
    if len(members) < MIN_FORMAL_ENSEMBLE_MODELS or any(model not in MODEL_IDS for model in members):
        raise ContractError("formal_ensemble_requires_at_least_two_models")
    if len(members) > len(MODEL_IDS):
        raise ContractError("formal_ensemble_model_set_invalid")
    return {
        model_set_hash(subset)
        for size in FORMAL_MODEL_MASK_SIZES
        if size <= len(members)
        for subset in itertools.combinations(members, size)
    }


def validate_formal_ensemble_artifact(
    artifact: ModelEnsembleArtifact,
    *,
    require_content_hash: bool = True,
) -> None:
    """Validate the immutable formal ensemble contract at the consumption edge."""

    try:
        train_start = datetime.fromisoformat(artifact.train_start_date).date()
        train_end = datetime.fromisoformat(artifact.train_end_date).date()
        recent_start = datetime.fromisoformat(artifact.recent_start_date).date()
        recent_end = datetime.fromisoformat(artifact.recent_end_date).date()
        holdout_start = datetime.fromisoformat(artifact.holdout_start_date).date()
        holdout_end = datetime.fromisoformat(artifact.holdout_end_date).date()
        valid_from = datetime.fromisoformat(
            artifact.valid_from_utc.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ContractError("formal_ensemble_temporal_boundary_invalid") from exc
    if not (
        train_start <= recent_start <= recent_end <= train_end
        < holdout_start <= holdout_end
    ):
        raise ContractError("formal_ensemble_train_holdout_not_disjoint")
    if float(artifact.effective_sample_n or 0.0) < MIN_FORMAL_ENSEMBLE_TRAIN_N:
        raise ContractError("formal_ensemble_training_sample_insufficient")
    holdout_calendar_days = (holdout_end - holdout_start).days + 1
    if holdout_calendar_days < MIN_FORMAL_ENSEMBLE_HOLDOUT_DAYS:
        raise ContractError("formal_ensemble_holdout_sample_insufficient")
    # The artifact cannot exist before the last holdout label has at least
    # reached its registry-local event date.  Exact settlement close is
    # enforced by the evidence builder, which owns the registry timezone.
    if valid_from.date() < holdout_end:
        raise ContractError("formal_ensemble_valid_before_holdout")

    candidate_models = tuple(sorted(str(model).strip().upper() for model in artifact.candidate_models))
    expected_masks = _formal_model_masks(candidate_models)
    actual_masks = set(artifact.supported_masks)
    if actual_masks != expected_masks:
        raise ContractError("formal_ensemble_exact_masks_incomplete")
    if not artifact.governance_complete:
        raise ContractError("ensemble_selection_evidence_required")
    if not isinstance(artifact.oos_metrics, Mapping) or any(
        key not in artifact.oos_metrics for key in ("brier_score", "nll", "crps")
    ):
        raise ContractError("formal_ensemble_oos_metrics_incomplete")
    for mask, weights in artifact.supported_masks.items():
        members = set(weights)
        if len(members) < MIN_FORMAL_ENSEMBLE_MODELS:
            raise ContractError("formal_ensemble_single_model_forbidden")
        if any(float(weights[model]) <= EPS for model in members):
            raise ContractError("formal_ensemble_weights_must_be_positive")
        if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-8:
            raise ContractError("formal_ensemble_weights_not_normalized")
        if model_set_hash(members) != mask:
            raise ContractError("formal_ensemble_exact_mask_key_invalid")
    if require_content_hash:
        identity = {
            "ensemble_artifact_id": artifact.ensemble_artifact_id,
            "station_id": artifact.station_id,
            "season_bin": artifact.season_bin,
            "lead_bucket": artifact.lead_bucket,
            "run_cycle": artifact.run_cycle,
            "settlement_definition": artifact.settlement_definition,
            "candidate_models": list(artifact.candidate_models),
            "supported_masks": {
                key: dict(value) for key, value in artifact.supported_masks.items()
            },
            "residual_covariance": {
                key: dict(value) for key, value in artifact.residual_covariance.items()
            },
            "correlation_penalty": artifact.correlation_penalty,
            "selection_evidence_sha256": artifact.selection_evidence_sha256,
            "previous_ensemble_artifact_sha256": artifact.previous_ensemble_artifact_sha256,
            "weight_method": artifact.weight_method,
            "effective_sample_n": artifact.effective_sample_n,
            "max_weight_delta": artifact.max_weight_delta,
            "train_start_date": artifact.train_start_date,
            "train_end_date": artifact.train_end_date,
            "recent_start_date": artifact.recent_start_date,
            "recent_end_date": artifact.recent_end_date,
            "holdout_start_date": artifact.holdout_start_date,
            "holdout_end_date": artifact.holdout_end_date,
            "oos_metrics": dict(artifact.oos_metrics),
            "valid_from_utc": artifact.valid_from_utc,
            "valid_until_utc": artifact.valid_until_utc,
            "promoted": artifact.promoted,
        }
        if content_sha256(identity) != artifact.content_sha256:
            raise ContractError("ensemble_artifact_content_sha256_mismatch")


def build_taf_nwp_live_compatibility_fingerprint(
    *,
    ensemble_artifact: ModelEnsembleArtifact,
    calibration_artifacts: Mapping[str, ModelCalibrationArtifact],
    available_models: Iterable[str],
) -> dict[str, Any]:
    """Fingerprint the exact live NWP probability combination used by P0.

    This is deliberately not the historical crossfit prediction artifact
    hash used to fit TAF.  A TAF artifact may target this fingerprint only
    through an explicit OOS transport bridge.
    """

    validate_formal_ensemble_artifact(ensemble_artifact)
    models = tuple(sorted({
        str(model).strip().upper() for model in available_models
    }))
    if (
        len(models) < MIN_FORMAL_ENSEMBLE_MODELS
        or any(model not in MODEL_IDS for model in models)
    ):
        raise ContractError("taf_nwp_live_fingerprint_model_set_invalid")
    weights = ensemble_artifact.resolve_weights(models)
    if set(calibration_artifacts) != set(models):
        raise ContractError(
            "taf_nwp_live_fingerprint_calibration_scope_incomplete"
        )
    calibration_identity: dict[str, dict[str, str]] = {}
    for model in models:
        artifact = calibration_artifacts[model]
        if (
            artifact.station_id,
            artifact.model_id,
            artifact.season_bin,
            artifact.lead_bucket,
            artifact.run_cycle,
            artifact.settlement_definition,
        ) != (
            ensemble_artifact.station_id,
            model,
            ensemble_artifact.season_bin,
            ensemble_artifact.lead_bucket,
            ensemble_artifact.run_cycle,
            ensemble_artifact.settlement_definition,
        ):
            raise ContractError(
                "taf_nwp_live_fingerprint_calibration_scope_mismatch"
            )
        calibration_identity[model] = {
            "calibration_artifact_id": artifact.calibration_artifact_id,
            "calibration_artifact_content_sha256": artifact.content_sha256,
        }
    identity = {
        "schema": TAF_NWP_LIVE_FINGERPRINT_SCHEMA,
        "station_id": ensemble_artifact.station_id,
        "season_bin": ensemble_artifact.season_bin,
        "lead_bucket": ensemble_artifact.lead_bucket,
        "run_cycle": ensemble_artifact.run_cycle,
        "settlement_definition": ensemble_artifact.settlement_definition,
        "available_model_set_hash": model_set_hash(models),
        "available_models": list(models),
        "resolved_weights": {
            model: float(weights[model]) for model in models
        },
        "ensemble_artifact_id": ensemble_artifact.ensemble_artifact_id,
        "ensemble_artifact_content_sha256": ensemble_artifact.content_sha256,
        "ensemble_selection_evidence_sha256": (
            ensemble_artifact.selection_evidence_sha256
        ),
        "ensemble_weight_method": ensemble_artifact.weight_method,
        "model_calibration_artifacts": calibration_identity,
    }
    return {
        **identity,
        "fingerprint_sha256": content_sha256(identity),
    }


def bind_taf_artifact_to_live_nwp_compatibility(
    *,
    taf_artifact: Mapping[str, Any] | Any,
    live_fingerprint: Mapping[str, Any],
    transport_oos_evidence: Mapping[str, Any],
) -> Any:
    """Return a new TAF artifact with a proved crossfit→live bridge.

    The historical crossfit artifact and current live ensemble remain
    different content-addressed objects.  This function binds them only when
    an independently content-addressed OOS transport evaluation proves that
    the TAF increment remains valid for the exact live probability
    combination fingerprint.
    """

    # Imported locally to keep the calibration and TAF modules acyclic.
    from .taf_revision import (
        TAF_NWP_CROSSFIT_ARTIFACT_KIND,
        TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION,
        TafRevisionArtifact,
        taf_scope_lead_bucket,
    )
    from .native_ensemble_contract import (
        TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
    )

    artifact = (
        taf_artifact
        if isinstance(taf_artifact, TafRevisionArtifact)
        else TafRevisionArtifact.from_mapping(taf_artifact)
    )
    if (
        not isinstance(live_fingerprint, Mapping)
        or live_fingerprint.get("schema") not in {
            TAF_NWP_LIVE_FINGERPRINT_SCHEMA,
            TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
        }
    ):
        raise ContractError("taf_nwp_live_fingerprint_schema_invalid")
    fingerprint_sha = str(
        live_fingerprint.get("fingerprint_sha256") or ""
    ).lower()
    fingerprint_identity = {
        str(key): value
        for key, value in live_fingerprint.items()
        if key != "fingerprint_sha256"
    }
    if (
        len(fingerprint_sha) != 64
        or content_sha256(fingerprint_identity) != fingerprint_sha
    ):
        raise ContractError("taf_nwp_live_fingerprint_content_sha_mismatch")
    if (
        str(live_fingerprint.get("station_id") or "").upper(),
        str(live_fingerprint.get("season_bin") or ""),
        taf_scope_lead_bucket(live_fingerprint),
        str(live_fingerprint.get("run_cycle") or ""),
        str(live_fingerprint.get("settlement_definition") or ""),
    ) != (
        artifact.station_id.upper(),
        artifact.season_bin,
        artifact.lead_bucket,
        artifact.run_cycle,
        artifact.settlement_definition,
    ):
        raise ContractError("taf_nwp_live_fingerprint_scope_mismatch")
    live_ensemble_sha = str(
        live_fingerprint.get("ensemble_artifact_content_sha256")
        or live_fingerprint.get("cross_ensemble_artifact_sha256")
        or ""
    ).lower()
    if artifact.nwp_combination_artifact_sha256 == live_ensemble_sha:
        raise ContractError(
            "taf_crossfit_live_artifact_hash_alias_forbidden"
        )

    allowed_evidence = {
        "schema",
        "evidence_id",
        "crossfit_prediction_artifact_sha256",
        "live_fingerprint_sha256",
        "station_id",
        "season_bin",
        "lead_bucket",
        "run_cycle",
        "settlement_definition",
        "sample_n",
        "effective_sample_n",
        "baseline_oos_metrics",
        "transport_oos_metrics",
        "nll_not_worse",
        "crps_not_worse",
        "oos_transport_validated",
        "content_sha256",
    }
    if (
        not isinstance(transport_oos_evidence, Mapping)
        or set(transport_oos_evidence) != allowed_evidence
        or transport_oos_evidence.get("schema")
        != TAF_NWP_TRANSPORT_EVIDENCE_SCHEMA
    ):
        raise ContractError("taf_nwp_transport_oos_evidence_invalid")
    evidence_identity = {
        str(key): value
        for key, value in transport_oos_evidence.items()
        if key != "content_sha256"
    }
    evidence_sha = str(
        transport_oos_evidence.get("content_sha256") or ""
    ).lower()
    if (
        len(evidence_sha) != 64
        or content_sha256(evidence_identity) != evidence_sha
    ):
        raise ContractError(
            "taf_nwp_transport_oos_evidence_content_sha_mismatch"
        )
    if (
        str(
            transport_oos_evidence.get(
                "crossfit_prediction_artifact_sha256"
            )
            or ""
        ).lower()
        != artifact.nwp_combination_artifact_sha256
        or str(
            transport_oos_evidence.get("live_fingerprint_sha256")
            or ""
        ).lower()
        != fingerprint_sha
    ):
        raise ContractError("taf_nwp_transport_oos_evidence_binding_mismatch")
    if (
        str(transport_oos_evidence.get("station_id") or "").upper(),
        str(transport_oos_evidence.get("season_bin") or ""),
        str(transport_oos_evidence.get("lead_bucket") or ""),
        str(transport_oos_evidence.get("run_cycle") or ""),
        str(transport_oos_evidence.get("settlement_definition") or ""),
    ) != (
        artifact.station_id.upper(),
        artifact.season_bin,
        artifact.lead_bucket,
        artifact.run_cycle,
        artifact.settlement_definition,
    ):
        raise ContractError("taf_nwp_transport_oos_evidence_scope_mismatch")
    try:
        sample_n = int(transport_oos_evidence.get("sample_n"))
        effective_n = float(
            transport_oos_evidence.get("effective_sample_n")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "taf_nwp_transport_oos_evidence_sample_invalid"
        ) from exc
    if sample_n < 24 or effective_n <= 0.0 or effective_n > sample_n:
        raise ContractError("taf_nwp_transport_oos_evidence_sample_invalid")
    baseline = transport_oos_evidence.get("baseline_oos_metrics")
    transported = transport_oos_evidence.get("transport_oos_metrics")
    if not isinstance(baseline, Mapping) or not isinstance(
        transported, Mapping
    ):
        raise ContractError("taf_nwp_transport_oos_metrics_invalid")
    try:
        for metric in ("brier_score", "nll", "crps"):
            baseline_value = float(baseline[metric])
            transported_value = float(transported[metric])
            if (
                not math.isfinite(baseline_value)
                or not math.isfinite(transported_value)
                or baseline_value < 0.0
                or transported_value < 0.0
            ):
                raise ValueError(metric)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("taf_nwp_transport_oos_metrics_invalid") from exc
    if (
        transport_oos_evidence.get("nll_not_worse") is not True
        or transport_oos_evidence.get("crps_not_worse") is not True
        or transport_oos_evidence.get("oos_transport_validated") is not True
        or float(transported["nll"]) > float(baseline["nll"]) + 1e-12
        or float(transported["crps"]) > float(baseline["crps"]) + 1e-12
    ):
        raise ContractError("taf_nwp_transport_oos_gate_failed")

    identity = artifact.to_dict(include_content_sha256=False)
    identity.update({
        "nwp_combination_artifact_kind": (
            TAF_NWP_CROSSFIT_ARTIFACT_KIND
        ),
        "nwp_live_compatibility_contract_version": (
            TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        ),
        "nwp_live_compatibility_fingerprint_sha256": fingerprint_sha,
        "nwp_live_compatibility_evidence_sha256": evidence_sha,
        "nwp_live_compatibility_validated": True,
    })
    return TafRevisionArtifact.from_mapping({
        **identity,
        "content_sha256": content_sha256(identity),
    })


def _raw_exact_scope(raw: Mapping[str, Any]) -> tuple[Any, ...]:
    """Extract optional exact-join fields without discarding unknown metadata."""

    aliases = (
        ("target_window_start_utc", "window_start_utc", "target_start_utc"),
        ("target_window_end_utc", "window_end_utc", "target_end_utc"),
        ("cutoff_utc", "decision_cutoff_utc", "as_of_cutoff_utc"),
        ("truth_revision", "truth_revision_id", "truth_revision_no"),
    )
    values: list[Any] = []
    for names in aliases:
        found = [raw.get(name) for name in names if raw.get(name) not in (None, "")]
        values.append(found[0] if found else None)
    return tuple(values)


def validate_exact_model_scope(
    runs: Sequence[CurrentAsOfModelRun | Mapping[str, Any]],
    *,
    expected_models: Iterable[str] | None = None,
    decision_at_utc: str,
) -> tuple[CurrentAsOfModelRun, ...]:
    """Enforce one settlement scope, cutoff and truth revision per exact mask."""

    if not runs:
        raise ContractError("zero_models_fail_closed")
    raw_rows = [dict(row) if isinstance(row, Mapping) else None for row in runs]
    parsed = tuple(
        row if isinstance(row, CurrentAsOfModelRun) else CurrentAsOfModelRun.from_mapping(row)
        for row in runs
    )
    models = tuple(sorted(row.model_id for row in parsed))
    if len(models) < MIN_FORMAL_ENSEMBLE_MODELS:
        raise ContractError("formal_ensemble_requires_at_least_two_models")
    if len(set(models)) != len(models):
        raise ContractError("duplicate_current_model_run")
    if expected_models is not None and set(models) != {
        str(item).strip().upper() for item in expected_models
    }:
        raise ContractError("exact_model_mask_mismatch")
    if any(raw is not None for raw in raw_rows):
        scopes = [_raw_exact_scope(raw or {}) for raw in raw_rows]
        for index in range(len(scopes[0])):
            populated = {scope[index] for scope in scopes if scope[index] is not None}
            if len(populated) > 1 or (populated and len(populated) != len(scopes)):
                raise ContractError("exact_model_scope_mismatch")
        cutoff = scopes[0][2]
        if cutoff is not None and not timestamp_lte(str(cutoff), decision_at_utc):
            raise ContractError("exact_model_cutoff_future")
    first = parsed[0]
    for row in parsed[1:]:
        if (
            row.station_id,
            row.target_local_date,
            row.season_bin,
            row.lead_bucket,
            row.run_cycle,
            row.settlement_definition,
            _run_init_key(row),
        ) != (
            first.station_id,
            first.target_local_date,
            first.season_bin,
            first.lead_bucket,
            first.run_cycle,
            first.settlement_definition,
            _run_init_key(first),
        ):
            raise ContractError("exact_model_scope_mismatch")
    return parsed


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_init_key(run: CurrentAsOfModelRun) -> datetime:
    """Return the canonical run initialization used by shared-run selection."""

    return _timestamp(run.run_init_utc).astimezone(timezone.utc)


def select_latest_complete_model_run_bundle(
    current_runs: Sequence[CurrentAsOfModelRun | Mapping[str, Any]],
    *,
    decision_at_utc: str,
    required_lead_bucket: str | None = None,
    required_run_cycle: str | None = None,
    required_settlement_definition: str | None = None,
) -> tuple[CurrentAsOfModelRun, ...]:
    """Select the newest complete same-run bundle visible at one decision clock.

    Publication and receive times are provider-local maturity evidence.  They
    never define the join identity.  A candidate bundle is identified by one
    station/day, one ``run_init_utc``, one lead bucket, one cycle and one
    settlement definition, and must contain exactly one row for every model
    present in the input.  When a required scope is supplied, D1/D2 (or cycle)
    candidates outside that scope are excluded before choosing the newest run.
    If the newest run is incomplete or not strict as-of, the newest older
    complete bundle is selected instead.
    """

    raw_items = tuple(current_runs)
    if not raw_items:
        raise ContractError("zero_models_fail_closed")
    rows = tuple(
        item if isinstance(item, CurrentAsOfModelRun) else CurrentAsOfModelRun.from_mapping(item)
        for item in raw_items
    )
    decision = _timestamp(decision_at_utc)
    requested_models = {row.model_id for row in rows}
    if len(requested_models) < MIN_FORMAL_ENSEMBLE_MODELS:
        raise ContractError("formal_ensemble_requires_at_least_two_models")
    if any(model not in MODEL_IDS for model in requested_models):
        raise ContractError("model_set_invalid")
    groups: dict[
        tuple[str, str, datetime, str, str, str, str],
        list[CurrentAsOfModelRun],
    ] = {}
    for row in rows:
        run_init = _run_init_key(row)
        expected_cycle = f"{run_init.hour:02d}Z"
        if row.run_cycle != expected_cycle:
            raise ContractError("current_model_run_cycle_mismatch")
        key = (
            row.station_id,
            row.target_local_date,
            run_init,
            row.season_bin,
            row.lead_bucket,
            row.run_cycle,
            row.settlement_definition,
        )
        groups.setdefault(key, []).append(row)

    candidates: list[tuple[datetime, tuple[CurrentAsOfModelRun, ...]]] = []
    for key, grouped in groups.items():
        if required_lead_bucket is not None and key[4] != required_lead_bucket:
            continue
        if required_run_cycle is not None and key[5] != required_run_cycle:
            continue
        if required_settlement_definition is not None and key[6] != required_settlement_definition:
            continue
        by_model: dict[str, list[CurrentAsOfModelRun]] = {}
        for row in grouped:
            by_model.setdefault(row.model_id, []).append(row)
        if set(by_model) != requested_models or any(len(items) != 1 for items in by_model.values()):
            continue
        selected = tuple(sorted((items[0] for items in by_model.values()), key=lambda item: item.model_id))
        if not all(row.is_asof(decision_at_utc) for row in selected):
            continue
        candidates.append((key[2], selected))

    if not candidates:
        # Preserve the more specific leakage diagnosis for the common case
        # where the caller supplied one otherwise complete bundle whose
        # source receipt is newer than the P0 decision clock.  Mixed D1/D2 or
        # incomplete multi-run inputs remain the generic no-bundle failure.
        exact_groups = []
        for grouped in groups.values():
            by_model: dict[str, list[CurrentAsOfModelRun]] = {}
            for row in grouped:
                by_model.setdefault(row.model_id, []).append(row)
            if set(by_model) == requested_models and all(len(items) == 1 for items in by_model.values()):
                exact_groups.append(tuple(items[0] for items in by_model.values()))
        if len(exact_groups) == 1 and not all(row.is_asof(decision_at_utc) for row in exact_groups[0]):
            raise ContractError("current_model_run_future_leakage")
        raise ContractError("no_complete_shared_model_run_asof")
    # Candidates are complete exact masks only.  Sorting by run initialization
    # gives the newest compatible publication bundle; an incomplete newest run
    # is therefore naturally skipped in favour of the previous compatible run.
    selected = max(candidates, key=lambda item: item[0])[1]
    raw_by_run_id = {
        str(raw.get("model_run_id") or ""): raw
        for raw in raw_items
        if isinstance(raw, Mapping)
    }
    selected_raw = [raw_by_run_id[row.model_run_id] for row in selected if row.model_run_id in raw_by_run_id]
    if selected_raw:
        validate_exact_model_scope(
            selected_raw,
            expected_models=requested_models,
            decision_at_utc=decision_at_utc,
        )
    else:
        validate_exact_model_scope(
            selected,
            expected_models=requested_models,
            decision_at_utc=decision_at_utc,
        )
    return selected


def select_newest_compatible_model_run_bundle(
    current_runs: Sequence[CurrentAsOfModelRun | Mapping[str, Any]],
    *,
    decision_at_utc: str,
    required_lead_bucket: str | None = None,
    required_run_cycle: str | None = None,
    required_settlement_definition: str | None = None,
) -> tuple[CurrentAsOfModelRun, ...]:
    """Named contract entry point for newest-compatible exact-mask fallback."""

    return select_latest_complete_model_run_bundle(
        current_runs,
        decision_at_utc=decision_at_utc,
        required_lead_bucket=required_lead_bucket,
        required_run_cycle=required_run_cycle,
        required_settlement_definition=required_settlement_definition,
    )


def fit_model_calibration_artifact(
    residuals: Sequence[ResidualObservation | Mapping[str, Any]],
    *,
    artifact_id: str,
    trained_through_utc: str,
    valid_from_utc: str,
    valid_until_utc: str,
    parent_bias_c: float = 0.0,
    parent_scale_c: float = 1.5,
    kappa: float = 8.0,
    bias_enabled: bool = True,
    promoted: bool = True,
) -> ModelCalibrationArtifact:
    """Fit one immutable, hierarchically shrunk model/station artifact.

    The fit is intentionally small and deterministic: the training boundary,
    residual sign and shrinkage inputs are explicit.  Production training can
    replace the estimator later without changing the online contract.
    """

    rows = [item if isinstance(item, ResidualObservation) else ResidualObservation.from_mapping(item) for item in residuals]
    if not rows:
        raise ContractError("calibration_residuals_empty")
    if len({row.residual_id for row in rows}) != len(rows):
        raise ContractError("calibration_residual_id_duplicate")
    if len({row.target_local_date for row in rows}) != len(rows):
        raise ContractError("calibration_target_date_duplicate")
    try:
        trained_through = datetime.fromisoformat(
            trained_through_utc.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        valid_from = datetime.fromisoformat(
            valid_from_utc.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        valid_until = datetime.fromisoformat(
            valid_until_utc.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (AttributeError, ValueError) as exc:
        raise ContractError("calibration_artifact_timestamp_invalid") from exc
    if valid_from >= valid_until:
        raise ContractError("calibration_artifact_validity_window_invalid")
    if trained_through >= valid_until:
        raise ContractError("calibration_artifact_expires_before_training_complete")
    first = rows[0]
    if any(
        (row.station_id, row.model_id, row.season_bin, row.lead_bucket, row.run_cycle) !=
        (first.station_id, first.model_id, first.season_bin, first.lead_bucket, first.run_cycle)
        for row in rows
    ):
        raise ContractError("calibration_residual_scope_mismatch")
    accepted = [
        row
        for row in rows
        if row.qc_status == "ACCEPTED"
        and timestamp_lte(row.paired_asof_utc, trained_through_utc)
    ]
    if not accepted:
        raise ContractError("calibration_no_accepted_asof_residuals")
    if not isinstance(bias_enabled, bool):
        raise ContractError("calibration_bias_enabled_invalid")
    if not math.isfinite(kappa) or kappa < 0:
        raise ContractError("calibration_kappa_invalid")
    if not math.isfinite(parent_scale_c) or parent_scale_c <= 0:
        raise ContractError("calibration_parent_scale_invalid")
    residual_values = [row.residual_c for row in accepted]
    mean_residual = sum(residual_values) / len(residual_values)
    n_eff = float(len({row.target_local_date for row in accepted}))
    rho = n_eff / (n_eff + kappa) if n_eff + kappa > EPS else 0.0
    # A short scope can estimate dispersion, but it cannot safely establish a
    # persistent mean bias.  Callers can therefore disable mean correction
    # while retaining the residual scale estimate; this keeps the artifact a
    # real calibration artifact without allowing a handful of days to move
    # the P0/P1 center in an unstable direction.
    bias = (
        rho * mean_residual + (1.0 - rho) * float(parent_bias_c)
        if bias_enabled
        else float(parent_bias_c)
    )
    variance = sum((value - mean_residual) ** 2 for value in residual_values) / max(1, len(residual_values) - 1)
    scale = math.sqrt(max(1e-6, rho * variance + (1.0 - rho) * parent_scale_c**2))
    identity = {
        "artifact_id": artifact_id,
        "station_id": first.station_id,
        "model_id": first.model_id,
        "season_bin": first.season_bin,
        "lead_bucket": first.lead_bucket,
        "run_cycle": first.run_cycle,
        "trained_through_utc": trained_through_utc,
        "sample_n": len(accepted),
        "effective_sample_n": n_eff,
        "bias_c": bias,
        "residual_scale_c": scale,
        "tail_parameters": {"lower_q": 0.05, "upper_q": 0.95},
        "valid_from_utc": valid_from_utc,
        "valid_until_utc": valid_until_utc,
        "training_residual_ids": sorted(row.residual_id for row in accepted),
    }
    return ModelCalibrationArtifact.from_mapping({
        "calibration_artifact_id": artifact_id,
        "station_id": first.station_id,
        "model_id": first.model_id,
        "season_bin": first.season_bin,
        "lead_bucket": first.lead_bucket,
        "run_cycle": first.run_cycle,
        "settlement_definition": "official_daily_tmax_c",
        "trained_through_utc": trained_through_utc,
        "sample_n": len(accepted),
        "effective_sample_n": n_eff,
        "bias_c": bias,
        "residual_scale_c": scale,
        "tail_parameters": {"lower_q": 0.05, "upper_q": 0.95},
        "valid_from_utc": valid_from_utc,
        "valid_until_utc": valid_until_utc,
        "content_sha256": content_sha256(identity),
        "promoted": bool(promoted),
    })


def resolve_active_calibration_artifact(
    *,
    activations: Sequence[CalibrationActivation | Mapping[str, Any]],
    artifacts: Mapping[str, ModelCalibrationArtifact],
    station_id: str,
    model_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    settlement_definition: str,
    decision_at_utc: str,
    required_authority_scope: str = "FORMAL_PROMOTED",
) -> tuple[ModelCalibrationArtifact, CalibrationActivation]:
    """Resolve exactly one promoted artifact by scope and decision time."""

    model = str(model_id).strip().upper()
    rows = [item if isinstance(item, CalibrationActivation) else CalibrationActivation.from_mapping(item) for item in activations]
    eligible = [
        row for row in rows
        if row.artifact_kind == "MODEL_CALIBRATION"
        and row.station_id == station_id
        and row.model_id_or_star == model
        and row.season_bin == season_bin
        and row.lead_bucket == lead_bucket
        and row.run_cycle == run_cycle
        and row.settlement_definition == settlement_definition
        and row.available_model_set_hash_or_star == "*"
        and row.authority_scope == required_authority_scope
        and timestamp_lte(row.activated_at_utc, decision_at_utc)
        and timestamp_in_window(decision_at_utc, row.valid_from_utc, row.valid_until_utc)
    ]
    if not eligible:
        raise ContractError("no_active_calibration_artifact")
    generation = max(row.generation for row in eligible)
    winners = [row for row in eligible if row.generation == generation]
    if len(winners) != 1:
        raise ContractError("ambiguous_active_calibration_artifact")
    activation = winners[0]
    artifact = artifacts.get(activation.artifact_id)
    if artifact is None:
        artifact = next(
            (candidate for candidate in artifacts.values() if candidate.calibration_artifact_id == activation.artifact_id),
            None,
        )
    if artifact is None:
        raise ContractError("calibration_artifact_missing")
    if required_authority_scope == "FORMAL_PROMOTED" and not artifact.promoted:
        raise ContractError("calibration_artifact_not_promoted")
    if required_authority_scope == "PRODUCTION_NO_LIVE_ONLY" and artifact.promoted:
        raise ContractError("no_live_calibration_artifact_must_remain_unpromoted")
    if (
        artifact.station_id,
        artifact.model_id,
        artifact.season_bin,
        artifact.lead_bucket,
        artifact.run_cycle,
        artifact.settlement_definition,
    ) != (station_id, model, season_bin, lead_bucket, run_cycle, settlement_definition):
        raise ContractError("calibration_artifact_scope_mismatch")
    if not timestamp_in_window(decision_at_utc, artifact.valid_from_utc, artifact.valid_until_utc):
        raise ContractError("calibration_artifact_expired")
    if not timestamp_lte(artifact.trained_through_utc, decision_at_utc):
        raise ContractError("calibration_artifact_trained_in_future")
    return artifact, activation


def normal_bucket_probability(mean: float, scale: float, lower: float | None, upper: float | None) -> float:
    """Return normal mass for a half-open bucket, including open tails."""

    if not math.isfinite(mean) or not math.isfinite(scale) or scale <= 0:
        raise ContractError("normal_distribution_parameters_invalid")
    cdf_lower = 0.0 if lower is None else 0.5 * (1.0 + math.erf((float(lower) - mean) / (scale * math.sqrt(2.0))))
    cdf_upper = 1.0 if upper is None else 0.5 * (1.0 + math.erf((float(upper) - mean) / (scale * math.sqrt(2.0))))
    return max(0.0, min(1.0, cdf_upper - cdf_lower))


def _resolve_ensemble_activation(
    activations: Sequence[CalibrationActivation | Mapping[str, Any]],
    *,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    settlement_definition: str,
    available_hash: str,
    decision_at_utc: str,
    required_authority_scope: str = "FORMAL_PROMOTED",
) -> CalibrationActivation:
    rows = [item if isinstance(item, CalibrationActivation) else CalibrationActivation.from_mapping(item) for item in activations]
    scope_eligible = [
        row for row in rows
        if row.artifact_kind == "MODEL_ENSEMBLE"
        and row.station_id == station_id
        and row.model_id_or_star == "*"
        and row.season_bin == season_bin
        and row.lead_bucket == lead_bucket
        and row.run_cycle == run_cycle
        and row.settlement_definition == settlement_definition
        and row.authority_scope == required_authority_scope
        and timestamp_lte(row.activated_at_utc, decision_at_utc)
        and timestamp_in_window(decision_at_utc, row.valid_from_utc, row.valid_until_utc)
    ]
    if not scope_eligible:
        raise ContractError("no_active_ensemble_artifact")
    # Generation is a scope-wide activation epoch, not a per-mask counter.
    # Once a newer model-selection epoch exists, an omitted old mask must not
    # silently reactivate an obsolete ensemble.
    generation = max(row.generation for row in scope_eligible)
    winners = [
        row
        for row in scope_eligible
        if row.generation == generation
        and row.available_model_set_hash_or_star == available_hash
    ]
    if not winners:
        raise ContractError("no_active_ensemble_artifact")
    if len(winners) != 1:
        raise ContractError("ambiguous_active_ensemble_artifact")
    return winners[0]


def resolve_active_ensemble_artifact(
    *,
    activations: Sequence[CalibrationActivation | Mapping[str, Any]],
    artifacts: Mapping[str, ModelEnsembleArtifact],
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    settlement_definition: str,
    available_models: Sequence[str],
    decision_at_utc: str,
    required_authority_scope: str = "FORMAL_PROMOTED",
) -> tuple[ModelEnsembleArtifact, CalibrationActivation]:
    """Resolve the exact activated ensemble for one model-bundle scope."""

    available_members = tuple(sorted(str(item).strip().upper() for item in available_models))
    _formal_model_masks(available_members)
    available_hash = model_set_hash(available_members)
    activation = _resolve_ensemble_activation(
        activations,
        station_id=station_id,
        season_bin=season_bin,
        lead_bucket=lead_bucket,
        run_cycle=run_cycle,
        settlement_definition=settlement_definition,
        available_hash=available_hash,
        decision_at_utc=decision_at_utc,
        required_authority_scope=required_authority_scope,
    )
    artifact = artifacts.get(activation.artifact_id)
    if artifact is None:
        artifact = next(
            (
                candidate
                for candidate in artifacts.values()
                if candidate.ensemble_artifact_id == activation.artifact_id
            ),
            None,
        )
    if artifact is None:
        raise ContractError("ensemble_artifact_missing")
    if required_authority_scope == "FORMAL_PROMOTED" and not artifact.promoted:
        raise ContractError("ensemble_artifact_not_active")
    if required_authority_scope == "PRODUCTION_NO_LIVE_ONLY" and artifact.promoted:
        raise ContractError("no_live_ensemble_artifact_must_remain_unpromoted")
    if (
        artifact.station_id,
        artifact.season_bin,
        artifact.lead_bucket,
        artifact.run_cycle,
        artifact.settlement_definition,
    ) != (
        station_id,
        season_bin,
        lead_bucket,
        run_cycle,
        settlement_definition,
    ):
        raise ContractError("ensemble_artifact_scope_mismatch")
    if not timestamp_in_window(decision_at_utc, artifact.valid_from_utc, artifact.valid_until_utc):
        raise ContractError("ensemble_artifact_not_active")
    validate_formal_ensemble_artifact(artifact)
    artifact.resolve_weights(available_members)
    return artifact, activation


def materialize_p0(
    current_runs: Sequence[CurrentAsOfModelRun | Mapping[str, Any]],
    *,
    diagnostic_runs: Sequence[CurrentAsOfModelRun | Mapping[str, Any]],
    calibration_artifacts: Mapping[str, ModelCalibrationArtifact],
    diagnostic_calibration_artifacts: Mapping[str, ModelCalibrationArtifact],
    ensemble_artifact: ModelEnsembleArtifact,
    calibration_activations: Sequence[CalibrationActivation | Mapping[str, Any]],
    ensemble_activation: CalibrationActivation | Mapping[str, Any] | None = None,
    bucket_intervals: Mapping[str, tuple[float | None, float | None]],
    station_id: str,
    event_date: str,
    decision_at_utc: str,
    fresh_until_utc: str,
    bridge_sha256: str,
    settlement_timezone: str,
    historical_tilt_proof: P0HistoricalTiltProof | Mapping[str, Any] | None = None,
    required_authority_scope: str = "FORMAL_PROMOTED",
) -> ProbabilityEnvelope:
    """Compile the NWP-only P0 vector.

    ``current_runs`` is the activated exact-mask probability ensemble.
    ``diagnostic_runs`` is the same-generation set of all calibrated models
    available as of the decision.  The latter may widen the model envelope but
    must never enter the probability mixture.
    """

    runs = list(
        select_latest_complete_model_run_bundle(
            current_runs,
            decision_at_utc=decision_at_utc,
            required_lead_bucket=ensemble_artifact.lead_bucket,
            required_run_cycle=ensemble_artifact.run_cycle,
            required_settlement_definition=ensemble_artifact.settlement_definition,
        )
    )
    if any(run.station_id != station_id or run.target_local_date != event_date for run in runs):
        raise ContractError("current_model_run_scope_mismatch")
    for run in runs:
        validate_prospective_capture_window(
            target_local_date=event_date,
            target_timezone=settlement_timezone,
            run_initialization_utc=_timestamp_text(_run_init_key(run)),
            decision_at_utc=decision_at_utc,
        )
    if any(not run.is_asof(decision_at_utc) for run in runs):
        raise ContractError("current_model_run_future_leakage")
    first = runs[0]
    if any(
        (run.season_bin, run.lead_bucket, run.run_cycle, run.settlement_definition)
        != (first.season_bin, first.lead_bucket, first.run_cycle, first.settlement_definition)
        for run in runs
    ):
        raise ContractError("current_model_run_scope_mismatch")
    first_run_init = _run_init_key(first)
    if any(_run_init_key(run) != first_run_init for run in runs):
        raise ContractError("current_model_run_generation_mismatch")
    model_run_initialization_utc = _timestamp_text(first_run_init)
    probability_bundle_ready_at_utc = max(
        (_timestamp(run.received_at_utc).astimezone(timezone.utc) for run in runs),
    ).isoformat().replace("+00:00", "Z")
    available_models = tuple(sorted(run.model_id for run in runs))
    if (
        len(available_models) < MIN_FORMAL_ENSEMBLE_MODELS
        or not set(available_models).issubset(set(MODEL_IDS))
    ):
        raise ContractError("p0_available_model_set_invalid")
    validate_exact_model_scope(
        runs,
        expected_models=available_models,
        decision_at_utc=decision_at_utc,
    )
    available_hash = model_set_hash(available_models)
    weights = ensemble_artifact.resolve_weights(available_models)

    diagnostic = list(
        select_latest_complete_model_run_bundle(
            diagnostic_runs,
            decision_at_utc=decision_at_utc,
            required_lead_bucket=ensemble_artifact.lead_bucket,
            required_run_cycle=ensemble_artifact.run_cycle,
            required_settlement_definition=ensemble_artifact.settlement_definition,
        )
    )
    if any(run.station_id != station_id or run.target_local_date != event_date for run in diagnostic):
        raise ContractError("diagnostic_model_run_scope_mismatch")
    for run in diagnostic:
        validate_prospective_capture_window(
            target_local_date=event_date,
            target_timezone=settlement_timezone,
            run_initialization_utc=_timestamp_text(_run_init_key(run)),
            decision_at_utc=decision_at_utc,
        )
    if any(not run.is_asof(decision_at_utc) for run in diagnostic):
        raise ContractError("diagnostic_model_run_future_leakage")
    if len({run.model_id for run in diagnostic}) != len(diagnostic):
        raise ContractError("duplicate_diagnostic_model_run")
    if any(
        (
            run.season_bin,
            run.lead_bucket,
            run.run_cycle,
            run.settlement_definition,
        )
        != (
            first.season_bin,
            first.lead_bucket,
            first.run_cycle,
            first.settlement_definition,
        )
        for run in diagnostic
    ):
        raise ContractError("diagnostic_model_run_scope_mismatch")
    if any(_run_init_key(run) != first_run_init for run in diagnostic):
        raise ContractError("diagnostic_model_run_generation_mismatch")
    diagnostic_models = tuple(sorted(run.model_id for run in diagnostic))
    if (
        not diagnostic_models
        or not set(diagnostic_models).issubset(set(MODEL_IDS))
        or not set(available_models).issubset(set(diagnostic_models))
    ):
        raise ContractError("p0_diagnostic_model_set_invalid")
    diagnostic_hash = model_set_hash(diagnostic_models)
    diagnostic_bundle_ready_at_utc = max(
        (_timestamp(run.received_at_utc).astimezone(timezone.utc) for run in diagnostic),
    ).isoformat().replace("+00:00", "Z")
    bundle_ready_at_utc = max(
        probability_bundle_ready_at_utc,
        diagnostic_bundle_ready_at_utc,
    )
    if (
        ensemble_artifact.station_id,
        ensemble_artifact.season_bin,
        ensemble_artifact.lead_bucket,
        ensemble_artifact.run_cycle,
        ensemble_artifact.settlement_definition,
    ) != (station_id, first.season_bin, first.lead_bucket, first.run_cycle, first.settlement_definition):
        raise ContractError("ensemble_artifact_scope_mismatch")
    validate_formal_ensemble_artifact(ensemble_artifact)
    if required_authority_scope == "FORMAL_PROMOTED" and not ensemble_artifact.promoted:
        raise ContractError("ensemble_artifact_not_active")
    if required_authority_scope == "PRODUCTION_NO_LIVE_ONLY" and ensemble_artifact.promoted:
        raise ContractError("no_live_ensemble_artifact_must_remain_unpromoted")
    if not timestamp_in_window(decision_at_utc, ensemble_artifact.valid_from_utc, ensemble_artifact.valid_until_utc):
        raise ContractError("ensemble_artifact_not_active")

    supplied_activation = (
        None
        if ensemble_activation is None
        else (
            ensemble_activation
            if isinstance(ensemble_activation, CalibrationActivation)
            else CalibrationActivation.from_mapping(ensemble_activation)
        )
    )
    activation_authority = list(calibration_activations)
    if supplied_activation is not None:
        existing_ids = {
            (
                item.activation_id
                if isinstance(item, CalibrationActivation)
                else str(item.get("activation_id") or "")
            )
            for item in activation_authority
        }
        if supplied_activation.activation_id not in existing_ids:
            activation_authority.append(supplied_activation)
    resolved_ensemble_artifact, resolved_ensemble_activation = (
        resolve_active_ensemble_artifact(
            activations=activation_authority,
            artifacts={
                ensemble_artifact.ensemble_artifact_id: ensemble_artifact
            },
            station_id=station_id,
            season_bin=first.season_bin,
            lead_bucket=first.lead_bucket,
            run_cycle=first.run_cycle,
            settlement_definition=first.settlement_definition,
            available_models=available_models,
            decision_at_utc=decision_at_utc,
            required_authority_scope=required_authority_scope,
        )
    )
    if (
        resolved_ensemble_artifact.content_sha256
        != ensemble_artifact.content_sha256
    ):
        raise ContractError("ensemble_artifact_activation_mismatch")
    if supplied_activation is not None:
        if (
            supplied_activation.activation_id
            != resolved_ensemble_activation.activation_id
            or supplied_activation.content_sha256
            != resolved_ensemble_activation.content_sha256
        ):
            raise ContractError("ensemble_activation_not_current_exact_winner")
    if not ensemble_artifact.governance_complete:
        raise ContractError("ensemble_selection_evidence_required")

    artifacts: dict[str, ModelCalibrationArtifact] = {}
    activation_ids: list[str] = [resolved_ensemble_activation.activation_id]
    for run in runs:
        artifact, activation = resolve_active_calibration_artifact(
            activations=calibration_activations,
            artifacts=calibration_artifacts,
            station_id=station_id,
            model_id=run.model_id,
            season_bin=run.season_bin,
            lead_bucket=run.lead_bucket,
            run_cycle=run.run_cycle,
            settlement_definition=run.settlement_definition,
            decision_at_utc=decision_at_utc,
            required_authority_scope=required_authority_scope,
        )
        artifacts[run.model_id] = artifact
        activation_ids.append(activation.activation_id)

    taf_nwp_live_compatibility = (
        build_taf_nwp_live_compatibility_fingerprint(
            ensemble_artifact=ensemble_artifact,
            calibration_artifacts=artifacts,
            available_models=available_models,
        )
    )
    taf_nwp_live_compatibility_json = json.dumps(
        taf_nwp_live_compatibility,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    diagnostic_artifacts: dict[str, ModelCalibrationArtifact] = {}
    diagnostic_activation_ids: list[str] = []
    for run in diagnostic:
        artifact, activation = resolve_active_calibration_artifact(
            activations=calibration_activations,
            artifacts=diagnostic_calibration_artifacts,
            station_id=station_id,
            model_id=run.model_id,
            season_bin=run.season_bin,
            lead_bucket=run.lead_bucket,
            run_cycle=run.run_cycle,
            settlement_definition=run.settlement_definition,
            decision_at_utc=decision_at_utc,
            required_authority_scope=required_authority_scope,
        )
        diagnostic_artifacts[run.model_id] = artifact
        diagnostic_activation_ids.append(activation.activation_id)

    unnormalized: dict[str, float] = {}
    for bucket, (lower, upper) in bucket_intervals.items():
        model_probability = sum(
            float(weights[run.model_id])
            * normal_bucket_probability(
                run.raw_tmax_c + artifacts[run.model_id].bias_c,
                artifacts[run.model_id].residual_scale_c,
                lower,
                upper,
            )
            for run in runs
        )
        unnormalized[str(bucket)] = model_probability
    total = sum(unnormalized.values())
    if not math.isfinite(total) or total <= 0.0:
        raise ContractError("p0_ensemble_probability_zero_mass")
    model_probabilities = {
        bucket: value / total for bucket, value in unnormalized.items()
    }
    calibrated_centers = {
        run.model_id: run.raw_tmax_c + diagnostic_artifacts[run.model_id].bias_c
        for run in diagnostic
    }
    model_envelope_lower_c = min(calibrated_centers.values())
    model_envelope_upper_c = max(calibrated_centers.values())
    model_envelope_span_c = model_envelope_upper_c - model_envelope_lower_c
    ordered_buckets = sorted(
        bucket_intervals,
        key=lambda bucket: (
            -math.inf
            if bucket_intervals[bucket][0] is None
            else float(bucket_intervals[bucket][0]),
            math.inf
            if bucket_intervals[bucket][1] is None
            else float(bucket_intervals[bucket][1]),
            str(bucket),
        ),
    )
    bucket_ordinals = {
        str(bucket): index for index, bucket in enumerate(ordered_buckets)
    }

    def envelope_bucket(value: float) -> str:
        matches = [
            str(bucket)
            for bucket, (lower, upper) in bucket_intervals.items()
            if (lower is None or value >= float(lower))
            and (upper is None or value < float(upper))
        ]
        if len(matches) != 1:
            raise ContractError("model_envelope_bucket_mapping_invalid")
        return matches[0]

    model_envelope_lower_bucket = envelope_bucket(model_envelope_lower_c)
    model_envelope_upper_bucket = envelope_bucket(model_envelope_upper_c)
    model_envelope_lower_ordinal = bucket_ordinals[model_envelope_lower_bucket]
    model_envelope_upper_ordinal = bucket_ordinals[model_envelope_upper_bucket]
    formal_model_members = []
    for run in runs:
        calibrated_center = run.raw_tmax_c + artifacts[run.model_id].bias_c
        formal_model_members.append({
            "model_id": run.model_id,
            "weight": float(weights[run.model_id]),
            "calibrated_center_c": calibrated_center,
            "calibrated_center_bucket": envelope_bucket(calibrated_center),
        })
    formal_model_members.sort(key=lambda item: str(item["model_id"]))
    formal_model_members_json = json.dumps(
        formal_model_members,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    formal_anchor_ordinals = [
        bucket_ordinals[str(item["calibrated_center_bucket"])]
        for item in formal_model_members
        if float(item["weight"]) > 0.0
    ]
    if len(formal_anchor_ordinals) < MIN_FORMAL_ENSEMBLE_MODELS:
        raise ContractError("formal_model_anchor_requires_two_members")
    formal_model_anchor_lower_ordinal = min(formal_anchor_ordinals)
    formal_model_anchor_upper_ordinal = max(formal_anchor_ordinals)
    formal_model_anchor_lower_bucket = ordered_buckets[
        formal_model_anchor_lower_ordinal
    ]
    formal_model_anchor_upper_bucket = ordered_buckets[
        formal_model_anchor_upper_ordinal
    ]
    peripheral_model_reliability_by_bucket = {
        str(bucket): 0.0 for bucket in bucket_intervals
    }
    for run in runs:
        calibrated_center = run.raw_tmax_c + artifacts[run.model_id].bias_c
        bucket = envelope_bucket(calibrated_center)
        peripheral_model_reliability_by_bucket[bucket] = min(
            1.0,
            peripheral_model_reliability_by_bucket[bucket]
            + float(weights[run.model_id]),
        )
    reliability_json = json.dumps(
        peripheral_model_reliability_by_bucket,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    bucket_centers_c = {}
    for bucket, (lower, upper) in bucket_intervals.items():
        if lower is None and upper is None:
            raise ContractError("p0m_unbounded_bucket_interval_invalid")
        if lower is None:
            bucket_centers_c[str(bucket)] = float(upper) - 0.5
        elif upper is None:
            bucket_centers_c[str(bucket)] = float(lower) + 0.5
        else:
            bucket_centers_c[str(bucket)] = (
                float(lower) + float(upper)
            ) / 2.0
    bucket_centers_json = json.dumps(
        bucket_centers_c,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    bucket_intervals_json = json.dumps(
        {
            str(bucket): [
                None if lower is None else float(lower),
                None if upper is None else float(upper),
            ]
            for bucket, (lower, upper) in bucket_intervals.items()
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    proof = (
        compile_p0_historical_zero_tilt_proof(
            station_id=station_id,
            target_local_date=event_date,
            settlement_timezone=settlement_timezone,
            settlement_definition=first.settlement_definition,
            season_bin=first.season_bin,
            lead_bucket=first.lead_bucket,
            run_cycle=first.run_cycle,
            as_of_utc=decision_at_utc,
            model_probabilities=model_probabilities,
            bucket_centers_c=bucket_centers_c,
            ensemble_artifact_sha256=ensemble_artifact.content_sha256,
            ensemble_selection_evidence_sha256=(
                ensemble_artifact.selection_evidence_sha256
            ),
            ensemble_activation_sha256=(
                resolved_ensemble_activation.content_sha256
            ),
        )
        if historical_tilt_proof is None
        else (
            historical_tilt_proof
            if isinstance(historical_tilt_proof, P0HistoricalTiltProof)
            else P0HistoricalTiltProof.from_mapping(historical_tilt_proof)
        )
    )
    expected_model_probability_sha = content_sha256({
        "bucket_centers_c": bucket_centers_c,
        "probabilities": model_probabilities,
    })
    if (
        proof.station_id != station_id
        or proof.target_local_date != event_date
        or proof.settlement_timezone != settlement_timezone
        or proof.settlement_definition != first.settlement_definition
        or proof.season_bin != first.season_bin
        or proof.lead_bucket != first.lead_bucket
        or proof.run_cycle != first.run_cycle
        or proof.as_of_utc != decision_at_utc
        or proof.model_probability_sha256 != expected_model_probability_sha
        or proof.ensemble_artifact_sha256
        != ensemble_artifact.content_sha256
        or proof.ensemble_activation_sha256
        != resolved_ensemble_activation.content_sha256
        or dict(proof.probabilities_before) != model_probabilities
        or dict(proof.bucket_centers_c) != bucket_centers_c
    ):
        raise ContractError("p0_historical_tilt_proof_scope_mismatch")
    probabilities = dict(proof.probabilities_after)
    core = {
        "stage": "P0-M",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "decision_at_utc": decision_at_utc,
        "model_run_initialization_utc": model_run_initialization_utc,
        "model_bundle_ready_at_utc": bundle_ready_at_utc,
        "probability_bundle_ready_at_utc": probability_bundle_ready_at_utc,
        "diagnostic_bundle_ready_at_utc": diagnostic_bundle_ready_at_utc,
        "bucket_ordinals": bucket_ordinals,
        "bucket_centers_c": bucket_centers_c,
        "probabilities": probabilities,
        "current_model_run_ids": [run.model_run_id for run in runs],
        "calibration_artifact_ids": {model: artifact.calibration_artifact_id for model, artifact in artifacts.items()},
        "diagnostic_model_run_ids": [run.model_run_id for run in diagnostic],
        "diagnostic_calibration_artifact_ids": {
            model: artifact.calibration_artifact_id
            for model, artifact in diagnostic_artifacts.items()
        },
        "diagnostic_activation_ids": diagnostic_activation_ids,
        "ensemble_artifact_id": ensemble_artifact.ensemble_artifact_id,
        "activation_ids": activation_ids,
        "available_model_set_hash": available_hash,
        "diagnostic_model_set_hash": diagnostic_hash,
        "selection_evidence_sha256": ensemble_artifact.selection_evidence_sha256,
        "ensemble_artifact_content_sha256": ensemble_artifact.content_sha256,
        "taf_nwp_live_compatibility_fingerprint_sha256": (
            taf_nwp_live_compatibility["fingerprint_sha256"]
        ),
        "model_envelope_lower_c": model_envelope_lower_c,
        "model_envelope_upper_c": model_envelope_upper_c,
        "model_envelope_span_c": model_envelope_span_c,
        "model_envelope_lower_bucket": model_envelope_lower_bucket,
        "model_envelope_upper_bucket": model_envelope_upper_bucket,
        "model_envelope_lower_ordinal": model_envelope_lower_ordinal,
        "model_envelope_upper_ordinal": model_envelope_upper_ordinal,
        "peripheral_model_reliability_by_bucket_json": reliability_json,
        "formal_model_members_json": formal_model_members_json,
        "formal_model_anchor_lower_bucket": formal_model_anchor_lower_bucket,
        "formal_model_anchor_upper_bucket": formal_model_anchor_upper_bucket,
        "formal_model_anchor_lower_ordinal": formal_model_anchor_lower_ordinal,
        "formal_model_anchor_upper_ordinal": formal_model_anchor_upper_ordinal,
        "p0_historical_tilt_proof_sha256": proof.content_sha256,
        "bridge_sha256": bridge_sha256,
    }
    posterior_sha = content_sha256(core)
    return ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0_" + posterior_sha[:32],
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until_utc,
        "bucket_ordinals": bucket_ordinals,
        "probabilities": probabilities,
        "posterior_version": "p0_nwp_historical_meta_anchor_v4",
        "posterior_sha256": posterior_sha,
        "bridge_sha256": bridge_sha256,
        "lineage": {
            "settlement_timezone": settlement_timezone,
            "season_bin": first.season_bin,
            "lead_bucket": first.lead_bucket,
            "run_cycle": first.run_cycle,
            "settlement_definition": first.settlement_definition,
            "model_run_ids": ",".join(sorted(run.model_run_id for run in runs)),
            "calibration_artifact_ids": ",".join(sorted(artifact.calibration_artifact_id for artifact in artifacts.values())),
            "diagnostic_model_run_ids": ",".join(
                sorted(run.model_run_id for run in diagnostic)
            ),
            "diagnostic_calibration_artifact_ids": ",".join(
                sorted(
                    artifact.calibration_artifact_id
                    for artifact in diagnostic_artifacts.values()
                )
            ),
            "diagnostic_activation_ids": ",".join(
                sorted(diagnostic_activation_ids)
            ),
            "ensemble_artifact_id": ensemble_artifact.ensemble_artifact_id,
            "activation_ids": ",".join(sorted(activation_ids)),
            "available_model_set_hash": available_hash,
            "diagnostic_model_set_hash": diagnostic_hash,
            "selection_evidence_sha256": str(ensemble_artifact.selection_evidence_sha256),
            "ensemble_artifact_content_sha256": (
                ensemble_artifact.content_sha256
            ),
            "ensemble_activation_content_sha256": (
                resolved_ensemble_activation.content_sha256
            ),
            "taf_nwp_live_compatibility_fingerprint_sha256": str(
                taf_nwp_live_compatibility["fingerprint_sha256"]
            ),
            "taf_nwp_live_compatibility_json": (
                taf_nwp_live_compatibility_json
            ),
            "model_envelope_lower_c": f"{model_envelope_lower_c:.12g}",
            "model_envelope_upper_c": f"{model_envelope_upper_c:.12g}",
            "model_envelope_span_c": f"{model_envelope_span_c:.12g}",
            "model_envelope_lower_bucket": model_envelope_lower_bucket,
            "model_envelope_upper_bucket": model_envelope_upper_bucket,
            "model_envelope_lower_ordinal": str(model_envelope_lower_ordinal),
            "model_envelope_upper_ordinal": str(model_envelope_upper_ordinal),
            "peripheral_model_reliability_by_bucket_json": reliability_json,
            "formal_model_members_json": formal_model_members_json,
            "formal_model_anchor_lower_bucket": formal_model_anchor_lower_bucket,
            "formal_model_anchor_upper_bucket": formal_model_anchor_upper_bucket,
            "formal_model_anchor_lower_ordinal": str(
                formal_model_anchor_lower_ordinal
            ),
            "formal_model_anchor_upper_ordinal": str(
                formal_model_anchor_upper_ordinal
            ),
            "p0_historical_tilt_proof_id": proof.proof_id,
            "p0_historical_tilt_proof_sha256": proof.content_sha256,
            "p0_historical_tilt_artifact_id": proof.meta_artifact_id,
            "p0_historical_climatology_artifact_id": (
                proof.climatology_artifact_id
            ),
            "p0_historical_applied_theta": f"{proof.applied_theta:.17g}",
            "p0_historical_theta_zero_fallback": (
                "true" if proof.theta_zero_fallback else "false"
            ),
            "bucket_centers_c_json": bucket_centers_json,
            "bucket_intervals_c_json": bucket_intervals_json,
            "model_run_initialization_utc": model_run_initialization_utc,
            "model_bundle_ready_at_utc": bundle_ready_at_utc,
            "probability_bundle_ready_at_utc": probability_bundle_ready_at_utc,
            "diagnostic_bundle_ready_at_utc": diagnostic_bundle_ready_at_utc,
        },
    })


__all__ = [
    "TAF_NWP_LIVE_FINGERPRINT_SCHEMA",
    "TAF_NWP_TRANSPORT_EVIDENCE_SCHEMA",
    "bind_taf_artifact_to_live_nwp_compatibility",
    "build_taf_nwp_live_compatibility_fingerprint",
    "fit_model_calibration_artifact",
    "materialize_p0",
    "normal_bucket_probability",
    "resolve_active_calibration_artifact",
    "select_latest_complete_model_run_bundle",
    "select_newest_compatible_model_run_bundle",
    "validate_exact_model_scope",
    "validate_formal_ensemble_artifact",
]
