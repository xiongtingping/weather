"""Copy verified P2 canonical evidence into an explicitly isolated shadow DB.

Production ``archive.db`` remains the evidence producer and is opened only for
reads.  The destination must carry the v2 isolated-shadow sentinel and already
have the exact P1 schema.  This is a replay-input bridge, not Transaction B:
it only mirrors canonical events/source relations and rejects every attempt to
write posterior, public fact, outbox, decision or order state.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from canonical_observation_writer import persist_canonical_observation_proposal
from p2_canonical_amsc_forward_capture_audit import audit_p2_canonical_amsc_forward_capture
from p2_canonical_amsc_forward_writer import build_physical_weather_day_target_identity
from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)
from sequential_bayes_schema_contract import verify_schema


SCHEMA_VERSION = "p2_canonical_shadow_mirror_v1"
MIRROR_TABLES = ("canonical_observation_events", "canonical_observation_event_sources")
FORBIDDEN_SHADOW_TABLES = (
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


def _load_json(value: Any, *, default: Any) -> Any:
    try:
        loaded = json.loads(_text(value))
    except (TypeError, ValueError):
        return default
    return loaded


def _assert_isolated_shadow_destination(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ValueError("p2_shadow_mirror_destination_manifest_missing")
    row = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ValueError("p2_shadow_mirror_destination_manifest_invalid")
    schema = verify_schema(conn)
    if schema.get("ok") is not True:
        raise ValueError("p2_shadow_mirror_destination_p1_schema_not_ready")


def _load_source_proposal(
    source_conn: sqlite3.Connection,
    *,
    target_identity: str,
) -> dict[str, Any]:
    rows = source_conn.execute(
        """
        SELECT canonical_observation_event_id,canonical_observation_key,target_identity,
               station_id,report_type,valid_time,revision_no,normalized_physical_sha256,
               selected_source_observation_id,selected_source_id,selection_role,
               selection_policy_version,first_transport_seen_at,first_actionable_seen_at,
               accepted_at,state,supersedes_event_id,conflict_group_id,
               parsed_observation_json,quality_flags_json,created_at
        FROM canonical_observation_events
        WHERE target_identity=?
        ORDER BY valid_time,revision_no,canonical_observation_event_id
        """,
        (_text(target_identity),),
    ).fetchall()
    events: list[dict[str, Any]] = []
    event_ids: list[str] = []
    for row in rows:
        event_id = _text(row[0])
        event_ids.append(event_id)
        parsed = _load_json(row[18], default=None)
        quality = _load_json(row[19], default=None)
        if not isinstance(parsed, Mapping) or not isinstance(quality, list):
            raise ValueError("p2_shadow_mirror_source_canonical_payload_invalid")
        events.append(
            {
                "canonical_observation_event_id": event_id,
                "canonical_observation_key": _text(row[1]),
                "target_identity": _text(row[2]),
                "station_id": _text(row[3]),
                "report_type": _text(row[4]),
                "valid_time": _text(row[5]),
                "revision_no": int(row[6] or 0),
                "normalized_physical_sha256": _text(row[7]),
                "selected_source_observation_id": _text(row[8]),
                "selected_source_id": _text(row[9]),
                "selection_role": _text(row[10]),
                "selection_policy_version": _text(row[11]),
                "first_transport_seen_at": _text(row[12]),
                "first_actionable_seen_at": _text(row[13]),
                "accepted_at": _text(row[14]),
                "state": _text(row[15]),
                "supersedes_event_id": _text(row[16]),
                "conflict_group_id": _text(row[17]),
                "parsed_observation": dict(parsed),
                "quality_flags": list(quality),
                "created_at": _text(row[20]),
            }
        )
    if not event_ids:
        return {"events": [], "relations": []}
    placeholders = ",".join("?" for _ in event_ids)
    relations = source_conn.execute(
        f"""
        SELECT canonical_observation_event_id,source_observation_id,source_event_id,
               source_id,relation,seen_at,raw_sha256
        FROM canonical_observation_event_sources
        WHERE canonical_observation_event_id IN ({placeholders})
        ORDER BY canonical_observation_event_id,source_observation_id
        """,
        event_ids,
    ).fetchall()
    return {
        "events": events,
        "relations": [
            {
                "canonical_observation_event_id": _text(row[0]),
                "source_observation_id": _text(row[1]),
                "source_event_id": _text(row[2]),
                "source_id": _text(row[3]),
                "relation": _text(row[4]),
                "seen_at": _text(row[5]),
                "raw_sha256": _text(row[6]),
            }
            for row in relations
        ],
    }


def mirror_verified_p2_canonical_events_to_isolated_shadow(
    source_conn: sqlite3.Connection,
    shadow_conn: sqlite3.Connection,
    *,
    event_date: str,
    raw_archive_dir: Path | str,
    station_id: str = "ZUUU",
) -> dict[str, Any]:
    """Copy a complete verified P2 physical day in one shadow-only transaction."""

    if source_conn.in_transaction:
        raise RuntimeError("p2_shadow_mirror_source_outer_transaction_forbidden")
    if shadow_conn.in_transaction:
        raise RuntimeError("p2_shadow_mirror_destination_outer_transaction_forbidden")
    day = _text(event_date)[:10]
    station = _text(station_id).upper()
    target_identity = build_physical_weather_day_target_identity(
        station_id=station,
        event_date=day,
    )
    # Hold one read snapshot across the raw/T1 proof and the canonical rows so
    # an archive writer cannot make a partially newer source proposal appear
    # verified. This transaction is read-only and is always rolled back.
    source_conn.execute("BEGIN")
    try:
        source_proof = audit_p2_canonical_amsc_forward_capture(
            source_conn,
            event_date=day,
            raw_archive_dir=raw_archive_dir,
            station_id=station,
        )
        if source_proof.get("ok") is not True:
            raise ValueError("p2_shadow_mirror_source_capture_not_verified")
        proposal = _load_source_proposal(source_conn, target_identity=target_identity)
        expected_events = int(source_proof.get("canonical", {}).get("event_count") or 0)
        if expected_events != len(proposal["events"]):
            raise ValueError("p2_shadow_mirror_source_event_count_binding_mismatch")
    finally:
        source_conn.rollback()
    _assert_isolated_shadow_destination(shadow_conn)
    before = {table: _count_rows(shadow_conn, table) for table in (*MIRROR_TABLES, *FORBIDDEN_SHADOW_TABLES)}
    shadow_conn.execute("BEGIN IMMEDIATE")
    try:
        persistence = persist_canonical_observation_proposal(shadow_conn, proposal)
        after = {table: _count_rows(shadow_conn, table) for table in (*MIRROR_TABLES, *FORBIDDEN_SHADOW_TABLES)}
        forbidden_changes = {
            table: {"before": before[table], "after": after[table]}
            for table in FORBIDDEN_SHADOW_TABLES
            if before[table] != after[table]
        }
        if forbidden_changes:
            raise RuntimeError("p2_shadow_mirror_forbidden_shadow_write_detected")
        shadow_conn.commit()
    except Exception:
        shadow_conn.rollback()
        raise
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "mirrored",
        "event_date": day,
        "station_id": station,
        "target_identity": target_identity,
        "source_capture_proof": {
            "strict_primary_count": source_proof.get("strict_primary", {}).get("count"),
            "canonical_event_count": source_proof.get("canonical", {}).get("event_count"),
            "canonical_relation_count": source_proof.get("canonical", {}).get("relation_count"),
        },
        "persistence": persistence,
        "row_counts": {"before": before, "after": after},
        "writes": {
            "production_archive": False,
            "shadow_canonical_events": int(persistence.get("inserted_events") or 0),
            "shadow_canonical_relations": int(persistence.get("inserted_relations") or 0),
            "forbidden_shadow_table_changes": {},
            "posterior": False,
            "public_fact": False,
            "outbox": False,
            "decision_or_order": False,
        },
        "boundary": {
            "source_archive_read_only": True,
            "requires_isolated_shadow_manifest": True,
            "replays_physical_evidence_only": True,
            "not_transaction_b": True,
            "not_p10_latency_evidence": True,
        },
    }


__all__ = ["SCHEMA_VERSION", "mirror_verified_p2_canonical_events_to_isolated_shadow"]
