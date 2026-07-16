"""Forward-only P2 writer for strict primary AMSC canonical observations.

The P1 schema is shared with later sequential-Bayes stages, but this module
owns a deliberately narrow first production use of it: raw-object-backed
primary AMSC METAR/SPECI rows become append-only ``CanonicalObservationEvent``
records.  It does not create a strong update, checkpoint, public fact, outbox,
decision, intent, or order.

The canonical stream is identified by the physical station day rather than a
mutable market slug or an unapproved resolution contract.  A later approved
``X -> S -> B`` target may consume this physical evidence, but cannot rewrite
its identity or arrival timestamps.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import sqlite3
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from canonical_observation_runtime import canonicalize_observations
from canonical_observation_writer import persist_canonical_observation_proposal
from reported_x_successor_schema_contract import verify_schema
from strict_amsc_hard_observation_reader import load_strict_amsc_primary_hard_observations


SCHEMA_VERSION = "p2_canonical_amsc_forward_writer_v1"
PHYSICAL_TARGET_IDENTITY_VERSION = "physical_weather_day_v1"
SELECTION_POLICY_VERSION = "amsc_primary_awc_confirmation_substitute_v1"
CANONICAL_TABLES = (
    "canonical_observation_events",
    "canonical_observation_event_sources",
)
FORBIDDEN_WRITE_TABLES = (
    "strong_observation_updates",
    "posterior_checkpoints",
    "realization_states",
    "posterior_commit_outbox",
    "event_fact_history",
    "event_fact_latest",
    "auto_trade_decisions",
    "decision_intents",
    "auto_trade_jobs",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_utc(value: Any) -> datetime | None:
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


def _parse_day(value: Any) -> str:
    text = _text(value)[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    )


def _count_rows(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0] or 0) if row else 0


def build_physical_weather_day_target_identity(*, station_id: str, event_date: str) -> str:
    """Return the immutable P2 stream identity before a market bridge exists."""

    station = _text(station_id).upper()
    day = _parse_day(event_date)
    if not re.fullmatch(r"[A-Z0-9]{4}", station):
        raise ValueError("p2_canonical_station_id_invalid")
    if not day:
        raise ValueError("p2_canonical_event_date_invalid")
    return f"{PHYSICAL_TARGET_IDENTITY_VERSION}:{station}:{day}"


def _load_existing_events(
    conn: sqlite3.Connection,
    *,
    target_identity: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT canonical_observation_event_id,canonical_observation_key,
               normalized_physical_sha256,revision_no,state,accepted_at,
               selected_source_id
        FROM canonical_observation_events
        WHERE target_identity=?
        ORDER BY valid_time,revision_no,canonical_observation_event_id
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


def _summary_of_strict_read(loaded: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": _text(loaded.get("schema")),
        "ok": loaded.get("ok") is True,
        "state": _text(loaded.get("state")),
        "source_observation_count": int(loaded.get("source_observation_count") or 0),
        "quarantined_source_count": len(loaded.get("blocked") or []),
        "block_reasons": list(loaded.get("block_reasons") or []),
    }


def build_p2_canonical_amsc_forward_append_plan(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    station_id: str = "ZUUU",
    raw_archive_dir: Path | str,
    decision_as_of: str,
    target_identity: str = "",
    source_observation_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a no-write canonical append proposal from strict AMSC evidence."""

    day = _parse_day(event_date)
    station = _text(station_id).upper()
    blockers: list[str] = []
    try:
        expected_target = build_physical_weather_day_target_identity(
            station_id=station,
            event_date=day,
        )
    except ValueError as exc:
        expected_target = ""
        blockers.append(str(exc))
    supplied_target = _text(target_identity)
    if supplied_target and expected_target and supplied_target != expected_target:
        blockers.append("p2_canonical_target_identity_must_be_physical_weather_day")
    as_of = _parse_utc(decision_as_of)
    if as_of is None:
        blockers.append("p2_canonical_decision_as_of_invalid")
    schema = verify_schema(conn)
    if schema.get("ok") is not True:
        blockers.append("p2_canonical_p1_schema_not_ready")

    loaded: dict[str, Any] = {}
    proposal: dict[str, Any] = {}
    existing: list[dict[str, Any]] = []
    identity = supplied_target or expected_target
    if not blockers:
        requested_ids = None if source_observation_ids is None else sorted({
            _text(value) for value in source_observation_ids if _text(value)
        })
        if requested_ids == []:
            loaded = {
                "schema": "strict_amsc_hard_observation_reader_v1",
                "ok": True,
                "state": "no_new_source_observations",
                "source_observations": [],
                "source_observation_count": 0,
                "blocked": [],
                "block_reasons": [],
            }
        else:
            loaded = load_strict_amsc_primary_hard_observations(
                conn,
                event_date=day,
                archive_dir=raw_archive_dir,
                source_observation_ids=requested_ids,
            )
        if loaded.get("ok") is not True:
            blockers.append("p2_canonical_strict_amsc_primary_unavailable")
        else:
            observations = list(loaded.get("source_observations") or [])
            invalid_station_count = sum(
                1 for row in observations if _text(row.get("station_id")).upper() != station
            )
            if invalid_station_count:
                blockers.append("p2_canonical_strict_amsc_station_binding_mismatch")
            else:
                existing = _load_existing_events(conn, target_identity=identity)
                proposal = canonicalize_observations(
                    observations,
                    target_identity=identity,
                    event_date=day,
                    decision_as_of=_text(decision_as_of),
                    allow_awc_substitute=False,
                    selection_policy_version=SELECTION_POLICY_VERSION,
                    existing_events=existing,
                )
                if proposal.get("rejected"):
                    blockers.append("p2_canonical_primary_proposal_rejected")
                events = list(proposal.get("events") or [])
                if any(_text(event.get("selected_source_id")) != "zuuu_metar_raw_amsc" for event in events):
                    blockers.append("p2_canonical_non_primary_source_proposed")
                if any(_text(event.get("state")) not in {"accepted_primary", "candidate", "conflict_blocked"} for event in events):
                    blockers.append("p2_canonical_unexpected_event_state")

    events = list(proposal.get("events") or [])
    relations = list(proposal.get("relations") or [])
    accepted = [event for event in events if _text(event.get("state")) == "accepted_primary"]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "target_identity": identity,
        "target_identity_kind": "physical_station_day_not_market_or_resolution_contract",
        "event_date": day,
        "station_id": station,
        "decision_as_of": _text(decision_as_of),
        "strict_source_read": _summary_of_strict_read(loaded),
        "existing_canonical_event_count": len(existing),
        "proposal": proposal,
        "proposal_summary": {
            "event_count": len(events),
            "accepted_primary_event_count": len(accepted),
            "relation_count": len(relations),
            "rejected_source_count": len(proposal.get("rejected") or []),
        },
        "schema_verification": schema,
        "write_scope": {
            "allowed_tables": list(CANONICAL_TABLES),
            "forbidden_tables": list(FORBIDDEN_WRITE_TABLES),
            "updates_existing_rows": False,
            "creates_strong_update": False,
            "creates_posterior": False,
            "creates_fact_or_latest": False,
            "creates_outbox": False,
            "creates_decision_or_order": False,
            "awc_primary_or_substitute": False,
            "historical_backfill": False,
        },
    }


def apply_p2_canonical_amsc_forward_append(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    station_id: str = "ZUUU",
    raw_archive_dir: Path | str,
    decision_as_of: str,
    target_identity: str = "",
    source_observation_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Atomically append the current strict AMSC canonical proposal.

    The caller must own the archive writer gate.  This function owns the
    SQLite transaction and refuses an outer transaction, so it cannot be
    accidentally merged with a posterior, fact or order write.
    """

    if conn.in_transaction:
        raise RuntimeError("p2_canonical_forward_outer_transaction_forbidden")
    # Enforce the write boundary at the SQLite VM level. The previous runtime
    # counted every row in every forbidden table before and after each event;
    # that diagnostic added hundreds of milliseconds to T1 -> T3 and still
    # detected a violation only after it happened. The authorizer rejects the
    # first forbidden INSERT/UPDATE/DELETE before SQLite executes it.
    forbidden_write_attempts: list[dict[str, Any]] = []
    write_actions = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}

    def authorize(action: int, arg1: str | None, arg2: str | None, db_name: str | None, trigger: str | None) -> int:
        table = _text(arg1)
        if action in write_actions and table in FORBIDDEN_WRITE_TABLES:
            forbidden_write_attempts.append(
                {"action": int(action), "table": table, "column": _text(arg2)}
            )
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    before = {table: _count_rows(conn, table) for table in CANONICAL_TABLES}
    conn.set_authorizer(authorize)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            plan = build_p2_canonical_amsc_forward_append_plan(
                conn,
                event_date=event_date,
                station_id=station_id,
                raw_archive_dir=raw_archive_dir,
                decision_as_of=decision_as_of,
                target_identity=target_identity,
                source_observation_ids=source_observation_ids,
            )
            if plan.get("ok") is not True:
                raise ValueError("p2_canonical_forward_plan_blocked:" + ",".join(plan.get("blockers") or []))
            persistence = persist_canonical_observation_proposal(conn, plan.get("proposal") or {})
            if forbidden_write_attempts:
                raise RuntimeError("p2_canonical_forward_forbidden_write_rejected")
            after = {table: _count_rows(conn, table) for table in CANONICAL_TABLES}
            conn.commit()
        except Exception:
            conn.rollback()
            if forbidden_write_attempts:
                raise RuntimeError("p2_canonical_forward_forbidden_write_rejected")
            raise
    finally:
        conn.set_authorizer(None)
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "applied",
        "plan": plan,
        "persistence": persistence,
        "row_counts": {
            "before": before,
            "after": after,
            "scope": "canonical_tables_only",
            "forbidden_tables_guard": "sqlite_authorizer_preexecution_deny",
        },
        "writes": {
            "canonical_events": int(persistence.get("inserted_events") or 0),
            "canonical_relations": int(persistence.get("inserted_relations") or 0),
            "forbidden_table_changes": {},
        },
        "shadow_or_capture_only": True,
        "places_orders": False,
    }


__all__ = [
    "CANONICAL_TABLES",
    "FORBIDDEN_WRITE_TABLES",
    "PHYSICAL_TARGET_IDENTITY_VERSION",
    "SCHEMA_VERSION",
    "apply_p2_canonical_amsc_forward_append",
    "build_p2_canonical_amsc_forward_append_plan",
    "build_physical_weather_day_target_identity",
]
