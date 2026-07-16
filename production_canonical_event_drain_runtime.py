"""Drain accepted AMSC canonical events into the unique production posterior."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from production_probability_p0_runtime import DEFAULT_RESOLUTION_AUTHORITY
from sequential_bayes_checkpoint_reader import load_latest_persisted_checkpoint
from production_sequential_bayes_event_runtime import (
    build_production_canonical_event_candidate,
    commit_production_canonical_event,
)


SCHEMA_VERSION = "production_canonical_event_drain_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def pending_production_canonical_event_ids(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    activation: Mapping[str, Any] | None = None,
) -> list[str]:
    target = f"physical_weather_day_v1:ZUUU:{_text(event_date)[:10]}"
    replay_generation = None
    activation_sha = _text((activation or {}).get("activation_contract_sha256"))
    if activation_sha:
        rows = conn.execute(
            """SELECT p.replay_generation
               FROM production_initial_posterior_lineage l
               JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
               WHERE l.target_identity=? AND l.activation_contract_sha256=?""",
            (target, activation_sha),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("production_probability_current_activation_lineage_not_unique")
        replay_generation = int(rows[0][0] or 0)
    latest = load_latest_persisted_checkpoint(
        conn,
        target_identity=target,
        event_date=_text(event_date)[:10],
        replay_generation=replay_generation,
    )
    consumed = set(str(value) for value in (latest or {}).get("consumed_event_ids") or [])
    return [
        _text(row[0])
        for row in conn.execute(
            """
            SELECT e.canonical_observation_event_id
            FROM canonical_observation_events e
            WHERE e.target_identity=?
              AND e.station_id='ZUUU'
              AND e.selected_source_id='zuuu_metar_raw_amsc'
              AND e.state='accepted_primary'
            ORDER BY e.valid_time,e.first_actionable_seen_at,e.canonical_observation_event_id
            """,
            (target,),
        ).fetchall()
        if _text(row[0]) and _text(row[0]) not in consumed
    ]


def drain_production_canonical_events(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    public_slug: str = "",
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
    max_events: int = 64,
    clock: Callable[[], str] = _now,
) -> dict[str, Any]:
    """Apply pending events in canonical order and stop on the first failure."""

    event_ids = pending_production_canonical_event_ids(
        conn, event_date=event_date, activation=activation
    )
    outcomes: list[dict[str, Any]] = []
    for event_id in event_ids[: max(0, int(max_events))]:
        started_at = _text(clock())
        prepare_started = time.monotonic()
        prepared = build_production_canonical_event_candidate(
            conn,
            activation=activation,
            event_date=event_date,
            canonical_observation_event_id=event_id,
            started_at=started_at,
            completed_at=started_at,
            public_slug=public_slug,
            resolution_authority_path=resolution_authority_path,
        )
        prepare_ms = round((time.monotonic() - prepare_started) * 1000.0, 3)
        if prepared.get("ok") is not True:
            outcomes.append({
                "canonical_observation_event_id": event_id,
                "prepared": prepared,
                "prepare_ms": prepare_ms,
                "ok": False,
            })
            break
        commit_started = time.monotonic()
        committed = commit_production_canonical_event(
            conn,
            activation=activation,
            prepared=prepared,
            completed_at=started_at,
        )
        commit_ms = round((time.monotonic() - commit_started) * 1000.0, 3)
        finished_at = _text(clock())
        row = conn.execute(
            "SELECT first_actionable_seen_at FROM canonical_observation_events WHERE canonical_observation_event_id=?",
            (event_id,),
        ).fetchone()
        latency_ms = None
        try:
            t1 = datetime.fromisoformat(_text(row[0]).replace("Z", "+00:00")) if row else None
            t4 = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            latency_ms = max(0.0, (t4 - t1).total_seconds() * 1000.0) if t1 is not None else None
        except ValueError:
            latency_ms = None
        outcome = {
            "canonical_observation_event_id": event_id,
            "prepared_state": prepared.get("state"),
            "commit": committed,
            "ok": committed.get("ok") is True,
            "started_at": started_at,
            "finished_at": finished_at,
            "prepare_ms": prepare_ms,
            "commit_ms": commit_ms,
            "probability_runtime_ms": round(prepare_ms + commit_ms, 3),
            "t1_to_ssot_commit_observed_ms": round(latency_ms, 3) if latency_ms is not None else None,
            "three_second_target_met": bool(latency_ms is not None and latency_ms <= 3000.0 and committed.get("ok") is True),
        }
        outcomes.append(outcome)
        if committed.get("ok") is not True:
            break
    success = all(row.get("ok") is True for row in outcomes)
    remaining = pending_production_canonical_event_ids(
        conn, event_date=event_date, activation=activation
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": success,
        "state": "drained" if success and not remaining else "partial_or_blocked",
        "event_date": _text(event_date)[:10],
        "selected_event_count": min(len(event_ids), max(0, int(max_events))),
        "committed_event_count": sum(1 for row in outcomes if row.get("ok") is True),
        "remaining_event_ids": remaining,
        "outcomes": outcomes,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "drain_production_canonical_events",
    "pending_production_canonical_event_ids",
]
