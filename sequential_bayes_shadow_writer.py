"""Atomic Transaction-B writer for the sequential Bayes shadow tables only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "sequential_bayes_shadow_writer_v1"


class ShadowTransactionConflict(RuntimeError):
    """The expected predecessor changed before Transaction B began."""


class ShadowTransactionIntegrityError(RuntimeError):
    """A retry found a partial state, which is an invariant violation."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _runtime_dependency_identity_from_path_auxiliary(value: Any) -> str:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError):
        return ""
    auxiliary = _mapping(parsed)
    return _text(auxiliary.get("resident_fast_lane_prewarm_dependency_identity"))


def materialize_strong_update_row(
    update: Mapping[str, Any],
    *,
    source_reliability_before: Mapping[str, Any],
    source_reliability_after: Mapping[str, Any],
    path_auxiliary: Mapping[str, Any],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    required = (
        "strong_observation_update_id",
        "target_identity",
        "previous_posterior_run_id",
        "previous_posterior_sha256",
        "previous_checkpoint_id",
        "canonical_observation_event_id",
        "forecast_evidence_snapshot_id",
        "prior_context_id",
        "model_artifact_sha256",
        "decision_as_of",
    )
    missing = [key for key in required if not _text(update.get(key))]
    if missing or _text(update.get("state")) not in {"applied", "neutral"}:
        raise ValueError(f"strong_update_not_persistable:{','.join(missing) or 'state'}")
    return {
        "strong_observation_update_id": _text(update.get("strong_observation_update_id")),
        "target_identity": _text(update.get("target_identity")),
        "previous_posterior_run_id": _text(update.get("previous_posterior_run_id")),
        "previous_posterior_sha256": _text(update.get("previous_posterior_sha256")),
        "previous_checkpoint_id": _text(update.get("previous_checkpoint_id")),
        "canonical_observation_event_id": _text(update.get("canonical_observation_event_id")),
        "forecast_evidence_snapshot_id": _text(update.get("forecast_evidence_snapshot_id")),
        "prior_context_id": _text(update.get("prior_context_id")),
        "model_artifact_sha256": _text(update.get("model_artifact_sha256")),
        "decision_as_of": _text(update.get("decision_as_of")),
        "state": _text(update.get("state")),
        "raw_log_score_json": _json(update.get("raw_log_score") or []),
        "raw_log_score_sha256": _text(update.get("raw_log_score_sha256")),
        "normalizer_offset": float(update.get("normalizer_offset") or 0.0),
        "posterior_delta_json": _json(update.get("posterior_delta") or []),
        "posterior_delta_sha256": _text(update.get("posterior_delta_sha256")),
        "pre_state_transition_json": _json(update.get("pre_state_transition") or {}),
        "post_state_transition_json": _json(update.get("post_state_transition") or {}),
        "source_reliability_before_json": _json(source_reliability_before),
        "source_reliability_after_json": _json(source_reliability_after),
        "path_auxiliary_json": _json(path_auxiliary),
        "observed_max_lower_bound_c": update.get("observed_max_lower_bound_c"),
        "quality_json": _json(update.get("quality") or {}),
        "block_reasons_json": _json(update.get("block_reasons") or []),
        "started_at": _text(started_at),
        "completed_at": _text(completed_at),
        "created_at": _text(completed_at),
    }


def materialize_checkpoint_row(
    checkpoint: Mapping[str, Any],
    *,
    resolution_bridge: Mapping[str, Any],
    trajectory_feature_state: Mapping[str, Any],
    source_reliability_state: Mapping[str, Any],
) -> dict[str, Any]:
    bridge = _mapping(resolution_bridge)
    if not bool(bridge.get("ok")) or _text(bridge.get("state")) not in {"available", "physical_only_market_projection_na"}:
        raise ValueError("checkpoint_resolution_bridge_not_available")
    if _text(bridge.get("resolution_contract_id")) != _text(checkpoint.get("resolution_contract_id")):
        raise ValueError("checkpoint_resolution_contract_mismatch")
    required = (
        "posterior_checkpoint_id", "target_identity", "posterior_run_id",
        "checkpoint_kind", "forecast_evidence_snapshot_id", "prior_context_id",
        "resolution_contract_id", "joint_posterior_sha256", "model_artifact_sha256",
        "created_at", "committed_at",
    )
    missing = [key for key in required if not _text(checkpoint.get(key))]
    if missing:
        raise ValueError(f"checkpoint_not_persistable:{','.join(missing)}")
    joint = checkpoint.get("joint_posterior") or []
    physical = checkpoint.get("physical_high_distribution") or {}
    latent = checkpoint.get("latent_post_state_distribution") or {}
    consumed = checkpoint.get("consumed_event_ids") or []
    return {
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "target_identity": _text(checkpoint.get("target_identity")),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "previous_checkpoint_id": _text(checkpoint.get("previous_checkpoint_id")),
        "checkpoint_kind": _text(checkpoint.get("checkpoint_kind")),
        "forecast_evidence_snapshot_id": _text(checkpoint.get("forecast_evidence_snapshot_id")),
        "prior_context_id": _text(checkpoint.get("prior_context_id")),
        "resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "last_strong_update_id": _text(checkpoint.get("last_strong_update_id")),
        "last_canonical_event_id": _text(checkpoint.get("last_canonical_event_id")),
        "canonical_sequence_sha256": _sha256(consumed),
        "temperature_grid_id": _sha256(sorted(str(key) for key in physical)),
        "state_grid_id": _sha256(sorted(str(key) for key in latent)),
        "joint_posterior_json": _json(joint),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "physical_high_distribution_json": _json(physical),
        "settlement_scalar_distribution_json": _json(bridge.get("settlement_scalar_distribution") or []),
        "market_bucket_distribution_json": _json(bridge.get("market_bucket_probabilities") or {}),
        "latent_post_state_json": _json(latent),
        "trajectory_feature_state_json": _json(trajectory_feature_state),
        "source_reliability_state_json": _json(source_reliability_state),
        "observed_max_lower_bound_c": checkpoint.get("observed_max_lower_bound_c"),
        "consumed_event_ids_json": _json(consumed),
        "consumed_ancestor_sha256": _text(checkpoint.get("consumed_ancestor_sha256")),
        "model_artifact_sha256": _text(checkpoint.get("model_artifact_sha256")),
        "replay_generation": int(checkpoint.get("replay_generation") or 0),
        "supersedes_checkpoint_id": _text(checkpoint.get("supersedes_checkpoint_id")),
        "state": "shadow_ready",
        "created_at": _text(checkpoint.get("created_at")),
        "committed_at": _text(checkpoint.get("committed_at")),
    }


def materialize_outbox_row(
    checkpoint_row: Mapping[str, Any],
    *,
    fact_snapshot_id: str,
    correlation_id: str,
    outbox_event_id: str,
) -> dict[str, Any]:
    return {
        "outbox_event_id": _text(outbox_event_id),
        "posterior_run_id": _text(checkpoint_row.get("posterior_run_id")),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "correlation_id": _text(correlation_id),
        "state": "pending",
        "created_at": _text(checkpoint_row.get("committed_at")),
        "attempt_count": 0,
        "claim_generation": 0,
    }


def _parse_utc(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso_ms(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def materialize_outbox_execution_contract_row(
    *,
    outbox_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    canonical_observation_event: Mapping[str, Any],
    inference_decision_deadline_at: str,
) -> dict[str, Any]:
    """Bind canonical T1 and the exact three-second deadline to outbox."""

    outbox = _mapping(outbox_row)
    checkpoint = _mapping(checkpoint_row)
    event = _mapping(canonical_observation_event)
    first_actionable = _parse_utc(event.get("first_actionable_seen_at"))
    committed = _parse_utc(checkpoint.get("committed_at"))
    inference_deadline = _parse_utc(inference_decision_deadline_at)
    if first_actionable is None or committed is None or inference_deadline is None:
        raise ValueError("outbox_execution_contract_time_missing_or_invalid")
    hard_deadline = first_actionable + timedelta(milliseconds=3000)
    eligible = committed <= hard_deadline
    return {
        "outbox_event_id": _text(outbox.get("outbox_event_id")),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "canonical_observation_event_id": _text(event.get("canonical_observation_event_id")),
        "first_actionable_seen_at": _iso_ms(first_actionable),
        "observation_to_order_hard_deadline_at": _iso_ms(hard_deadline),
        "inference_decision_deadline_at": _iso_ms(inference_deadline),
        "transaction_b_committed_at": _iso_ms(committed),
        "execution_eligible_at_commit": 1 if eligible else 0,
        "execution_na_reason": "" if eligible else "transaction_b_committed_after_observation_to_order_hard_deadline",
        "deadline_policy": "canonical_first_actionable_plus_3000ms",
        "created_at": _iso_ms(committed),
    }


def _latest_checkpoint(conn: sqlite3.Connection, target_identity: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT
            posterior_checkpoint_id,
            posterior_run_id,
            joint_posterior_sha256,
            canonical_sequence_sha256,
            resolution_contract_id,
            model_artifact_sha256
        FROM posterior_checkpoints
        WHERE target_identity = ?
        ORDER BY replay_generation DESC, rowid DESC
        LIMIT 1
        """,
        (_text(target_identity),),
    ).fetchone()


def _insert_update(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(
        f"INSERT INTO strong_observation_updates({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )


def _insert_checkpoint(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(
        f"INSERT INTO posterior_checkpoints({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )


def _insert_realization(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(
        f"INSERT INTO realization_states({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )


def _insert_outbox(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(
        f"INSERT INTO posterior_commit_outbox({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )


def _insert_outbox_execution_contract(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(
        f"INSERT INTO posterior_outbox_execution_contracts({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )


def commit_shadow_transaction_b(
    conn: sqlite3.Connection,
    *,
    expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str,
    strong_update_row: Mapping[str, Any] | None,
    checkpoint_row: Mapping[str, Any],
    realization_state_row: Mapping[str, Any],
    outbox_row: Mapping[str, Any],
    outbox_execution_contract_row: Mapping[str, Any],
) -> dict[str, Any]:
    """CAS + all-or-nothing write into P1 shadow tables.

    This intentionally does not write event_fact_history/latest and no consumer
    is authorized to treat its outbox as a production order trigger.
    """

    checkpoint = _mapping(checkpoint_row)
    update = _mapping(strong_update_row)
    realization = _mapping(realization_state_row)
    outbox = _mapping(outbox_row)
    execution_contract = _mapping(outbox_execution_contract_row)
    target_identity = _text(checkpoint.get("target_identity"))
    if not target_identity:
        raise ValueError("transaction_b_target_identity_missing")
    ids = {
        "update": _text(update.get("strong_observation_update_id")),
        "checkpoint": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization": _text(realization.get("realization_state_id")),
        "outbox": _text(outbox.get("outbox_event_id")),
        "execution_contract": _text(execution_contract.get("outbox_event_id")),
    }
    if not all(ids.values()):
        raise ValueError("transaction_b_row_identity_missing")
    if _text(checkpoint.get("previous_checkpoint_id")) != _text(expected_previous_checkpoint_id):
        raise ShadowTransactionIntegrityError("transaction_b_checkpoint_predecessor_mismatch")
    if _text(update.get("previous_checkpoint_id")) != _text(expected_previous_checkpoint_id):
        raise ShadowTransactionIntegrityError("transaction_b_update_predecessor_mismatch")
    if _text(update.get("previous_posterior_run_id")) != _text(expected_previous_posterior_run_id):
        raise ShadowTransactionIntegrityError("transaction_b_update_previous_run_mismatch")
    if _text(update.get("target_identity")) != target_identity:
        raise ShadowTransactionIntegrityError("transaction_b_update_target_binding_mismatch")
    if _text(checkpoint.get("last_strong_update_id")) != ids["update"]:
        raise ShadowTransactionIntegrityError("transaction_b_checkpoint_update_binding_mismatch")
    if _text(checkpoint.get("last_canonical_event_id")) != _text(update.get("canonical_observation_event_id")):
        raise ShadowTransactionIntegrityError("transaction_b_checkpoint_canonical_event_binding_mismatch")
    if _text(checkpoint.get("model_artifact_sha256")) != _text(update.get("model_artifact_sha256")):
        raise ShadowTransactionIntegrityError("transaction_b_checkpoint_artifact_binding_mismatch")
    if _text(checkpoint.get("forecast_evidence_snapshot_id")) != _text(update.get("forecast_evidence_snapshot_id")):
        raise ShadowTransactionIntegrityError("transaction_b_checkpoint_forecast_binding_mismatch")
    if _text(checkpoint.get("prior_context_id")) != _text(update.get("prior_context_id")):
        raise ShadowTransactionIntegrityError("transaction_b_checkpoint_prior_binding_mismatch")
    if _text(realization.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")):
        raise ShadowTransactionIntegrityError("transaction_b_realization_run_binding_mismatch")
    if _text(realization.get("resolution_contract_id")) != _text(checkpoint.get("resolution_contract_id")):
        raise ShadowTransactionIntegrityError("transaction_b_realization_resolution_binding_mismatch")
    if _text(outbox.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")):
        raise ShadowTransactionIntegrityError("transaction_b_outbox_run_binding_mismatch")
    if _text(outbox.get("fact_snapshot_id")) != _text(realization.get("fact_snapshot_id")):
        raise ShadowTransactionIntegrityError("transaction_b_outbox_fact_binding_mismatch")
    if ids["execution_contract"] != ids["outbox"]:
        raise ShadowTransactionIntegrityError("transaction_b_execution_contract_outbox_binding_mismatch")
    if _text(execution_contract.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")):
        raise ShadowTransactionIntegrityError("transaction_b_execution_contract_run_binding_mismatch")
    if _text(execution_contract.get("canonical_observation_event_id")) != _text(update.get("canonical_observation_event_id")):
        raise ShadowTransactionIntegrityError("transaction_b_execution_contract_event_binding_mismatch")
    if not _text(outbox.get("correlation_id")):
        raise ValueError("transaction_b_correlation_id_missing")
    existing_counts = {
        "update": int(conn.execute("SELECT COUNT(*) FROM strong_observation_updates WHERE strong_observation_update_id=?", (ids["update"],)).fetchone()[0]) if ids["update"] else 0,
        "checkpoint": int(conn.execute("SELECT COUNT(*) FROM posterior_checkpoints WHERE posterior_checkpoint_id=?", (ids["checkpoint"],)).fetchone()[0]),
        "realization": int(conn.execute("SELECT COUNT(*) FROM realization_states WHERE realization_state_id=?", (ids["realization"],)).fetchone()[0]),
        "outbox": int(conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox WHERE outbox_event_id=?", (ids["outbox"],)).fetchone()[0]),
        "execution_contract": int(conn.execute("SELECT COUNT(*) FROM posterior_outbox_execution_contracts WHERE outbox_event_id=?", (ids["execution_contract"],)).fetchone()[0]),
    }
    required_existing = list(existing_counts.values())
    if all(required_existing):
        existing_update = conn.execute(
            "SELECT path_auxiliary_json FROM strong_observation_updates WHERE strong_observation_update_id=?",
            (ids["update"],),
        ).fetchone()
        existing_dependency_identity = _runtime_dependency_identity_from_path_auxiliary(
            existing_update[0] if existing_update is not None else ""
        )
        requested_dependency_identity = _runtime_dependency_identity_from_path_auxiliary(
            update.get("path_auxiliary_json")
        )
        if existing_dependency_identity != requested_dependency_identity:
            raise ShadowTransactionIntegrityError("transaction_b_existing_runtime_dependency_binding_mismatch")
        return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": True, "shadow_only": True, "ids": ids}
    if any(required_existing):
        raise ShadowTransactionIntegrityError("transaction_b_partial_state_detected")
    latest = _latest_checkpoint(conn, target_identity)
    actual_checkpoint = _text(latest["posterior_checkpoint_id"]) if latest else ""
    actual_run = _text(latest["posterior_run_id"]) if latest else ""
    if actual_checkpoint != _text(expected_previous_checkpoint_id) or actual_run != _text(expected_previous_posterior_run_id):
        raise ShadowTransactionConflict(
            f"previous_checkpoint_cas_mismatch:expected={expected_previous_checkpoint_id}/{expected_previous_posterior_run_id}:actual={actual_checkpoint}/{actual_run}"
        )
    if _text(latest["joint_posterior_sha256"]) != _text(update.get("previous_posterior_sha256")):
        raise ShadowTransactionConflict("previous_posterior_sha256_cas_mismatch")
    conn.execute("SAVEPOINT sequential_bayes_shadow_tx_b")
    try:
        if ids["update"]:
            _insert_update(conn, update)
        _insert_checkpoint(conn, checkpoint)
        _insert_realization(conn, realization)
        _insert_outbox(conn, outbox)
        _insert_outbox_execution_contract(conn, execution_contract)
    except Exception:
        conn.execute("ROLLBACK TO sequential_bayes_shadow_tx_b")
        conn.execute("RELEASE sequential_bayes_shadow_tx_b")
        raise
    conn.execute("RELEASE sequential_bayes_shadow_tx_b")
    return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": False, "shadow_only": True, "ids": ids}


def commit_shadow_physical_posterior_transaction(
    conn: sqlite3.Connection,
    *,
    expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str,
    strong_update_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically commit X-only evidence when market-bucket B is explicitly NA."""

    checkpoint = _mapping(checkpoint_row)
    update = _mapping(strong_update_row)
    target_identity = _text(checkpoint.get("target_identity"))
    update_id = _text(update.get("strong_observation_update_id"))
    checkpoint_id = _text(checkpoint.get("posterior_checkpoint_id"))
    if not target_identity or not update_id or not checkpoint_id:
        raise ValueError("physical_posterior_transaction_identity_missing")
    if _text(checkpoint.get("previous_checkpoint_id")) != _text(expected_previous_checkpoint_id):
        raise ShadowTransactionIntegrityError("physical_posterior_transaction_checkpoint_predecessor_mismatch")
    if _text(update.get("previous_checkpoint_id")) != _text(expected_previous_checkpoint_id):
        raise ShadowTransactionIntegrityError("physical_posterior_transaction_update_predecessor_mismatch")
    if _text(update.get("previous_posterior_run_id")) != _text(expected_previous_posterior_run_id):
        raise ShadowTransactionIntegrityError("physical_posterior_transaction_update_previous_run_mismatch")
    if _text(update.get("target_identity")) != target_identity:
        raise ShadowTransactionIntegrityError("physical_posterior_transaction_update_target_binding_mismatch")
    if _text(checkpoint.get("last_strong_update_id")) != update_id:
        raise ShadowTransactionIntegrityError("physical_posterior_transaction_checkpoint_update_binding_mismatch")
    if _text(checkpoint.get("last_canonical_event_id")) != _text(update.get("canonical_observation_event_id")):
        raise ShadowTransactionIntegrityError("physical_posterior_transaction_checkpoint_canonical_event_binding_mismatch")
    if _text(checkpoint.get("model_artifact_sha256")) != _text(update.get("model_artifact_sha256")):
        raise ShadowTransactionIntegrityError("physical_posterior_transaction_checkpoint_artifact_binding_mismatch")
    if _text(checkpoint.get("forecast_evidence_snapshot_id")) != _text(update.get("forecast_evidence_snapshot_id")):
        raise ShadowTransactionIntegrityError("physical_posterior_transaction_checkpoint_forecast_binding_mismatch")
    if _text(checkpoint.get("prior_context_id")) != _text(update.get("prior_context_id")):
        raise ShadowTransactionIntegrityError("physical_posterior_transaction_checkpoint_prior_binding_mismatch")
    existing_update = int(conn.execute(
        "SELECT COUNT(*) FROM strong_observation_updates WHERE strong_observation_update_id=?", (update_id,)
    ).fetchone()[0])
    existing_checkpoint = int(conn.execute(
        "SELECT COUNT(*) FROM posterior_checkpoints WHERE posterior_checkpoint_id=?", (checkpoint_id,)
    ).fetchone()[0])
    if existing_update and existing_checkpoint:
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "idempotent": True,
            "shadow_only": True,
            "market_bucket_projection_state": "na",
            "ids": {"update": update_id, "checkpoint": checkpoint_id},
        }
    if existing_update or existing_checkpoint:
        raise ShadowTransactionIntegrityError("physical_posterior_transaction_partial_state_detected")
    latest = _latest_checkpoint(conn, target_identity)
    actual_checkpoint = _text(latest["posterior_checkpoint_id"]) if latest else ""
    actual_run = _text(latest["posterior_run_id"]) if latest else ""
    if actual_checkpoint != _text(expected_previous_checkpoint_id) or actual_run != _text(expected_previous_posterior_run_id):
        raise ShadowTransactionConflict(
            f"physical_posterior_transaction_previous_checkpoint_cas_mismatch:expected={expected_previous_checkpoint_id}/{expected_previous_posterior_run_id}:actual={actual_checkpoint}/{actual_run}"
        )
    if _text(latest["joint_posterior_sha256"]) != _text(update.get("previous_posterior_sha256")):
        raise ShadowTransactionConflict("physical_posterior_transaction_previous_posterior_sha256_cas_mismatch")
    conn.execute("SAVEPOINT sequential_bayes_shadow_physical_tx")
    try:
        _insert_update(conn, update)
        _insert_checkpoint(conn, checkpoint)
    except Exception:
        conn.execute("ROLLBACK TO sequential_bayes_shadow_physical_tx")
        conn.execute("RELEASE sequential_bayes_shadow_physical_tx")
        raise
    conn.execute("RELEASE sequential_bayes_shadow_physical_tx")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "idempotent": False,
        "shadow_only": True,
        "market_bucket_projection_state": "na",
        "ids": {"update": update_id, "checkpoint": checkpoint_id},
    }
