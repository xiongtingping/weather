"""Dedicated production consumer for committed, latest posterior outbox events.

This module owns a deliberately small boundary.  It neither acquires weather
or market data nor recalculates a posterior.  A host must freeze the market,
capital/risk, transition and activation inputs *before* the event is leased.
The worker then executes ``claim → decision/intents → outbox completed`` in
one outer SQLite transaction.  It never calls CLOB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping

from lineage_identity_runtime import lineage_id_from_parts
from production_dynamic_three_leg_cycle_runner import (
    run_production_dynamic_three_leg_cycle,
)


SCHEMA_VERSION = "production_posterior_commit_outbox_worker_v1"
BACKFILL_AUDIT_INTERVAL_SEC = 5.0
_last_backfill_audit_monotonic = 0.0
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
REQUIRED_AUTO_DECISION_COLUMNS = {
    "run_id", "run_date", "slug", "event_date", "outcome", "side", "action",
    "decided_state", "will_order", "reason_codes_json", "reasons_json",
    "details_json", "created_at", "correlation_id", "fact_snapshot_id",
    "posterior_run_id", "decision_id", "order_intent_id",
}


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
    decision_columns = {_text(row[1]) for row in conn.execute("PRAGMA table_info(auto_trade_decisions)")}
    missing_columns = sorted(REQUIRED_AUTO_DECISION_COLUMNS - decision_columns)
    if missing_columns:
        raise ValueError(
            "production_posterior_outbox_auto_decision_columns_missing:"
            + ",".join(missing_columns)
        )


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
           AND h.fact_snapshot_id IS NOT NULL AND h.fact_snapshot_id<>''
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


def _terminalize_expired_outbox(
    conn: sqlite3.Connection, *, decision_as_of: str,
) -> list[str]:
    """Close expired executable events without ever creating a late decision.

    An observation's three-second execution opportunity is a hard deadline, not
    a retry window.  Keeping such rows ``pending`` makes the durable ledger claim
    that work remains actionable even though the claim CAS correctly refuses it.
    """

    rows = conn.execute(
        """
        SELECT o.outbox_event_id,o.posterior_run_id,o.fact_snapshot_id,o.correlation_id,
               h.slug,h.event_date,x.observation_to_order_hard_deadline_at
          FROM posterior_commit_outbox o
          JOIN posterior_outbox_execution_contracts x
            ON x.outbox_event_id=o.outbox_event_id AND x.posterior_run_id=o.posterior_run_id
          JOIN event_fact_history h
            ON h.posterior_run_id=o.posterior_run_id AND h.fact_snapshot_id=o.fact_snapshot_id
           AND h.fact_snapshot_id IS NOT NULL AND h.fact_snapshot_id<>''
         WHERE (o.state='pending'
             OR (o.state='claimed' AND o.lease_until IS NOT NULL AND o.lease_until<=?))
           AND x.execution_eligible_at_commit=1
           AND julianday(x.observation_to_order_hard_deadline_at)<julianday(?)
         ORDER BY o.created_at,o.outbox_event_id
        """,
        (_text(decision_as_of), _text(decision_as_of)),
    ).fetchall()
    event_ids = [_text(row[0]) for row in rows if _text(row[0])]
    if not event_ids:
        return []
    placeholders = ",".join("?" for _ in event_ids)
    conn.execute(
        f"""
        UPDATE posterior_commit_outbox
           SET state='dead_letter',completed_at=?,lease_owner=NULL,lease_until=NULL,
               error_class='observation_to_order_deadline_expired',
               last_error='late_execution_forbidden',
               terminal_reason='production_observation_to_order_hard_deadline_expired'
         WHERE outbox_event_id IN ({placeholders})
           AND (state='pending'
             OR (state='claimed' AND lease_until IS NOT NULL AND lease_until<=?))
        """,
        (_text(decision_as_of), *event_ids, _text(decision_as_of)),
    )
    _persist_expired_no_order_decisions(conn, rows=rows, decided_at=decision_as_of)
    return event_ids


def _persist_expired_no_order_decisions(
    conn: sqlite3.Connection, *, rows: list[sqlite3.Row | tuple[Any, ...]], decided_at: str,
) -> list[str]:
    decision_ids: list[str] = []
    for row in rows:
        outbox_event_id = _text(row[0])
        decision_id = lineage_id_from_parts(
            "decision_id",
            {
                "outbox_event_id": outbox_event_id,
                "action": "HOLD",
                "reason_code": "production_observation_to_order_hard_deadline_expired",
            },
        )
        details = {
            "schema": "dynamic_three_leg_no_order_decision_v1",
            "outbox_event_id": outbox_event_id,
            "posterior_run_id": _text(row[1]),
            "fact_snapshot_id": _text(row[2]),
            "state": "blocked_no_order",
            "will_order": False,
            "observation_to_order_hard_deadline_at": _text(row[6]),
            "late_execution_forbidden": True,
            "called_gateway": False,
        }
        reason_codes_json = '["production_observation_to_order_hard_deadline_expired"]'
        reasons_json = '[{"reason_family":"execution_deadline","reason_code":"production_observation_to_order_hard_deadline_expired"}]'
        details_json = json.dumps(
            details, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        expected = (
            "hold", "blocked", 0, reason_codes_json, reasons_json, details_json,
            _text(row[3]), _text(row[2]), _text(row[1]),
        )
        existing = conn.execute(
            """
            SELECT action,decided_state,will_order,reason_codes_json,reasons_json,
                   details_json,correlation_id,fact_snapshot_id,posterior_run_id
              FROM auto_trade_decisions WHERE decision_id=?
               AND decision_id IS NOT NULL AND decision_id<>''
            """,
            (decision_id,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("production_expired_no_order_decision_replay_drift")
            continue
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              run_id,run_date,slug,event_date,outcome,side,action,decided_state,
              will_order,reason_codes_json,reasons_json,details_json,created_at,
              correlation_id,fact_snapshot_id,posterior_run_id,decision_id,order_intent_id
            ) VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            """,
            (
                _text(row[5]), _text(row[4]), _text(row[5]), "", "", "hold",
                "blocked", 0,
                reason_codes_json, reasons_json, details_json,
                _text(decided_at), _text(row[3]), _text(row[2]), _text(row[1]), decision_id,
            ),
        )
        decision_ids.append(decision_id)
    return decision_ids


def _backfill_expired_no_order_decisions(
    conn: sqlite3.Connection, *, decision_as_of: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT o.outbox_event_id,o.posterior_run_id,o.fact_snapshot_id,o.correlation_id,
               h.slug,h.event_date,x.observation_to_order_hard_deadline_at
          FROM posterior_commit_outbox o
          JOIN posterior_outbox_execution_contracts x
            ON x.outbox_event_id=o.outbox_event_id AND x.posterior_run_id=o.posterior_run_id
          JOIN event_fact_history h
            ON h.posterior_run_id=o.posterior_run_id AND h.fact_snapshot_id=o.fact_snapshot_id
           AND h.fact_snapshot_id IS NOT NULL AND h.fact_snapshot_id<>''
         WHERE o.state='dead_letter'
           AND o.terminal_reason='production_observation_to_order_hard_deadline_expired'
         ORDER BY o.created_at,o.outbox_event_id
        """
    ).fetchall()
    return _persist_expired_no_order_decisions(
        conn, rows=rows, decided_at=decision_as_of,
    )


def _outbox_maintenance_needed(
    conn: sqlite3.Connection, *, decision_as_of: str,
) -> bool:
    """Return whether expiry maintenance needs a SQLite writer transaction.

    The resident polls every 50ms.  Taking ``BEGIN IMMEDIATE`` on every idle
    poll starves the latency-critical canonical writer even when there is
    nothing to mutate.  Keep the common idle path strictly read-only.
    """

    row = conn.execute(
        """
        SELECT 1
          FROM posterior_commit_outbox o
          JOIN posterior_outbox_execution_contracts x
            ON x.outbox_event_id=o.outbox_event_id AND x.posterior_run_id=o.posterior_run_id
         WHERE (o.state='pending'
             OR (o.state='claimed' AND o.lease_until IS NOT NULL AND o.lease_until<=?))
           AND x.execution_eligible_at_commit=1
           AND julianday(x.observation_to_order_hard_deadline_at)<julianday(?)
         LIMIT 1
        """,
        (_text(decision_as_of), _text(decision_as_of)),
    ).fetchone()
    if row is not None:
        return True
    global _last_backfill_audit_monotonic
    audit_now = time.monotonic()
    if audit_now - _last_backfill_audit_monotonic < BACKFILL_AUDIT_INTERVAL_SEC:
        return False
    _last_backfill_audit_monotonic = audit_now

    # ``auto_trade_decisions`` is large.  Never run a correlated JSON scan on
    # every 50ms tick; the deterministic decision identity has a unique index,
    # so point lookups make this O(number of expired outboxes).
    expired_rows = conn.execute(
        """
        SELECT outbox_event_id
          FROM posterior_commit_outbox
         WHERE state='dead_letter'
           AND terminal_reason='production_observation_to_order_hard_deadline_expired'
        """
    ).fetchall()
    for expired in expired_rows:
        outbox_event_id = _text(expired[0])
        decision_id = lineage_id_from_parts(
            "decision_id",
            {
                "outbox_event_id": outbox_event_id,
                "action": "HOLD",
                "reason_code": "production_observation_to_order_hard_deadline_expired",
            },
        )
        decision = conn.execute(
            """SELECT details_json FROM auto_trade_decisions
                WHERE decision_id=? AND decision_id IS NOT NULL AND decision_id<>''""",
            (decision_id,),
        ).fetchone()
        if decision is None:
            return True
        try:
            details = json.loads(_text(decision[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return True
        if (
            _text(details.get("outbox_event_id")) != outbox_event_id
            or details.get("late_execution_forbidden") is not True
        ):
            return True
    return False


def _run_outbox_maintenance_if_needed(
    conn: sqlite3.Connection, *, decision_as_of: str,
) -> tuple[list[str], list[str]]:
    if not _outbox_maintenance_needed(conn, decision_as_of=decision_as_of):
        return [], []
    conn.execute("BEGIN IMMEDIATE")
    try:
        terminalized = _terminalize_expired_outbox(conn, decision_as_of=decision_as_of)
        backfilled = _backfill_expired_no_order_decisions(conn, decision_as_of=decision_as_of)
        conn.commit()
        return terminalized, backfilled
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _validated_inputs(outbox: Mapping[str, Any], raw_inputs: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Reject incomplete or cross-posterior inputs before the lease mutation."""

    inputs = _mapping(raw_inputs)
    blockers: list[str] = []
    if inputs.get("ok") is False:
        blockers.append("production_posterior_outbox_input_provider_blocked")
        provider_reasons = inputs.get("block_reasons")
        if isinstance(provider_reasons, (list, tuple)):
            blockers.extend(_text(reason) for reason in provider_reasons if _text(reason))
        elif _text(provider_reasons):
            blockers.append(_text(provider_reasons))
        return inputs, sorted(set(blockers))
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
                  AND h.fact_snapshot_id IS NOT NULL AND h.fact_snapshot_id<>''
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
            terminalized: list[str] = []
            backfilled_decisions: list[str] = []
            if not ready:
                try:
                    terminalized, backfilled_decisions = _run_outbox_maintenance_if_needed(
                        self._conn, decision_as_of=decision_as_of,
                    )
                except Exception:
                    if self._conn.in_transaction:
                        self._conn.rollback()
                    return {
                        "schema": SCHEMA_VERSION,
                        "ok": False,
                        "state": "blocked",
                        "reason": "production_posterior_outbox_expiry_terminalization_failed",
                        "places_orders": False,
                    }
                # Maintenance may have changed readiness (for example an
                # expired lease).  Re-read from the durable SSOT.
                ready = _ready_latest_rows(self._conn, decision_as_of=decision_as_of, limit=max_events)
            if not ready:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": True,
                    "state": "idle_no_latest_outbox_ready",
                    "outcomes": [],
                    "terminalized_expired_outbox_event_ids": terminalized,
                    "backfilled_expired_no_order_decision_ids": backfilled_decisions,
                    "places_orders": False,
                }
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
                "terminalized_expired_outbox_event_ids": terminalized,
                "backfilled_expired_no_order_decision_ids": backfilled_decisions,
                "boundary": {"dedicated_posterior_outbox_only": True, "generic_task_queue": False,
                             "weather_or_market_acquisition": False, "posterior_recalculation": False,
                             "signer_or_clob": False, "frozen_inputs_before_lease": True,
                             "claim_decision_intent_outbox_single_transaction": True},
            }
        finally:
            self._lock.release()


__all__ = [
    "DEFAULT_LEASE_MS", "MAX_LEASE_MS", "REQUIRED_AUTO_DECISION_COLUMNS",
    "REQUIRED_INPUTS", "SCHEMA_VERSION",
    "ProductionPosteriorCommitOutboxWorker", "claim_production_posterior_commit_outbox",
]
