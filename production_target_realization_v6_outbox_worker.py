"""Unique Target Realization v6 consumer for committed posterior outbox rows."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from production_dynamic_three_leg_v6_transaction_c_writer import commit_target_realization_v6_transaction_c
from production_posterior_commit_outbox_worker import (
    DEFAULT_LEASE_MS,
    MAX_LEASE_MS,
    _backfill_expired_no_order_decisions,
    _ready_latest_rows,
    _run_outbox_maintenance_if_needed,
    claim_production_posterior_commit_outbox,
)


SCHEMA_VERSION = "production_target_realization_v6_outbox_worker_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class ProductionTargetRealizationV6OutboxWorker:
    def __init__(self, conn: sqlite3.Connection, *, lease_owner: str, lease_ms: int = DEFAULT_LEASE_MS) -> None:
        required = {
            "target_realization_v6_plans", "dynamic_three_leg_v6_main_yes_cycles",
            "dynamic_three_leg_v6_capital_allocations", "dynamic_three_leg_v6_risk_reservations",
            "dynamic_three_leg_v6_transaction_c_commits", "posterior_commit_outbox",
            "dynamic_three_leg_v6_standing_exit_release_requests",
            "posterior_outbox_execution_contracts", "auto_trade_decisions", "posterior_execution_intents",
        }
        missing = sorted(required - _tables(conn))
        if missing:
            raise ValueError("target_realization_v6_worker_tables_missing:" + ",".join(missing))
        if not _text(lease_owner) or not 0 < int(lease_ms) <= MAX_LEASE_MS:
            raise ValueError("target_realization_v6_worker_lease_invalid")
        self.conn = conn
        self.owner = _text(lease_owner)
        self.lease_ms = int(lease_ms)
        self.lock = threading.Lock()

    def process_ready_latest_outbox(
        self,
        *,
        decision_as_of: str,
        frozen_inputs_for: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        exact_exit_reason_policy_sha256: str,
        max_events: int = 1,
    ) -> dict[str, Any]:
        worker_started = time.monotonic()
        now = _parse(decision_as_of)
        if now is None:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "v6_worker_decision_time_invalid", "places_orders": False}
        if not self.lock.acquire(blocking=False):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "v6_worker_concurrent_dispatch_blocked", "places_orders": False}
        try:
            if self.conn.in_transaction:
                return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "v6_worker_outer_transaction_open", "places_orders": False}
            stage_started = time.monotonic()
            ready = _ready_latest_rows(self.conn, decision_as_of=decision_as_of, limit=max_events)
            ready_query_ms = round((time.monotonic() - stage_started) * 1000.0, 3)
            terminalized: list[str] = []
            backfilled: list[str] = []
            maintenance_ms = 0.0
            if not ready:
                stage_started = time.monotonic()
                terminalized, backfilled = _run_outbox_maintenance_if_needed(
                    self.conn, decision_as_of=decision_as_of,
                )
                maintenance_ms = round((time.monotonic() - stage_started) * 1000.0, 3)
                ready = _ready_latest_rows(self.conn, decision_as_of=decision_as_of, limit=max_events)
            if not ready:
                return {"schema": SCHEMA_VERSION, "ok": True, "state": "idle_no_latest_outbox_ready", "outcomes": [], "terminalized": terminalized, "backfilled": backfilled, "places_orders": False, "latency_stages_ms": {"ready_query_ms": ready_query_ms, "maintenance_ms": maintenance_ms, "worker_total_ms": round((time.monotonic() - worker_started) * 1000.0, 3), "idle_writer_transaction_opened": bool(terminalized or backfilled)}}
            outcomes = []
            for outbox in ready:
                outcome_started = time.monotonic()
                latency_stages_ms: dict[str, float] = {}
                try:
                    stage_started = time.monotonic()
                    inputs = dict(frozen_inputs_for(dict(outbox)))
                    latency_stages_ms["freeze_inputs_and_local_exchange_snapshot_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
                except Exception as exc:
                    latency_stages_ms["freeze_inputs_and_local_exchange_snapshot_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
                    outcomes.append({"outbox_event_id": outbox["outbox_event_id"], "ok": False, "state": "input_provider_exception", "exception_type": type(exc).__name__, "latency_stages_ms": latency_stages_ms})
                    continue
                effective = _parse(inputs.get("decision_as_of"))
                deadline = _parse(outbox.get("observation_to_order_hard_deadline_at"))
                if inputs.get("ok") is not True or effective is None or deadline is None or effective < now or effective > deadline:
                    latency_stages_ms["outbox_to_blocked_total_ms"] = round((time.monotonic() - outcome_started) * 1000.0, 3)
                    outcomes.append({"outbox_event_id": outbox["outbox_event_id"], "ok": False, "state": "blocked_before_claim", "block_reasons": list(inputs.get("block_reasons") or ["v6_worker_effective_time_or_input_invalid"]), "latency_stages_ms": latency_stages_ms})
                    continue
                stage_started = time.monotonic()
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    claimed = claim_production_posterior_commit_outbox(
                        self.conn, outbox=outbox, lease_owner=self.owner,
                        claimed_at=_iso(effective), lease_until=_iso(effective + timedelta(milliseconds=self.lease_ms)),
                    )
                    if claimed.get("ok") is not True:
                        self.conn.rollback()
                        outcomes.append({"outbox_event_id": outbox["outbox_event_id"], "ok": False, "state": "claim_blocked"})
                        continue
                    result = commit_target_realization_v6_transaction_c(
                        self.conn, outbox_event_id=_text(outbox.get("outbox_event_id")),
                        lease_owner=self.owner, claim_generation=int(claimed["claim_generation"]),
                        plan=dict(inputs["v6_plan"]), decisions=list(inputs["decisions"]),
                        intents=list(inputs["intents"]), production_activation=dict(inputs["production_activation"]),
                        coverage_release_requests=list(inputs.get("coverage_release_requests") or []),
                        exact_exit_reason_policy_sha256=_text(exact_exit_reason_policy_sha256),
                        committed_at=_iso(effective),
                    )
                    if result.get("ok") is not True:
                        self.conn.rollback()
                        outcomes.append({"outbox_event_id": outbox["outbox_event_id"], "ok": False, "state": "transaction_c_blocked", "result": result})
                        continue
                    self.conn.commit()
                    latency_stages_ms["claim_and_transaction_c_commit_ms"] = round((time.monotonic() - stage_started) * 1000.0, 3)
                    latency_stages_ms["outbox_to_prepared_intent_total_ms"] = round((time.monotonic() - outcome_started) * 1000.0, 3)
                    outcomes.append({"outbox_event_id": outbox["outbox_event_id"], "ok": True, "state": "transaction_c_committed", "result": result, "latency_stages_ms": latency_stages_ms})
                except Exception as exc:
                    if self.conn.in_transaction:
                        self.conn.rollback()
                    outcomes.append({"outbox_event_id": outbox["outbox_event_id"], "ok": False, "state": "transaction_c_exception_rolled_back", "exception_type": type(exc).__name__})
            ok = bool(outcomes) and all(row.get("ok") is True for row in outcomes)
            return {"schema": SCHEMA_VERSION, "ok": ok, "state": "dispatched" if ok else "partial_or_blocked", "outcomes": outcomes, "places_orders": False, "latency_stages_ms": {"ready_query_ms": ready_query_ms, "maintenance_ms": maintenance_ms, "worker_total_ms": round((time.monotonic() - worker_started) * 1000.0, 3)}}
        finally:
            self.lock.release()


__all__ = ["ProductionTargetRealizationV6OutboxWorker", "SCHEMA_VERSION"]
