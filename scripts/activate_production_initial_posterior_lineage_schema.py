#!/usr/bin/env python3
"""Controlled additive migration for immutable production P0 lineage.

The default mode is read-only.  Apply requires an existing SQLite backup,
an explicit writer-quiescent assertion, and the exact confirmation phrase.
It creates only one table, one index, and two append-only triggers.
"""

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

from production_initial_posterior_writer import (  # noqa: E402
    LINEAGE_INDEX,
    LINEAGE_TABLE,
    initialize_production_initial_posterior_schema,
)


SCHEMA_VERSION = "production_initial_posterior_lineage_schema_activation_v1"
CONFIRMATION_PHRASE = "ACTIVATE_PRODUCTION_INITIAL_POSTERIOR_LINEAGE_SCHEMA"
REQUIRED_COLUMNS = frozenset({
    "posterior_checkpoint_id", "target_identity", "posterior_run_id",
    "prior_context_id", "prior_artifact_sha256", "prior_model_artifact_sha256", "weak_likelihood_artifact_sha256",
    "weak_likelihood_sha256", "forecast_evidence_snapshot_id",
    "strong_lr_artifact_sha256", "source_reliability_initial_state_sha256",
    "resolution_contract_id", "initial_evidence_sha256",
    "activation_contract_sha256", "lineage_json", "created_at",
})
REQUIRED_TRIGGERS = frozenset({
    f"{LINEAGE_TABLE}_no_update",
    f"{LINEAGE_TABLE}_no_delete",
})


def _sqlite_backup_valid(path: Path, db: Path) -> bool:
    if path.resolve() == db.resolve() or not path.is_file() or path.stat().st_size < 100:
        return False
    try:
        return path.open("rb").read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _inspect(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    triggers = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({LINEAGE_TABLE})")
    } if LINEAGE_TABLE in tables else set()
    return {
        "table_present": LINEAGE_TABLE in tables,
        "index_present": LINEAGE_INDEX in indexes,
        "triggers_present": sorted(REQUIRED_TRIGGERS & triggers),
        "missing_columns": sorted(REQUIRED_COLUMNS - columns),
        "missing_triggers": sorted(REQUIRED_TRIGGERS - triggers),
        "ok": (
            LINEAGE_TABLE in tables
            and LINEAGE_INDEX in indexes
            and not (REQUIRED_COLUMNS - columns)
            and not (REQUIRED_TRIGGERS - triggers)
        ),
    }


def build_contract(
    db_path: Path | str,
    *,
    apply: bool = False,
    backup_path: Path | str | None = None,
    writer_quiescent: bool = False,
    confirm: str = "",
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    backup = Path(backup_path).expanduser().resolve() if backup_path else None
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        before = _inspect(conn)
        shadow_manifest_present = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_shadow_runtime_manifest'"
        ).fetchone())
    blockers: list[str] = []
    if apply and not writer_quiescent:
        blockers.append("writer_quiescent_assertion_required")
    if apply and confirm != CONFIRMATION_PHRASE:
        blockers.append("confirmation_phrase_required")
    if apply and (backup is None or not _sqlite_backup_valid(backup, db)):
        blockers.append("valid_external_or_separate_sqlite_backup_required")
    if shadow_manifest_present:
        blockers.append("production_schema_refuses_shadow_database")
    actions = [] if before["ok"] else [
        "create_production_initial_posterior_lineage_table",
        "create_production_initial_lineage_target_index",
        "create_append_only_update_delete_triggers",
    ]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "already_applied" if before["ok"] else "apply_ready" if not blockers else "blocked",
        "db": str(db),
        "backup": str(backup) if backup else "",
        "apply_requested": apply,
        "blockers": blockers,
        "actions": actions,
        "before": before,
        "read_only": True,
        "guards": {
            "historical_backfill": False,
            "updates_existing_rows": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "places_orders": False,
            "restarts_services": False,
        },
    }


def apply_contract(
    db_path: Path | str,
    *,
    backup_path: Path | str,
    writer_quiescent: bool,
    confirm: str,
) -> dict[str, Any]:
    contract = build_contract(
        db_path, apply=True, backup_path=backup_path,
        writer_quiescent=writer_quiescent, confirm=confirm,
    )
    if contract["blockers"]:
        return {**contract, "execution_mode": "blocked_no_write"}
    if contract["before"]["ok"]:
        return {**contract, "execution_mode": "already_applied_no_write", "after": contract["before"]}
    db = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(db, timeout=5.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        initialize_production_initial_posterior_schema(conn)
        after = _inspect(conn)
        if not after["ok"]:
            raise RuntimeError("production_initial_lineage_schema_verification_failed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        **contract,
        "ok": True,
        "state": "applied",
        "execution_mode": "applied",
        "read_only": False,
        "after": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--backup")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = (
        apply_contract(
            args.db, backup_path=args.backup or "", writer_quiescent=args.writer_quiescent,
            confirm=args.confirm,
        )
        if args.apply
        else build_contract(args.db, backup_path=args.backup)
    )
    print(json.dumps(result, ensure_ascii=False, indent=None if args.summary else 2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
