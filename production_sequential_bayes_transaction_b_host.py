"""Resident, no-network host for the sole production Transaction-B commit.

Inference must complete before this boundary.  The host consumes only a
frozen candidate and its persisted predecessor, then delegates the single
durable operation to the production public-SSOT writer.  It has no market,
strategy, signer, or CLOB dependency.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Mapping

from production_probability_activation_contract import (
    DEFAULT_PATH,
    load_production_probability_activation,
)
from production_sequential_bayes_public_ssot_writer import (
    commit_production_public_physical_ssot_transaction,
    commit_production_public_ssot_transaction_b,
    materialize_production_public_physical_posterior_fact_row,
    materialize_production_public_posterior_fact_row,
)


SCHEMA_VERSION = "production_sequential_bayes_transaction_b_host_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class ProductionSequentialBayesTransactionBHost:
    """Serializes commits; activation is loaded once from a frozen contract."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        activation: Mapping[str, Any] | None = None,
        activation_path: str = str(DEFAULT_PATH),
    ) -> None:
        self._conn = conn
        self._activation = _mapping(activation) if activation is not None else load_production_probability_activation(activation_path)
        self._lock = threading.Lock()

    def commit_candidate(
        self,
        *,
        candidate: Mapping[str, Any],
        previous_checkpoint: Mapping[str, Any],
        slug: str,
        event_date: str,
        fact_as_of: str,
    ) -> dict[str, Any]:
        """Publish one frozen bridge-closed candidate and its outbox atomically."""

        value, previous = _mapping(candidate), _mapping(previous_checkpoint)
        if (
            _text(value.get("state")) != "candidate_ready"
            or _text(value.get("candidate_mode")) != "production"
            or value.get("shadow_only") is not False
        ):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_transaction_b_candidate_not_ready", "places_orders": False}
        checkpoint, realization, outbox, contract = (
            _mapping(value.get("checkpoint_row")), _mapping(value.get("realization_state_row")),
            _mapping(value.get("outbox_row")), _mapping(value.get("outbox_execution_contract_row")),
        )
        if _text(checkpoint.get("state")) != "production_ready":
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_transaction_b_shared_candidate_state_invalid", "places_orders": False}
        if _text(checkpoint.get("previous_checkpoint_id")) != _text(previous.get("posterior_checkpoint_id")) or _text(outbox.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_transaction_b_candidate_predecessor_or_outbox_binding_mismatch", "places_orders": False}
        public = materialize_production_public_posterior_fact_row(
            activation=self._activation, strong_update_row=_mapping(value.get("strong_update_row")), checkpoint_row=checkpoint, realization_state_row=realization, slug=slug,
            event_date=event_date, fact_as_of=fact_as_of,
            correlation_id=_text(outbox.get("correlation_id")),
            resident_fast_lane_prewarm_dependency_identity=_text(value.get("resident_fast_lane_prewarm_dependency_identity")),
        )
        if not self._lock.acquire(blocking=False):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_transaction_b_concurrent_commit_blocked", "places_orders": False}
        try:
            result = commit_production_public_ssot_transaction_b(
                self._conn, activation=self._activation,
                expected_previous_checkpoint_id=_text(previous.get("posterior_checkpoint_id")),
                expected_previous_posterior_run_id=_text(previous.get("posterior_run_id")),
                strong_update_row=_mapping(value.get("strong_update_row")), checkpoint_row=checkpoint,
                realization_state_row=realization, outbox_row=outbox,
                outbox_execution_contract_row=contract, public_fact_row=public,
            )
        finally:
            self._lock.release()
        return {"schema": SCHEMA_VERSION, "ok": result.get("ok") is True, "state": "committed" if result.get("ok") else "blocked", "transaction_b": result, "places_orders": False, "clob": False}

    def commit_physical_only_candidate(
        self,
        *,
        candidate: Mapping[str, Any],
        previous_checkpoint: Mapping[str, Any],
        slug: str,
        event_date: str,
        fact_as_of: str,
        fact_snapshot_id: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Publish X-only posterior when bridge is unavailable; never creates outbox."""

        value, previous = _mapping(candidate), _mapping(previous_checkpoint)
        if (
            _text(value.get("state")) != "physical_only_candidate_ready"
            or _text(value.get("candidate_mode")) != "production"
            or value.get("shadow_only") is not False
        ):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_physical_candidate_not_ready", "places_orders": False}
        checkpoint = _mapping(value.get("checkpoint_row"))
        if _text(checkpoint.get("state")) != "shadow_ready":
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_physical_shared_candidate_state_invalid", "places_orders": False}
        checkpoint["state"] = "production_ready"
        if _text(checkpoint.get("previous_checkpoint_id")) != _text(previous.get("posterior_checkpoint_id")):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_physical_candidate_predecessor_binding_mismatch", "places_orders": False}
        public = materialize_production_public_physical_posterior_fact_row(
            activation=self._activation, strong_update_row=_mapping(value.get("strong_update_row")), checkpoint_row=checkpoint, fact_snapshot_id=fact_snapshot_id, slug=slug, event_date=event_date,
            fact_as_of=fact_as_of, correlation_id=correlation_id,
            market_bucket_probabilities_na_reason=_text(value.get("market_bucket_probabilities_na_reason")),
            resident_fast_lane_prewarm_dependency_identity=_text(value.get("resident_fast_lane_prewarm_dependency_identity")),
        )
        if not self._lock.acquire(blocking=False):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_transaction_b_concurrent_commit_blocked", "places_orders": False}
        try:
            result = commit_production_public_physical_ssot_transaction(
                self._conn, activation=self._activation,
                expected_previous_checkpoint_id=_text(previous.get("posterior_checkpoint_id")),
                expected_previous_posterior_run_id=_text(previous.get("posterior_run_id")),
                strong_update_row=_mapping(value.get("strong_update_row")), checkpoint_row=checkpoint,
                public_fact_row=public,
            )
        finally:
            self._lock.release()
        return {"schema": SCHEMA_VERSION, "ok": result.get("ok") is True, "state": "committed_physical_only" if result.get("ok") else "blocked", "transaction_b": result, "places_orders": False, "clob": False}


__all__ = ["SCHEMA_VERSION", "ProductionSequentialBayesTransactionBHost"]
