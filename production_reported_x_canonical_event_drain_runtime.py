"""Canonical METAR/SPECI drain for the reported-X successor epoch."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from production_probability_p0_runtime import DEFAULT_RESOLUTION_AUTHORITY
from production_reported_x_event_runtime import (
    build_reported_x_canonical_event_candidate,
    commit_reported_x_canonical_event,
)
from reported_x_successor_checkpoint_reader import load_latest_reported_x_checkpoint


SCHEMA_VERSION = "production_reported_x_canonical_event_drain_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _monotonic_started_at(conn: sqlite3.Connection, *, target: str, clock_value: str) -> str:
    row = conn.execute(
        """SELECT committed_at FROM posterior_checkpoints
           WHERE target_identity=? AND state='committed'
           ORDER BY replay_generation DESC,rowid DESC LIMIT 1""",
        (target,),
    ).fetchone()
    wall = _parse_time(clock_value)
    predecessor = _parse_time(row[0] if row else "")
    if wall is None:
        raise ValueError("reported_x_drain_clock_invalid")
    selected = max(item for item in (wall, predecessor) if item is not None)
    return selected.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def pending_reported_x_canonical_event_ids(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
) -> list[str]:
    day = _text(event_date)[:10]
    target = f"physical_weather_day_v1:ZUUU:{day}"
    generations = conn.execute(
        """SELECT p.replay_generation FROM production_initial_posterior_lineage l
           JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
           WHERE l.target_identity=? AND l.activation_contract_sha256=?""",
        (target, _text(activation.get("activation_contract_sha256"))),
    ).fetchall()
    if len(generations) != 1:
        raise ValueError("reported_x_current_activation_lineage_not_unique")
    latest = load_latest_reported_x_checkpoint(
        conn,
        target_identity=target,
        event_date=day,
        replay_generation=int(generations[0][0] or 0),
    )
    consumed = set(_text(item) for item in (latest or {}).get("consumed_event_ids") or [])
    return [
        _text(row[0])
        for row in conn.execute(
            """SELECT canonical_observation_event_id FROM canonical_observation_events
               WHERE target_identity=? AND station_id='ZUUU'
                 AND selected_source_id='zuuu_metar_raw_amsc'
                 AND state='accepted_primary'
               ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id""",
            (target,),
        ).fetchall()
        if _text(row[0]) and _text(row[0]) not in consumed
    ]


def drain_reported_x_canonical_events(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    event_date: str,
    public_slug: str = "",
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
    max_events: int = 64,
    clock: Callable[[], str] = _now,
) -> dict[str, Any]:
    event_ids = pending_reported_x_canonical_event_ids(
        conn, activation=activation, event_date=event_date
    )
    target = f"physical_weather_day_v1:ZUUU:{_text(event_date)[:10]}"
    outcomes: list[dict[str, Any]] = []
    for event_id in event_ids[: max(0, int(max_events))]:
        instant = _monotonic_started_at(
            conn, target=target, clock_value=_text(clock())
        )
        prepared = build_reported_x_canonical_event_candidate(
            conn,
            activation=activation,
            event_date=event_date,
            canonical_observation_event_id=event_id,
            started_at=instant,
            completed_at=instant,
            public_slug=public_slug,
            resolution_authority_path=resolution_authority_path,
        )
        if prepared.get("ok") is not True:
            outcomes.append({"canonical_observation_event_id": event_id, "ok": False, "prepared": prepared})
            break
        committed = commit_reported_x_canonical_event(
            conn, activation=activation, prepared=prepared, completed_at=instant
        )
        outcomes.append(
            {
                "canonical_observation_event_id": event_id,
                "ok": committed.get("ok") is True,
                "prepared_state": prepared.get("state"),
                "commit": committed,
            }
        )
        if committed.get("ok") is not True:
            break
    remaining = pending_reported_x_canonical_event_ids(
        conn, activation=activation, event_date=event_date
    )
    ok = all(item.get("ok") is True for item in outcomes)
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "state": "drained" if ok and not remaining else "partial_or_blocked",
        "event_date": _text(event_date)[:10],
        "selected_event_count": min(len(event_ids), max(0, int(max_events))),
        "committed_event_count": sum(1 for item in outcomes if item.get("ok") is True),
        "remaining_event_ids": remaining,
        "outcomes": outcomes,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "drain_reported_x_canonical_events",
    "pending_reported_x_canonical_event_ids",
]
