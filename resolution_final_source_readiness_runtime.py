"""Read-only preflight for final X/S evidence before label-registry append.

Legacy scalar settlement rows are intentionally reported only as diagnostics.
They never satisfy either final-source role and cannot be promoted into a
raw-object-backed X→S→B label.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "resolution_final_source_readiness_audit_v1"
PHYSICAL_ROLE = "physical_daily_high_final_truth"
SETTLEMENT_ROLE = "resolution_settlement_final"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {str(row[1] or "") for row in conn.execute(f"PRAGMA table_info({name})")}


def _final_source_summary(conn: sqlite3.Connection, *, role: str) -> dict[str, Any]:
    if not _table_exists(conn, "source_observations"):
        return {"role": role, "state": "source_observations_table_missing", "row_count": 0}
    columns = _table_columns(conn, "source_observations")
    required = {"source_id", "source_class", "role", "event_date"}
    if not required.issubset(columns):
        return {
            "role": role,
            "state": "source_observations_schema_incomplete",
            "missing_columns": sorted(required - columns),
            "row_count": 0,
        }
    rows = conn.execute(
        """
        SELECT source_id, event_date, station_id,
               COALESCE(raw_sha256,''), COALESCE(raw_ref,''),
               COALESCE(fetched_at,'')
        FROM source_observations
        WHERE source_class='settlement_source' AND role=?
        ORDER BY event_date, source_id
        """,
        (role,),
    ).fetchall()
    complete = [
        row
        for row in rows
        if _text(row[0]) and _text(row[1]) and _text(row[2]) and _text(row[3]) and _text(row[4]) and _text(row[5])
    ]
    event_days = sorted({_text(row[1])[:10] for row in complete if _text(row[1])})
    return {
        "role": role,
        "state": "available" if complete else "missing",
        "row_count": len(rows),
        "complete_raw_lineage_row_count": len(complete),
        "event_days": event_days,
        "source_ids": sorted({_text(row[0]) for row in complete if _text(row[0])}),
    }


def _legacy_scalar_diagnostics(conn: sqlite3.Connection) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for table in ("event_daily_temps", "event_runs"):
        if not _table_exists(conn, table):
            continue
        columns = _table_columns(conn, table)
        required = {"event_date", "resolution_source", "settlement_actual"}
        if not required.issubset(columns):
            values[table] = {"state": "schema_incomplete", "missing_columns": sorted(required - columns)}
            continue
        count, event_days, source_count = conn.execute(
            f"""
            SELECT COUNT(*), COUNT(DISTINCT event_date), COUNT(DISTINCT resolution_source)
            FROM {table}
            WHERE settlement_actual IS NOT NULL
            """
        ).fetchone()
        sources = [
            _text(row[0])
            for row in conn.execute(
                f"""
                SELECT DISTINCT resolution_source FROM {table}
                WHERE settlement_actual IS NOT NULL AND COALESCE(resolution_source,'')<>''
                ORDER BY resolution_source
                """
            ).fetchall()
        ]
        values[table] = {
            "state": "legacy_scalar_diagnostic_only",
            "row_count": int(count or 0),
            "event_day_count": int(event_days or 0),
            "resolution_source_count": int(source_count or 0),
            "resolution_sources": sources,
            "eligible_as_final_x_s_b_label": False,
        }
    return values


def build_resolution_final_source_readiness_audit(
    *,
    db_path: Path,
    registry_readiness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Report final-source/registry prerequisites without writing or inferring truth."""

    db = Path(db_path).expanduser().resolve()
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        physical = _final_source_summary(conn, role=PHYSICAL_ROLE)
        settlement = _final_source_summary(conn, role=SETTLEMENT_ROLE)
        legacy = _legacy_scalar_diagnostics(conn)
    physical_days = set(physical.get("event_days") or [])
    settlement_days = set(settlement.get("event_days") or [])
    paired_days = sorted(physical_days & settlement_days)
    blockers: list[str] = []
    if physical.get("state") != "available":
        blockers.append("final_physical_daily_high_source_not_configured_or_unavailable")
    if settlement.get("state") != "available":
        blockers.append("final_resolution_settlement_source_not_configured_or_unavailable")
    if not paired_days:
        blockers.append("final_x_and_s_source_observation_days_do_not_overlap")
    registry = dict(registry_readiness or {})
    if registry and registry.get("strict_x_s_b_bridge_proven") is not True:
        blockers.append("final_label_registry_not_ready")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready_for_final_label_proposal" if not blockers else "blocked",
        "db": str(db),
        "required_roles": [PHYSICAL_ROLE, SETTLEMENT_ROLE],
        "physical_final_source": physical,
        "settlement_final_source": settlement,
        "paired_final_event_days": paired_days,
        "legacy_scalar_diagnostics": legacy,
        "registry_readiness": registry,
        "blockers": sorted(set(blockers)),
        "boundary": {
            "legacy_scalar_not_promoted": True,
            "requires_explicit_final_source_roles": True,
            "requires_raw_object_lineage": True,
            "does_not_build_or_persist_labels": True,
        },
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "read_only": True,
    }


__all__ = [
    "PHYSICAL_ROLE",
    "SCHEMA_VERSION",
    "SETTLEMENT_ROLE",
    "build_resolution_final_source_readiness_audit",
]
