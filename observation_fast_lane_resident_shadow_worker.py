"""Resident, isolated shadow core for canonical observation → P8/P9 fast lane.

Network polling belongs before T1 and is deliberately absent.  The worker
accepts an already received source observation, persists its canonical fact,
then dispatches the one existing P8→P9 shadow path in the same process.  It
never uses a generic queue, starts a subprocess, refreshes forecasts, calls a
signer, or submits an order.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import threading
from typing import Any, Callable, Mapping

from canonical_observation_runtime import canonicalize_observations
from canonical_observation_writer import persist_canonical_observation_proposal
from observation_fast_lane_contract import (
    HARD_EXECUTION_DEADLINE_MS,
    validate_observation_fast_lane_topology,
)
from observation_fast_lane_shadow_orchestrator import run_observation_fast_lane_shadow
from resident_fast_lane_prewarm_contract import validate_resident_fast_lane_prewarm
from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)


SCHEMA_VERSION = "observation_fast_lane_resident_shadow_worker_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ValueError("resident_fast_lane_shadow_manifest_missing")
    row = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ValueError("resident_fast_lane_shadow_manifest_invalid")


def _existing_events(conn: sqlite3.Connection, *, target_identity: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT canonical_observation_event_id, canonical_observation_key,
               normalized_physical_sha256, revision_no, state, accepted_at,
               selected_source_id
        FROM canonical_observation_events
        WHERE target_identity=?
        """,
        (_text(target_identity),),
    ).fetchall()
    return [
        {
            "canonical_observation_event_id": _text(row[0]),
            "canonical_observation_key": _text(row[1]),
            "normalized_physical_sha256": _text(row[2]),
            "revision_no": int(row[3] or 0),
            "state": _text(row[4]),
            "accepted_at": _text(row[5]),
            "selected_source_id": _text(row[6]),
        }
        for row in rows
    ]


def _materialized(conn: sqlite3.Connection, *, canonical_observation_event_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM strong_observation_updates WHERE canonical_observation_event_id=? LIMIT 1",
        (_text(canonical_observation_event_id),),
    ).fetchone()
    return row is not None


class ResidentObservationFastLaneShadowWorker:
    """Single-process shadow dispatcher with immutable hot-path boundaries."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        topology: Mapping[str, Any],
        prewarmed_components: Mapping[str, Any],
        runner: Callable[..., Mapping[str, Any]] = run_observation_fast_lane_shadow,
        timestamp_observer: Callable[[], str] | None = None,
    ) -> None:
        _assert_isolated_shadow_db(conn)
        topology_result = validate_observation_fast_lane_topology(
            topology,
            require_execution_readiness=False,
        )
        if topology_result.get("ok") is not True:
            raise ValueError("resident_fast_lane_topology_invalid")
        prewarm = validate_resident_fast_lane_prewarm(prewarmed_components)
        if prewarm.get("ok") is not True:
            raise ValueError(
                "resident_fast_lane_prewarm_invalid:"
                + ",".join(str(value) for value in prewarm.get("blockers") or [])
            )
        self._conn = conn
        self._topology = dict(topology)
        self._prewarmed_components = _mapping(prewarmed_components)
        self._prewarm_dependency_identity = _text(prewarm.get("dependency_identity_sha256"))
        self._runner = runner
        self._timestamp_observer = timestamp_observer or _utc_now_iso
        self._lock = threading.Lock()
        self._completed_event_ids: set[str] = set()

    def process_source_observation(
        self,
        *,
        source_observation: Mapping[str, Any],
        target_identity: str,
        event_date: str,
        decision_as_of: str,
        canonicalized_at: str,
        posterior_transaction_inputs: Mapping[str, Any],
        decision_cycle_inputs: Mapping[str, Any],
        allow_awc_substitute: bool = False,
    ) -> dict[str, Any]:
        """Persist one canonical fact then run at most one isolated fast-lane update.

        The canonical write is intentionally committed before Transaction B so
        a later artifact/decision block cannot discard a received observation.
        ``decision_as_of`` and ``canonicalized_at`` remain ingress causality
        inputs only; neither is used as P10 telemetry or to authorize a
        future observation. The resident clock supplies the canonicalization
        cutoff and captures T2 immediately after canonical append returns.
        """

        if not self._lock.acquire(blocking=False):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "resident_fast_lane_concurrent_dispatch_blocked",
                "shadow_only": True,
                "places_orders": False,
            }
        try:
            dispatch_observed_at = _text(self._timestamp_observer())
            if _parse_dt(dispatch_observed_at) is None:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "resident_fast_lane_dispatch_clock_invalid",
                    "shadow_only": True,
                    "places_orders": False,
                }
            existing = _existing_events(self._conn, target_identity=target_identity)
            proposal = canonicalize_observations(
                [source_observation],
                target_identity=target_identity,
                event_date=event_date,
                decision_as_of=dispatch_observed_at,
                allow_awc_substitute=allow_awc_substitute,
                existing_events=existing,
            )
            if proposal.get("rejected"):
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "resident_fast_lane_canonical_rejected",
                    "canonical_proposal": proposal,
                    "shadow_only": True,
                    "places_orders": False,
                }
            self._conn.execute("SAVEPOINT resident_fast_lane_canonical_append")
            try:
                persisted = persist_canonical_observation_proposal(self._conn, proposal)
            except Exception:
                self._conn.execute("ROLLBACK TO resident_fast_lane_canonical_append")
                self._conn.execute("RELEASE resident_fast_lane_canonical_append")
                raise
            self._conn.execute("RELEASE resident_fast_lane_canonical_append")
            canonical_persisted_at = _text(self._timestamp_observer())

            candidate_ids = {
                _text(event.get("canonical_observation_event_id"))
                for event in proposal.get("events") or []
                if _text(event.get("state")).startswith("accepted")
            }
            candidate_ids.update(
                _text(relation.get("canonical_observation_event_id"))
                for relation in proposal.get("relations") or []
                if _text(relation.get("canonical_observation_event_id"))
            )
            candidate_ids.discard("")
            accepted_ids = [
                event_id
                for event_id in sorted(candidate_ids)
                if any(row["canonical_observation_event_id"] == event_id and row["state"].startswith("accepted") for row in _existing_events(self._conn, target_identity=target_identity))
                or any(
                    _text(event.get("canonical_observation_event_id")) == event_id
                    and _text(event.get("state")).startswith("accepted")
                    for event in proposal.get("events") or []
                )
            ]
            if not accepted_ids:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": True,
                    "state": "canonical_persisted_no_accepted_event",
                    "canonical_proposal": proposal,
                    "canonical_persistence": persisted,
                    "canonicalized_at": canonical_persisted_at,
                    "shadow_only": True,
                    "places_orders": False,
                }
            if len(accepted_ids) != 1:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "resident_fast_lane_single_observation_mapped_to_multiple_accepted_events",
                    "canonical_proposal": proposal,
                    "canonical_persistence": persisted,
                    "canonicalized_at": canonical_persisted_at,
                    "shadow_only": True,
                    "places_orders": False,
                }
            canonical_event_id = accepted_ids[0]
            if canonical_event_id in self._completed_event_ids or _materialized(
                self._conn, canonical_observation_event_id=canonical_event_id
            ):
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": True,
                    "state": "canonical_event_already_materialized",
                    "canonical_observation_event_id": canonical_event_id,
                    "canonical_proposal": proposal,
                    "canonical_persistence": persisted,
                    "canonicalized_at": canonical_persisted_at,
                    "shadow_only": True,
                    "places_orders": False,
                }
            event_row = next(
                (
                    event
                    for event in proposal.get("events") or []
                    if _text(event.get("canonical_observation_event_id")) == canonical_event_id
                ),
                None,
            )
            if not isinstance(event_row, Mapping):
                event_row = next(
                    (event for event in _existing_events(self._conn, target_identity=target_identity) if event["canonical_observation_event_id"] == canonical_event_id),
                    {},
                )
            first_actionable_seen_at = _text(event_row.get("first_actionable_seen_at"))
            if not first_actionable_seen_at:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "resident_fast_lane_canonical_actionable_time_missing",
                    "canonical_observation_event_id": canonical_event_id,
                    "shadow_only": True,
                    "places_orders": False,
                }
            parsed_t1 = _parse_dt(first_actionable_seen_at)
            parsed_t2 = _parse_dt(canonical_persisted_at)
            if parsed_t1 is None or parsed_t2 is None or parsed_t2 < parsed_t1:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "canonical_persisted_fast_lane_blocked",
                    "reason": "resident_fast_lane_canonical_persistence_timestamp_invalid",
                    "canonical_observation_event_id": canonical_event_id,
                    "canonical_proposal": proposal,
                    "canonical_persistence": persisted,
                    "canonicalized_at": canonical_persisted_at,
                    "shadow_only": True,
                    "places_orders": False,
                }
            if (parsed_t2 - parsed_t1).total_seconds() * 1000.0 > HARD_EXECUTION_DEADLINE_MS:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "canonical_persisted_fast_lane_blocked",
                    "reason": "resident_fast_lane_observation_hard_deadline_exceeded_before_transaction_b",
                    "canonical_observation_event_id": canonical_event_id,
                    "canonical_proposal": proposal,
                    "canonical_persistence": persisted,
                    "canonicalized_at": canonical_persisted_at,
                    "shadow_only": True,
                    "places_orders": False,
                }
            runtime_prewarm = validate_resident_fast_lane_prewarm(
                self._prewarmed_components,
                decision_as_of=canonical_persisted_at,
            )
            if runtime_prewarm.get("ok") is not True:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "canonical_persisted_fast_lane_blocked",
                    "reason": "resident_fast_lane_prewarm_stale_or_invalid",
                    "canonical_observation_event_id": canonical_event_id,
                    "canonical_proposal": proposal,
                    "canonical_persistence": persisted,
                    "canonicalized_at": canonical_persisted_at,
                    "prewarm": runtime_prewarm,
                    "shadow_only": True,
                    "places_orders": False,
                }
            posterior_inputs = _mapping(posterior_transaction_inputs)
            if posterior_inputs.get("shadow_only") is not True:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "resident_fast_lane_shadow_only_posterior_input_required",
                    "shadow_only": True,
                    "places_orders": False,
                }
            supplied_id = _text(posterior_inputs.get("canonical_observation_event_id"))
            if supplied_id and supplied_id != canonical_event_id:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "resident_fast_lane_canonical_event_binding_mismatch",
                    "canonical_observation_event_id": canonical_event_id,
                    "shadow_only": True,
                    "places_orders": False,
                }
            supplied_prewarm_identity = _text(
                posterior_inputs.get("resident_fast_lane_prewarm_dependency_identity")
            )
            if (
                supplied_prewarm_identity
                and supplied_prewarm_identity != self._prewarm_dependency_identity
            ):
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "canonical_persisted_fast_lane_blocked",
                    "reason": "resident_fast_lane_prewarm_identity_binding_mismatch",
                    "canonical_observation_event_id": canonical_event_id,
                    "canonical_proposal": proposal,
                    "canonical_persistence": persisted,
                    "canonicalized_at": canonical_persisted_at,
                    "prewarm": runtime_prewarm,
                    "shadow_only": True,
                    "places_orders": False,
                }
            posterior_inputs["canonical_observation_event_id"] = canonical_event_id
            posterior_inputs[
                "resident_fast_lane_prewarm_dependency_identity"
            ] = self._prewarm_dependency_identity
            result = dict(
                self._runner(
                    self._conn,
                    first_actionable_seen_at=first_actionable_seen_at,
                    canonicalized_at=canonical_persisted_at,
                    topology=self._topology,
                    posterior_transaction_inputs=posterior_inputs,
                    decision_cycle_inputs=_mapping(decision_cycle_inputs),
                )
            )
            if result.get("ok") is True:
                self._completed_event_ids.add(canonical_event_id)
            return {
                "schema": SCHEMA_VERSION,
                "canonical_observation_event_id": canonical_event_id,
                "canonical_proposal": proposal,
                "canonical_persistence": persisted,
                "canonicalized_at": canonical_persisted_at,
                "dispatch_observed_at": dispatch_observed_at,
                "fast_lane": result,
                "ok": result.get("ok") is True,
                "state": "shadow_fast_lane_committed" if result.get("ok") is True else "canonical_persisted_fast_lane_blocked",
                "shadow_only": True,
                "places_orders": False,
                "boundary": {
                    "network_polling_before_t1_not_owned": True,
                    "no_subprocess_per_event": True,
                    "no_synchronous_forecast_refresh": True,
                    "no_generic_task_queue": True,
                    "canonical_fact_retained_when_transaction_b_blocks": True,
                    "canonicalized_at_captured_after_append": True,
                    "caller_canonicalized_at_not_used_for_p10_telemetry": True,
                    "caller_decision_as_of_not_used_for_canonicalization_cutoff": True,
                },
            }
        finally:
            self._lock.release()


__all__ = [
    "SCHEMA_VERSION",
    "ResidentObservationFastLaneShadowWorker",
]
