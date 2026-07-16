"""Resident host for one fully prepared, isolated P10 shadow cycle.

The host owns no network acquisition, forecast refresh, model training, signer,
or CLOB call.  An external source adapter supplies one already received AMSC
observation; this host uses only prewarmed, immutable cycle inputs to execute
the existing journal → ingress → P8 → P9 shadow path in one resident process.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import threading
from typing import Any, Callable, Mapping

from amsc_specli_resident_shadow_ingress import AmscSpecliResidentShadowIngress
from observation_fast_lane_resident_shadow_worker import (
    ResidentObservationFastLaneShadowWorker,
)
from observation_fast_lane_shadow_orchestrator import run_observation_fast_lane_shadow
from resident_amsc_specli_arrival_journal_shadow import (
    ResidentAmscSpecliArrivalJournalShadow,
)
from resident_p10_shadow_prepared_cycle import (
    DEFAULT_TOPOLOGY,
    load_resident_p10_shadow_cycle,
    validate_resident_p10_shadow_cycle,
)
from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)


SCHEMA_VERSION = "resident_p10_shadow_host_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_shadow_host_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ValueError("resident_p10_shadow_host_manifest_missing")
    manifest = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if manifest is None or _text(manifest[0]) != SHADOW_MANIFEST_STATE:
        raise ValueError("resident_p10_shadow_host_manifest_invalid")
    required = (
        "canonical_observation_events",
        "posterior_checkpoints",
        "posterior_commit_outbox",
        "event_fact_history",
        "event_fact_latest",
        "trading_plans",
        "auto_trade_decisions",
        "decision_intents",
    )
    missing = [table for table in required if not _table_exists(conn, table)]
    if missing:
        raise ValueError("resident_p10_shadow_host_required_tables_missing:" + ",".join(missing))


def _checkpoint_persisted(conn: sqlite3.Connection, checkpoint: Mapping[str, Any]) -> bool:
    row = conn.execute(
        """
        SELECT posterior_run_id,joint_posterior_sha256
        FROM posterior_checkpoints WHERE posterior_checkpoint_id=?
        """,
        (_text(checkpoint.get("posterior_checkpoint_id")),),
    ).fetchone()
    return bool(
        row
        and _text(row[0]) == _text(checkpoint.get("posterior_run_id"))
        and _text(row[1]) == _text(checkpoint.get("joint_posterior_sha256"))
    )


def _parse_json_list(value: Any) -> list[Any] | None:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError):
        return None
    return list(parsed) if isinstance(parsed, list) else None


def _load_latest_persisted_checkpoint(
    conn: sqlite3.Connection,
    *,
    target_identity: str,
    event_date: str,
) -> dict[str, Any] | None:
    """Rebuild the next-update runtime state from durable P1 fields only."""

    row = conn.execute(
        """
        SELECT posterior_checkpoint_id,posterior_run_id,target_identity,
               joint_posterior_json,joint_posterior_sha256,observed_max_lower_bound_c,
               consumed_event_ids_json,consumed_ancestor_sha256,checkpoint_kind,
               forecast_evidence_snapshot_id,prior_context_id,resolution_contract_id,
               model_artifact_sha256
        FROM posterior_checkpoints
        WHERE target_identity=?
        ORDER BY replay_generation DESC, rowid DESC
        LIMIT 1
        """,
        (_text(target_identity),),
    ).fetchone()
    if row is None:
        return None
    joint = _parse_json_list(row[3])
    consumed = _parse_json_list(row[6])
    if joint is None or consumed is None or _sha256(joint) != _text(row[4]):
        raise ValueError("resident_p10_shadow_persisted_checkpoint_payload_invalid")
    consumed_ids = [_text(value) for value in consumed if _text(value)]
    return {
        "posterior_checkpoint_id": _text(row[0]),
        "posterior_run_id": _text(row[1]),
        "target_identity": _text(row[2]),
        "event_date": _text(event_date),
        "joint_posterior": joint,
        "joint_posterior_sha256": _text(row[4]),
        "observed_max_lower_bound_c": row[5],
        "consumed_event_ids": consumed_ids,
        # P1 run identity is intentionally derived from this durable canonical
        # sequence, never from a lost process-local source-event list.
        "source_event_ids": list(consumed_ids),
        "consumed_ancestor_sha256": _text(row[7]),
        "checkpoint_kind": _text(row[8]),
        "forecast_evidence_snapshot_id": _text(row[9]),
        "prior_context_id": _text(row[10]),
        "resolution_contract_id": _text(row[11]),
        "model_artifact_sha256": _text(row[12]),
    }


def load_latest_persisted_checkpoint(
    conn: sqlite3.Connection,
    *,
    target_identity: str,
    event_date: str,
) -> dict[str, Any] | None:
    """Public read-only recovery API for prepared-cycle assembly."""

    return _load_latest_persisted_checkpoint(
        conn,
        target_identity=target_identity,
        event_date=event_date,
    )


def _checkpoint_matches_frozen_cycle(checkpoint: Mapping[str, Any], cycle: Mapping[str, Any]) -> bool:
    """Prevent a host restart from switching to a different P8 dependency set."""

    posterior = _mapping(cycle.get("posterior_context"))
    resolution = _mapping(posterior.get("resolution_contract"))
    artifact = _mapping(posterior.get("approved_lr_artifact"))
    required_bindings = (
        ("forecast_evidence_snapshot_id", posterior.get("forecast_evidence_snapshot_id")),
        ("prior_context_id", posterior.get("prior_context_id")),
        ("resolution_contract_id", resolution.get("resolution_contract_id")),
    )
    if any(_text(checkpoint.get(key)) != _text(expected) for key, expected in required_bindings):
        return False
    # P0 can be initialized by the prior/weak artifact; every post-observation
    # checkpoint must remain tied to the frozen approved strong-LR artifact.
    if _text(checkpoint.get("checkpoint_kind")) != "initial":
        return _text(checkpoint.get("model_artifact_sha256")) == _text(artifact.get("model_artifact_sha256"))
    return True


class ResidentP10ShadowHost:
    """Stateful, one-target resident host over prepared immutable inputs."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        prepared_cycle_id: str,
        timestamp_observer: Callable[[], str] | None = None,
    ) -> None:
        _assert_shadow_host_db(conn)
        cycle = load_resident_p10_shadow_cycle(conn, prepared_cycle_id=_text(prepared_cycle_id))
        validation = validate_resident_p10_shadow_cycle(cycle)
        if validation.get("ok") is not True:
            raise ValueError("resident_p10_shadow_cycle_invalid:" + ",".join(validation.get("blockers") or []))
        previous = _mapping(cycle.get("previous_checkpoint"))
        if not _checkpoint_persisted(conn, previous):
            raise ValueError("resident_p10_shadow_previous_checkpoint_not_persisted")
        self._conn = conn
        self._cycle = cycle
        self._previous_checkpoint = _load_latest_persisted_checkpoint(
            conn,
            target_identity=_text(cycle.get("target_identity")),
            event_date=_text(cycle.get("event_date")),
        ) or previous
        if not _checkpoint_matches_frozen_cycle(self._previous_checkpoint, cycle):
            raise ValueError("resident_p10_shadow_latest_checkpoint_cycle_binding_mismatch")
        self._clock = timestamp_observer or _utc_now_iso
        self._lock = threading.Lock()

        def runner(connection: sqlite3.Connection, **kwargs: Any) -> Mapping[str, Any]:
            return run_observation_fast_lane_shadow(
                connection,
                commit_timestamp_observer=self._clock,
                **kwargs,
            )

        worker = ResidentObservationFastLaneShadowWorker(
            conn,
            topology=_mapping(cycle.get("topology") or DEFAULT_TOPOLOGY),
            prewarmed_components=_mapping(cycle.get("prewarmed_components")),
            runner=runner,
            timestamp_observer=self._clock,
        )
        self._journal = ResidentAmscSpecliArrivalJournalShadow(
            conn,
            ingress=AmscSpecliResidentShadowIngress(worker),
        )

    @property
    def current_checkpoint(self) -> dict[str, Any]:
        return dict(self._previous_checkpoint)

    def readiness(self) -> dict[str, Any]:
        validation = validate_resident_p10_shadow_cycle(self._cycle)
        checkpoint_persisted = _checkpoint_persisted(self._conn, self._previous_checkpoint)
        checkpoint_binding_valid = _checkpoint_matches_frozen_cycle(self._previous_checkpoint, self._cycle)
        blockers = list(validation.get("blockers") or [])
        if not checkpoint_persisted:
            blockers.append("resident_p10_shadow_latest_checkpoint_not_persisted")
        if not checkpoint_binding_valid:
            blockers.append("resident_p10_shadow_latest_checkpoint_cycle_binding_mismatch")
        blockers = sorted(set(blockers))
        return {
            **validation,
            "schema": SCHEMA_VERSION,
            "prepared_cycle_schema": validation.get("schema"),
            "ok": not blockers,
            "state": "ready" if not blockers else "blocked",
            "blockers": blockers,
            "previous_checkpoint_persisted": checkpoint_persisted,
            "latest_checkpoint_cycle_binding_valid": checkpoint_binding_valid,
            "resumed_from_latest_persisted_checkpoint": True,
            "shadow_only": True,
            "places_orders": False,
        }

    def _dispatch_ids(self, source_observation: Mapping[str, Any]) -> dict[str, str] | None:
        source = _mapping(source_observation)
        identity = {
            "target_identity": _text(self._cycle.get("target_identity")),
            "event_date": _text(self._cycle.get("event_date")),
            "source_observation_id": _text(source.get("source_observation_id")),
            "raw_sha256": _text(source.get("raw_sha256")),
            "valid_time": _text(source.get("valid_time")),
        }
        if not all(identity.values()):
            return None
        digest = _sha256(identity)[:24]
        return {
            "correlation_id": f"p10corr_{digest}",
            "fact_snapshot_id": f"p10fact_{digest}",
            "outbox_event_id": f"p10out_{digest}",
        }

    def process_source_observation(self, *, source_observation: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one source arrival using only the prewarmed cycle context."""

        if not self._lock.acquire(blocking=False):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "resident_p10_shadow_host_concurrent_dispatch_blocked",
                "shadow_only": True,
                "places_orders": False,
            }
        try:
            ready = self.readiness()
            if ready.get("ok") is not True:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "resident_p10_shadow_host_cycle_not_ready",
                    "readiness": ready,
                    "shadow_only": True,
                    "places_orders": False,
                }
            ids = self._dispatch_ids(source_observation)
            if ids is None:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "resident_p10_shadow_host_source_identity_missing",
                    "shadow_only": True,
                    "places_orders": False,
                }
            now = _text(self._clock())
            posterior_context = _mapping(self._cycle.get("posterior_context"))
            decision_context = _mapping(self._cycle.get("decision_context"))
            posterior_inputs = {
                "public_slug": _text(self._cycle.get("public_slug")),
                "public_event_date": _text(self._cycle.get("event_date")),
                "public_fact_as_of": now,
                "shadow_only": True,
                "previous_checkpoint": dict(self._previous_checkpoint),
                "forecast_evidence_snapshot_id": _text(posterior_context.get("forecast_evidence_snapshot_id")),
                "prior_context_id": _text(posterior_context.get("prior_context_id")),
                "approved_lr_artifact": _mapping(posterior_context.get("approved_lr_artifact")),
                "resolution_contract": _mapping(posterior_context.get("resolution_contract")),
                "realization_policy": _mapping(posterior_context.get("realization_policy")),
                "temperature_quantization_c": posterior_context.get("temperature_quantization_c"),
                "fact_snapshot_id": ids["fact_snapshot_id"],
                "correlation_id": ids["correlation_id"],
                "outbox_event_id": ids["outbox_event_id"],
                "decision_as_of": now,
                "deadline_at": now,
                "started_at": now,
                "completed_at": now,
                "source_reliability_before": _mapping(posterior_context.get("source_reliability_before")),
                "source_reliability_after": _mapping(posterior_context.get("source_reliability_after")),
                "path_auxiliary": _mapping(posterior_context.get("path_auxiliary")),
            }
            decision_inputs = {
                "outbox_event_id": ids["outbox_event_id"],
                "lease_owner": _text(decision_context.get("lease_owner")),
                "claimed_at": now,
                "lease_until": now,
                "slug": _text(self._cycle.get("public_slug")),
                "event_date": _text(self._cycle.get("event_date")),
                "market_snapshot": _mapping(decision_context.get("market_snapshot")),
                "capital_plan": _mapping(decision_context.get("capital_plan")),
                "strategy_artifact_sha256": _text(decision_context.get("strategy_artifact_sha256")),
            }
            result = dict(
                self._journal.process_source_observation(
                    source_observation=source_observation,
                    target_identity=_text(self._cycle.get("target_identity")),
                    event_date=_text(self._cycle.get("event_date")),
                    decision_as_of=now,
                    canonicalized_at=now,
                    posterior_transaction_inputs=posterior_inputs,
                    decision_cycle_inputs=decision_inputs,
                )
            )
            posterior = _mapping(
                _mapping(
                    _mapping(_mapping(result.get("ingress")).get("fast_lane")).get("fast_lane")
                ).get("posterior")
            )
            checkpoint = _mapping(posterior.get("checkpoint"))
            if checkpoint and _text(checkpoint.get("posterior_checkpoint_id")):
                self._previous_checkpoint = checkpoint
            return {
                "schema": SCHEMA_VERSION,
                "ok": result.get("ok") is True,
                "state": "dispatched" if result.get("ok") is True else "dispatched_or_blocked",
                "dispatch_ids": ids,
                "checkpoint_advanced": bool(checkpoint),
                "current_checkpoint_id": _text(self._previous_checkpoint.get("posterior_checkpoint_id")),
                "result": result,
                "shadow_only": True,
                "places_orders": False,
                "boundary": {
                    "network_acquisition": False,
                    "forecast_refresh_on_hot_path": False,
                    "generic_task_queue": False,
                    "production_archive_write": False,
                    "clob": False,
                },
            }
        finally:
            self._lock.release()


__all__ = [
    "DEFAULT_TOPOLOGY",
    "SCHEMA_VERSION",
    "ResidentP10ShadowHost",
    "load_latest_persisted_checkpoint",
    "validate_resident_p10_shadow_cycle",
]
