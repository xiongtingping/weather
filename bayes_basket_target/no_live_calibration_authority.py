"""Compile explicit production-no-live-only calibration activations.

This is not a promotion path.  It accepts reviewed historical/OOS candidate
evidence, keeps every artifact unpromoted, and emits activations whose
authority can only be consumed when a caller explicitly requests
``PRODUCTION_NO_LIVE_ONLY``.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Mapping

from .contracts import (
    ContractError,
    ModelCalibrationArtifact,
    ModelEnsembleArtifact,
    ModelEnsembleSelectionEvidence,
    MODEL_IDS,
    MIN_FORMAL_ENSEMBLE_MODELS,
    content_sha256,
    model_set_hash,
)
from .calibration import validate_formal_ensemble_artifact
from .model_sources import (
    model_id_for_provider,
    provider_role as canonical_provider_role,
    provider_source_class,
)


SCHEMA = "bayes_basket_target_no_live_calibration_authority_v1"
AUTHORITY = "PRODUCTION_NO_LIVE_ONLY"
FORMAL_TRUTH = "FORMAL_STATION_LANE_TRUTH"
HISTORICAL_AWC_REPLAY = "AWC_HISTORICAL_REPLAY_INGRESS"


def _ensemble_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in raw.items() if key != "content_sha256"}


def _validate_aggregator_lineage(raw_models: list[Mapping[str, Any]]) -> set[str]:
    """Validate model-role lineage and return the model roles in the packet."""

    model_ids: set[str] = set()
    seen_scope: dict[tuple[Any, ...], tuple[str, bool]] = {}
    for raw in raw_models:
        if not isinstance(raw, Mapping):
            raise ContractError("no_live_calibration_model_row_invalid")
        model = str(raw.get("model_id") or "").strip().upper()
        if model not in MODEL_IDS:
            raise ContractError("no_live_calibration_model_id_invalid")
        is_aggregator = bool(raw.get("aggregator_or_republisher"))
        provider_role = str(raw.get("provider_role") or "").strip().lower()
        source_class = str(raw.get("source_class") or "").strip().lower()
        provider_id = str(raw.get("provider_id") or "").strip().lower()
        if is_aggregator:
            if not provider_id:
                raise ContractError("no_live_calibration_aggregator_provider_missing")
            if model_id_for_provider(provider_id) != model:
                raise ContractError("no_live_calibration_model_provider_mismatch")
            if canonical_provider_role(provider_id) != provider_role:
                raise ContractError("no_live_calibration_aggregator_role_missing")
            if provider_source_class(provider_id) != "aggregator_republisher":
                raise ContractError("no_live_calibration_aggregator_provider_invalid")
            if source_class != "aggregator_republisher":
                raise ContractError("no_live_calibration_aggregator_source_class_invalid")
            if bool(raw.get("native_arrival_indexed")):
                raise ContractError("no_live_calibration_aggregator_native_arrival_forbidden")
            if bool(raw.get("prospective_strict_asof_eligible")):
                raise ContractError("no_live_calibration_aggregator_prospective_forbidden")
            if bool(raw.get("historical_arrival_time_observed")) and bool(
                raw.get("prospective_strict_asof_eligible")
            ):
                raise ContractError("no_live_calibration_aggregator_native_arrival_forged")
        elif (
            provider_id in {
                "icon_global_open_meteo",
                "gem_global_open_meteo",
                "cma_grapes_global_open_meteo",
            }
            or source_class == "aggregator_republisher"
        ):
            raise ContractError("no_live_calibration_aggregator_lineage_mismatch")
        model_ids.add(model)
        scope = (
            model,
            str(raw.get("station_id") or ""),
            str(raw.get("target_local_date") or ""),
            str(raw.get("run_init_utc") or raw.get("run_initialization_utc") or ""),
            str(raw.get("lead_bucket") or ""),
            str(raw.get("run_cycle") or ""),
            str(raw.get("settlement_definition") or ""),
        )
        previous = seen_scope.get(scope)
        if previous is not None and previous[1] != is_aggregator:
            # Official/native overlap must already have been resolved upstream;
            # never let this authority silently choose a republisher.
            raise ContractError("no_live_calibration_aggregator_overlap_unresolved")
        seen_scope[scope] = (provider_id, is_aggregator)
    if len(model_ids) < MIN_FORMAL_ENSEMBLE_MODELS:
        raise ContractError("no_live_calibration_formal_model_set_too_small")
    return model_ids


def _validate_exact_oos_scope(
    *,
    item: ModelEnsembleArtifact,
    selection: ModelEnsembleSelectionEvidence,
    raw_oos: list[Any],
    raw_model_rows: list[Mapping[str, Any]],
) -> None:
    """Require OOS rows to describe the same exact mask and scope."""

    if selection.selected_models != item.candidate_models:
        raise ContractError("no_live_ensemble_selection_model_mask_mismatch")
    scoped_oos = [
        row for row in raw_oos
        if isinstance(row, Mapping)
        and str(row.get("station_id") or "") == item.station_id
        and str(row.get("season_bin") or "") == item.season_bin
        and int(row.get("holdout_n") or 0) > 0
    ]
    for model in item.candidate_models:
        model_rows = [row for row in scoped_oos if str(row.get("model_id") or "").upper() == model]
        if not model_rows:
            raise ContractError("no_live_calibration_oos_scope_incomplete")
        for row in model_rows:
            for field, expected in (
                ("lead_bucket", item.lead_bucket),
                ("run_cycle", item.run_cycle),
                ("settlement_definition", item.settlement_definition),
            ):
                if row.get(field) not in (None, "") and str(row[field]) != expected:
                    raise ContractError("no_live_calibration_oos_exact_scope_mismatch")
    scope_models = {
        str(row.get("model_id") or "").upper()
        for row in raw_model_rows
        if str(row.get("station_id") or item.station_id) == item.station_id
        and str(row.get("model_id") or "").upper() in item.candidate_models
        and (not row.get("season_bin") or str(row.get("season_bin")) == item.season_bin)
        and (not row.get("lead_bucket") or str(row.get("lead_bucket")) == item.lead_bucket)
        and (not row.get("run_cycle") or str(row.get("run_cycle")) == item.run_cycle)
        and (not row.get("settlement_definition") or str(row.get("settlement_definition")) == item.settlement_definition)
    }
    if scope_models and not set(item.candidate_models).issubset(scope_models):
        raise ContractError("no_live_calibration_exact_model_mask_incomplete")

    optional_scope_fields = (
        "target_window_start_utc", "target_window_end_utc", "cutoff_utc", "truth_revision",
        "truth_revision_id", "truth_revision_no",
    )
    for field in optional_scope_fields:
        values = {
            str(row.get(field))
            for row in scoped_oos
            if row.get(field) not in (None, "")
        }
        if len(values) > 1:
            raise ContractError("no_live_calibration_oos_exact_scope_mismatch")


def _validate_activation_identity(row: Mapping[str, Any]) -> None:
    content = str(row.get("content_sha256") or "").lower()
    identity = {str(key): value for key, value in row.items() if key != "content_sha256"}
    if content_sha256(identity) != content:
        raise ContractError("no_live_calibration_activation_content_sha256_mismatch")


def _validate_truth_authority_bindings(
    evidence: Mapping[str, Any],
    *,
    artifact_stations: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    checks = evidence.get("checks")
    boundary = evidence.get("boundary")
    if not isinstance(checks, Mapping) or not isinstance(boundary, Mapping):
        raise ContractError("no_live_calibration_truth_authority_missing")
    canonical_only = checks.get("truth_source_canonical_ingress") is True
    temporary_explicit = checks.get(
        "temporary_awc_calibration_label_explicit"
    ) is True
    # Temporary AMSC/AWC labels are retired audit facts.  They cannot be used
    # to construct a new calibration artifact or activation, even in no-live.
    if temporary_explicit:
        raise ContractError("no_live_calibration_temporary_awc_retired")
    if not canonical_only:
        raise ContractError("no_live_calibration_truth_authority_invalid")
    raw_bindings = evidence.get("truth_authority_bindings")
    if not isinstance(raw_bindings, list) or any(
        not isinstance(row, Mapping) for row in raw_bindings
    ):
        raise ContractError(
            "no_live_calibration_truth_authority_bindings_invalid"
        )
    bindings: list[dict[str, Any]] = []
    by_station: dict[str, dict[str, Any]] = {}
    temporary_stations: list[str] = []
    for raw in raw_bindings:
        row = dict(raw)
        supplied = str(row.get("content_sha256") or "").lower()
        identity = {
            str(key): value
            for key, value in row.items()
            if key != "content_sha256"
        }
        if supplied != content_sha256(identity):
            raise ContractError(
                "no_live_calibration_truth_authority_binding_hash_mismatch"
            )
        station = str(row.get("station_id") or "").strip().upper()
        authority = str(row.get("truth_label_authority") or "")
        if (
            not station
            or station in by_station
            or authority != FORMAL_TRUTH
            or str(row.get("source_lane") or "").upper() != "AWC"
            or str(row.get("source_role") or "") != HISTORICAL_AWC_REPLAY
            or str(row.get("authority_scope") or "") != AUTHORITY
            or not str(row.get("settlement_timezone") or "").strip()
            or row.get("reference_only") is not False
            or len(str(row.get("dataset_content_sha256") or "")) != 64
        ):
            raise ContractError(
                "no_live_calibration_truth_authority_binding_scope_invalid"
            )
        by_station[station] = row
        bindings.append(row)
    if artifact_stations != set(by_station):
        raise ContractError(
            "no_live_calibration_truth_authority_binding_incomplete"
        )
    return bindings, sorted(temporary_stations)


def _validate_candidate_score_partition(selection: ModelEnsembleSelectionEvidence) -> None:
    """Ensure the selector ranked only formal 2+/3-model candidates for promotion."""

    # The ranked partition covers every available model, while the selected
    # winner may legitimately be a strict two-model subset.  Building the
    # expected partition from only the winner falsely rejected a complete
    # three-model search whenever a two-model ensemble won.
    candidate_pool = tuple(sorted({
        str(model).strip().upper()
        for row in selection.candidate_scores
        if isinstance(row, Mapping)
        for model in row.get("models", [])
        if str(model).strip()
    }))
    if not set(selection.selected_models) <= set(candidate_pool):
        raise ContractError("no_live_ensemble_selected_models_outside_candidate_pool")
    expected = {
        tuple(sorted(subset))
        for size in range(1, len(candidate_pool) + 1)
        for subset in combinations(candidate_pool, size)
    }
    actual = {
        tuple(sorted(str(model).strip().upper() for model in row.get("models", [])))
        for row in selection.candidate_scores
        if isinstance(row, Mapping)
    }
    if actual != expected:
        raise ContractError("no_live_ensemble_candidate_partition_incomplete")
    for row in selection.candidate_scores:
        models = tuple(sorted(str(model).strip().upper() for model in row.get("models", [])))
        if len(models) == 1:
            if row.get("formal_selection_eligible") is not False or row.get("candidate_role") != "single_model_ablation":
                raise ContractError("no_live_single_model_promotion_forbidden")
        elif row.get("formal_selection_eligible") is not True or row.get("candidate_role") != "formal_ensemble":
            raise ContractError("no_live_formal_candidate_role_invalid")
    if len(selection.selected_models) < MIN_FORMAL_ENSEMBLE_MODELS:
        raise ContractError("no_live_single_model_promotion_forbidden")


def build_no_live_calibration_artifact_packet(
    evidence: Mapping[str, Any],
    *,
    oos_audit: Mapping[str, Any],
    calibration_generation: int,
    ensemble_generation: int,
) -> dict[str, Any]:
    if calibration_generation < 1 or ensemble_generation < 1:
        raise ContractError("no_live_calibration_generation_invalid")
    if not isinstance(evidence, Mapping) or evidence.get("schema") != "bayes_basket_target_calibration_evidence_v1":
        raise ContractError("no_live_calibration_evidence_schema_invalid")
    checks = evidence.get("checks")
    boundary = evidence.get("boundary")
    if not isinstance(checks, Mapping) or not isinstance(boundary, Mapping):
        raise ContractError("no_live_calibration_evidence_checks_missing")
    required_true = (
        "calibration_window_sufficient",
        "candidate_artifact_count_positive",
        "all_candidates_unpromoted",
    )
    missing = [key for key in required_true if checks.get(key) is not True]
    if missing:
        raise ContractError("no_live_calibration_gate_failed:" + ",".join(missing))
    if any(boundary.get(key) is not False for key in (
        "writes_production_state", "calls_order_gateway", "places_orders",
        "live_order_authorized",
    )):
        raise ContractError("no_live_calibration_boundary_invalid")
    if (
        not isinstance(oos_audit, Mapping)
        or oos_audit.get("schema") != "bayes_basket_target_calibration_oos_audit_v1"
    ):
        raise ContractError("no_live_calibration_oos_audit_invalid")
    oos_checks = oos_audit.get("checks")
    oos_input = oos_audit.get("input_artifact")
    oos_boundary = oos_audit.get("boundary")
    if not isinstance(oos_checks, Mapping) or not isinstance(oos_input, Mapping):
        raise ContractError("no_live_calibration_oos_audit_incomplete")
    required_oos_checks = (
        "chronological_holdout_no_leakage",
        "metric_rows_consistent",
        "ensemble_oos_metrics_materialized",
    )
    failed_oos = [key for key in required_oos_checks if oos_checks.get(key) is not True]
    if failed_oos:
        raise ContractError("no_live_calibration_oos_gate_failed:" + ",".join(failed_oos))
    if str(oos_input.get("receipt_sha256") or "") != str(evidence.get("receipt_sha256") or ""):
        raise ContractError("no_live_calibration_oos_input_lineage_mismatch")
    if not isinstance(oos_boundary, Mapping) or any(
        oos_boundary.get(key) is not False
        for key in ("writes_production_db", "calls_order_gateway", "places_orders", "live_order_authorized")
    ):
        raise ContractError("no_live_calibration_oos_boundary_invalid")

    raw_models = evidence.get("calibration_model_rows")
    if not isinstance(raw_models, list) or not raw_models:
        raise ContractError("no_live_calibration_model_rows_missing")
    available_model_ids = _validate_aggregator_lineage(raw_models)

    raw_artifacts = evidence.get("calibration_artifacts")
    raw_ensembles = evidence.get("ensemble_artifacts")
    raw_selection = evidence.get("ensemble_selection_evidence")
    raw_oos = evidence.get("oos_reports")
    if not isinstance(raw_artifacts, list) or not isinstance(raw_ensembles, list) or not isinstance(raw_selection, list) or not isinstance(raw_oos, list):
        raise ContractError("no_live_calibration_candidate_lists_missing")
    truth_authority_bindings, temporary_awc_stations = (
        _validate_truth_authority_bindings(
            evidence,
            artifact_stations={
                str(row.get("station_id") or "").strip().upper()
                for row in raw_artifacts
                if isinstance(row, Mapping)
            },
        )
    )
    artifacts = [ModelCalibrationArtifact.from_mapping(row) for row in raw_artifacts]
    selection_evidence = [
        ModelEnsembleSelectionEvidence.from_mapping(row) for row in raw_selection
    ]
    for raw in raw_ensembles:
        if not isinstance(raw, Mapping):
            raise ContractError("no_live_calibration_ensemble_row_invalid")
        if content_sha256(_ensemble_identity(raw)) != str(raw.get("content_sha256") or "").lower():
            raise ContractError("no_live_ensemble_artifact_content_sha256_mismatch")
    ensembles = [ModelEnsembleArtifact.from_mapping(row) for row in raw_ensembles]
    if not artifacts or not ensembles or any(item.promoted for item in (*artifacts, *ensembles)):
        raise ContractError("no_live_calibration_candidates_must_be_unpromoted")
    oos_scopes = {
        (str(row.get("station_id") or ""), str(row.get("model_id") or "").upper(), str(row.get("season_bin") or ""))
        for row in raw_oos if isinstance(row, Mapping) and int(row.get("holdout_n") or 0) > 0
    }
    artifact_scopes = {(item.station_id, item.model_id, item.season_bin) for item in artifacts}
    if not artifact_scopes.issubset(oos_scopes):
        raise ContractError("no_live_calibration_oos_scope_incomplete")
    selection_by_sha = {item.content_sha256: item for item in selection_evidence}
    audited_rows_by_selection_sha = {
        str(row.get("selection_evidence_sha256") or ""): row
        for row in (oos_audit.get("ensemble_report_rows") or [])
        if isinstance(row, Mapping)
    }
    eligible_ensembles: list[ModelEnsembleArtifact] = []
    for item in ensembles:
        validate_formal_ensemble_artifact(item)
        if not set(item.candidate_models).issubset(available_model_ids):
            raise ContractError("no_live_calibration_exact_model_mask_incomplete")
        selected = selection_by_sha.get(str(item.selection_evidence_sha256))
        if selected is None:
            raise ContractError("no_live_ensemble_selection_evidence_missing")
        _validate_candidate_score_partition(selected)
        audited_row = audited_rows_by_selection_sha.get(selected.content_sha256)
        if audited_row is None:
            raise ContractError("no_live_ensemble_selection_not_oos_audited")
        if (
            selected.station_id != item.station_id
            or selected.season_bin != item.season_bin
            or selected.lead_bucket != item.lead_bucket
            or selected.run_cycle != item.run_cycle
            or selected.settlement_definition != item.settlement_definition
            or selected.selected_models != item.candidate_models
            or dict(selected.selected_weights) != dict(
                item.supported_masks[model_set_hash(item.candidate_models)]
            )
            or selected.previous_ensemble_artifact_sha256
            != item.previous_ensemble_artifact_sha256
            or selected.max_weight_delta != item.max_weight_delta
            or dict(selected.oos_metrics) != dict(item.oos_metrics)
            or float(selected.effective_sample_n) != float(item.effective_sample_n or 0.0)
            or dict(selected.residual_covariance) != dict(item.residual_covariance)
        ):
            raise ContractError("no_live_ensemble_selection_evidence_mismatch")
        _validate_exact_oos_scope(
            item=item,
            selection=selected,
            raw_oos=raw_oos,
            raw_model_rows=raw_models,
        )
        if (
            audited_row.get("probability_non_degradation_vs_uniform") is not True
        ):
            continue
        for metric in ("brier_score", "nll", "crps"):
            if metric in audited_row and abs(
                float(audited_row[metric]) - float(item.oos_metrics[metric])
            ) > 1e-12:
                raise ContractError("no_live_ensemble_oos_metric_mismatch")
        eligible_ensembles.append(item)
    if not eligible_ensembles:
        raise ContractError("no_live_calibration_no_scope_passed_probability_oos")
    eligible_scope_models = {
        (
            item.station_id, item.season_bin, item.lead_bucket, item.run_cycle,
            item.settlement_definition, model,
        )
        for item in eligible_ensembles
        for model in item.candidate_models
    }
    artifacts = [
        item for item in artifacts
        if (
            item.station_id, item.season_bin, item.lead_bucket, item.run_cycle,
            item.settlement_definition, item.model_id,
        ) in eligible_scope_models
    ]
    ensembles = eligible_ensembles
    eligible_selection_shas = {
        str(item.selection_evidence_sha256) for item in eligible_ensembles
    }
    selection_evidence = [
        item for item in selection_evidence
        if item.content_sha256 in eligible_selection_shas
    ]

    activated_at = str(evidence.get("as_of_utc") or "").strip()
    if not activated_at:
        raise ContractError("no_live_calibration_activated_at_missing")
    activations: list[dict[str, Any]] = []
    for item in artifacts:
        identity = {
            "artifact_kind": "MODEL_CALIBRATION",
            "artifact_id": item.calibration_artifact_id,
            "station_id": item.station_id,
            "model_id_or_star": item.model_id,
            "season_bin": item.season_bin,
            "lead_bucket": item.lead_bucket,
            "run_cycle": item.run_cycle,
            "settlement_definition": item.settlement_definition,
            "available_model_set_hash_or_star": "*",
            "generation": calibration_generation,
            "valid_from_utc": item.valid_from_utc,
            "valid_until_utc": item.valid_until_utc,
            "activated_at_utc": activated_at,
            "authority_scope": AUTHORITY,
        }
        activation_id = "no-live-cal-" + content_sha256(identity)[:32]
        activations.append({
            "activation_id": activation_id,
            **identity,
            "content_sha256": content_sha256({"activation_id": activation_id, **identity}),
        })
    for item in ensembles:
        candidate_models = set(item.candidate_models)
        if not item.supported_masks:
            raise ContractError("no_live_ensemble_supported_masks_missing")
        for available_hash, weights in sorted(item.supported_masks.items()):
            models = set(weights)
            if not models or not models.issubset(candidate_models):
                raise ContractError("no_live_ensemble_supported_mask_invalid")
            if model_set_hash(tuple(sorted(models))) != available_hash:
                raise ContractError("no_live_ensemble_supported_mask_hash_mismatch")
            identity = {
                "artifact_kind": "MODEL_ENSEMBLE",
                "artifact_id": item.ensemble_artifact_id,
                "station_id": item.station_id,
                "model_id_or_star": "*",
                "season_bin": item.season_bin,
                "lead_bucket": item.lead_bucket,
                "run_cycle": item.run_cycle,
                "settlement_definition": item.settlement_definition,
                "available_model_set_hash_or_star": available_hash,
                "generation": ensemble_generation,
                "valid_from_utc": item.valid_from_utc,
                "valid_until_utc": item.valid_until_utc,
                "activated_at_utc": activated_at,
                "authority_scope": AUTHORITY,
            }
            activation_id = "no-live-ens-" + content_sha256(identity)[:32]
            activations.append({
                "activation_id": activation_id,
                **identity,
                "content_sha256": content_sha256({"activation_id": activation_id, **identity}),
            })
    activation_keys: set[tuple[Any, ...]] = set()
    for row in activations:
        _validate_activation_identity(row)
        key = (
            row["artifact_kind"],
            row["station_id"],
            row["model_id_or_star"],
            row["season_bin"],
            row["lead_bucket"],
            row["run_cycle"],
            row["settlement_definition"],
            row["available_model_set_hash_or_star"],
            row["generation"],
        )
        if key in activation_keys:
            raise ContractError("no_live_calibration_activation_duplicate")
        activation_keys.add(key)
    return {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "calibration_artifacts": [
            row for row in raw_artifacts
            if any(
                item.calibration_artifact_id
                == str(row.get("calibration_artifact_id") or "")
                for item in artifacts
            )
        ],
        "ensemble_selection_evidence": [
            row for row in raw_selection
            if str(row.get("content_sha256") or "") in eligible_selection_shas
        ],
        "ensemble_artifacts": [
            row for row in raw_ensembles
            if any(
                item.ensemble_artifact_id
                == str(row.get("ensemble_artifact_id") or "")
                for item in ensembles
            )
        ],
        "calibration_activations": activations,
        "calibration_promotions": [],
        "truth_authority_bindings": truth_authority_bindings,
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [],
        "cash_phase_policies": [],
        "policy_promotions": [],
        "authority_receipt": {
            "schema": SCHEMA,
            "authority_scope": AUTHORITY,
            "evidence_sha256": str(evidence.get("evidence_sha256") or ""),
            "oos_evidence_sha256": str(oos_audit.get("evidence_sha256") or ""),
            "artifact_count": len(artifacts),
            "ensemble_count": len(ensembles),
            "rejected_ensemble_scope_count": len(raw_ensembles) - len(ensembles),
            "activation_count": len(activations),
            "temporary_awc_calibration_station_count": len(
                temporary_awc_stations
            ),
            "temporary_awc_calibration_station_ids":
                temporary_awc_stations,
            "temporary_awc_realtime_truth_authorized": False,
            "temporary_awc_p1_authorized": False,
            "temporary_awc_hard_no_authorized": False,
            "formal_promotion_created": False,
            "live_order_authorized": False,
        },
    }


__all__ = ["AUTHORITY", "SCHEMA", "build_no_live_calibration_artifact_packet"]
