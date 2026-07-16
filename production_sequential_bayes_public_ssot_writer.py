"""Production-only atomic writer for the sequential Bayes public SSOT.

This is deliberately separate from the shadow writer.  It accepts already
computed, frozen Transaction-B rows and performs no inference, market read,
order signing, or CLOB call.  Its only job is the all-or-nothing durable
commit that makes the posterior public and, when a market projection exists,
hands the exact committed posterior to the execution outbox.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from production_probability_activation_contract import (
    SCHEMA_VERSION as ACTIVATION_SCHEMA,
    ProductionProbabilityActivationError,
    validate_production_probability_activation,
)


SCHEMA_VERSION = "production_sequential_bayes_public_ssot_writer_v1"


class ProductionTransactionConflict(RuntimeError):
    """The frozen predecessor is no longer the latest committed checkpoint."""


class ProductionTransactionIntegrityError(RuntimeError):
    """The frozen Transaction-B bundle or database boundary is invalid."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _parse_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_production_boundary(conn: sqlite3.Connection, activation: Mapping[str, Any]) -> dict[str, Any]:
    if conn.in_transaction:
        raise ProductionTransactionIntegrityError("production_transaction_b_requires_no_ambient_transaction")
    if _table_exists(conn, "v2_shadow_runtime_manifest"):
        raise ProductionTransactionIntegrityError("production_transaction_b_refuses_shadow_database")
    required_tables = (
        "strong_observation_updates", "posterior_checkpoints", "realization_states",
        "posterior_commit_outbox", "posterior_outbox_execution_contracts",
        "event_fact_history", "event_fact_latest", "production_initial_posterior_lineage",
    )
    missing = [name for name in required_tables if not _table_exists(conn, name)]
    if missing:
        raise ProductionTransactionIntegrityError("production_transaction_b_schema_missing:" + ",".join(missing))
    try:
        return validate_production_probability_activation(activation)
    except ProductionProbabilityActivationError as exc:
        raise ProductionTransactionIntegrityError("production_transaction_b_activation_not_authorized:" + str(exc)) from exc


def _runtime_dependency_identity_from_fact_json(value: Any) -> str:
    ssot = _mapping(_parse_json(value).get("posterior_ssot_fact"))
    lineage = _mapping(ssot.get("runtime_dependency_lineage"))
    return _text(lineage.get("resident_fast_lane_prewarm_dependency_identity"))


def materialize_production_public_posterior_fact_row(
    *, activation: Mapping[str, Any], strong_update_row: Mapping[str, Any], checkpoint_row: Mapping[str, Any], realization_state_row: Mapping[str, Any],
    slug: str, event_date: str, fact_as_of: str, correlation_id: str,
    resident_fast_lane_prewarm_dependency_identity: str = "",
) -> dict[str, Any]:
    """Build the production public fact from one frozen checkpoint/state pair."""

    authorized = validate_production_probability_activation(activation)
    checkpoint, realization = _mapping(checkpoint_row), _mapping(realization_state_row)
    auxiliary = _parse_json(_mapping(strong_update_row).get("path_auxiliary_json"))
    termination_risk = _mapping(auxiliary.get("termination_risk"))
    auxiliary_timing = _mapping(auxiliary.get("peak_timing_state"))
    if (
        _text(termination_risk.get("schema")) != "termination_risk_marginal_v1"
        or _text(termination_risk.get("state")) != "available"
        or termination_risk.get("updates_posterior") is not False
        or _text(termination_risk.get("canonical_observation_event_id")) != _text(checkpoint.get("last_canonical_event_id"))
    ):
        raise ProductionTransactionIntegrityError("production_public_ssot_termination_risk_contract_invalid")
    required = (
        "posterior_checkpoint_id", "posterior_run_id", "resolution_contract_id",
        "joint_posterior_sha256", "physical_high_distribution_json",
        "settlement_scalar_distribution_json", "market_bucket_distribution_json", "committed_at",
    )
    missing = [key for key in required if not _text(checkpoint.get(key))]
    if missing:
        raise ValueError("production_public_ssot_checkpoint_fields_missing:" + ",".join(missing))
    if _text(realization.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")):
        raise ProductionTransactionIntegrityError("production_public_ssot_realization_run_binding_mismatch")
    if _text(realization.get("resolution_contract_id")) != _text(checkpoint.get("resolution_contract_id")):
        raise ProductionTransactionIntegrityError("production_public_ssot_realization_resolution_binding_mismatch")
    if not all(_text(value) for value in (realization.get("realization_state_id"), realization.get("fact_snapshot_id"), slug, event_date, fact_as_of, correlation_id)):
        raise ValueError("production_public_ssot_fact_identity_missing")
    realization_payload = _parse_json(realization.get("state_json"))
    timing = _mapping(_mapping(realization_payload.get("projection")).get("peak_timing_state"))
    if (
        _text(timing.get("schema")) != "peak_timing_state_l3_v1"
        or timing.get("ok") is not True
        or _text(timing.get("state")) not in {"available", "na"}
        or _text(timing.get("canonical_observation_event_id")) != _text(checkpoint.get("last_canonical_event_id"))
        or _text(timing.get("strong_observation_update_id")) != _text(checkpoint.get("last_strong_update_id"))
    ):
        raise ProductionTransactionIntegrityError("production_public_ssot_peak_timing_contract_invalid")
    if auxiliary_timing and dict(auxiliary_timing) != dict(timing):
        raise ProductionTransactionIntegrityError(
            "production_public_ssot_peak_timing_auxiliary_projection_mismatch"
        )
    ssot: dict[str, Any] = {
        "schema": "sequential_temperature_posterior_ssot_v2",
        "state": "available",
        "writer_mode": "production",
        "fact_snapshot_id": _text(realization.get("fact_snapshot_id")),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization_state_id": _text(realization.get("realization_state_id")),
        "resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "physical_high_distribution": _parse_json(checkpoint.get("physical_high_distribution_json")),
        "settlement_scalar_distribution": _parse_json(checkpoint.get("settlement_scalar_distribution_json")),
        "market_bucket_probabilities": _parse_json(checkpoint.get("market_bucket_distribution_json")),
        "last_canonical_event_id": _text(checkpoint.get("last_canonical_event_id")),
        "last_strong_update_id": _text(checkpoint.get("last_strong_update_id")),
        "observed_max_lower_bound_c": checkpoint.get("observed_max_lower_bound_c"),
        "committed_at": _text(checkpoint.get("committed_at")),
        "remaining_warming_marginal": list(auxiliary.get("remaining_warming_marginal") or []),
        "termination_risk": dict(termination_risk),
        "peak_timing_state": dict(timing),
        "probability_runtime_identity": {
            "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
            "runtime_manifest_sha256": _text(authorized.get("runtime_manifest_sha256")),
            "bayes_formula_id": _text(authorized.get("bayes_formula_id")),
            "composer_schema": _text(authorized.get("composer_schema")),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "strong_update_schema": _text(authorized.get("strong_update_schema")),
            "prior_model_artifact_sha256": _text(authorized.get("prior_model_artifact_sha256")),
            "weak_likelihood_artifact_sha256": _text(authorized.get("weak_likelihood_artifact_sha256")),
            "strong_lr_artifact_sha256": _text(authorized.get("strong_lr_artifact_sha256")),
        },
    }
    if _text(resident_fast_lane_prewarm_dependency_identity):
        ssot["runtime_dependency_lineage"] = {
            "resident_fast_lane_prewarm_dependency_identity": _text(resident_fast_lane_prewarm_dependency_identity),
        }
    return {
        "run_date": _text(event_date), "slug": _text(slug), "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of), "composer_version": "sequential_bayes_v2_production",
        "fact_json": _json({"posterior_ssot_fact": ssot}), "created_at": _text(fact_as_of),
        "correlation_id": _text(correlation_id), "fact_snapshot_id": ssot["fact_snapshot_id"],
        "posterior_run_id": ssot["posterior_run_id"],
    }


def materialize_production_public_physical_posterior_fact_row(
    *, activation: Mapping[str, Any], strong_update_row: Mapping[str, Any], checkpoint_row: Mapping[str, Any], fact_snapshot_id: str, slug: str, event_date: str,
    fact_as_of: str, correlation_id: str, market_bucket_probabilities_na_reason: str,
    resident_fast_lane_prewarm_dependency_identity: str = "",
) -> dict[str, Any]:
    """Build a public physical-X SSOT without a market projection or outbox."""

    authorized = validate_production_probability_activation(activation)
    checkpoint = _mapping(checkpoint_row)
    auxiliary = _parse_json(_mapping(strong_update_row).get("path_auxiliary_json"))
    termination_risk = _mapping(auxiliary.get("termination_risk"))
    timing = _mapping(auxiliary.get("peak_timing_state"))
    if (
        _text(termination_risk.get("schema")) != "termination_risk_marginal_v1"
        or _text(termination_risk.get("state")) != "available"
        or termination_risk.get("updates_posterior") is not False
        or _text(termination_risk.get("canonical_observation_event_id")) != _text(checkpoint.get("last_canonical_event_id"))
    ):
        raise ProductionTransactionIntegrityError("production_physical_ssot_termination_risk_contract_invalid")
    if (
        _text(timing.get("schema")) != "peak_timing_state_l3_v1"
        or timing.get("ok") is not True
        or _text(timing.get("state")) not in {"available", "na"}
        or _text(timing.get("canonical_observation_event_id"))
        != _text(checkpoint.get("last_canonical_event_id"))
        or _text(timing.get("strong_observation_update_id"))
        != _text(checkpoint.get("last_strong_update_id"))
    ):
        raise ProductionTransactionIntegrityError(
            "production_physical_ssot_peak_timing_contract_invalid"
        )
    required = ("posterior_checkpoint_id", "posterior_run_id", "resolution_contract_id", "joint_posterior_sha256", "physical_high_distribution_json", "committed_at")
    missing = [key for key in required if not _text(checkpoint.get(key))]
    if missing:
        raise ValueError("production_physical_ssot_checkpoint_fields_missing:" + ",".join(missing))
    if not all(_text(value) for value in (fact_snapshot_id, slug, event_date, fact_as_of, correlation_id, market_bucket_probabilities_na_reason)):
        raise ValueError("production_physical_ssot_fact_identity_missing")
    ssot: dict[str, Any] = {
        "schema": "sequential_temperature_posterior_ssot_v2", "state": "physical_only_market_projection_na",
        "writer_mode": "production", "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization_state_id": "", "realization_state_na_reason": "market_bucket_projection_unavailable",
        "resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "physical_high_distribution": _parse_json(checkpoint.get("physical_high_distribution_json")),
        "settlement_scalar_distribution": [], "settlement_scalar_distribution_na_reason": _text(market_bucket_probabilities_na_reason),
        "market_bucket_probabilities": {}, "market_bucket_probabilities_na_reason": _text(market_bucket_probabilities_na_reason),
        "last_canonical_event_id": _text(checkpoint.get("last_canonical_event_id")),
        "last_strong_update_id": _text(checkpoint.get("last_strong_update_id")),
        "observed_max_lower_bound_c": checkpoint.get("observed_max_lower_bound_c"), "committed_at": _text(checkpoint.get("committed_at")),
        "remaining_warming_marginal": list(auxiliary.get("remaining_warming_marginal") or []),
        "termination_risk": dict(termination_risk),
        "peak_timing_state": dict(timing),
        "probability_runtime_identity": {
            "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
            "runtime_manifest_sha256": _text(authorized.get("runtime_manifest_sha256")),
            "bayes_formula_id": _text(authorized.get("bayes_formula_id")),
            "composer_schema": _text(authorized.get("composer_schema")),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "strong_update_schema": _text(authorized.get("strong_update_schema")),
            "prior_model_artifact_sha256": _text(authorized.get("prior_model_artifact_sha256")),
            "weak_likelihood_artifact_sha256": _text(authorized.get("weak_likelihood_artifact_sha256")),
            "strong_lr_artifact_sha256": _text(authorized.get("strong_lr_artifact_sha256")),
        },
    }
    if _text(resident_fast_lane_prewarm_dependency_identity):
        ssot["runtime_dependency_lineage"] = {"resident_fast_lane_prewarm_dependency_identity": _text(resident_fast_lane_prewarm_dependency_identity)}
    return {
        "run_date": _text(event_date), "slug": _text(slug), "event_date": _text(event_date), "fact_as_of": _text(fact_as_of),
        "composer_version": "sequential_bayes_v2_production", "fact_json": _json({"posterior_ssot_fact": ssot}),
        "created_at": _text(fact_as_of), "correlation_id": _text(correlation_id),
        "fact_snapshot_id": ssot["fact_snapshot_id"], "posterior_run_id": ssot["posterior_run_id"],
    }


def _insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", tuple(row[field] for field in fields))


def _active_replay_generation(
    conn: sqlite3.Connection,
    target_identity: str,
    activation_contract_sha256: str,
) -> int:
    rows = conn.execute(
        """SELECT p.replay_generation
           FROM production_initial_posterior_lineage l
           JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
           WHERE l.target_identity=? AND l.activation_contract_sha256=?""",
        (_text(target_identity), _text(activation_contract_sha256)),
    ).fetchall()
    if len(rows) != 1:
        raise ProductionTransactionIntegrityError(
            "production_transaction_b_active_replay_generation_not_unique"
        )
    return int(rows[0][0] or 0)


def _latest_checkpoint(
    conn: sqlite3.Connection,
    target_identity: str,
    activation_contract_sha256: str,
) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    replay_generation = _active_replay_generation(
        conn, target_identity, activation_contract_sha256
    )
    return conn.execute(
        """SELECT posterior_checkpoint_id,posterior_run_id,joint_posterior_sha256,
                  forecast_evidence_snapshot_id,prior_context_id,resolution_contract_id,
                  model_artifact_sha256
           FROM posterior_checkpoints WHERE target_identity=? AND replay_generation=?
           ORDER BY rowid DESC LIMIT 1""",
        (target_identity, replay_generation),
    ).fetchone()


def _checkpoint_by_id(conn: sqlite3.Connection, checkpoint_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """SELECT posterior_checkpoint_id,target_identity,posterior_run_id,joint_posterior_sha256,
                  forecast_evidence_snapshot_id,prior_context_id,resolution_contract_id,
                  model_artifact_sha256,replay_generation
           FROM posterior_checkpoints WHERE posterior_checkpoint_id=?""",
        (_text(checkpoint_id),),
    ).fetchone()


def _assert_bundle(
    conn: sqlite3.Connection, *, expected_previous_checkpoint_id: str, expected_previous_posterior_run_id: str,
    validated_activation: Mapping[str, Any],
    strong_update_row: Mapping[str, Any], checkpoint_row: Mapping[str, Any], realization_state_row: Mapping[str, Any] | None,
    outbox_row: Mapping[str, Any] | None, outbox_execution_contract_row: Mapping[str, Any] | None,
) -> dict[str, str]:
    update, checkpoint = _mapping(strong_update_row), _mapping(checkpoint_row)
    realization, outbox, execution = _mapping(realization_state_row), _mapping(outbox_row), _mapping(outbox_execution_contract_row)
    target = _text(checkpoint.get("target_identity"))
    ids = {"update": _text(update.get("strong_observation_update_id")), "checkpoint": _text(checkpoint.get("posterior_checkpoint_id"))}
    if not target or not all(ids.values()):
        raise ProductionTransactionIntegrityError("production_transaction_b_identity_missing")
    if _text(checkpoint.get("previous_checkpoint_id")) != _text(expected_previous_checkpoint_id) or _text(update.get("previous_checkpoint_id")) != _text(expected_previous_checkpoint_id):
        raise ProductionTransactionIntegrityError("production_transaction_b_checkpoint_predecessor_mismatch")
    if _text(update.get("previous_posterior_run_id")) != _text(expected_previous_posterior_run_id):
        raise ProductionTransactionIntegrityError("production_transaction_b_previous_run_mismatch")
    predecessor = _checkpoint_by_id(conn, expected_previous_checkpoint_id)
    if predecessor is None:
        raise ProductionTransactionIntegrityError("production_transaction_b_predecessor_missing")
    if (
        _text(predecessor["target_identity"]) != target
        or _text(predecessor["posterior_run_id"]) != _text(expected_previous_posterior_run_id)
        or _text(predecessor["joint_posterior_sha256"]) != _text(update.get("previous_posterior_sha256"))
    ):
        raise ProductionTransactionIntegrityError("production_transaction_b_predecessor_identity_mismatch")
    active_generation = _active_replay_generation(
        conn,
        target,
        _text(validated_activation.get("activation_contract_sha256")),
    )
    if (
        int(predecessor["replay_generation"] or 0) != active_generation
        or int(checkpoint.get("replay_generation") or 0) != active_generation
    ):
        raise ProductionTransactionIntegrityError(
            "production_transaction_b_active_replay_generation_mismatch"
        )
    bindings = (
        (_text(update.get("target_identity")), target, "update_target"),
        (_text(checkpoint.get("last_strong_update_id")), ids["update"], "checkpoint_update"),
        (_text(checkpoint.get("last_canonical_event_id")), _text(update.get("canonical_observation_event_id")), "checkpoint_event"),
        (_text(checkpoint.get("model_artifact_sha256")), _text(update.get("model_artifact_sha256")), "artifact"),
        (_text(checkpoint.get("forecast_evidence_snapshot_id")), _text(update.get("forecast_evidence_snapshot_id")), "forecast"),
        (_text(checkpoint.get("prior_context_id")), _text(update.get("prior_context_id")), "prior"),
    )
    if any(left != right for left, right, _label in bindings):
        raise ProductionTransactionIntegrityError("production_transaction_b_internal_binding_mismatch")
    frozen_context = (
        ("prior_context_id", "prior"),
        ("resolution_contract_id", "resolution"),
        ("model_artifact_sha256", "strong_artifact"),
    )
    drift = [
        label for field, label in frozen_context
        if _text(checkpoint.get(field)) != _text(predecessor[field])
    ]
    if drift:
        raise ProductionTransactionIntegrityError(
            "production_transaction_b_frozen_p0_context_drift:" + ",".join(drift)
        )
    if _text(checkpoint.get("forecast_evidence_snapshot_id")) != _text(predecessor["forecast_evidence_snapshot_id"]):
        raise ProductionTransactionIntegrityError(
            "production_transaction_b_forecast_snapshot_predecessor_drift"
        )
    if _text(checkpoint.get("model_artifact_sha256")) != _text(validated_activation.get("strong_lr_artifact_sha256")):
        raise ProductionTransactionIntegrityError("production_transaction_b_activation_strong_artifact_mismatch")
    initial = conn.execute(
        """SELECT prior_model_artifact_sha256,weak_likelihood_artifact_sha256,
                  strong_lr_artifact_sha256,activation_contract_sha256
           FROM production_initial_posterior_lineage
           WHERE target_identity=? AND activation_contract_sha256=?
           ORDER BY created_at,rowid""",
        (target, _text(validated_activation.get("activation_contract_sha256"))),
    ).fetchall()
    if len(initial) != 1:
        raise ProductionTransactionIntegrityError("production_transaction_b_initial_lineage_missing")
    expected_initial = (
        _text(validated_activation.get("prior_model_artifact_sha256")),
        _text(validated_activation.get("weak_likelihood_artifact_sha256")),
        _text(validated_activation.get("strong_lr_artifact_sha256")),
        _text(validated_activation.get("activation_contract_sha256")),
    )
    if tuple(_text(value) for value in initial[0]) != expected_initial:
        raise ProductionTransactionIntegrityError("production_transaction_b_activation_initial_lineage_mismatch")
    if realization:
        ids["realization"] = _text(realization.get("realization_state_id"))
        ids["outbox"] = _text(outbox.get("outbox_event_id"))
        if not all(ids.values()) or _text(realization.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")) or _text(realization.get("resolution_contract_id")) != _text(checkpoint.get("resolution_contract_id")):
            raise ProductionTransactionIntegrityError("production_transaction_b_realization_binding_mismatch")
        if _text(outbox.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")) or _text(outbox.get("fact_snapshot_id")) != _text(realization.get("fact_snapshot_id")) or not _text(outbox.get("correlation_id")):
            raise ProductionTransactionIntegrityError("production_transaction_b_outbox_binding_mismatch")
        if _text(execution.get("outbox_event_id")) != ids["outbox"] or _text(execution.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")) or _text(execution.get("canonical_observation_event_id")) != _text(update.get("canonical_observation_event_id")):
            raise ProductionTransactionIntegrityError("production_transaction_b_execution_contract_binding_mismatch")
    elif outbox or execution:
        raise ProductionTransactionIntegrityError("production_physical_transaction_must_not_create_outbox")
    persisted_specs = [
        ("strong_observation_updates", "strong_observation_update_id", ids["update"]),
        ("posterior_checkpoints", "posterior_checkpoint_id", ids["checkpoint"]),
    ]
    if realization:
        persisted_specs.extend([
            ("realization_states", "realization_state_id", ids["realization"]),
            ("posterior_commit_outbox", "outbox_event_id", ids["outbox"]),
            ("posterior_outbox_execution_contracts", "outbox_event_id", ids["outbox"]),
        ])
    persisted_counts = [
        int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (ident,)).fetchone()[0])
        for table, column, ident in persisted_specs
    ]
    if any(persisted_counts) and not all(persisted_counts):
        raise ProductionTransactionIntegrityError("production_transaction_b_partial_state_detected")
    # A retry must validate its frozen identity/bindings above, but must not
    # re-run predecessor CAS after the first commit advanced the pointer.
    if all(persisted_counts):
        return ids
    latest = _latest_checkpoint(
        conn,
        target,
        _text(validated_activation.get("activation_contract_sha256")),
    )
    actual_checkpoint, actual_run = (_text(latest["posterior_checkpoint_id"]), _text(latest["posterior_run_id"])) if latest else ("", "")
    if (actual_checkpoint, actual_run) != (_text(expected_previous_checkpoint_id), _text(expected_previous_posterior_run_id)):
        raise ProductionTransactionConflict("production_transaction_b_predecessor_cas_mismatch")
    if _text(latest["joint_posterior_sha256"] if latest else "") != _text(update.get("previous_posterior_sha256")):
        raise ProductionTransactionConflict("production_transaction_b_previous_posterior_sha256_cas_mismatch")
    return ids


def _existing_public_fact(conn: sqlite3.Connection, public_row: Mapping[str, Any]) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """SELECT id,fact_snapshot_id,fact_json,posterior_run_id,slug,event_date,
                  fact_as_of,correlation_id
           FROM event_fact_history
           WHERE fact_snapshot_id=? AND fact_snapshot_id<>'' LIMIT 1""",
        (_text(public_row.get("fact_snapshot_id")),),
    ).fetchone()


def _insert_public_and_latest(
    conn: sqlite3.Connection,
    public_row: Mapping[str, Any],
    *,
    update_latest: bool,
) -> tuple[int, bool, bool]:
    existing = _existing_public_fact(conn, public_row)
    if existing is None:
        _insert(conn, "event_fact_history", public_row)
        fact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        idempotent = False
    else:
        exact_fields = {
            "fact_json": public_row.get("fact_json"),
            "posterior_run_id": public_row.get("posterior_run_id"),
            "slug": public_row.get("slug"),
            "event_date": public_row.get("event_date"),
            "fact_as_of": public_row.get("fact_as_of"),
            "correlation_id": public_row.get("correlation_id"),
        }
        if any(_text(existing[key]) != _text(value) for key, value in exact_fields.items()):
            raise ProductionTransactionIntegrityError("production_public_ssot_existing_identity_drift")
        fact_id, idempotent = int(existing["id"]), True
    if update_latest:
        conn.execute(
            """INSERT INTO event_fact_latest(slug,event_date,fact_id,updated_at,fact_snapshot_id,correlation_id)
               VALUES(?,?,?,?,?,?) ON CONFLICT(slug,event_date) DO UPDATE SET
                 fact_id=excluded.fact_id,updated_at=excluded.updated_at,fact_snapshot_id=excluded.fact_snapshot_id,correlation_id=excluded.correlation_id""",
            (_text(public_row.get("slug")), _text(public_row.get("event_date")), fact_id, _text(public_row.get("fact_as_of")), _text(public_row.get("fact_snapshot_id")), _text(public_row.get("correlation_id"))),
        )
    return fact_id, idempotent, update_latest


def commit_production_public_ssot_transaction_b(
    conn: sqlite3.Connection, *, activation: Mapping[str, Any], expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str, strong_update_row: Mapping[str, Any], checkpoint_row: Mapping[str, Any],
    realization_state_row: Mapping[str, Any], outbox_row: Mapping[str, Any], outbox_execution_contract_row: Mapping[str, Any],
    public_fact_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Commit available posterior plus execution handoff in one production transaction."""

    authorized = _assert_production_boundary(conn, activation)
    checkpoint, realization, outbox, public = dict(_mapping(checkpoint_row)), _mapping(realization_state_row), _mapping(outbox_row), _mapping(public_fact_row)
    if _text(checkpoint.get("state")) not in {"production_ready", "committed"}:
        raise ProductionTransactionIntegrityError("production_transaction_b_checkpoint_state_invalid")
    checkpoint["state"] = "committed"
    if _text(public.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")) or _text(public.get("fact_snapshot_id")) != _text(realization.get("fact_snapshot_id")):
        raise ProductionTransactionIntegrityError("production_public_ssot_public_fact_binding_mismatch")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids = _assert_bundle(conn, expected_previous_checkpoint_id=expected_previous_checkpoint_id, expected_previous_posterior_run_id=expected_previous_posterior_run_id, validated_activation=authorized, strong_update_row=strong_update_row, checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox, outbox_execution_contract_row=outbox_execution_contract_row)
        existing = [conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (ident,)).fetchone()[0] for table, column, ident in (("strong_observation_updates", "strong_observation_update_id", ids["update"]), ("posterior_checkpoints", "posterior_checkpoint_id", ids["checkpoint"]), ("realization_states", "realization_state_id", ids["realization"]), ("posterior_commit_outbox", "outbox_event_id", ids["outbox"]), ("posterior_outbox_execution_contracts", "outbox_event_id", ids["outbox"]))]
        if any(existing) and not all(existing):
            raise ProductionTransactionIntegrityError("production_transaction_b_partial_state_detected")
        if not any(existing):
            _insert(conn, "strong_observation_updates", strong_update_row); _insert(conn, "posterior_checkpoints", checkpoint); _insert(conn, "realization_states", realization); _insert(conn, "posterior_commit_outbox", outbox); _insert(conn, "posterior_outbox_execution_contracts", outbox_execution_contract_row)
        latest = _latest_checkpoint(
            conn,
            _text(checkpoint.get("target_identity")),
            _text(authorized.get("activation_contract_sha256")),
        )
        publish_latest = _text(latest["posterior_checkpoint_id"] if latest else "") == _text(checkpoint.get("posterior_checkpoint_id"))
        fact_id, public_idempotent, latest_repointed = _insert_public_and_latest(conn, public, update_latest=publish_latest)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": bool(any(existing) and public_idempotent), "production": True, "fact_id": fact_id, "ids": ids, "latest_repointed": latest_repointed}


def commit_production_public_physical_ssot_transaction(
    conn: sqlite3.Connection, *, activation: Mapping[str, Any], expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str, strong_update_row: Mapping[str, Any], checkpoint_row: Mapping[str, Any], public_fact_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Commit physical-X posterior when X→S→B is unavailable; never creates an outbox."""

    authorized = _assert_production_boundary(conn, activation)
    checkpoint, public = dict(_mapping(checkpoint_row)), _mapping(public_fact_row)
    if _text(checkpoint.get("state")) not in {"production_ready", "committed"}:
        raise ProductionTransactionIntegrityError("production_transaction_b_checkpoint_state_invalid")
    checkpoint["state"] = "committed"
    if _text(public.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")):
        raise ProductionTransactionIntegrityError("production_physical_ssot_public_fact_binding_mismatch")
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids = _assert_bundle(conn, expected_previous_checkpoint_id=expected_previous_checkpoint_id, expected_previous_posterior_run_id=expected_previous_posterior_run_id, validated_activation=authorized, strong_update_row=strong_update_row, checkpoint_row=checkpoint, realization_state_row=None, outbox_row=None, outbox_execution_contract_row=None)
        existing = [conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (ident,)).fetchone()[0] for table, column, ident in (("strong_observation_updates", "strong_observation_update_id", ids["update"]), ("posterior_checkpoints", "posterior_checkpoint_id", ids["checkpoint"]))]
        if any(existing) and not all(existing):
            raise ProductionTransactionIntegrityError("production_physical_transaction_partial_state_detected")
        if not any(existing):
            _insert(conn, "strong_observation_updates", strong_update_row); _insert(conn, "posterior_checkpoints", checkpoint)
        latest = _latest_checkpoint(
            conn,
            _text(checkpoint.get("target_identity")),
            _text(authorized.get("activation_contract_sha256")),
        )
        publish_latest = _text(latest["posterior_checkpoint_id"] if latest else "") == _text(checkpoint.get("posterior_checkpoint_id"))
        fact_id, public_idempotent, latest_repointed = _insert_public_and_latest(conn, public, update_latest=publish_latest)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": bool(any(existing) and public_idempotent), "production": True, "physical_only": True, "fact_id": fact_id, "ids": ids, "latest_repointed": latest_repointed}


__all__ = [
    "ACTIVATION_SCHEMA", "ProductionTransactionConflict", "ProductionTransactionIntegrityError", "SCHEMA_VERSION",
    "commit_production_public_physical_ssot_transaction", "commit_production_public_ssot_transaction_b",
    "materialize_production_public_physical_posterior_fact_row", "materialize_production_public_posterior_fact_row",
]
