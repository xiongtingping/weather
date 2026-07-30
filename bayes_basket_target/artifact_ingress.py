"""Append-only ingress for governed target-lane artifacts.

The packet is produced offline by a reviewed calibration/policy job.  This
module only validates contracts and appends rows; it never trains, reads the
legacy posterior tables, changes activation files, or touches execution data.

Unpromoted station/cash artifacts are accepted only under the explicit
``PRODUCTION_NO_LIVE_CANDIDATE`` authority. That scope is evidence collection
only and cannot include a promotion receipt or live/gateway authority.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping, Sequence

from .cash_phase import BasketCashPhasePolicyArtifact
from .cash_phase_authority import (
    CANDIDATE_AUTHORITY_SCOPE,
    FORMAL_AUTHORITY_SCOPE,
    CashPhaseAuthorityManifest,
)
from .contracts import (
    CalibrationActivation,
    ContractError,
    ModelCalibrationArtifact,
    ModelEnsembleArtifact,
    ModelEnsembleSelectionEvidence,
)
from .policy import GlobalBasketPriorArtifact, PolicyArtifact, StationBasketPolicyActivation
from .resource_bounds import assert_connection_target_capacity
from .ssot import production_ssot_schema_status


SCHEMA = "bayes_basket_target_artifact_ingress_v1"
CONFIRMATION = "INGEST_BAYES_BASKET_TARGET_ARTIFACTS"
CALIBRATION_ACTIVATION_COLUMNS = (
    "activation_id",
    "artifact_kind",
    "artifact_id",
    "station_id",
    "model_id_or_star",
    "season_bin",
    "lead_bucket",
    "run_cycle",
    "settlement_definition",
    "available_model_set_hash_or_star",
    "generation",
    "valid_from_utc",
    "valid_until_utc",
    "activated_at_utc",
    "authority_scope",
    "content_sha256",
)
CALIBRATION_ACTIVATION_SCOPE_COLUMNS = (
    "artifact_kind",
    "station_id",
    "model_id_or_star",
    "season_bin",
    "lead_bucket",
    "run_cycle",
    "available_model_set_hash_or_star",
)


def _sha(value: Any, name: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ContractError(f"{name}_invalid")
    return text


def _rows(packet: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = packet.get(key, [])
    if not isinstance(value, list):
        raise ContractError(f"{key}_list_required")
    if any(not isinstance(item, Mapping) for item in value):
        raise ContractError(f"{key}_row_invalid")
    return [dict(item) for item in value]


def _insert_once(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: Sequence[str],
    values: Sequence[Any],
    primary_key: str,
    primary_value: Any,
) -> bool:
    existing = conn.execute(
        f"SELECT {','.join(columns)} FROM {table} WHERE {primary_key}=?",
        (primary_value,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != tuple(values):
            raise ContractError(f"{table}_content_conflict")
        return False
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(values),
    )
    return True


def _insert_calibration_activation_once(
    conn: sqlite3.Connection,
    item: CalibrationActivation,
) -> bool:
    """Append one activation with canonical replay and monotonic generations.

    ``activation_id`` is the immutable replay identity.  A changed activation
    in the same database-enforced business scope must use a generation that is
    strictly greater than every existing generation in that scope.  This
    converts an otherwise opaque SQLite UNIQUE failure into a fail-closed
    domain contract and prevents packet order from rewinding an activation.
    """

    columns = CALIBRATION_ACTIVATION_COLUMNS
    values = tuple(getattr(item, key) for key in columns)
    existing = conn.execute(
        f"SELECT {','.join(columns)} FROM calibration_activation "
        "WHERE activation_id=?",
        (item.activation_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise ContractError("calibration_activation_content_conflict")
        return False

    scope_values = tuple(
        getattr(item, key) for key in CALIBRATION_ACTIVATION_SCOPE_COLUMNS
    )
    predicates = " AND ".join(
        f"{key}=?" for key in CALIBRATION_ACTIVATION_SCOPE_COLUMNS
    )
    max_row = conn.execute(
        "SELECT MAX(generation) FROM calibration_activation "
        f"WHERE {predicates}",
        scope_values,
    ).fetchone()
    max_generation = int(max_row[0] or 0)
    if item.generation <= max_generation:
        raise ContractError(
            "calibration_activation_generation_not_unique_highest"
        )

    conn.execute(
        "INSERT INTO calibration_activation"
        f"({','.join(columns)}) VALUES"
        f"({','.join('?' for _ in columns)})",
        values,
    )
    return True


def _promotion_rows(packet: Mapping[str, Any], key: str, *, table: str, primary_key: str) -> list[dict[str, Any]]:
    rows = _rows(packet, key)
    for row in rows:
        _sha(row.get("receipt_sha256"), f"{key}_receipt_sha256")
        decision = str(row.get("decision") or "").upper()
        if decision not in {"PROMOTED", "REJECTED", "SUPERSEDED"}:
            raise ContractError(f"{key}_decision_invalid")
        if int(row.get("decision_seq", 0)) < 1:
            raise ContractError(f"{key}_decision_seq_invalid")
        if not str(row.get(primary_key) or "").strip():
            raise ContractError(f"{key}_{primary_key}_missing")
        row["_table"] = table
    return rows


def _validate_cash_phase_authority_binding(
    packet: Mapping[str, Any],
    cash_policies: Sequence[BasketCashPhasePolicyArtifact],
    cash_manifests: Sequence[CashPhaseAuthorityManifest],
    *,
    candidate_bundle: bool,
) -> None:
    """Require one exact authority manifest for every cash policy.

    Candidate and formal scopes use the same content-addressed binding rule.
    Promotion status changes the required scope; it never makes the authority
    manifest optional or creates a compatibility ingress path.
    """

    has_manifest_field = "cash_phase_authority_manifests" in packet
    if cash_policies and not has_manifest_field:
        raise ContractError("cash_phase_authority_manifests_required")
    if not has_manifest_field:
        return
    if len(cash_manifests) != len(cash_policies):
        raise ContractError("cash_phase_authority_manifest_count_mismatch")

    policy_by_version: dict[str, BasketCashPhasePolicyArtifact] = {}
    for policy in cash_policies:
        if policy.policy_version in policy_by_version:
            raise ContractError("cash_phase_authority_policy_version_duplicate")
        policy_by_version[policy.policy_version] = policy

    manifest_by_version: dict[str, CashPhaseAuthorityManifest] = {}
    for manifest in cash_manifests:
        if manifest.policy_version in manifest_by_version:
            raise ContractError("cash_phase_authority_manifest_policy_version_duplicate")
        manifest_by_version[manifest.policy_version] = manifest

    expected_scope = (
        CANDIDATE_AUTHORITY_SCOPE if candidate_bundle else FORMAL_AUTHORITY_SCOPE
    )
    if cash_policies and expected_scope == FORMAL_AUTHORITY_SCOPE:
        raise ContractError("cash_phase_formal_oos_authority_schema_missing")
    if set(policy_by_version) != set(manifest_by_version):
        raise ContractError("cash_phase_authority_manifest_policy_set_mismatch")
    for policy_version, policy in policy_by_version.items():
        manifest = manifest_by_version[policy_version]
        if manifest.authority_scope != expected_scope:
            raise ContractError("cash_phase_authority_manifest_scope_mismatch")
        if manifest.station_id != policy.station_id:
            raise ContractError("cash_phase_authority_manifest_station_mismatch")
        if manifest.artifact_sha256 != policy.content_sha256:
            raise ContractError("cash_phase_authority_manifest_artifact_mismatch")
        if manifest.station_timezone != policy.station_timezone:
            raise ContractError("cash_phase_authority_manifest_timezone_mismatch")
        if (
            manifest.valid_from_utc != policy.valid_from_utc
            or manifest.valid_until_utc != policy.valid_until_utc
        ):
            raise ContractError("cash_phase_authority_manifest_validity_mismatch")


def ingest_artifact_bundle(
    conn: sqlite3.Connection,
    packet: Mapping[str, Any],
    *,
    commit: bool = False,
    finalize_transaction: bool = True,
) -> dict[str, Any]:
    if not commit and not finalize_transaction:
        raise ContractError(
            "artifact_ingress_uncommitted_finalize_mode_invalid"
        )
    if not isinstance(packet, Mapping) or packet.get("schema") != SCHEMA:
        raise ContractError("artifact_ingress_schema_invalid")
    status = production_ssot_schema_status(conn)
    if not status["ok"]:
        raise ContractError("production_ssot_schema_incomplete")
    if commit:
        assert_connection_target_capacity(conn)

    calibration = [ModelCalibrationArtifact.from_mapping(row) for row in _rows(packet, "calibration_artifacts")]
    selection_evidence = [
        ModelEnsembleSelectionEvidence.from_mapping(row)
        for row in _rows(packet, "ensemble_selection_evidence")
    ]
    ensembles = [ModelEnsembleArtifact.from_mapping(row) for row in _rows(packet, "ensemble_artifacts")]
    selection_by_sha = {item.content_sha256: item for item in selection_evidence}
    for item in ensembles:
        if item.governance_complete:
            evidence = selection_by_sha.get(str(item.selection_evidence_sha256))
            if evidence is None:
                raise ContractError("ensemble_selection_evidence_not_in_bundle")
            if (
                evidence.station_id != item.station_id
                or evidence.season_bin != item.season_bin
                or evidence.lead_bucket != item.lead_bucket
                or evidence.run_cycle != item.run_cycle
                or evidence.settlement_definition != item.settlement_definition
                or evidence.selected_models != item.candidate_models
            ):
                raise ContractError("ensemble_selection_evidence_scope_mismatch")
    activations = [CalibrationActivation.from_mapping(row) for row in _rows(packet, "calibration_activations")]
    priors = [GlobalBasketPriorArtifact.from_mapping(row) for row in _rows(packet, "global_priors")]
    policies = [PolicyArtifact.from_mapping(row) for row in _rows(packet, "policy_artifacts")]
    policy_activations = [StationBasketPolicyActivation.from_mapping(row) for row in _rows(packet, "policy_activations")]
    cash_policies = [BasketCashPhasePolicyArtifact.from_mapping(row) for row in _rows(packet, "cash_phase_policies")]
    cash_manifests = [
        CashPhaseAuthorityManifest.from_mapping(row)
        for row in _rows(packet, "cash_phase_authority_manifests")
    ]
    calibration_promotions = _promotion_rows(packet, "calibration_promotions", table="calibration_artifact_promotion", primary_key="promotion_decision_id")
    policy_promotions = _promotion_rows(packet, "policy_promotions", table="basket_policy_promotion", primary_key="promotion_id")
    contains_unpromoted_policy = any(
        not item.promoted
        for item in (*priors, *policies, *policy_activations, *cash_policies)
    )
    if contains_unpromoted_policy:
        if str(packet.get("policy_authority_scope") or "").strip().upper() != CANDIDATE_AUTHORITY_SCOPE:
            raise ContractError("candidate_policy_authority_scope_required")
        if packet.get("execution_mode") != "production_no_live":
            raise ContractError("candidate_policy_execution_mode_invalid")
        for key in (
            "live_order_authorized",
            "live_submit_authorized",
            "places_orders",
            "calls_order_gateway",
        ):
            if packet.get(key) is not False:
                raise ContractError(f"candidate_policy_{key}_must_be_false")
        if policy_promotions:
            raise ContractError("candidate_policy_promotion_receipts_forbidden")

    _validate_cash_phase_authority_binding(
        packet,
        cash_policies,
        cash_manifests,
        candidate_bundle=any(not item.promoted for item in cash_policies),
    )

    savepoint = "basket_target_artifact_ingress"
    conn.execute(f"SAVEPOINT {savepoint}")
    counts = {"calibration_artifacts": 0, "ensemble_selection_evidence": 0, "ensemble_artifacts": 0, "calibration_activations": 0, "global_priors": 0, "policy_artifacts": 0, "policy_activations": 0, "cash_phase_policies": 0, "cash_phase_authority_manifests": 0, "calibration_promotions": 0, "policy_promotions": 0}
    try:
        for item in calibration:
            values = (
                item.calibration_artifact_id, item.station_id, item.model_id, item.season_bin,
                item.lead_bucket, item.run_cycle, item.settlement_definition, item.trained_through_utc,
                item.sample_n, item.effective_sample_n, item.bias_c, item.residual_scale_c,
                json.dumps(dict(item.tail_parameters), sort_keys=True), item.valid_from_utc,
                item.valid_until_utc, item.content_sha256,
            )
            counts["calibration_artifacts"] += _insert_once(conn, table="calibration_model_artifact", primary_key="calibration_artifact_id", primary_value=item.calibration_artifact_id, columns=("calibration_artifact_id","station_id","model_id","season_bin","lead_bucket","run_cycle","settlement_definition","trained_through_utc","sample_n","effective_sample_n","bias_c","residual_scale_c","tail_parameters_json","valid_from_utc","valid_until_utc","content_sha256"), values=values)
        for item in selection_evidence:
            columns = (
                "selection_evidence_id","station_id","season_bin","lead_bucket",
                "run_cycle","settlement_definition","method_version",
                "train_start_date","train_end_date","recent_start_date",
                "recent_end_date","holdout_start_date","holdout_end_date",
                "candidate_scores_json","selected_models_json",
                "selected_weights_json","oos_metrics_json",
                "residual_covariance_json","residual_correlation_json",
                "effective_sample_n","previous_ensemble_artifact_sha256",
                "max_weight_delta","actual_max_weight_delta","content_sha256",
            )
            values = (
                item.selection_evidence_id,item.station_id,item.season_bin,
                item.lead_bucket,item.run_cycle,item.settlement_definition,
                item.method_version,item.train_start_date,item.train_end_date,
                item.recent_start_date,item.recent_end_date,
                item.holdout_start_date,item.holdout_end_date,
                json.dumps(list(item.candidate_scores),sort_keys=True),
                json.dumps(list(item.selected_models),sort_keys=True),
                json.dumps(dict(item.selected_weights),sort_keys=True),
                json.dumps(dict(item.oos_metrics),sort_keys=True),
                json.dumps(dict(item.residual_covariance),sort_keys=True),
                json.dumps(dict(item.residual_correlation),sort_keys=True),
                item.effective_sample_n,item.previous_ensemble_artifact_sha256,
                item.max_weight_delta,item.actual_max_weight_delta,
                item.content_sha256,
            )
            counts["ensemble_selection_evidence"] += _insert_once(
                conn, table="calibration_ensemble_selection_evidence",
                primary_key="selection_evidence_id",
                primary_value=item.selection_evidence_id,
                columns=columns, values=values,
            )
        for item in ensembles:
            values = (item.ensemble_artifact_id,item.station_id,item.season_bin,item.lead_bucket,item.run_cycle,item.settlement_definition,json.dumps(list(item.candidate_models),sort_keys=True),json.dumps(dict(item.supported_masks),sort_keys=True),json.dumps(dict(item.residual_covariance),sort_keys=True),item.correlation_penalty,item.selection_evidence_sha256,item.previous_ensemble_artifact_sha256,item.weight_method,item.effective_sample_n,item.max_weight_delta,item.train_start_date,item.train_end_date,item.recent_start_date,item.recent_end_date,item.holdout_start_date,item.holdout_end_date,json.dumps(dict(item.oos_metrics),sort_keys=True) if item.oos_metrics else None,item.valid_from_utc,item.valid_until_utc,item.content_sha256)
            counts["ensemble_artifacts"] += _insert_once(conn, table="calibration_ensemble_artifact", primary_key="ensemble_artifact_id", primary_value=item.ensemble_artifact_id, columns=("ensemble_artifact_id","station_id","season_bin","lead_bucket","run_cycle","settlement_definition","candidate_models_json","supported_masks_json","residual_covariance_json","correlation_penalty","selection_evidence_sha256","previous_ensemble_artifact_sha256","weight_method","effective_sample_n","max_weight_delta","train_start_date","train_end_date","recent_start_date","recent_end_date","holdout_start_date","holdout_end_date","oos_metrics_json","valid_from_utc","valid_until_utc","content_sha256"), values=values)
        for item in activations:
            counts["calibration_activations"] += (
                _insert_calibration_activation_once(conn, item)
            )
        for item in priors:
            values = (item.global_prior_id,item.strategy_version,json.dumps(dict(item.family_parameters),sort_keys=True),json.dumps(dict(item.default_policy),sort_keys=True),item.valid_from_utc,item.valid_until_utc,item.content_sha256)
            counts["global_priors"] += _insert_once(conn, table="basket_global_prior_artifact", primary_key="global_prior_id", primary_value=item.global_prior_id, columns=("global_prior_id","strategy_version","family_parameters_json","default_policy_json","valid_from_utc","valid_until_utc","content_sha256"), values=values)
        for item in policies:
            values = (item.artifact_id,item.family,item.station_id,item.stage,item.season_bin,item.maturity_bin,item.liquidity_bin,item.strategy_version,json.dumps(dict(item.theta_local),sort_keys=True),item.effective_sample_n,item.kappa,item.trained_through_utc,item.valid_from_utc,item.valid_until_utc,item.content_sha256)
            counts["policy_artifacts"] += _insert_once(conn, table="basket_station_policy_artifact", primary_key="artifact_id", primary_value=item.artifact_id, columns=("artifact_id","family","station_id","stage","season_bin","maturity_bin","liquidity_bin","strategy_version","theta_local_json","effective_sample_n","kappa","trained_through_utc","valid_from_utc","valid_until_utc","content_sha256"), values=values)
        for item in policy_activations:
            values = (item.activation_id,item.station_id,item.stage,item.season_bin,item.maturity_bin,item.liquidity_bin,item.strategy_version,item.global_prior_id,item.target_artifact_id,item.reserve_artifact_id,item.cadence_artifact_id,json.dumps(list(item.fallback_families),sort_keys=True),item.generation,item.valid_from_utc,item.valid_until_utc,item.activated_at_utc,item.content_sha256)
            counts["policy_activations"] += _insert_once(conn, table="basket_policy_activation", primary_key="activation_id", primary_value=item.activation_id, columns=("activation_id","station_id","stage","season_bin","maturity_bin","liquidity_bin","strategy_version","global_prior_id","target_artifact_id","reserve_artifact_id","cadence_artifact_id","fallback_families_json","generation","valid_from_utc","valid_until_utc","activated_at_utc","content_sha256"), values=values)
        for item in cash_policies:
            values = (item.policy_version,item.station_id,item.station_timezone,item.heavy_to_standard_local_time,item.standard_to_tail_local_time,json.dumps(dict(item.cash_by_phase),sort_keys=True),item.valid_from_utc,item.valid_until_utc,item.evidence_status,int(item.promoted),item.content_sha256)
            counts["cash_phase_policies"] += _insert_once(conn, table="basket_cash_phase_policy_artifact", primary_key="policy_version", primary_value=item.policy_version, columns=("policy_version","station_id","station_timezone","heavy_to_standard_local_time","standard_to_tail_local_time","cash_by_phase_json","valid_from_utc","valid_until_utc","evidence_status","promoted","content_sha256"), values=values)
        for item in cash_manifests:
            values = (
                item.manifest_sha256,
                item.station_id,
                item.policy_version,
                item.artifact_sha256,
                item.station_timezone,
                item.valid_from_utc,
                item.valid_until_utc,
                item.authority_scope,
            )
            counts["cash_phase_authority_manifests"] += _insert_once(
                conn,
                table="basket_cash_phase_authority_manifest",
                primary_key="manifest_sha256",
                primary_value=item.manifest_sha256,
                columns=(
                    "manifest_sha256",
                    "station_id",
                    "policy_version",
                    "artifact_sha256",
                    "station_timezone",
                    "valid_from_utc",
                    "valid_until_utc",
                    "authority_scope",
                ),
                values=values,
            )
        for item in calibration_promotions:
            values = (item["promotion_decision_id"],item["artifact_kind"],item["artifact_id"],int(item["decision_seq"]),str(item["decision"]).upper(),item["gate_version"],item["decided_at_utc"],item["receipt_sha256"],item.get("content_sha256",item["receipt_sha256"]))
            counts["calibration_promotions"] += _insert_once(conn, table="calibration_artifact_promotion", primary_key="promotion_decision_id", primary_value=item["promotion_decision_id"], columns=("promotion_decision_id","artifact_kind","artifact_id","decision_seq","decision","gate_version","decided_at_utc","receipt_sha256","content_sha256"), values=values)
        for item in policy_promotions:
            values = (item["promotion_id"],item["artifact_kind"],item["artifact_id"],int(item["decision_seq"]),str(item["decision"]).upper(),item["gate_version"],item["decided_at_utc"],item["receipt_sha256"])
            counts["policy_promotions"] += _insert_once(conn, table="basket_policy_promotion", primary_key="promotion_id", primary_value=item["promotion_id"], columns=("promotion_id","artifact_kind","artifact_id","decision_seq","decision","gate_version","decided_at_utc","receipt_sha256"), values=values)
        if commit:
            conn.execute(f"RELEASE {savepoint}")
            if finalize_transaction:
                conn.commit()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    state = (
        "COMMITTED"
        if commit and finalize_transaction
        else "STAGED_IN_CALLER_TRANSACTION"
        if commit
        else "VALIDATED_NO_WRITE"
    )
    return {
        "schema": SCHEMA,
        "state": state,
        "counts": counts,
        "places_orders": False,
        "calls_order_gateway": False,
    }


__all__ = ["CONFIRMATION", "SCHEMA", "ingest_artifact_bundle"]
