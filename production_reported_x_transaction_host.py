"""Resident physical-only commit host for the reported-X successor epoch."""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Mapping

from production_reported_x_public_ssot_writer import (
    commit_reported_x_public_physical_transaction,
    materialize_reported_x_public_physical_fact_row,
)


SCHEMA_VERSION = "production_reported_x_transaction_host_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class ProductionReportedXTransactionHost:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        activation: Mapping[str, Any],
    ) -> None:
        self._conn = conn
        self._activation = _mapping(activation)
        self._lock = threading.Lock()

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
        value = _mapping(candidate)
        previous = _mapping(previous_checkpoint)
        if (
            _text(value.get("state")) != "physical_only_candidate_ready"
            or _text(value.get("candidate_mode")) != "production"
            or value.get("shadow_only") is not False
        ):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "reported_x_candidate_not_ready",
                "places_orders": False,
            }
        checkpoint = _mapping(value.get("checkpoint_row"))
        if _text(checkpoint.get("state")) != "shadow_ready":
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "reported_x_shared_candidate_state_invalid",
                "places_orders": False,
            }
        checkpoint["state"] = "production_ready"
        if _text(checkpoint.get("previous_checkpoint_id")) != _text(
            previous.get("posterior_checkpoint_id")
        ):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "reported_x_candidate_predecessor_mismatch",
                "places_orders": False,
            }
        public = materialize_reported_x_public_physical_fact_row(
            activation=self._activation,
            strong_update_row=_mapping(value.get("strong_update_row")),
            checkpoint_row=checkpoint,
            fact_snapshot_id=fact_snapshot_id,
            slug=slug,
            event_date=event_date,
            fact_as_of=fact_as_of,
            correlation_id=correlation_id,
            market_bucket_probabilities_na_reason=_text(
                value.get("market_bucket_probabilities_na_reason")
            ),
            resident_fast_lane_prewarm_dependency_identity=_text(
                value.get("resident_fast_lane_prewarm_dependency_identity")
            ),
        )
        if not self._lock.acquire(blocking=False):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "reported_x_concurrent_commit_blocked",
                "places_orders": False,
            }
        try:
            committed = commit_reported_x_public_physical_transaction(
                self._conn,
                activation=self._activation,
                expected_previous_checkpoint_id=_text(
                    previous.get("posterior_checkpoint_id")
                ),
                expected_previous_posterior_run_id=_text(
                    previous.get("posterior_run_id")
                ),
                strong_update_row=_mapping(value.get("strong_update_row")),
                checkpoint_row=checkpoint,
                public_fact_row=public,
            )
        finally:
            self._lock.release()
        return {
            "schema": SCHEMA_VERSION,
            "ok": committed.get("ok") is True,
            "state": "committed_physical_only"
            if committed.get("ok") is True
            else "blocked",
            "transaction_b": committed,
            "places_orders": False,
            "clob": False,
        }


__all__ = ["ProductionReportedXTransactionHost", "SCHEMA_VERSION"]
