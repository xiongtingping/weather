"""Dedicated production consumer for committed, latest posterior outbox events.

This module owns a deliberately small boundary.  It neither acquires weather
or market data nor recalculates a posterior.  A host must freeze the market,
capital/risk, transition and activation inputs *before* the event is leased.
The worker then executes ``claim → decision/intents → outbox completed`` in
one outer SQLite transaction.  It never calls CLOB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import threading
from typing import Any, Callable, Mapping

from production_dynamic_three_leg_cycle_runner import (
    run_production_dynamic_three_leg_cycle,
)


SCHEMA_VERSION = "production_posterior_commit_outbox_worker_v1"
DEFAULT_LEASE_MS = 1_000
MAX_LEASE_MS = 5_000
REQUIRED_INPUTS = (
    "market_snapshot",
    "desired_trading_plan",
    "transition_plan",
    "capital_snapshot",
    "risk_snapshot",
    "production_activation",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _assert_production_outbox_schema(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "v2_shadow_runtime_manifest"):
        raise ValueError("production_posterior_outbox_refuses_shadow_manifest")
    required = (
        "event_fact_history", "event_fact_latest", "posterior_commit_outbox",
        "posterior_outbox_execution_contracts", "dynamic_three_leg_transition_plans",
        "dynamic_three_leg_risk_reservations", "posterior_execution_intents",
        "auto_trade_decisions",
    )
    missing = [table for table in required if not _table_exists(conn, table)]
    if missing:
        raise ValueError("production_posterior_outbox_tables_missing:" + ",".join(missing))


def _ready_latest_rows(
    conn: sqlite3.Connection, *, decision_as_of: str, limit: int,
) -> list[dict[str, Any]]:
    """Read only events whose exact posterior remains the public latest SSOT."""

    rows = conn.execute(
        """
        SELECT o.outbox_event_id,o.posterior_run_id,o.fact_snapshot_id,o.correlation_id,
               o.state,o.created_at,o.next_attempt_at,o.lease_until,o.claim_generation,
               h.id,h.slug,h.event_date,x.canonical_observation_event_id,
               x.first_actionable_seen_at,x.observation_to_order_hard_deadline_at,
               x.transaction_b_committed_at,x.execution_eligible_at_commit,x.execution_na_reason
          FROM posterior_commit_outbox o
          JOIN posterior_outbox_execution_contracts x
            ON x.outbox_event_id=o.outbox_event_id AND x.posterior_run_id=o.posterior_run_id
          JOIN event_fact_history h
            ON h.posterior_run_id=o.posterior_run_id AND h.fact_snapshot_id=o.fact_snapshot_id
          JOIN event_fact_latest l
            ON l.fact_id=h.id AND l.slug=h.slug AND l.event_date=h.event_date
         WHERE ((o.state='pending' AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?))
             OR (o.state='claimed' AND o.lease_until IS NOT NULL AND o.lease_until<=?))
           AND x.execution_eligible_at_commit=1
           AND julianday(x.observation_to_order_hard_deadline_at)>=julianday(?)
         ORDER BY h.event_date,o.created_at,o.outbox_event_id
         LIMIT ?
        """,
        (_text(decision_as_of), _text(decision_as_of), _text(decision_as_of), max(1, int(limit))),
    ).fetchall()
    names = (
        "outbox_event_id", "posterior_run_id", "fact_snapshot_id", "correlation_id",
        "outbox_state", "outbox_created_at", "next_attempt_at", "lease_until",
        "claim_generation", "fact_id", "slug", "event_date",
        "canonical_observation_event_id", "first_actionable_seen_at",
        "observation_to_order_hard_deadline_at", "transaction_b_committed_at",
        "execution_eligible_at_commit", "execution_na_reason",
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        item = {name: row[index] for index, name in enumerate(names)}
        item["claim_generation"] = int(item["claim_generation"] or 0)
        item["fact_id"] = int(item["fact_id"] or 0)
        item["execution_eligible_at_commit"] = bool(item["execution_eligible_at_commit"])
        result.append({key: _text(value) if isinstance(value, str) else value for key, value in item.items()})
    return result


def _validated_inputs(outbox: Mapping[str, Any], raw_inputs: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Reject incomplete or cross-posterior inputs before the lease mutation."""

    inputs = _mapping(raw_inputs)
    blockers: list[str] = []
    if _text(inputs.get("slug")) != _text(outbox.get("slug")):
        blockers.append("production_posterior_outbox_slug_mismatch")
    if _text(inputs.get("event_date")) != _text(outbox.get("event_date")):
        blockers.append("production_posterior_outbox_event_date_mismatch")
    for key in REQUIRED_INPUTS:
        if not _mapping(inputs.get(key)):
            blockers.append(f"production_posterior_outbox_{key}_missing")
    desired = _mapping(inputs.get("desired_trading_plan"))
    transition = _mapping(inputs.get("transition_plan"))
    market = _mapping(inputs.get("market_snapshot"))
    if _text(desired.get("posterior_run_id")) != _text(outbox.get("posterior_run_id")):
        blockers.append("production_posterior_outbox_desired_posterior_mismatch")
    if _text(desired.get("fact_snapshot_id")) != _text(outbox.get("fact_snapshot_id")):
        blockers.append("production_posterior_outbox_desired_fact_mismatch")
    if _text(transition.get("posterior_run_id")) != _text(outbox.get("posterior_run_id")):
        blockers.append("production_posterior_outbox_transition_posterior_mismatch")
    if _text(transition.get("trading_plan_id")) != _text(desired.get("trading_plan_id")):
        blockers.append("production_posterior_outbox_transition_plan_mismatch")
    if _text(market.get("market_snapshot_id")) != _text(desired.get("market_snapshot_id")):
        blockers.append("production_posterior_outbox_market_snapshot_mismatch")
    return inputs, sorted(set(blockers))


def claim_production_posterior_commit_outbox(
    conn: sqlite3.Connection, *, outbox: Mapping[str, Any], lease_owner: str,
    claimed_at: str, lease_until: str,
) -> dict[str, Any]:
    """Lease an exact latest, in-deadline event with one guarded CAS."""

    _assert_production_outbox_schema(conn)
    event_id, owner = _text(outbox.get("outbox_event_id")), _text(lease_owner)
    now, expiry = _parse_dt(claimed_at), _parse_dt(lease_until)
    if not event_id or not owner or now is None or expiry is None or expiry <= now:
        raise ValueError("production_posterior_outbox_claim_identity_or_lease_invalid")
    expected_run, expected_fact = _text(outbox.get("posterior_run_id")), _text(outbox.get("fact_snapshot_id"))
    cur = conn.execute(
        """
        UPDATE posterior_commit_outbox
           SET state='claimed',claimed_at=?,lease_owner=?,lease_until=?,
               claim_generation=claim_generation+1,attempt_count=attempt_count+1
         WHERE outbox_event_id=? AND posterior_run_id=? AND fact_snapshot_id=?
           AND (state='pending' OR (state='claimed' AND lease_until IS NOT NULL AND lease_until<=?))
           AND EXISTS (
               SELECT 1
                 FROM posterior_outbox_execution_contracts x
                 JOIN event_fact_history h
                   ON h.posterior_run_id=posterior_commit_outbox.posterior_run_id
                  AND h.fact_snapshot_id=posterior_commit_outbox.fact_snapshot_id
                 JOIN event_fact_latest l
                   ON l.fact_id=h.id AND l.slug=h.slug AND l.event_date=h.event_date
                WHERE x.outbox_event_id=posterior_commit_outbox.outbox_event_id
                  AND x.posterior_run_id=posterior_commit_outbox.posterior_run_id
                  AND x.execution_eligible_at_commit=1
                  AND julianday(x.observation_to_order_hard_deadline_at)>=julianday(?)
           )
        """,
        (_text(claimed_at), owner, _text(lease_until), event_id, expected_run, expected_fact,
         _text(claimed_at), _text(claimed_at)),
    )
    if cur.rowcount != 1:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_posterior_outbox_claim_cas_or_latest_conflict", "places_orders": False}
    row = conn.execute(
        "SELECT claim_generation FROM posterior_commit_outbox WHERE outbox_event_id=?", (event_id,)
    ).fetchone()
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "claimed", "outbox_event_id": event_id,
            "claim_generation": int(row[0]), "places_orders": False}


class ProductionPosteriorCommitOutboxWorker:
    """Single-process consumer; activation and CLOB authority stay downstream."""

    def __init__(self, conn: sqlite3.Connection, *, lease_owner: str,
                 lease_ms: int = DEFAULT_LEASE_MS,
                 runner: Callable[..., Mapping[str, Any]] = run_production_dynamic_three_leg_cycle) -> None:
        _assert_production_outbox_schema(conn)
        if not _text(lease_owner):
            raise ValueError("production_posterior_outbox_lease_owner_missing")
        if int(lease_ms) <= 0 or int(lease_ms) > MAX_LEASE_MS:
            raise ValueError("production_posterior_outbox_lease_ms_invalid")
        self._conn, self._lease_owner, self._lease_ms, self._runner = conn, _text(lease_owner), int(lease_ms), runner
        self._lock = threading.Lock()

    def process_ready_latest_outbox(
        self, *, decision_as_of: str,
        frozen_inputs_for: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        max_events: int = 1,
    ) -> dict[str, Any]:
        now = _parse_dt(decision_as_of)
        if now is None:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_posterior_outbox_decision_time_invalid", "places_orders": False}
        if not self._lock.acquire(blocking=False):
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_posterior_outbox_concurrent_dispatch_blocked", "places_orders": False}
        try:
            # The claim and Transaction C must have exactly one durability
            # boundary.  An inherited transaction would make T4 ownership and
            # rollback semantics ambiguous, so the dedicated resident owns it.
            if self._conn.in_transaction:
                return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "production_posterior_outbox_outer_transaction_open", "places_orders": False}
            ready = _ready_latest_rows(self._conn, decision_as_of=decision_as_of, limit=max_events)
            if not ready:
                return {"schema": SCHEMA_VERSION, "ok": True, "state": "idle_no_latest_outbox_ready", "outcomes": [], "places_orders": False}
            outcomes: list[dict[str, Any]] = []
            last_lease_until = ""
            for outbox in ready:
                try:
                    supplied = _mapping(frozen_inputs_for(dict(outbox)))
                except Exception as exc:
                    outcomes.append({"outbox": outbox, "ok": False, "state": "input_provider_exception_before_claim", "exception_type": type(exc).__name__})
                    continue
                inputs, blockers = _validated_inputs(outbox, supplied)
                effective_now = _parse_dt(inputs.get("decision_as_of") or decision_as_of)
                hard_deadline = _parse_dt(outbox.get("observation_to_order_hard_deadline_at"))
                if (
                    effective_now is None
                    or effective_now < now
                    or hard_deadline is None
                    or effective_now > hard_deadline
                ):
                    blockers.append("production_posterior_outbox_effective_decision_time_invalid_or_expired")
                if blockers:
                    outcomes.append({"outbox": outbox, "ok": False, "state": "blocked_before_claim", "blockers": blockers})
                    continue
                try:
                    effective_as_of = _iso(effective_now)
                    lease_until = _iso(effective_now + timedelta(milliseconds=self._lease_ms))
                    last_lease_until = lease_until
                    self._conn.execute("BEGIN IMMEDIATE")
                    claimed = claim_production_posterior_commit_outbox(
                        self._conn, outbox=outbox, lease_owner=self._lease_owner,
                        claimed_at=effective_as_of, lease_until=lease_until,
                    )
                    if not claimed.get("ok"):
                        self._conn.rollback()
                        outcomes.append({"outbox": outbox, "ok": False, "state": "claim_blocked", "result": claimed})
                        continue
                    result = self._runner(
                        self._conn, outbox_event_id=_text(outbox.get("outbox_event_id")),
                        lease_owner=self._lease_owner, claim_generation=int(claimed["claim_generation"]),
                        slug=_text(outbox.get("slug")), event_date=_text(outbox.get("event_date")),
                        desired_trading_plan=_mapping(inputs["desired_trading_plan"]),
                        transition_plan=_mapping(inputs["transition_plan"]),
                        market_snapshot=_mapping(inputs["market_snapshot"]),
                        capital_snapshot=_mapping(inputs["capital_snapshot"]),
                        risk_snapshot=_mapping(inputs["risk_snapshot"]),
                        production_activation=_mapping(inputs["production_activation"]),
                        decision_as_of=effective_as_of,
                    )
                    if result.get("ok") is True:
                        self._conn.commit()
                        outcomes.append({"outbox": outbox, "ok": True, "state": "runner_completed", "result": dict(result)})
                    else:
                        # A blocked decision must not leave a lease or an
                        # unbound reservation behind.  The next committed
                        # posterior receives a clean, independently frozen
                        # opportunity.
                        self._conn.rollback()
                        outcomes.append({"outbox": outbox, "ok": False, "state": "runner_blocked", "result": dict(result)})
                except Exception as exc:
                    if self._conn.in_transaction:
                        self._conn.rollback()
                    outcomes.append({"outbox": outbox, "ok": False, "state": "runner_exception_rolled_back", "exception_type": type(exc).__name__})
            all_ok = bool(outcomes) and all(item.get("ok") is True for item in outcomes)
            return {
                "schema": SCHEMA_VERSION, "ok": all_ok,
                "state": "dispatched" if all_ok else "partial_or_blocked",
                "decision_as_of": _text(decision_as_of), "lease_owner": self._lease_owner,
                "lease_until": last_lease_until, "outcomes": outcomes, "places_orders": False,
                "boundary": {"dedicated_posterior_outbox_only": True, "generic_task_queue": False,
                             "weather_or_market_acquisition": False, "posterior_recalculation": False,
                             "signer_or_clob": False, "frozen_inputs_before_lease": True,
                             "claim_decision_intent_outbox_single_transaction": True},
            }
        finally:
            self._lock.release()


__all__ = [
    "DEFAULT_LEASE_MS", "MAX_LEASE_MS", "REQUIRED_INPUTS", "SCHEMA_VERSION",
    "ProductionPosteriorCommitOutboxWorker", "claim_production_posterior_commit_outbox",
]
