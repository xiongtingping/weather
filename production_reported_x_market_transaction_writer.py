"""Atomic successor Transaction B writer for X posterior + B projection + outbox.

This module is dormant while the resolution bridge activation is disabled.
It performs no inference, market fetch, signing, or CLOB call.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from lineage_identity_runtime import build_realization_state_id
from production_reported_x_public_ssot_writer import _assert_boundary
from production_sequential_bayes_public_ssot_writer import (
    ProductionTransactionIntegrityError,
    _insert,
    _insert_public_and_latest,
    _latest_checkpoint,
)
from reported_x_successor_checkpoint_runtime import (
    build_successor_public_ssot_surfaces,
    validate_successor_checkpoint_row,
)
from resolution_bridge_activation_runtime import validate_resolution_bridge_activation


SCHEMA_VERSION = "production_reported_x_market_transaction_writer_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(_text(value) or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha256(value: Any) -> bool:
    text = _text(value).lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _count(conn: sqlite3.Connection, table: str, column: str, identity: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (_text(identity),)).fetchone()[0])


def _validate_market_bundle(
    *,
    probability_activation: Mapping[str, Any],
    bridge_activation: Mapping[str, Any],
    expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str,
    strong_update_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    realization_state_row: Mapping[str, Any],
    outbox_row: Mapping[str, Any],
    outbox_execution_contract_row: Mapping[str, Any],
    public_fact_row: Mapping[str, Any],
) -> dict[str, str]:
    update = _mapping(strong_update_row)
    checkpoint = _mapping(checkpoint_row)
    realization = _mapping(realization_state_row)
    outbox = _mapping(outbox_row)
    execution = _mapping(outbox_execution_contract_row)
    public = _mapping(public_fact_row)
    bridge = validate_resolution_bridge_activation(bridge_activation)
    if _text(bridge.get("state")) != "production_enabled":
        raise ProductionTransactionIntegrityError("reported_x_market_bridge_activation_not_enabled")
    if validate_successor_checkpoint_row(checkpoint).get("ok") is not True:
        raise ProductionTransactionIntegrityError("reported_x_market_checkpoint_invalid")
    ids = {
        "update": _text(update.get("strong_observation_update_id")),
        "checkpoint": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization": _text(realization.get("realization_state_id")),
        "outbox": _text(outbox.get("outbox_event_id")),
    }
    if not all(ids.values()):
        raise ProductionTransactionIntegrityError("reported_x_market_bundle_identity_missing")
    if (
        _text(checkpoint.get("previous_checkpoint_id")) != _text(expected_previous_checkpoint_id)
        or _text(update.get("previous_checkpoint_id")) != _text(expected_previous_checkpoint_id)
        or _text(update.get("previous_posterior_run_id")) != _text(expected_previous_posterior_run_id)
    ):
        raise ProductionTransactionIntegrityError("reported_x_market_bundle_predecessor_mismatch")
    if (
        _text(update.get("target_identity")) != _text(checkpoint.get("target_identity"))
        or _text(checkpoint.get("forecast_evidence_snapshot_id")) != _text(update.get("forecast_evidence_snapshot_id"))
        or _text(checkpoint.get("prior_context_id")) != _text(update.get("prior_context_id"))
        or _text(checkpoint.get("last_strong_update_id")) != ids["update"]
        or _text(checkpoint.get("last_canonical_event_id")) != _text(update.get("canonical_observation_event_id"))
        or _text(checkpoint.get("model_artifact_sha256")) != _text(update.get("model_artifact_sha256"))
        or _text(update.get("model_artifact_sha256")) != _text(probability_activation.get("strong_lr_artifact_sha256"))
    ):
        raise ProductionTransactionIntegrityError("reported_x_market_bundle_probability_binding_mismatch")
    state_payload = _parse(realization.get("state_json"))
    projection = _mapping(state_payload.get("projection"))
    state_sha256 = _text(realization.get("state_sha256"))
    expected_state_id = build_realization_state_id(
        posterior_run_id=_text(realization.get("posterior_run_id")),
        fact_snapshot_id=_text(realization.get("fact_snapshot_id")),
        state_sha256=state_sha256,
    )
    if (
        _text(state_payload.get("schema")) != "realization_state_l3_v2"
        or not _sha256(state_sha256)
        or _sha(state_payload) != state_sha256
        or ids["realization"] != expected_state_id
        or _text(realization.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id"))
        or _text(projection.get("posterior_checkpoint_id")) != ids["checkpoint"]
        or _text(projection.get("physical_resolution_contract_id")) != _text(checkpoint.get("resolution_contract_id"))
        or _text(projection.get("resolution_contract_id")) != _text(realization.get("resolution_contract_id"))
        or _text(realization.get("current_report_bucket"))
        or _text(projection.get("current_report_bucket"))
    ):
        raise ProductionTransactionIntegrityError("reported_x_market_bundle_realization_binding_mismatch")
    if (
        _text(outbox.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id"))
        or _text(outbox.get("fact_snapshot_id")) != _text(realization.get("fact_snapshot_id"))
        or _text(execution.get("outbox_event_id")) != ids["outbox"]
        or _text(execution.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id"))
        or _text(execution.get("canonical_observation_event_id")) != _text(update.get("canonical_observation_event_id"))
        or _text(outbox.get("state")) != "pending"
        or int(outbox.get("attempt_count") or 0) != 0
        or int(outbox.get("claim_generation") or 0) != 0
        or not _text(outbox.get("correlation_id"))
        or _text(execution.get("deadline_policy")) != "canonical_first_actionable_plus_3000ms"
        or int(execution.get("execution_eligible_at_commit") or 0) not in {0, 1}
        or not all(_text(execution.get(field)) for field in (
            "first_actionable_seen_at",
            "observation_to_order_hard_deadline_at",
            "inference_decision_deadline_at",
            "transaction_b_committed_at",
        ))
    ):
        raise ProductionTransactionIntegrityError("reported_x_market_bundle_outbox_binding_mismatch")
    eligible = int(execution.get("execution_eligible_at_commit") or 0)
    if (eligible == 1 and _text(execution.get("execution_na_reason"))) or (
        eligible == 0
        and _text(execution.get("execution_na_reason"))
        != "transaction_b_committed_after_observation_to_order_hard_deadline"
    ):
        raise ProductionTransactionIntegrityError("reported_x_market_bundle_execution_eligibility_invalid")
    public_ssot = _mapping(_parse(public.get("fact_json")).get("posterior_ssot_fact"))
    probabilities = _mapping(public_ssot.get("market_bucket_probabilities"))
    try:
        normalized = bool(probabilities) and abs(sum(float(value) for value in probabilities.values()) - 1.0) <= 1e-8
    except (TypeError, ValueError):
        normalized = False
    surfaces = build_successor_public_ssot_surfaces(checkpoint)
    if (
        _text(public.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id"))
        or _text(public.get("fact_snapshot_id")) != _text(realization.get("fact_snapshot_id"))
        or _text(public_ssot.get("realization_state_id")) != ids["realization"]
        or _text(public_ssot.get("resolution_contract_id")) != _text(realization.get("resolution_contract_id"))
        or _text(public_ssot.get("physical_resolution_contract_id")) != _text(checkpoint.get("resolution_contract_id"))
        or _text(public_ssot.get("bridge_artifact_sha256")) != _text(bridge.get("artifact_sha256"))
        or not _sha256(public_ssot.get("bridge_artifact_sha256"))
        or not _sha256(public_ssot.get("target_contract_sha256"))
        or surfaces.get("ok") is not True
        or dict(_mapping(public_ssot.get("reported_physical_x_distribution")))
        != dict(_mapping(surfaces.get("reported_physical_x_distribution")))
        or _text(public_ssot.get("joint_posterior_sha256")) != _text(checkpoint.get("joint_posterior_sha256"))
        or _text(public_ssot.get("last_canonical_event_id")) != _text(checkpoint.get("last_canonical_event_id"))
        or _text(public_ssot.get("last_strong_update_id")) != ids["update"]
        or not normalized
        or _text(public_ssot.get("current_report_bucket"))
    ):
        raise ProductionTransactionIntegrityError("reported_x_market_bundle_public_ssot_binding_mismatch")
    return ids


def commit_reported_x_market_transaction_b(
    conn: sqlite3.Connection,
    *,
    probability_activation: Mapping[str, Any],
    bridge_activation: Mapping[str, Any],
    expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str,
    strong_update_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    realization_state_row: Mapping[str, Any],
    outbox_row: Mapping[str, Any],
    outbox_execution_contract_row: Mapping[str, Any],
    public_fact_row: Mapping[str, Any],
) -> dict[str, Any]:
    authorized = _assert_boundary(conn, probability_activation)
    checkpoint = dict(_mapping(checkpoint_row))
    if _text(checkpoint.get("state")) not in {"production_ready", "committed"}:
        raise ProductionTransactionIntegrityError("reported_x_market_checkpoint_state_invalid")
    checkpoint["state"] = "committed"
    ids = _validate_market_bundle(
        probability_activation=authorized,
        bridge_activation=bridge_activation,
        expected_previous_checkpoint_id=expected_previous_checkpoint_id,
        expected_previous_posterior_run_id=expected_previous_posterior_run_id,
        strong_update_row=strong_update_row,
        checkpoint_row=checkpoint,
        realization_state_row=realization_state_row,
        outbox_row=outbox_row,
        outbox_execution_contract_row=outbox_execution_contract_row,
        public_fact_row=public_fact_row,
    )
    specs = (
        ("strong_observation_updates", "strong_observation_update_id", ids["update"]),
        ("posterior_checkpoints", "posterior_checkpoint_id", ids["checkpoint"]),
        ("realization_states", "realization_state_id", ids["realization"]),
        ("posterior_commit_outbox", "outbox_event_id", ids["outbox"]),
        ("posterior_outbox_execution_contracts", "outbox_event_id", ids["outbox"]),
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = [_count(conn, *spec) for spec in specs]
        if any(existing) and not all(existing):
            raise ProductionTransactionIntegrityError("reported_x_market_transaction_partial_state_detected")
        if not any(existing):
            latest = _latest_checkpoint(
                conn,
                _text(checkpoint.get("target_identity")),
                _text(authorized.get("activation_contract_sha256")),
            )
            if (
                latest is None
                or _text(latest["posterior_checkpoint_id"]) != _text(expected_previous_checkpoint_id)
                or _text(latest["posterior_run_id"]) != _text(expected_previous_posterior_run_id)
            ):
                raise ProductionTransactionIntegrityError("reported_x_market_transaction_predecessor_cas_failed")
            if _text(latest["joint_posterior_sha256"] if latest else "") != _text(
                _mapping(strong_update_row).get("previous_posterior_sha256")
            ):
                raise ProductionTransactionIntegrityError(
                    "reported_x_market_transaction_previous_posterior_sha256_cas_failed"
                )
            predecessor = conn.execute(
                """SELECT forecast_evidence_snapshot_id,prior_context_id,resolution_contract_id,
                          model_artifact_sha256,replay_generation
                   FROM posterior_checkpoints WHERE posterior_checkpoint_id=?""",
                (_text(expected_previous_checkpoint_id),),
            ).fetchone()
            if predecessor is None:
                raise ProductionTransactionIntegrityError(
                    "reported_x_market_transaction_predecessor_missing"
                )
            frozen = (
                ("forecast_evidence_snapshot_id", 0),
                ("prior_context_id", 1),
                ("resolution_contract_id", 2),
                ("model_artifact_sha256", 3),
            )
            if any(
                _text(checkpoint.get(field)) != _text(predecessor[index])
                for field, index in frozen
            ) or int(checkpoint.get("replay_generation") or 0) != int(predecessor[4] or 0):
                raise ProductionTransactionIntegrityError(
                    "reported_x_market_transaction_frozen_epoch_context_drift"
                )
            lineage = conn.execute(
                """SELECT prior_model_artifact_sha256,weak_likelihood_artifact_sha256,
                          strong_lr_artifact_sha256,activation_contract_sha256
                   FROM production_initial_posterior_lineage
                   WHERE target_identity=? AND activation_contract_sha256=?""",
                (
                    _text(checkpoint.get("target_identity")),
                    _text(authorized.get("activation_contract_sha256")),
                ),
            ).fetchall()
            expected_lineage = (
                _text(authorized.get("prior_model_artifact_sha256")),
                _text(authorized.get("weak_likelihood_artifact_sha256")),
                _text(authorized.get("strong_lr_artifact_sha256")),
                _text(authorized.get("activation_contract_sha256")),
            )
            if len(lineage) != 1 or tuple(_text(item) for item in lineage[0]) != expected_lineage:
                raise ProductionTransactionIntegrityError(
                    "reported_x_market_transaction_initial_lineage_mismatch"
                )
            _insert(conn, "strong_observation_updates", strong_update_row)
            _insert(conn, "posterior_checkpoints", checkpoint)
            _insert(conn, "realization_states", realization_state_row)
            _insert(conn, "posterior_commit_outbox", outbox_row)
            _insert(conn, "posterior_outbox_execution_contracts", outbox_execution_contract_row)
        latest = _latest_checkpoint(
            conn,
            _text(checkpoint.get("target_identity")),
            _text(authorized.get("activation_contract_sha256")),
        )
        publish_latest = _text(latest["posterior_checkpoint_id"] if latest else "") == ids["checkpoint"]
        fact_id, public_idempotent, latest_repointed = _insert_public_and_latest(
            conn, public_fact_row, update_latest=publish_latest
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "committed",
        "idempotent": bool(any(existing) and public_idempotent),
        "fact_id": fact_id,
        "ids": ids,
        "latest_repointed": latest_repointed,
        "production": True,
        "places_orders": False,
        "clob": False,
    }


__all__ = ["SCHEMA_VERSION", "commit_reported_x_market_transaction_b"]
