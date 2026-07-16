"""Atomic v6 Transaction B for X posterior, B projection and outbox.

The writer performs no inference, network I/O, signing or CLOB call.  The
physical-only writer remains the sole branch when the bridge/context is
missing; this market branch requires an independently enabled bridge.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from lineage_identity_runtime import build_realization_state_id
from production_probability_v6_public_ssot_writer import (
    V6TransactionIntegrityError,
    _assert_boundary,
    _assert_bundle,
    _insert,
    _mapping,
    _parse,
    _text,
    _upsert_public,
    _validate_reported_distribution,
)
from resolution_bridge_activation_runtime import validate_resolution_bridge_activation


SCHEMA_VERSION = "production_probability_v6_market_transaction_writer_v1"


def _count(conn: sqlite3.Connection, table: str, column: str, identity: str) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (_text(identity),)
        ).fetchone()[0]
    )


def _validate_bundle(
    *,
    bridge_activation: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    update: Mapping[str, Any],
    realization: Mapping[str, Any],
    outbox: Mapping[str, Any],
    execution: Mapping[str, Any],
    public: Mapping[str, Any],
) -> dict[str, str]:
    bridge = validate_resolution_bridge_activation(bridge_activation)
    if _text(bridge.get("state")) != "production_enabled":
        raise V6TransactionIntegrityError("v6_market_bridge_activation_not_enabled")
    ids = {
        "update": _text(update.get("strong_observation_update_id")),
        "checkpoint": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization": _text(realization.get("realization_state_id")),
        "outbox": _text(outbox.get("outbox_event_id")),
    }
    if not all(ids.values()):
        raise V6TransactionIntegrityError("v6_market_bundle_identity_missing")
    state_payload = _parse(realization.get("state_json"), {})
    projection = _mapping(_mapping(state_payload).get("projection"))
    state_sha = _text(realization.get("state_sha256"))
    expected_realization = build_realization_state_id(
        posterior_run_id=_text(realization.get("posterior_run_id")),
        fact_snapshot_id=_text(realization.get("fact_snapshot_id")),
        state_sha256=state_sha,
    )
    import hashlib

    canonical_state = json.dumps(
        state_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    if (
        _text(_mapping(state_payload).get("schema")) != "realization_state_l3_v2"
        or hashlib.sha256(canonical_state.encode("utf-8")).hexdigest() != state_sha
        or ids["realization"] != expected_realization
        or _text(realization.get("posterior_run_id"))
        != _text(checkpoint.get("posterior_run_id"))
        or _text(projection.get("posterior_checkpoint_id")) != ids["checkpoint"]
        or _text(projection.get("physical_resolution_contract_id"))
        != _text(checkpoint.get("resolution_contract_id"))
        or _text(projection.get("resolution_contract_id"))
        != _text(realization.get("resolution_contract_id"))
        or _text(projection.get("current_report_bucket"))
    ):
        raise V6TransactionIntegrityError("v6_market_realization_binding_invalid")
    if (
        _text(outbox.get("posterior_run_id"))
        != _text(checkpoint.get("posterior_run_id"))
        or _text(outbox.get("fact_snapshot_id"))
        != _text(realization.get("fact_snapshot_id"))
        or _text(execution.get("outbox_event_id")) != ids["outbox"]
        or _text(execution.get("posterior_run_id"))
        != _text(checkpoint.get("posterior_run_id"))
        or _text(execution.get("canonical_observation_event_id"))
        != _text(update.get("canonical_observation_event_id"))
        or _text(outbox.get("state")) != "pending"
        or int(outbox.get("attempt_count") or 0) != 0
        or int(outbox.get("claim_generation") or 0) != 0
        or _text(execution.get("deadline_policy"))
        != "canonical_first_actionable_plus_3000ms"
        or int(execution.get("execution_eligible_at_commit") or 0) not in {0, 1}
    ):
        raise V6TransactionIntegrityError("v6_market_outbox_binding_invalid")
    public_payload = _mapping(_parse(public.get("fact_json"), {}))
    ssot = _mapping(public_payload.get("posterior_ssot_fact"))
    probabilities = _mapping(ssot.get("market_bucket_probabilities"))
    try:
        normalized = bool(probabilities) and abs(
            sum(float(value) for value in probabilities.values()) - 1.0
        ) <= 1e-8
    except (TypeError, ValueError):
        normalized = False
    reported = _validate_reported_distribution(checkpoint)
    if (
        _text(public.get("posterior_run_id"))
        != _text(checkpoint.get("posterior_run_id"))
        or _text(public.get("fact_snapshot_id"))
        != _text(realization.get("fact_snapshot_id"))
        or _text(ssot.get("realization_state_id")) != ids["realization"]
        or _text(ssot.get("resolution_contract_id"))
        != _text(realization.get("resolution_contract_id"))
        or _text(ssot.get("physical_resolution_contract_id"))
        != _text(checkpoint.get("resolution_contract_id"))
        or _text(ssot.get("bridge_artifact_sha256"))
        != _text(bridge.get("artifact_sha256"))
        or dict(_mapping(ssot.get("reported_physical_x_distribution"))) != reported
        or _text(ssot.get("posterior_checkpoint_id")) != ids["checkpoint"]
        or _text(ssot.get("last_canonical_event_id"))
        != _text(checkpoint.get("last_canonical_event_id"))
        or _text(ssot.get("last_strong_update_id")) != ids["update"]
        or _text(ssot.get("current_report_bucket"))
        or not normalized
    ):
        raise V6TransactionIntegrityError("v6_market_public_ssot_binding_invalid")
    return ids


def commit_v6_market_transaction_b(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
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
    authorized = _assert_boundary(conn, activation)
    required = (
        "realization_states",
        "posterior_commit_outbox",
        "posterior_outbox_execution_contracts",
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
        raise V6TransactionIntegrityError(
            "v6_market_transaction_tables_missing:" + ",".join(missing)
        )
    update = dict(_mapping(strong_update_row))
    checkpoint = dict(_mapping(checkpoint_row))
    realization = dict(_mapping(realization_state_row))
    outbox = dict(_mapping(outbox_row))
    execution = dict(_mapping(outbox_execution_contract_row))
    public = dict(_mapping(public_fact_row))
    if _text(checkpoint.get("state")) not in {
        "production_successor_ready",
        "committed",
    }:
        raise V6TransactionIntegrityError("v6_market_checkpoint_state_invalid")
    checkpoint["state"] = "committed"
    ids = _validate_bundle(
        bridge_activation=bridge_activation,
        checkpoint=checkpoint,
        update=update,
        realization=realization,
        outbox=outbox,
        execution=execution,
        public=public,
    )
    specs = (
        ("strong_observation_updates", "strong_observation_update_id", ids["update"]),
        ("posterior_checkpoints", "posterior_checkpoint_id", ids["checkpoint"]),
        ("realization_states", "realization_state_id", ids["realization"]),
        ("posterior_commit_outbox", "outbox_event_id", ids["outbox"]),
        (
            "posterior_outbox_execution_contracts",
            "outbox_event_id",
            ids["outbox"],
        ),
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = [_count(conn, *spec) for spec in specs]
        if any(existing) and not all(existing):
            raise V6TransactionIntegrityError(
                "v6_market_transaction_partial_state_detected"
            )
        if not any(existing):
            _assert_bundle(
                conn,
                authorized=authorized,
                expected_previous_checkpoint_id=expected_previous_checkpoint_id,
                expected_previous_posterior_run_id=expected_previous_posterior_run_id,
                update=update,
                checkpoint=checkpoint,
            )
            _insert(conn, "strong_observation_updates", update)
            _insert(conn, "posterior_checkpoints", checkpoint)
            _insert(conn, "realization_states", realization)
            _insert(conn, "posterior_commit_outbox", outbox)
            _insert(conn, "posterior_outbox_execution_contracts", execution)
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
        "idempotent": bool(all(existing) and public_idempotent),
        "ids": ids,
        "fact_id": fact_id,
        "creates_outbox": True,
        "production": True,
        "places_orders": False,
        "clob": False,
    }


__all__ = ["SCHEMA_VERSION", "commit_v6_market_transaction_b"]
