"""Read-only evidence audit for P2 primary AMSC canonical capture.

This verifies that every strict raw-object/manifest/T1-bound primary AMSC
source observation for one physical station day has a corresponding accepted
canonical event and source relation.  It deliberately does not inspect or
authorize posterior/decision/order surfaces: P2 capture is evidence only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from p2_canonical_amsc_forward_writer import build_physical_weather_day_target_identity
from strict_amsc_hard_observation_reader import load_strict_amsc_primary_hard_observations


SCHEMA_VERSION = "p2_canonical_amsc_forward_capture_audit_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    )


def audit_p2_canonical_amsc_forward_capture(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    raw_archive_dir: Path | str,
    station_id: str = "ZUUU",
) -> dict[str, Any]:
    """Audit one physical day without creating any evidence or state."""

    day = _text(event_date)[:10]
    station = _text(station_id).upper()
    blockers: list[str] = []
    try:
        target_identity = build_physical_weather_day_target_identity(
            station_id=station,
            event_date=day,
        )
    except ValueError as exc:
        target_identity = ""
        blockers.append(str(exc))
    required_tables = ("canonical_observation_events", "canonical_observation_event_sources")
    missing_tables = [table for table in required_tables if not _table_exists(conn, table)]
    if missing_tables:
        blockers.append("p2_canonical_capture_tables_missing")

    strict: Mapping[str, Any] = {}
    canonical_rows: list[dict[str, Any]] = []
    relation_rows: list[dict[str, Any]] = []
    if not blockers:
        strict = load_strict_amsc_primary_hard_observations(
            conn,
            event_date=day,
            archive_dir=raw_archive_dir,
        )
        if strict.get("ok") is not True:
            blockers.append("p2_canonical_strict_primary_amsc_unavailable")
        rows = conn.execute(
            """
            SELECT canonical_observation_event_id,selected_source_observation_id,
                   selected_source_id,report_type,valid_time,first_transport_seen_at,
                   first_actionable_seen_at,state
            FROM canonical_observation_events
            WHERE target_identity=?
            ORDER BY valid_time,canonical_observation_event_id
            """,
            (target_identity,),
        ).fetchall()
        canonical_rows = [
            {
                "canonical_observation_event_id": _text(row[0]),
                "selected_source_observation_id": _text(row[1]),
                "selected_source_id": _text(row[2]),
                "report_type": _text(row[3]),
                "valid_time": _text(row[4]),
                "first_transport_seen_at": _text(row[5]),
                "first_actionable_seen_at": _text(row[6]),
                "state": _text(row[7]),
            }
            for row in rows
        ]
        relations = conn.execute(
            """
            SELECT canonical_observation_event_id,source_observation_id,source_event_id,
                   source_id,relation,seen_at,raw_sha256
            FROM canonical_observation_event_sources
            WHERE canonical_observation_event_id IN (
                SELECT canonical_observation_event_id
                FROM canonical_observation_events WHERE target_identity=?
            )
            ORDER BY canonical_observation_event_id,source_observation_id
            """,
            (target_identity,),
        ).fetchall()
        relation_rows = [
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
        ]

    strict_rows = list(strict.get("source_observations") or [])
    strict_by_id = {
        _text(row.get("source_observation_id")): row
        for row in strict_rows
        if _text(row.get("source_observation_id"))
    }
    selected_ids = {_text(row.get("selected_source_observation_id")) for row in canonical_rows}
    relation_ids = {_text(row.get("source_observation_id")) for row in relation_rows}
    missing_selected = sorted(set(strict_by_id) - selected_ids)
    missing_relation = sorted(set(strict_by_id) - relation_ids)
    unexpected_selected = sorted(selected_ids - set(strict_by_id))
    non_primary_events = [
        row["canonical_observation_event_id"]
        for row in canonical_rows
        if row["selected_source_id"] != "zuuu_metar_raw_amsc" or row["state"] != "accepted_primary"
    ]
    event_time_mismatches: list[str] = []
    for event in canonical_rows:
        source = strict_by_id.get(event["selected_source_observation_id"])
        payload = source.get("payload") if isinstance(source, Mapping) else {}
        if not isinstance(payload, Mapping):
            payload = {}
        if source and (
            _text(event["first_transport_seen_at"]) != _text(payload.get("first_seen_at"))
            or _text(event["first_actionable_seen_at"]) != _text(payload.get("as_of_eligible_at"))
        ):
            event_time_mismatches.append(event["canonical_observation_event_id"])
    if strict_rows and not canonical_rows:
        blockers.append("p2_canonical_primary_capture_missing")
    if missing_selected:
        blockers.append("p2_canonical_strict_primary_selected_event_missing")
    if missing_relation:
        blockers.append("p2_canonical_strict_primary_relation_missing")
    if unexpected_selected:
        blockers.append("p2_canonical_unbound_selected_source_present")
    if non_primary_events:
        blockers.append("p2_canonical_non_primary_or_nonaccepted_event_present")
    if event_time_mismatches:
        blockers.append("p2_canonical_event_t1_binding_mismatch")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "read_only": True,
        "event_date": day,
        "station_id": station,
        "target_identity": target_identity,
        "strict_primary": {
            "count": len(strict_by_id),
            "quarantined_count": len(strict.get("blocked") or []),
            "state": _text(strict.get("state")),
            "block_reasons": list(strict.get("block_reasons") or []),
        },
        "canonical": {
            "event_count": len(canonical_rows),
            "relation_count": len(relation_rows),
            "missing_selected_source_observation_ids": missing_selected,
            "missing_relation_source_observation_ids": missing_relation,
            "unexpected_selected_source_observation_ids": unexpected_selected,
            "non_primary_or_nonaccepted_event_ids": non_primary_events,
            "event_t1_binding_mismatch_ids": event_time_mismatches,
        },
        "boundary": {
            "primary_amsc_only": True,
            "physical_station_day_identity": True,
            "posterior_or_transaction_b_evidence": False,
            "decision_or_order_evidence": False,
            "p10_latency_evidence": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "audit_p2_canonical_amsc_forward_capture"]
