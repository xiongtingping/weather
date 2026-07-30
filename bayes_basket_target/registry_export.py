"""Export one bounded event-date registry scope from the isolated target SSOT.

This package module is the sole implementation.  The resident NWP producer
and the command-line adapter share it so registry lineage cannot diverge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import resolve_target_database  # noqa: E402
from bayes_basket_target.market_registry import SCHEMA, SNAPSHOT_TABLE, TABLE  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json_if_changed  # noqa: E402


COLUMNS = (
    "slug", "event_date", "outcome", "city_slug", "station_id",
    "settlement_timezone", "settlement_unit", "source_lane",
    "source_eligible", "source_block_reason", "resolution_url",
    "rules_sha256", "yes_token", "no_token", "market_active",
    "market_accepting_orders", "registry_json", "refreshed_at",
)
SOURCE_SCOPE = "target_local_registry_scope_export"


def export_scope(*, db_path: Path, event_date: str, output: Path) -> dict:
    db = resolve_target_database(db_path)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        owner = conn.execute(
            f"SELECT s.snapshot_sha256,s.source_schema,s.exported_at_utc "
            f"FROM {SNAPSHOT_TABLE} s WHERE EXISTS ("
            f"SELECT 1 FROM {TABLE} r WHERE r.snapshot_sha256=s.snapshot_sha256 "
            "AND r.event_date=? AND r.source_eligible=1 AND r.market_active=1 "
            "AND r.market_accepting_orders=1) "
            "ORDER BY s.ingested_at_utc DESC,s.snapshot_sha256 DESC LIMIT 1",
            (event_date,),
        ).fetchone()
        if owner is None:
            raise RuntimeError("target_registry_event_scope_missing")
        rows = [
            {column: row[column] for column in COLUMNS}
            for row in conn.execute(
                f"SELECT {','.join(COLUMNS)} FROM {TABLE} "
                "WHERE snapshot_sha256=? AND event_date=? "
                "ORDER BY station_id,slug,outcome",
                (owner["snapshot_sha256"], event_date),
            ).fetchall()
        ]
    generated = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    identity = {
        "schema": SCHEMA,
        "source_schema": str(owner["source_schema"]),
        "source_scope": SOURCE_SCOPE,
        "source_snapshot_sha256": str(owner["snapshot_sha256"]),
        "source_database_path": str(db),
        "exported_at_utc": generated,
        "filters": {"event_date": event_date},
        "row_count": len(rows),
        "rows": rows,
        "boundary": {
            "target_only": True,
            "reads_target_database": True,
            "writes_target_database": False,
            "reads_shared_live_database": False,
            "writes_shared_live_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    payload = {**identity, "snapshot_sha256": content_sha256(identity)}
    write_bounded_json_if_changed(output, payload)
    return payload
