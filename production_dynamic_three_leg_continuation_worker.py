"""Resident consumer for same-posterior sell-to-buy continuation events."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from production_dynamic_three_leg_continuation_writer import (
    commit_production_dynamic_three_leg_continuation,
)


SCHEMA_VERSION = "production_dynamic_three_leg_continuation_worker_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _dt(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def process_dynamic_three_leg_continuation_once(
    conn: sqlite3.Connection,
    *,
    preliminary_as_of: str,
    lease_owner: str,
    frozen_inputs_for: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    if conn.in_transaction:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "continuation_worker_outer_transaction_open", "places_orders": False}
    row = conn.execute(
        """SELECT c.continuation_event_id,c.posterior_run_id,c.fact_snapshot_id,c.slug,c.event_date,
                  c.transition_generation,c.state,c.claim_generation,c.lease_until,
                  h.posterior_run_id,h.fact_snapshot_id
             FROM dynamic_three_leg_continuation_events c
             LEFT JOIN event_fact_latest l ON l.slug=c.slug AND l.event_date=c.event_date
             LEFT JOIN event_fact_history h ON h.id=l.fact_id
            WHERE c.state='pending' OR (c.state='claimed' AND c.lease_until<=?)
            ORDER BY c.created_at,c.continuation_event_id LIMIT 1""",
        (_text(preliminary_as_of),),
    ).fetchone()
    if row is None:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "idle_no_continuation", "places_orders": False}
    event = {
        "continuation_event_id": _text(row[0]), "posterior_run_id": _text(row[1]),
        "fact_snapshot_id": _text(row[2]), "slug": _text(row[3]), "event_date": _text(row[4]),
        "transition_generation": int(row[5]),
    }
    if (_text(row[9]), _text(row[10])) != (event["posterior_run_id"], event["fact_snapshot_id"]):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE dynamic_three_leg_continuation_events
                  SET state='superseded',last_error='posterior_superseded',updated_at=?
                WHERE continuation_event_id=? AND state IN ('pending','claimed')""",
            (_text(preliminary_as_of), event["continuation_event_id"]),
        )
        conn.commit()
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "superseded_by_new_posterior", "places_orders": False}
    try:
        inputs = _mapping(frozen_inputs_for(event))
    except Exception as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "continuation_input_provider_exception", "exception_type": type(exc).__name__, "places_orders": False}
    if inputs.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "continuation_frozen_inputs_blocked", "input_result": inputs, "places_orders": False}
    effective = _dt(inputs.get("decision_as_of"))
    preliminary = _dt(preliminary_as_of)
    if effective is None or preliminary is None or effective < preliminary:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "continuation_effective_time_invalid", "places_orders": False}
    effective_at = _iso(effective)
    lease_until = _iso(effective + timedelta(seconds=1))
    conn.execute("BEGIN IMMEDIATE")
    try:
        changed = conn.execute(
            """UPDATE dynamic_three_leg_continuation_events
                  SET state='claimed',lease_owner=?,lease_until=?,claim_generation=claim_generation+1,updated_at=?
                WHERE continuation_event_id=?
                  AND (state='pending' OR (state='claimed' AND lease_until<=?))""",
            (_text(lease_owner), lease_until, effective_at, event["continuation_event_id"], effective_at),
        ).rowcount
        if changed != 1:
            conn.rollback()
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "claim_conflict", "places_orders": False}
        generation = conn.execute(
            "SELECT claim_generation FROM dynamic_three_leg_continuation_events WHERE continuation_event_id=?",
            (event["continuation_event_id"],),
        ).fetchone()[0]
        result = commit_production_dynamic_three_leg_continuation(
            conn,
            continuation_event_id=event["continuation_event_id"],
            lease_owner=_text(lease_owner),
            claim_generation=int(generation),
            desired_trading_plan=_mapping(inputs.get("desired_trading_plan")),
            transition_plan=_mapping(inputs.get("transition_plan")),
            market_snapshot=_mapping(inputs.get("market_snapshot")),
            capital_snapshot=_mapping(inputs.get("capital_snapshot")),
            risk_snapshot=_mapping(inputs.get("risk_snapshot")),
            production_activation=_mapping(inputs.get("production_activation")),
            decision_as_of=effective_at,
        )
        if result.get("ok") is not True:
            conn.rollback()
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "writer_blocked", "writer_result": result, "places_orders": False}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "continuation_committed", "writer_result": result, "places_orders": False}


__all__ = ["SCHEMA_VERSION", "process_dynamic_three_leg_continuation_once"]
