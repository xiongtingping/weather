"""Keep the market registry's rows for the snapshots that are still read.

Every registry refresh writes a complete copy of the fleet's markets under a
new ``snapshot_sha256``.  Nothing deletes the previous copy, so by
2026-09-07 the table held 1,176 full copies -- 1,471,712 rows for about
2,810 markets, 2.58 GB, growing 270-320 MB a day and the largest single
consumer of a database 4.78 GB from its ceiling.

The copies are not only disk.  No reader scopes a query to a snapshot: the
sha is carried on a decision as *which* registry it was made against, never
used as a filter, so every registry read sees the union of all 1,176.  A
single ``SELECT DISTINCT settlement_timezone ... WHERE station_id=? AND
event_date=?`` therefore scanned 2,233 rows to return one answer, 38.3ms
warm -- and that lookup runs once per scope inside the TAF ingest, which
measured 98.8s of a 197.2s P0-M stage on cold pages.

So the retention has two jobs and they are the same job: stop the file
growing, and stop every registry read paying for history nobody queries.

What is kept:

  * every snapshot *header* row, forever.  They are 1,176 rows of a few
    hundred bytes and they carry the identity a decision references.  A
    decision must always be able to name the registry it was made against,
    which is what the header is; the row copies are what nothing reads.
  * every registry row belonging to a snapshot ingested within the
    retention window, and the newest snapshot's rows whatever their age --
    a fleet that stops refreshing must not lose its current registry to a
    clock.
  * for each station-day whose event date is within ``REPLAY_RETENTION_DAYS``,
    the rows of the newest snapshot that lists it as an eligible, active,
    order-accepting market -- the one ``market_registry._latest_snapshot``
    resolves for that scope.  About 500 rows a day.  A decision's compile
    cannot run without its station-day's registry, so without these a
    replay of production's own decisions (the paper ledger, user ruling
    2026-09-24: 「账本对齐」) loses every event day three days after its
    last refresh: on 2026-09-25 no event date before 09-21 could be
    recompiled at all.

Deleting does not shrink the file: this database is ``auto_vacuum=NONE``
with an empty freelist, and a full VACUUM needs about as much free disk as
the database is large -- 38GB against 19GB free, so it cannot run.  What
deletion buys is pages on the freelist that the next writes reuse instead
of extending the file.  The ceiling stops approaching; it does not recede.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEMA = "market_registry_retention_v1"
#: How much registry history stays queryable as rows.
#:
#: Three days covers the event days a cycle can still act on (the open day,
#: the listed day, and the one behind them) with a day's margin.  Older
#: snapshots keep their header and lose their row copies.
RETENTION_DAYS = 3
#: How long a station-day's own registry stays replayable (see above).
REPLAY_RETENTION_DAYS = 45
#: Never delete more than this in one pass.  A retention sweep that frees
#: 900,000 rows in one transaction holds the single write lock for as long
#: as that takes, and this database has one writer for everything.
MAX_DELETE_ROWS_PER_PASS = 120_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _replay_keepers(
    conn: sqlite3.Connection, *, since_event_date: str,
) -> dict[str, set[str]]:
    """snapshot sha -> the "station|event_date" scopes it is the newest for.

    The newest by header ingest time among the snapshots that list the scope
    as eligible, active and accepting orders, where the table carries those
    flags (narrow fixtures do not; every row then counts).
    """

    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(basket_target_market_registry)")
    }
    flags = "".join(
        f" AND r.{name}=1"
        for name in ("source_eligible", "market_active", "market_accepting_orders")
        if name in columns
    )
    newest: dict[str, tuple[str, str]] = {}
    for station, day, sha, ingested in conn.execute(
        "SELECT DISTINCT r.station_id, r.event_date, r.snapshot_sha256, s.ingested_at_utc"
        " FROM basket_target_market_registry r"
        " JOIN basket_target_market_registry_snapshot s ON s.snapshot_sha256=r.snapshot_sha256"
        " WHERE r.event_date>=?" + flags,
        (since_event_date,),
    ):
        scope = f"{station}|{day}"
        if scope not in newest or str(ingested) > newest[scope][0]:
            newest[scope] = (str(ingested), str(sha))
    keepers: dict[str, set[str]] = {}
    for scope, (_ingested, sha) in newest.items():
        keepers.setdefault(sha, set()).add(scope)
    return keepers


def plan_market_registry_retention(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str | None = None,
    retention_days: int = RETENTION_DAYS,
    replay_days: int = REPLAY_RETENTION_DAYS,
) -> dict[str, Any]:
    """Which snapshots have row copies nothing reads any more.

    Planned separately from applying, for the reason the metar projection
    was split the same way: the common pass has nothing to delete, and a
    pass with nothing to delete must not open a write transaction at all.
    """

    now = (
        _utc_now() if as_of_utc is None
        else datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
    )
    cutoff = (now - timedelta(days=int(retention_days))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    newest = conn.execute(
        "SELECT snapshot_sha256 FROM basket_target_market_registry_snapshot"
        " ORDER BY ingested_at_utc DESC LIMIT 1"
    ).fetchone()
    newest_sha = str(newest[0]) if newest else ""
    candidates = [
        str(row[0]) for row in conn.execute(
            "SELECT s.snapshot_sha256"
            " FROM basket_target_market_registry_snapshot s"
            " WHERE s.ingested_at_utc < ?"
            "   AND EXISTS (SELECT 1 FROM basket_target_market_registry r"
            "               WHERE r.snapshot_sha256=s.snapshot_sha256)"
            " ORDER BY s.ingested_at_utc ASC",
            (cutoff,),
        )
        if str(row[0]) != newest_sha
    ]
    keepers = _replay_keepers(
        conn,
        since_event_date=(now - timedelta(days=int(replay_days))).strftime("%Y-%m-%d"),
    )
    stale: list[str] = []
    protected: dict[str, list[str]] = {}
    for sha in candidates:
        kept = keepers.get(sha, set())
        if not kept:
            stale.append(sha)
            continue
        deletable = sum(
            int(count) for station, day, count in conn.execute(
                "SELECT station_id, event_date, COUNT(*) FROM basket_target_market_registry"
                " WHERE snapshot_sha256=? GROUP BY station_id, event_date",
                (sha,),
            )
            if f"{station}|{day}" not in kept
        )
        if deletable:
            stale.append(sha)
            protected[sha] = sorted(kept)
    return {
        "schema": SCHEMA,
        "state": "planned",
        "cutoff_utc": cutoff,
        "retention_days": int(retention_days),
        "newest_snapshot_sha256": newest_sha,
        "stale_snapshot_count": len(stale),
        "stale_snapshots": stale,
        "replay_retention_days": int(replay_days),
        "protected_scopes": protected,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


def apply_market_registry_retention(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    *,
    max_rows: int = MAX_DELETE_ROWS_PER_PASS,
) -> dict[str, Any]:
    """Drop the row copies the plan named, bounded, newest-kept.

    The caller owns the transaction.  Snapshots are dropped oldest first, and
    whole except for the station-days a snapshot is the replay keeper of
    (``protected_scopes``): those scopes stay complete, so the registry a
    scope resolves to is still a whole copy of that scope.
    """

    deleted_rows = 0
    deleted_snapshots: list[str] = []
    for sha in plan.get("stale_snapshots") or ():
        if deleted_rows >= max_rows:
            break
        kept = (plan.get("protected_scopes") or {}).get(str(sha)) or []
        if kept:
            cursor = conn.execute(
                "DELETE FROM basket_target_market_registry WHERE snapshot_sha256=?"
                " AND (station_id || '|' || event_date) NOT IN"
                " (SELECT value FROM json_each(?))",
                (str(sha), json.dumps(list(kept))),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM basket_target_market_registry WHERE snapshot_sha256=?",
                (str(sha),),
            )
        deleted_rows += int(cursor.rowcount or 0)
        deleted_snapshots.append(str(sha))
    return {
        **plan,
        "state": "applied",
        "deleted_snapshot_count": len(deleted_snapshots),
        "deleted_row_count": deleted_rows,
        "deferred_snapshot_count": (
            len(plan.get("stale_snapshots") or ()) - len(deleted_snapshots)
        ),
        "max_rows_per_pass": int(max_rows),
    }


__all__ = [
    "MAX_DELETE_ROWS_PER_PASS",
    "REPLAY_RETENTION_DAYS",
    "RETENTION_DAYS",
    "SCHEMA",
    "apply_market_registry_retention",
    "plan_market_registry_retention",
]
