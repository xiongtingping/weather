"""Dedicated resident consumer for the isolated P10 posterior outbox.

The worker is deliberately narrow: it reads only committed posterior outbox
events that still own ``event_fact_latest`` and sends them to the one
dynamic three-leg shadow decision cycle.  It is not a generic task queue, never
acquires weather data, and never signs or submits an order.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import threading
from typing import Any, Callable, Mapping

from dynamic_three_leg_shadow_decision_writer import run_isolated_dynamic_three_leg_shadow_decision_cycle
from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)


SCHEMA_VERSION = "resident_posterior_commit_outbox_shadow_worker_v1"
DEFAULT_LEASE_MS = 1_000
MAX_LEASE_MS = 5_000
REQUIRED_DECISION_INPUTS = (
    "market_snapshot",
    "capital_plan",
    "settlement_window_policy",
    "strategy_artifact_sha256",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    )


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ValueError("resident_outbox_shadow_manifest_missing")
    manifest = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if manifest is None or _text(manifest[0]) != SHADOW_MANIFEST_STATE:
        raise ValueError("resident_outbox_shadow_manifest_invalid")
    required = (
        "event_fact_history",
        "event_fact_latest",
        "posterior_commit_outbox",
        "posterior_outbox_execution_contracts",
    )
    missing = [table for table in required if not _table_exists(conn, table)]
    if missing:
        raise ValueError(f"resident_outbox_shadow_tables_missing:{','.join(missing)}")


def _ready_latest_outbox_rows(
    conn: sqlite3.Connection,
    *,
    decision_as_of: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Return only outbox rows whose run remains the public latest SSOT.

    A superseded posterior must not consume the resident decision budget.  It
    is intentionally left untouched for explicit replay/audit treatment;
    this worker never rewrites outbox state merely because a newer posterior
    exists.
    """

    rows = conn.execute(
        """
        SELECT o.outbox_event_id,o.posterior_run_id,o.fact_snapshot_id,o.correlation_id,
               o.state,o.created_at,o.next_attempt_at,o.lease_until,o.claim_generation,
               h.id,h.slug,h.event_date,x.canonical_observation_event_id,
               x.first_actionable_seen_at,x.observation_to_order_hard_deadline_at,
               x.transaction_b_committed_at,x.execution_eligible_at_commit,x.execution_na_reason
        FROM posterior_commit_outbox o
        JOIN posterior_outbox_execution_contracts x
          ON x.outbox_event_id=o.outbox_event_id
         AND x.posterior_run_id=o.posterior_run_id
        JOIN event_fact_history h
          ON h.posterior_run_id=o.posterior_run_id
         AND h.fact_snapshot_id=o.fact_snapshot_id
        JOIN event_fact_latest l
          ON l.fact_id=h.id
         AND l.slug=h.slug
         AND l.event_date=h.event_date
        WHERE (
            (o.state='pending' AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?))
            OR (o.state='claimed' AND o.lease_until IS NOT NULL AND o.lease_until<=?)
        )
          AND x.execution_eligible_at_commit=1
          AND julianday(x.observation_to_order_hard_deadline_at)>=julianday(?)
        ORDER BY h.event_date ASC, o.created_at ASC, o.outbox_event_id ASC
        LIMIT ?
        """,
        (_text(decision_as_of), _text(decision_as_of), _text(decision_as_of), max(1, int(limit))),
    ).fetchall()
    return [
        {
            "outbox_event_id": _text(row[0]),
            "posterior_run_id": _text(row[1]),
            "fact_snapshot_id": _text(row[2]),
            "correlation_id": _text(row[3]),
            "outbox_state": _text(row[4]),
            "outbox_created_at": _text(row[5]),
            "next_attempt_at": _text(row[6]),
            "lease_until": _text(row[7]),
            "claim_generation": int(row[8] or 0),
            "fact_id": int(row[9] or 0),
            "slug": _text(row[10]),
            "event_date": _text(row[11]),
            "canonical_observation_event_id": _text(row[12]),
            "first_actionable_seen_at": _text(row[13]),
            "observation_to_order_hard_deadline_at": _text(row[14]),
            "transaction_b_committed_at": _text(row[15]),
            "execution_eligible_at_commit": bool(row[16]),
            "execution_na_reason": _text(row[17]),
        }
        for row in rows
    ]


def _validated_decision_inputs(
    outbox: Mapping[str, Any],
    raw_inputs: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    inputs = _mapping(raw_inputs)
    blockers: list[str] = []
    if _text(inputs.get("slug")) != _text(outbox.get("slug")):
        blockers.append("resident_outbox_shadow_slug_mismatch")
    if _text(inputs.get("event_date")) != _text(outbox.get("event_date")):
        blockers.append("resident_outbox_shadow_event_date_mismatch")
    for key in REQUIRED_DECISION_INPUTS:
        value = inputs.get(key)
        if key in {"market_snapshot", "capital_plan", "settlement_window_policy"}:
            if not _mapping(value):
                blockers.append(f"resident_outbox_shadow_{key}_missing")
        elif not _text(value):
            blockers.append(f"resident_outbox_shadow_{key}_missing")
    return inputs, sorted(set(blockers))


class ResidentPosteriorCommitOutboxShadowWorker:
    """Single-process, injected-clock shadow consumer of committed latest SSOT.

    The host owns waking and supplies the actual decision timestamp and frozen
    market/capital snapshots.  This class owns neither a polling loop nor a
    network client; therefore it cannot silently become a generic queue or a
    live executor.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lease_owner: str,
        lease_ms: int = DEFAULT_LEASE_MS,
        runner: Callable[..., Mapping[str, Any]] = run_isolated_dynamic_three_leg_shadow_decision_cycle,
    ) -> None:
        _assert_isolated_shadow_db(conn)
        owner = _text(lease_owner)
        if not owner:
            raise ValueError("resident_outbox_shadow_lease_owner_missing")
        duration = int(lease_ms)
        if duration <= 0 or duration > MAX_LEASE_MS:
            raise ValueError("resident_outbox_shadow_lease_ms_invalid")
        self._conn = conn
        self._lease_owner = owner
        self._lease_ms = duration
        self._runner = runner
        self._lock = threading.Lock()

    def process_ready_latest_outbox(
        self,
        *,
        decision_as_of: str,
        decision_inputs_for: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        max_events: int = 1,
    ) -> dict[str, Any]:
        """Dispatch latest committed posterior events in source order.

        ``decision_as_of`` is caller instrumentation, not a locally fabricated
        timestamp.  Input validation precedes any outbox lease mutation.
        """

        now = _parse_dt(decision_as_of)
        if now is None:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "resident_outbox_shadow_decision_time_missing_or_invalid",
                "shadow_only": True,
                "places_orders": False,
            }
        if not self._lock.acquire(blocking=False):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "resident_outbox_shadow_concurrent_dispatch_blocked",
                "shadow_only": True,
                "places_orders": False,
            }
        try:
            ready = _ready_latest_outbox_rows(
                self._conn,
                decision_as_of=_text(decision_as_of),
                limit=max_events,
            )
            if not ready:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": True,
                    "state": "idle_no_latest_outbox_ready",
                    "outcomes": [],
                    "shadow_only": True,
                    "places_orders": False,
                }
            lease_until = _iso(now + timedelta(milliseconds=self._lease_ms))
            outcomes: list[dict[str, Any]] = []
            for outbox in ready:
                try:
                    supplied_inputs = _mapping(decision_inputs_for(dict(outbox)))
                except Exception as exc:
                    outcomes.append(
                        {
                            "outbox": outbox,
                            "ok": False,
                            "state": "input_provider_exception_before_claim",
                            "exception_type": type(exc).__name__,
                            "reason": str(exc),
                        }
                    )
                    continue
                inputs, blockers = _validated_decision_inputs(outbox, supplied_inputs)
                if blockers:
                    outcomes.append(
                        {
                            "outbox": outbox,
                            "ok": False,
                            "state": "blocked_before_claim",
                            "blockers": blockers,
                        }
                    )
                    continue
                try:
                    result = self._runner(
                        self._conn,
                        outbox_event_id=_text(outbox.get("outbox_event_id")),
                        lease_owner=self._lease_owner,
                        claimed_at=_text(decision_as_of),
                        lease_until=lease_until,
                        slug=_text(outbox.get("slug")),
                        event_date=_text(outbox.get("event_date")),
                        market_snapshot=_mapping(inputs.get("market_snapshot")),
                        capital_plan=_mapping(inputs.get("capital_plan")),
                        settlement_window_policy=_mapping(inputs.get("settlement_window_policy")),
                        strategy_artifact_sha256=_text(inputs.get("strategy_artifact_sha256")),
                    )
                    outcomes.append(
                        {
                            "outbox": outbox,
                            "ok": result.get("ok") is True,
                            "state": "runner_completed" if result.get("ok") is True else "runner_blocked",
                            "result": dict(result),
                        }
                    )
                except Exception as exc:
                    # A runner exception may have occurred after a lease CAS.
                    # Leave that lease intact for expiry/retry; never mutate it
                    # speculatively from this dispatcher.
                    outcomes.append(
                        {
                            "outbox": outbox,
                            "ok": False,
                            "state": "runner_exception_lease_retained",
                            "exception_type": type(exc).__name__,
                            "reason": str(exc),
                        }
                    )
            return {
                "schema": SCHEMA_VERSION,
                "ok": all(item.get("ok") is True for item in outcomes),
                "state": "dispatched" if all(item.get("ok") is True for item in outcomes) else "partial_or_blocked",
                "decision_as_of": _text(decision_as_of),
                "lease_owner": self._lease_owner,
                "lease_until": lease_until,
                "outcomes": outcomes,
                "shadow_only": True,
                "places_orders": False,
                "boundary": {
                    "dedicated_posterior_outbox_only": True,
                    "generic_task_queue": False,
                    "network_acquisition": False,
                    "signer_or_clob": False,
                    "production_archive_write": False,
                },
            }
        finally:
            self._lock.release()


__all__ = [
    "DEFAULT_LEASE_MS",
    "MAX_LEASE_MS",
    "REQUIRED_DECISION_INPUTS",
    "SCHEMA_VERSION",
    "ResidentPosteriorCommitOutboxShadowWorker",
]
