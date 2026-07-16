#!/usr/bin/env python3
"""Read-only audit for append-only source-observation identity and replay readiness."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from source_observation_replay_eligibility_runtime import (  # noqa: E402
    classify_source_observation_replay_eligibility,
)

DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "source_observation_append_only_audit_v1"


def _table_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return list(conn.execute(f'PRAGMA table_info("{table}")').fetchall())


def _has_unique_source_observation_index(conn: sqlite3.Connection) -> bool:
    for row in conn.execute('PRAGMA index_list("source_observations")').fetchall():
        index_name = str(row[1] or "")
        is_unique = bool(row[2])
        if not is_unique or not index_name:
            continue
        columns = [str(item[2] or "") for item in conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()]
        if columns == ["source_observation_id"]:
            return True
    return False


def build_source_observation_append_only_audit(db_path: Path | str = DEFAULT_DB) -> dict[str, Any]:
    """Inspect the current source ledger without changing its schema or rows."""

    db = Path(db_path).expanduser().resolve()
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            table_exists = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_observations'"
                ).fetchone()
            )
            if not table_exists:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "db": str(db),
                    "blockers": ["source_observations_table_missing"],
                    "guards": {"writes_archive_db": False, "places_orders": False},
                }
            columns = _table_columns(conn, "source_observations")
            column_names = {str(row[1] or "") for row in columns}
            source_event_is_pk = any(str(row[1] or "") == "source_event_id" and int(row[5] or 0) > 0 for row in columns)
            source_observation_unique = _has_unique_source_observation_index(conn)
            required = {
                "source_event_id",
                "source_observation_id",
                "source_id",
                "event_date",
                "valid_time",
                "fetched_at",
                "raw_ref",
                "raw_sha256",
            }
            missing_columns = sorted(required - column_names)
            if missing_columns:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "db": str(db),
                    "append_only_contract_ok": False,
                    "strict_asof_replay_ready": False,
                    "source_event_id_primary_key": source_event_is_pk,
                    "source_observation_id_unique": source_observation_unique,
                    "missing_required_columns": missing_columns,
                    "blockers": ["source_observations_required_columns_missing"],
                    "guards": {"writes_archive_db": False, "places_orders": False},
                }
            parser_select = "parser_version" if "parser_version" in column_names else "'' AS parser_version"
            selected_columns = (
                "source_event_id,source_observation_id,source_id,raw_sha256,raw_ref,"
                f"fetched_at,valid_time,{parser_select}"
            )
            names = (
                "source_event_id",
                "source_observation_id",
                "source_id",
                "raw_sha256",
                "raw_ref",
                "fetched_at",
                "valid_time",
                "parser_version",
            )
            records = [
                dict(zip(names, row))
                for row in conn.execute(f"SELECT {selected_columns} FROM source_observations")
            ]
    except (OSError, sqlite3.Error) as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "db": str(db),
            "blockers": [f"source_observations_unreadable:{type(exc).__name__}"],
            "guards": {"writes_archive_db": False, "places_orders": False},
        }
    classifications = [classify_source_observation_replay_eligibility(record) for record in records]
    strict_records = [
        record
        for record, classification in zip(records, classifications)
        if classification.get("strict_source_lineage_candidate") is True
    ]
    text = lambda value: str(value or "").strip()
    totals = {
        "row_count": len(records),
        "strict_source_lineage_candidate_count": len(strict_records),
        "legacy_derived_excluded_count": len(records) - len(strict_records),
        "distinct_source_event_id_count": len({text(record.get("source_event_id")) for record in records if text(record.get("source_event_id"))}),
        "distinct_source_observation_id_count": len({text(record.get("source_observation_id")) for record in records if text(record.get("source_observation_id"))}),
        "missing_source_event_id_count": sum(not text(record.get("source_event_id")) for record in records),
        "missing_source_observation_id_count": sum(not text(record.get("source_observation_id")) for record in records),
        "missing_source_id_count": sum(not text(record.get("source_id")) for record in records),
        "missing_raw_sha256_count": sum(not text(record.get("raw_sha256")) for record in records),
        "missing_raw_ref_count": sum(not text(record.get("raw_ref")) for record in records),
        "missing_fetched_at_count": sum(not text(record.get("fetched_at")) for record in records),
        "missing_valid_time_count": sum(not text(record.get("valid_time")) for record in strict_records),
    }
    blockers: list[str] = []
    if not source_event_is_pk:
        blockers.append("source_event_id_primary_key_missing")
    if not source_observation_unique:
        blockers.append("source_observation_id_unique_index_missing")
    if totals["row_count"] != totals["distinct_source_event_id_count"] or totals["missing_source_event_id_count"]:
        blockers.append("source_event_identity_not_append_only_unique")
    if totals["row_count"] != totals["distinct_source_observation_id_count"] or totals["missing_source_observation_id_count"]:
        blockers.append("source_observation_identity_not_unique")
    if not totals["row_count"]:
        blockers.append("source_observation_history_empty")
    if any(totals[key] for key in ("missing_source_id_count", "missing_raw_sha256_count", "missing_raw_ref_count", "missing_fetched_at_count")):
        blockers.append("source_observation_raw_arrival_lineage_incomplete")
    append_only_contract_ok = not blockers
    strict_asof_replay_ready = (
        append_only_contract_ok
        and totals["strict_source_lineage_candidate_count"] > 0
        and totals["missing_valid_time_count"] == 0
    )
    if totals["missing_valid_time_count"]:
        blockers.append("source_observation_valid_time_incomplete")
    return {
        "schema": SCHEMA_VERSION,
        "ok": strict_asof_replay_ready,
        "state": "ready" if strict_asof_replay_ready else "blocked",
        "db": str(db),
        "append_only_contract_ok": append_only_contract_ok,
        "strict_asof_replay_ready": strict_asof_replay_ready,
        "source_event_id_primary_key": source_event_is_pk,
        "source_observation_id_unique": source_observation_unique,
        "missing_required_columns": missing_columns,
        "totals": totals,
        "blockers": sorted(set(blockers)),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    report = build_source_observation_append_only_audit(Path(args.db))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
