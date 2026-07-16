"""Durable, isolated first-seen journal for the resident AMSC shadow lane.

This module is deliberately not a watcher. A host supplies one already parsed
source observation at a time. The journal commits the physical arrival's first
seen timestamp before it invokes the resident ingress, so a restart or a later
re-fetch cannot mint a new T1. It never performs network I/O, starts a child
process, touches the generic queue, or permits production writes/orders.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import threading
from typing import Any, Mapping, Protocol

from amsc_specli_resident_arrival_contract import (
    build_amsc_specli_resident_arrival_envelope,
)
from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)


SCHEMA_VERSION = "resident_amsc_specli_arrival_journal_shadow_v1"
JOURNAL_TABLE = "resident_amsc_specli_arrival_journal"
TERMINAL_STATES = frozenset({"dispatched", "deadline_blocked"})


class _ArrivalIngress(Protocol):
    def process_arrival_envelope(self, **kwargs: Any) -> Mapping[str, Any]: ...


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


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    )


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ValueError("resident_arrival_journal_shadow_manifest_missing")
    row = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ValueError("resident_arrival_journal_shadow_manifest_invalid")


def initialize_resident_amsc_specli_arrival_journal_shadow_schema(
    conn: sqlite3.Connection,
) -> None:
    """Create only isolated-shadow journal state."""

    _assert_isolated_shadow_db(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {JOURNAL_TABLE} (
            physical_arrival_key TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            station_id TEXT NOT NULL,
            report_type TEXT NOT NULL,
            valid_time TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            first_actionable_seen_at TEXT NOT NULL,
            first_response_completed_at TEXT NOT NULL,
            latest_source_observation_id TEXT NOT NULL,
            latest_source_event_id TEXT NOT NULL,
            state TEXT NOT NULL,
            dispatch_count INTEGER NOT NULL CHECK(dispatch_count >= 0),
            terminal_reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{JOURNAL_TABLE}_state
        ON {JOURNAL_TABLE}(state, first_actionable_seen_at)
        """
    )
    conn.commit()


def _physical_arrival_key(envelope: Mapping[str, Any]) -> str:
    source = _mapping(envelope.get("source_observation"))
    payload = _mapping(source.get("payload"))
    identity = {
        "source_id": _text(source.get("source_id")),
        "station_id": _text(source.get("station_id")).upper(),
        "report_type": _text(payload.get("report_type") or source.get("report_type")).upper(),
        "valid_time": _text(source.get("valid_time")),
        "raw_sha256": _text(source.get("raw_sha256")),
    }
    encoded = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "amscarr_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _row_dict(row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any]:
    if row is None:
        return {}
    keys = (
        "physical_arrival_key",
        "source_id",
        "station_id",
        "report_type",
        "valid_time",
        "raw_sha256",
        "first_seen_at",
        "first_actionable_seen_at",
        "first_response_completed_at",
        "latest_source_observation_id",
        "latest_source_event_id",
        "state",
        "dispatch_count",
        "terminal_reason",
        "created_at",
        "updated_at",
    )
    return {key: row[index] for index, key in enumerate(keys)}


def _deadline_blocked(result: Mapping[str, Any]) -> bool:
    ingress = _mapping(result.get("ingress"))
    blockers = {str(value) for value in ingress.get("blockers") or []}
    return "amsc_specli_ingress_observation_hard_deadline_exceeded" in blockers


def _source_with_journal_t1(source_observation: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    source = copy.deepcopy(_mapping(source_observation))
    payload = _mapping(source.get("payload"))
    first_seen_at = _text(row.get("first_seen_at"))
    payload["first_seen_at"] = first_seen_at
    payload["arrival_time_quality"] = "observed_first_seen"
    as_of = _parse_dt(payload.get("as_of_eligible_at"))
    first_seen = _parse_dt(first_seen_at)
    if as_of is None or first_seen is None or as_of < first_seen:
        payload["as_of_eligible_at"] = first_seen_at
    source["payload"] = payload
    return source


class ResidentAmscSpecliArrivalJournalShadow:
    """Persist source first-seen identity before resident shadow dispatch."""

    def __init__(self, conn: sqlite3.Connection, *, ingress: _ArrivalIngress) -> None:
        _assert_isolated_shadow_db(conn)
        self._conn = conn
        self._ingress = ingress
        self._lock = threading.Lock()
        initialize_resident_amsc_specli_arrival_journal_shadow_schema(conn)

    def _capture_or_load(self, envelope: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Commit the immutable first-seen row before any posterior work."""

        if self._conn.in_transaction:
            raise RuntimeError("resident_arrival_journal_outer_transaction_forbidden")
        key = _physical_arrival_key(envelope)
        source = _mapping(envelope.get("source_observation"))
        payload = _mapping(source.get("payload"))
        now = _text(envelope.get("first_actionable_seen_at"))
        if not now:
            raise ValueError("resident_arrival_journal_first_actionable_missing")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                f"SELECT * FROM {JOURNAL_TABLE} WHERE physical_arrival_key=?",
                (key,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    f"""
                    INSERT INTO {JOURNAL_TABLE}(
                        physical_arrival_key,source_id,station_id,report_type,valid_time,raw_sha256,
                        first_seen_at,first_actionable_seen_at,first_response_completed_at,
                        latest_source_observation_id,latest_source_event_id,state,dispatch_count,
                        terminal_reason,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        key,
                        _text(source.get("source_id")),
                        _text(source.get("station_id")).upper(),
                        _text(payload.get("report_type") or source.get("report_type")).upper(),
                        _text(source.get("valid_time")),
                        _text(source.get("raw_sha256")),
                        _text(envelope.get("first_transport_seen_at")),
                        _text(envelope.get("first_actionable_seen_at")),
                        _text(envelope.get("response_completed_at")),
                        _text(source.get("source_observation_id")),
                        _text(source.get("source_event_id")),
                        "pending",
                        0,
                        "",
                        now,
                        now,
                    ),
                )
                row_dict = _row_dict(
                    self._conn.execute(
                        f"SELECT * FROM {JOURNAL_TABLE} WHERE physical_arrival_key=?", (key,)
                    ).fetchone()
                )
                is_new = True
            else:
                row_dict = _row_dict(row)
                is_new = False
            self._conn.commit()
            return row_dict, is_new
        except Exception:
            self._conn.rollback()
            raise

    def _record_dispatch_result(
        self,
        *,
        key: str,
        source_observation: Mapping[str, Any],
        decision_as_of: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self._conn.in_transaction:
            raise RuntimeError("resident_arrival_journal_outer_transaction_forbidden")
        state = "dispatched" if result.get("ok") is True else "retryable"
        terminal_reason = ""
        if _deadline_blocked(result):
            state = "deadline_blocked"
            terminal_reason = "amsc_specli_ingress_observation_hard_deadline_exceeded"
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                f"""
                UPDATE {JOURNAL_TABLE}
                SET latest_source_observation_id=?, latest_source_event_id=?, state=?,
                    dispatch_count=dispatch_count+1, terminal_reason=?, updated_at=?
                WHERE physical_arrival_key=?
                """,
                (
                    _text(source_observation.get("source_observation_id")),
                    _text(source_observation.get("source_event_id")),
                    state,
                    terminal_reason,
                    _text(decision_as_of),
                    key,
                ),
            )
            row = self._conn.execute(
                f"SELECT * FROM {JOURNAL_TABLE} WHERE physical_arrival_key=?", (key,)
            ).fetchone()
            self._conn.commit()
            return _row_dict(row)
        except Exception:
            self._conn.rollback()
            raise

    def process_source_observation(
        self,
        *,
        source_observation: Mapping[str, Any],
        target_identity: str,
        event_date: str,
        decision_as_of: str,
        canonicalized_at: str,
        posterior_transaction_inputs: Mapping[str, Any],
        decision_cycle_inputs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Capture then dispatch one physical arrival; duplicate terminal rows stop."""

        if not self._lock.acquire(blocking=False):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "resident_arrival_journal_concurrent_dispatch_blocked",
                "shadow_only": True,
                "places_orders": False,
            }
        try:
            incoming_envelope = build_amsc_specli_resident_arrival_envelope(source_observation)
            if incoming_envelope.get("ok") is not True:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked_before_journal_capture",
                    "arrival_envelope": incoming_envelope,
                    "shadow_only": True,
                    "places_orders": False,
                }
            journal_row, is_new = self._capture_or_load(incoming_envelope)
            key = _text(journal_row.get("physical_arrival_key"))
            if not is_new and _text(journal_row.get("state")) in TERMINAL_STATES:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": True,
                    "state": "duplicate_physical_arrival_suppressed",
                    "journal": journal_row,
                    "first_actionable_seen_at": _text(journal_row.get("first_actionable_seen_at")),
                    "shadow_only": True,
                    "places_orders": False,
                }
            journal_source = _source_with_journal_t1(source_observation, journal_row)
            envelope = build_amsc_specli_resident_arrival_envelope(journal_source)
            if envelope.get("ok") is not True:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked_after_journal_capture",
                    "journal": journal_row,
                    "arrival_envelope": envelope,
                    "shadow_only": True,
                    "places_orders": False,
                }
            ingress_result = dict(
                self._ingress.process_arrival_envelope(
                    arrival_envelope=envelope,
                    target_identity=_text(target_identity),
                    event_date=_text(event_date),
                    decision_as_of=_text(decision_as_of),
                    canonicalized_at=_text(canonicalized_at),
                    posterior_transaction_inputs=_mapping(posterior_transaction_inputs),
                    decision_cycle_inputs=_mapping(decision_cycle_inputs),
                )
            )
            journal = self._record_dispatch_result(
                key=key,
                source_observation=journal_source,
                decision_as_of=decision_as_of,
                result=ingress_result,
            )
            return {
                "schema": SCHEMA_VERSION,
                "ok": ingress_result.get("ok") is True,
                "state": "dispatched" if ingress_result.get("ok") is True else "ingress_blocked",
                "is_new_physical_arrival": is_new,
                "journal": journal,
                "arrival_envelope": envelope,
                "ingress": ingress_result,
                "shadow_only": True,
                "places_orders": False,
                "boundary": {
                    "durable_first_seen_before_dispatch": True,
                    "re_fetch_resets_t1": False,
                    "generic_task_queue": False,
                    "network_acquisition": False,
                    "production_archive_write": False,
                },
            }
        finally:
            self._lock.release()


__all__ = [
    "JOURNAL_TABLE",
    "SCHEMA_VERSION",
    "ResidentAmscSpecliArrivalJournalShadow",
    "initialize_resident_amsc_specli_arrival_journal_shadow_schema",
]
