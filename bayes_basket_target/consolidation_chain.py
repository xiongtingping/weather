"""The chain's consolidation tilt: decided once by the P0 opener, then read.

The ledger's measured rule lets the P0 opener's lights set the whole
chain's stake factor, and pricing the stateless alternative killed it:
re-reading the lights at every stage cost 81.8 USDC on the same book with
every drop-1/2/3 excision negative, because consolidation grows through
the day and late top-ups -- the profitable adds -- kept getting cut by
lights the opener never saw.  So production inherits the opener's factor,
which needs one row of cross-cycle state, and this module is that row.

Reads tolerate a missing table and return None, so the read-only compile
path (``production_io``, whose contract is never to write SQLite) can ask
without creating; the writer ensures the schema on first use.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS basket_target_consolidation_chain_tilt ("
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " tilt_factor REAL NOT NULL CHECK(tilt_factor > 0 AND tilt_factor <= 1),"
    " signals_lit_json TEXT NOT NULL,"
    " decided_at_utc TEXT NOT NULL,"
    " plan_sha256 TEXT NOT NULL DEFAULT '',"
    " PRIMARY KEY(station_id, event_date))"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA_SQL)


def load_chain_tilt(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> float | None:
    """The opener's recorded factor, or None when no opener has decided."""

    try:
        row = conn.execute(
            "SELECT tilt_factor FROM basket_target_consolidation_chain_tilt"
            " WHERE station_id=? AND event_date=?",
            (str(station_id), str(event_date)),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return float(row[0]) if row else None


def record_chain_opener_tilt(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    plan: Mapping[str, Any],
    decided_at_utc: str,
) -> bool:
    """Record the opener's factor from an authorized P0 plan, once.

    INSERT OR IGNORE keeps the first authorized P0 decision the opener
    forever -- a later P0 cycle re-authorizing does not re-decide, which
    is exactly the ledger's entry-cycle-first contract.  Returns whether
    a row was written.
    """

    if str(plan.get("stage") or "") != "P0":
        return False
    if str(plan.get("status") or "") != "ready_no_live":
        return False
    audit = plan.get("delta_v2_audit")
    tilt = (
        audit.get("consolidation_tilt")
        if isinstance(audit, Mapping) else None
    )
    if not isinstance(tilt, Mapping):
        return False
    if str(tilt.get("basis") or "") != "p0_own_lights":
        # An inherited factor means an opener row already exists; a
        # no-record basis at P0 cannot happen (P0 always earns its own).
        return False
    ensure_schema(conn)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO basket_target_consolidation_chain_tilt"
        " (station_id, event_date, tilt_factor, signals_lit_json,"
        "  decided_at_utc, plan_sha256)"
        " VALUES (?,?,?,?,?,?)",
        (
            str(station_id),
            str(event_date),
            float(tilt.get("factor") or 1.0),
            json.dumps(list(tilt.get("signals_lit") or [])),
            str(decided_at_utc),
            str(plan.get("production_plan_sha256") or ""),
        ),
    )
    return cursor.rowcount > 0
