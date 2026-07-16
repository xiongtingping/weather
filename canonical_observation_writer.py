"""Append-only persistence owner for CanonicalObservationEvent proposals."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping


SCHEMA_VERSION = "canonical_observation_writer_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _require(event: Mapping[str, Any], *keys: str) -> None:
    missing = [key for key in keys if not _text(event.get(key))]
    if missing:
        raise ValueError(f"canonical_event_required_fields_missing:{','.join(missing)}")


def persist_canonical_observation_proposal(
    conn: sqlite3.Connection,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Insert a read-only proposal without mutating prior canonical records.

    The caller owns the outer transaction.  Existing event/relation rows are
    idempotently retained; no conflict path is allowed to overwrite them.
    """

    events = proposal.get("events") if isinstance(proposal.get("events"), list) else []
    relations = proposal.get("relations") if isinstance(proposal.get("relations"), list) else []
    inserted_events = 0
    existing_events = 0
    inserted_relations = 0
    existing_relations = 0
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("canonical_event_not_mapping")
        _require(
            event,
            "canonical_observation_event_id",
            "canonical_observation_key",
            "target_identity",
            "station_id",
            "report_type",
            "valid_time",
            "normalized_physical_sha256",
            "selected_source_observation_id",
            "selected_source_id",
            "selection_role",
            "selection_policy_version",
            "first_transport_seen_at",
            "first_actionable_seen_at",
            "accepted_at",
            "state",
            "created_at",
        )
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO canonical_observation_events(
                canonical_observation_event_id, canonical_observation_key,
                target_identity, station_id, report_type, valid_time, revision_no,
                normalized_physical_sha256, selected_source_observation_id,
                selected_source_id, selection_role, selection_policy_version,
                first_transport_seen_at, first_actionable_seen_at, accepted_at,
                state, supersedes_event_id, conflict_group_id,
                parsed_observation_json, quality_flags_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _text(event.get("canonical_observation_event_id")),
                _text(event.get("canonical_observation_key")),
                _text(event.get("target_identity")),
                _text(event.get("station_id")),
                _text(event.get("report_type")),
                _text(event.get("valid_time")),
                int(event.get("revision_no") or 0),
                _text(event.get("normalized_physical_sha256")),
                _text(event.get("selected_source_observation_id")),
                _text(event.get("selected_source_id")),
                _text(event.get("selection_role")),
                _text(event.get("selection_policy_version")),
                _text(event.get("first_transport_seen_at")),
                _text(event.get("first_actionable_seen_at")),
                _text(event.get("accepted_at")),
                _text(event.get("state")),
                _text(event.get("supersedes_event_id")),
                _text(event.get("conflict_group_id")),
                _json(event.get("parsed_observation")),
                _json(event.get("quality_flags") or []),
                _text(event.get("created_at")),
            ),
        )
        if cur.rowcount:
            inserted_events += 1
        else:
            existing_events += 1
    for relation in relations:
        if not isinstance(relation, Mapping):
            raise ValueError("canonical_relation_not_mapping")
        _require(
            relation,
            "canonical_observation_event_id",
            "source_observation_id",
            "source_event_id",
            "source_id",
            "relation",
            "seen_at",
            "raw_sha256",
        )
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO canonical_observation_event_sources(
                canonical_observation_event_id, source_observation_id,
                source_event_id, source_id, relation, seen_at, raw_sha256
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                _text(relation.get("canonical_observation_event_id")),
                _text(relation.get("source_observation_id")),
                _text(relation.get("source_event_id")),
                _text(relation.get("source_id")),
                _text(relation.get("relation")),
                _text(relation.get("seen_at")),
                _text(relation.get("raw_sha256")),
            ),
        )
        if cur.rowcount:
            inserted_relations += 1
        else:
            existing_relations += 1
    return {
        "schema": SCHEMA_VERSION,
        "inserted_events": inserted_events,
        "existing_events": existing_events,
        "inserted_relations": inserted_relations,
        "existing_relations": existing_relations,
        "updates_existing_rows": False,
    }
