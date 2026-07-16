from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Mapping


SCHEMA = "target_realization_execution_lock_v1"
ACTIVE_STATES = {"active", "pending"}


def ensure_target_realization_execution_lock_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS target_realization_execution_locks (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            outcome TEXT NOT NULL,
            side TEXT NOT NULL,
            lock_type TEXT NOT NULL,
            state TEXT NOT NULL,
            source_order_id TEXT,
            plan_id TEXT,
            locked_at TEXT NOT NULL,
            expires_at TEXT,
            details_json TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (slug, event_date, outcome, side, lock_type)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_target_realization_execution_locks_active
        ON target_realization_execution_locks(
            slug, event_date, outcome, side, state, updated_at DESC
        )
        """
    )


def upsert_target_realization_execution_lock(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    lock_type: str,
    state: str,
    locked_at: str,
    updated_at: str,
    source_order_id: str = "",
    plan_id: str = "",
    expires_at: str = "",
    details: Mapping[str, Any] | None = None,
) -> None:
    ensure_target_realization_execution_lock_schema(conn)
    conn.execute(
        """
        INSERT INTO target_realization_execution_locks(
            slug, event_date, outcome, side, lock_type, state,
            source_order_id, plan_id, locked_at, expires_at,
            details_json, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug, event_date, outcome, side, lock_type) DO UPDATE SET
            state = excluded.state,
            source_order_id = CASE
                WHEN COALESCE(excluded.source_order_id, '') <> ''
                THEN excluded.source_order_id
                ELSE target_realization_execution_locks.source_order_id
            END,
            plan_id = CASE
                WHEN COALESCE(excluded.plan_id, '') <> ''
                THEN excluded.plan_id
                ELSE target_realization_execution_locks.plan_id
            END,
            locked_at = CASE
                WHEN target_realization_execution_locks.locked_at IS NULL
                  OR target_realization_execution_locks.locked_at = ''
                THEN excluded.locked_at
                ELSE target_realization_execution_locks.locked_at
            END,
            expires_at = CASE
                WHEN COALESCE(excluded.expires_at, '') <> ''
                THEN excluded.expires_at
                ELSE target_realization_execution_locks.expires_at
            END,
            details_json = CASE
                WHEN COALESCE(excluded.details_json, '') <> ''
                THEN excluded.details_json
                ELSE target_realization_execution_locks.details_json
            END,
            updated_at = excluded.updated_at
        """,
        (
            str(slug or "").strip(),
            str(event_date or "").strip()[:10],
            str(outcome or "").strip(),
            str(side or "").strip().upper(),
            str(lock_type or "").strip(),
            str(state or "").strip().lower(),
            str(source_order_id or "").strip(),
            str(plan_id or "").strip(),
            str(locked_at or "").strip(),
            str(expires_at or "").strip(),
            (
                json.dumps(dict(details), ensure_ascii=False, sort_keys=True)
                if isinstance(details, Mapping) and bool(details)
                else ""
            ),
            str(updated_at or "").strip(),
        ),
    )


def active_target_realization_execution_locks(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> List[Dict[str, Any]]:
    try:
        ensure_target_realization_execution_lock_schema(conn)
        rows = conn.execute(
            """
            SELECT slug, event_date, outcome, side, lock_type, state,
                   source_order_id, plan_id, locked_at, expires_at,
                   details_json, updated_at
            FROM target_realization_execution_locks
            WHERE slug = ?
              AND substr(event_date, 1, 10) = ?
              AND state IN ('active', 'pending')
            ORDER BY updated_at DESC, outcome, side, lock_type
            """,
            (str(slug or "").strip(), str(event_date or "").strip()[:10]),
        ).fetchall()
    except sqlite3.Error:
        return []
    result: List[Dict[str, Any]] = []
    for row in rows:
        raw = dict(row) if isinstance(row, sqlite3.Row) else {
            "slug": row[0],
            "event_date": row[1],
            "outcome": row[2],
            "side": row[3],
            "lock_type": row[4],
            "state": row[5],
            "source_order_id": row[6],
            "plan_id": row[7],
            "locked_at": row[8],
            "expires_at": row[9],
            "details_json": row[10],
            "updated_at": row[11],
        }
        try:
            details = json.loads(str(raw.get("details_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
        raw["details"] = details if isinstance(details, dict) else {}
        result.append(raw)
    return result


def has_active_target_realization_execution_lock(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
) -> str:
    locks = active_target_realization_execution_locks(
        conn,
        slug=slug,
        event_date=event_date,
    )
    for row in locks:
        if (
            str(row.get("outcome") or "") == str(outcome or "").strip()
            and str(row.get("side") or "").upper() == str(side or "").strip().upper()
        ):
            return str(row.get("lock_type") or "")
    return ""
