"""Atomic physical-only Transaction B writer for production-successor v6."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from physical_state_bayes_v6.contracts import SCHEMA_VERSION as STATE_SCHEMA
from physical_state_bayes_v6.schema_contract import verify_schema
from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    PUBLIC_SSOT_SCHEMA,
    STRONG_UPDATE_SCHEMA,
    validate_v6_activation,
)


SCHEMA_VERSION = "production_probability_v6_public_ssot_writer_v1"


class V6TransactionConflict(RuntimeError):
    pass


class V6TransactionIntegrityError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _parse(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return default


def _insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )


def _validate_reported_distribution(checkpoint: Mapping[str, Any]) -> dict[str, float]:
    raw = _parse(checkpoint.get("reported_physical_x_distribution_json"), {})
    if not isinstance(raw, Mapping) or not raw:
        raise V6TransactionIntegrityError("v6_reported_x_distribution_missing")
    result: dict[str, float] = {}
    for key, value in raw.items():
        numeric_key = float(key)
        probability = float(value)
        if not numeric_key.is_integer() or probability < 0.0:
            raise V6TransactionIntegrityError("v6_reported_x_distribution_invalid")
        result[str(int(numeric_key))] = probability
    if abs(sum(result.values()) - 1.0) > 1e-8:
        raise V6TransactionIntegrityError("v6_reported_x_distribution_not_normalized")
    lower = checkpoint.get("reported_x_lower_bound_c")
    if lower is not None and any(int(key) < int(lower) for key in result):
        raise V6TransactionIntegrityError("v6_reported_x_mass_below_hard_bound")
    return result


def materialize_v6_public_physical_fact_row(
    *,
    activation: Mapping[str, Any],
    strong_update_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
    market_bucket_probabilities_na_reason: str,
) -> dict[str, Any]:
    authorized = validate_v6_activation(activation)
    update = _mapping(strong_update_row)
    checkpoint = _mapping(checkpoint_row)
    if _text(update.get("probability_update_schema")) != STRONG_UPDATE_SCHEMA:
        raise V6TransactionIntegrityError("v6_public_update_schema_invalid")
    if int(update.get("no_post_emission_transition") or 0) != 1:
        raise V6TransactionIntegrityError("v6_public_post_emission_transition_not_retired")
    if _parse(update.get("post_state_transition_json"), None) != {}:
        raise V6TransactionIntegrityError("v6_public_post_state_transition_not_empty")
    if (
        _text(checkpoint.get("checkpoint_probability_schema")) != CHECKPOINT_SCHEMA
        or _text(checkpoint.get("state_space_schema")) != STATE_SCHEMA
    ):
        raise V6TransactionIntegrityError("v6_public_checkpoint_schema_invalid")
    reported = _validate_reported_distribution(checkpoint)
    auxiliary = _parse(update.get("path_auxiliary_json"), {})
    termination = dict(_mapping(_mapping(auxiliary).get("termination_risk")))
    if (
        _text(termination.get("schema"))
        != "termination_risk_marginal_v3_physical_state_bayes_v6"
        or termination.get("updates_posterior") is not False
        or _text(termination.get("canonical_observation_event_id"))
        != _text(checkpoint.get("last_canonical_event_id"))
    ):
        raise V6TransactionIntegrityError("v6_public_termination_contract_invalid")
    required = (
        fact_snapshot_id,
        slug,
        event_date,
        fact_as_of,
        correlation_id,
        market_bucket_probabilities_na_reason,
        checkpoint.get("posterior_checkpoint_id"),
        checkpoint.get("posterior_run_id"),
        checkpoint.get("joint_posterior_sha256"),
        checkpoint.get("resolution_contract_id"),
    )
    if any(not _text(value) for value in required):
        raise ValueError("v6_public_fact_identity_missing")
    ssot = {
        "schema": PUBLIC_SSOT_SCHEMA,
        "state": "physical_only_market_projection_na",
        "writer_mode": "production",
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "probability_target_type": _text(checkpoint.get("probability_target_type")),
        "reported_physical_x_distribution": reported,
        "reported_x_lower_bound_c": checkpoint.get("reported_x_lower_bound_c"),
        "latent_y_public": False,
        "settlement_scalar_distribution": [],
        "market_bucket_probabilities": {},
        "market_bucket_probabilities_na_reason": _text(market_bucket_probabilities_na_reason),
        "last_canonical_event_id": _text(checkpoint.get("last_canonical_event_id")),
        "last_strong_update_id": _text(checkpoint.get("last_strong_update_id")),
        "committed_at": _text(checkpoint.get("committed_at")),
        "termination_risk": termination,
        "duration_clocks": _parse(checkpoint.get("duration_clocks_json"), {}),
        "probability_runtime_identity": {
            "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
            "runtime_manifest_sha256": _text(authorized.get("runtime_manifest_sha256")),
            "bayes_formula_id": _text(authorized.get("bayes_formula_id")),
            "composer_schema": _text(authorized.get("composer_schema")),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "strong_update_schema": _text(authorized.get("strong_update_schema")),
            "transition_artifact_sha256": _text(authorized.get("transition_artifact_sha256")),
            "observation_artifact_sha256": _text(authorized.get("observation_artifact_sha256")),
            "prior_model_artifact_sha256": _text(authorized.get("prior_model_artifact_sha256")),
            "weak_likelihood_artifact_sha256": _text(authorized.get("weak_likelihood_artifact_sha256")),
            "v5_fallback_forbidden": True,
        },
    }
    return {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": "physical_state_bayes_v6_production",
        "fact_json": _json({"posterior_ssot_fact": ssot}),
        "created_at": _text(fact_as_of),
        "correlation_id": _text(correlation_id),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
    }


def _assert_boundary(conn: sqlite3.Connection, activation: Mapping[str, Any]) -> dict[str, Any]:
    if conn.in_transaction:
        raise V6TransactionIntegrityError("v6_transaction_requires_no_ambient_transaction")
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_shadow_runtime_manifest'"
    ).fetchone():
        raise V6TransactionIntegrityError("v6_transaction_refuses_shadow_database")
    authorized = validate_v6_activation(activation)
    schema = verify_schema(conn)
    if schema.get("ok") is not True:
        raise V6TransactionIntegrityError("v6_transaction_schema_unavailable")
    required = (
        "canonical_observation_events",
        "strong_observation_updates",
        "posterior_checkpoints",
        "production_initial_posterior_lineage",
        "event_fact_history",
        "event_fact_latest",
    )
    missing = [
        table
        for table in required
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is None
    ]
    if missing:
        raise V6TransactionIntegrityError("v6_transaction_tables_missing:" + ",".join(missing))
    return authorized


def _active_generation(
    conn: sqlite3.Connection, target: str, activation_contract_sha256: str
) -> int:
    rows = conn.execute(
        "SELECT p.replay_generation FROM production_initial_posterior_lineage l "
        "JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id "
        "WHERE l.target_identity=? AND l.activation_contract_sha256=?",
        (target, activation_contract_sha256),
    ).fetchall()
    if len(rows) != 1:
        raise V6TransactionIntegrityError("v6_active_replay_generation_not_unique")
    return int(rows[0][0] or 0)


def _assert_bundle(
    conn: sqlite3.Connection,
    *,
    authorized: Mapping[str, Any],
    expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str,
    update: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
) -> None:
    target = _text(checkpoint.get("target_identity"))
    predecessor = conn.execute(
        "SELECT target_identity,posterior_run_id,joint_posterior_sha256,replay_generation,"
        "prior_context_id,resolution_contract_id,forecast_evidence_snapshot_id,model_artifact_sha256 "
        "FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
        (_text(expected_previous_checkpoint_id),),
    ).fetchone()
    if predecessor is None:
        raise V6TransactionIntegrityError("v6_predecessor_missing")
    if (
        _text(predecessor[0]) != target
        or _text(predecessor[1]) != _text(expected_previous_posterior_run_id)
        or _text(predecessor[2]) != _text(update.get("previous_posterior_sha256"))
    ):
        raise V6TransactionIntegrityError("v6_predecessor_identity_mismatch")
    generation = _active_generation(
        conn, target, _text(authorized.get("activation_contract_sha256"))
    )
    latest = conn.execute(
        "SELECT posterior_checkpoint_id FROM posterior_checkpoints "
        "WHERE target_identity=? AND replay_generation=? ORDER BY rowid DESC LIMIT 1",
        (target, generation),
    ).fetchone()
    if latest is None or _text(latest[0]) != _text(expected_previous_checkpoint_id):
        raise V6TransactionConflict("v6_predecessor_is_not_latest")
    if int(predecessor[3] or 0) != generation or int(checkpoint.get("replay_generation") or 0) != generation:
        raise V6TransactionIntegrityError("v6_replay_generation_mismatch")
    bindings = (
        (checkpoint.get("previous_checkpoint_id"), expected_previous_checkpoint_id),
        (update.get("previous_checkpoint_id"), expected_previous_checkpoint_id),
        (update.get("previous_posterior_run_id"), expected_previous_posterior_run_id),
        (update.get("target_identity"), target),
        (checkpoint.get("last_strong_update_id"), update.get("strong_observation_update_id")),
        (checkpoint.get("last_canonical_event_id"), update.get("canonical_observation_event_id")),
        (checkpoint.get("prior_context_id"), predecessor[4]),
        (checkpoint.get("resolution_contract_id"), predecessor[5]),
        (checkpoint.get("forecast_evidence_snapshot_id"), predecessor[6]),
        (checkpoint.get("model_artifact_sha256"), predecessor[7]),
        (checkpoint.get("model_artifact_sha256"), authorized.get("activation_contract_sha256")),
        (checkpoint.get("transition_artifact_sha256"), authorized.get("transition_artifact_sha256")),
        (checkpoint.get("observation_artifact_sha256"), authorized.get("observation_artifact_sha256")),
        (update.get("transition_artifact_sha256"), authorized.get("transition_artifact_sha256")),
        (update.get("observation_artifact_sha256"), authorized.get("observation_artifact_sha256")),
    )
    if any(_text(left) != _text(right) for left, right in bindings):
        raise V6TransactionIntegrityError("v6_transaction_bundle_binding_mismatch")
    event = conn.execute(
        "SELECT state FROM canonical_observation_events WHERE canonical_observation_event_id=?",
        (_text(update.get("canonical_observation_event_id")),),
    ).fetchone()
    if event is None or _text(event[0]) not in {"accepted_primary", "accepted_substitute"}:
        raise V6TransactionIntegrityError("v6_canonical_event_not_accepted")
    if (
        _text(update.get("probability_update_schema")) != STRONG_UPDATE_SCHEMA
        or int(update.get("no_post_emission_transition") or 0) != 1
        or _parse(update.get("post_state_transition_json"), None) != {}
        or _text(checkpoint.get("checkpoint_probability_schema")) != CHECKPOINT_SCHEMA
        or _text(checkpoint.get("state_space_schema")) != STATE_SCHEMA
    ):
        raise V6TransactionIntegrityError("v6_transaction_semantics_invalid")
    _validate_reported_distribution(checkpoint)


def _upsert_public(
    conn: sqlite3.Connection, public: Mapping[str, Any]
) -> tuple[int, bool]:
    existing = conn.execute(
        "SELECT id,fact_json,posterior_run_id,slug,event_date,fact_as_of,correlation_id "
        "FROM event_fact_history WHERE fact_snapshot_id=? AND fact_snapshot_id<>''",
        (_text(public.get("fact_snapshot_id")),),
    ).fetchone()
    if existing is None:
        _insert(conn, "event_fact_history", public)
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]), False
    expected = (
        public.get("fact_json"),
        public.get("posterior_run_id"),
        public.get("slug"),
        public.get("event_date"),
        public.get("fact_as_of"),
        public.get("correlation_id"),
    )
    if any(_text(left) != _text(right) for left, right in zip(existing[1:], expected)):
        raise V6TransactionIntegrityError("v6_public_fact_existing_identity_drift")
    return int(existing[0]), True


def commit_v6_public_physical_transaction(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str,
    strong_update_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    public_fact_row: Mapping[str, Any],
) -> dict[str, Any]:
    authorized = _assert_boundary(conn, activation)
    update = dict(_mapping(strong_update_row))
    checkpoint = dict(_mapping(checkpoint_row))
    public = dict(_mapping(public_fact_row))
    if _text(checkpoint.get("state")) not in {"production_successor_ready", "committed"}:
        raise V6TransactionIntegrityError("v6_checkpoint_state_invalid")
    if _text(public.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")):
        raise V6TransactionIntegrityError("v6_public_checkpoint_binding_mismatch")
    checkpoint["state"] = "committed"
    conn.execute("BEGIN IMMEDIATE")
    try:
        update_id = _text(update.get("strong_observation_update_id"))
        checkpoint_id = _text(checkpoint.get("posterior_checkpoint_id"))
        present = (
            int(conn.execute("SELECT COUNT(*) FROM strong_observation_updates WHERE strong_observation_update_id=?", (update_id,)).fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM posterior_checkpoints WHERE posterior_checkpoint_id=?", (checkpoint_id,)).fetchone()[0]),
        )
        if any(present) and not all(present):
            raise V6TransactionIntegrityError("v6_transaction_partial_state_detected")
        if all(present):
            persisted = conn.execute(
                "SELECT posterior_run_id,joint_posterior_sha256 FROM posterior_checkpoints "
                "WHERE posterior_checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
            latest = conn.execute(
                "SELECT posterior_checkpoint_id FROM posterior_checkpoints WHERE target_identity=? "
                "AND replay_generation=? ORDER BY rowid DESC LIMIT 1",
                (_text(checkpoint.get("target_identity")), int(checkpoint.get("replay_generation") or 0)),
            ).fetchone()
            if (
                persisted is None
                or _text(persisted[0]) != _text(checkpoint.get("posterior_run_id"))
                or _text(persisted[1]) != _text(checkpoint.get("joint_posterior_sha256"))
                or latest is None
                or _text(latest[0]) != checkpoint_id
            ):
                raise V6TransactionIntegrityError("v6_idempotent_successor_identity_drift")
        else:
            _assert_bundle(
                conn,
                authorized=authorized,
                expected_previous_checkpoint_id=expected_previous_checkpoint_id,
                expected_previous_posterior_run_id=expected_previous_posterior_run_id,
                update=update,
                checkpoint=checkpoint,
            )
        if not any(present):
            _insert(conn, "strong_observation_updates", update)
            _insert(conn, "posterior_checkpoints", checkpoint)
        fact_id, public_idempotent = _upsert_public(conn, public)
        conn.execute(
            "INSERT INTO event_fact_latest(slug,event_date,fact_id,updated_at,fact_snapshot_id,correlation_id) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(slug,event_date) DO UPDATE SET "
            "fact_id=excluded.fact_id,updated_at=excluded.updated_at,"
            "fact_snapshot_id=excluded.fact_snapshot_id,correlation_id=excluded.correlation_id",
            (
                _text(public.get("slug")),
                _text(public.get("event_date")),
                fact_id,
                _text(public.get("fact_as_of")),
                _text(public.get("fact_snapshot_id")),
                _text(public.get("correlation_id")),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "committed",
        "production": True,
        "idempotent": bool(all(present) and public_idempotent),
        "posterior_checkpoint_id": checkpoint_id,
        "fact_id": fact_id,
        "creates_outbox": False,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "V6TransactionConflict",
    "V6TransactionIntegrityError",
    "commit_v6_public_physical_transaction",
    "materialize_v6_public_physical_fact_row",
]
