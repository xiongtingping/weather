#!/usr/bin/env python3
"""Atomically add only the production dynamic-three-leg execution schema.

This cutover is deliberately independent of forecast training coverage and P1
forecast ingress.  Default mode is read-only.  Apply requires an explicit
confirmation and an operator assertion that archive writers are quiescent.
It never activates decision writing or live order submission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sequential_bayes_schema_contract import INDEX_DDL, TABLE_DDL  # noqa: E402


SCHEMA_VERSION = "dynamic_three_leg_execution_schema_cutover_v1"
MIGRATION_ID = "dynamic_three_leg_execution_schema_v1"
CONFIRMATION = "ACTIVATE_DYNAMIC_THREE_LEG_EXECUTION_SCHEMA"
TABLE_NAMES = (
    "posterior_outbox_execution_contracts",
    "dynamic_three_leg_transition_plans",
    "dynamic_three_leg_risk_reservations",
    "posterior_execution_intents",
)
INDEX_NAMES = (
    "idx_outbox_execution_deadline",
    "idx_transition_plans_phase_updated",
    "idx_dynamic_three_leg_risk_reservations_active",
    "idx_posterior_execution_intents_ready",
    "idx_posterior_execution_intents_lot",
)
REQUIRED_BASE_TABLES = (
    "posterior_commit_outbox",
    "event_fact_history",
    "event_fact_latest",
    "auto_trade_decisions",
)
MAX_BACKUP_AGE_SECONDS = 900


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_sql(value: Any) -> str:
    text = " ".join(_text(value).lower().split())
    text = text.replace("create table if not exists ", "create table ", 1)
    text = text.replace("create index if not exists ", "create index ", 1)
    return text.rstrip(";").strip()


def _objects(conn: sqlite3.Connection, object_type: str) -> dict[str, str]:
    return {
        _text(row[0]): _text(row[1])
        for row in conn.execute(
            "SELECT name,COALESCE(sql,'') FROM sqlite_master WHERE type=?",
            (object_type,),
        ).fetchall()
    }


def _schema_identity(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _verify_fresh_backup(
    source: Path,
    backup: Path | None,
    *,
    full_integrity_check: bool = False,
) -> dict[str, Any]:
    blockers: list[str] = []
    if backup is None:
        return {"ok": False, "blockers": ["fresh_backup_required"]}
    target = backup.expanduser().resolve()
    if target == source:
        return {"ok": False, "blockers": ["backup_path_must_differ_from_source"], "path": str(target)}
    try:
        age_seconds = max(0.0, time.time() - target.stat().st_mtime)
    except OSError:
        return {"ok": False, "blockers": ["backup_file_missing"], "path": str(target)}
    if age_seconds > MAX_BACKUP_AGE_SECONDS:
        blockers.append("backup_not_fresh")
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_conn, sqlite3.connect(
            f"file:{target}?mode=ro", uri=True
        ) as backup_conn:
            source_pages = int(source_conn.execute("PRAGMA page_count").fetchone()[0])
            backup_pages = int(backup_conn.execute("PRAGMA page_count").fetchone()[0])
            source_page_size = int(source_conn.execute("PRAGMA page_size").fetchone()[0])
            backup_page_size = int(backup_conn.execute("PRAGMA page_size").fetchone()[0])
            source_application_id = int(source_conn.execute("PRAGMA application_id").fetchone()[0])
            backup_application_id = int(backup_conn.execute("PRAGMA application_id").fetchone()[0])
            source_schema = _schema_identity(source_conn)
            backup_schema = _schema_identity(backup_conn)
            source_quick = backup_quick = None
            if full_integrity_check:
                source_quick = _text(source_conn.execute("PRAGMA quick_check").fetchone()[0])
                backup_quick = _text(backup_conn.execute("PRAGMA quick_check").fetchone()[0])
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return {
            "ok": False,
            "blockers": [f"backup_verification_failed:{type(exc).__name__}"],
            "path": str(target),
        }
    if full_integrity_check and (source_quick != "ok" or backup_quick != "ok"):
        blockers.append("source_or_backup_quick_check_failed")
    if source_pages != backup_pages:
        blockers.append("backup_page_count_mismatch")
    if source_page_size != backup_page_size:
        blockers.append("backup_page_size_mismatch")
    if source_application_id != backup_application_id:
        blockers.append("backup_application_id_mismatch")
    if source_schema != backup_schema:
        blockers.append("backup_schema_identity_mismatch")
    return {
        "ok": not blockers,
        "blockers": sorted(set(blockers)),
        "path": str(target),
        "age_seconds": age_seconds,
        "maximum_age_seconds": MAX_BACKUP_AGE_SECONDS,
        "source_page_count": source_pages,
        "backup_page_count": backup_pages,
        "source_schema_sha256": source_schema,
        "backup_schema_sha256": backup_schema,
        "integrity_check": {
            "mode": "full_quick_check" if full_integrity_check else "sqlite_backup_parity",
            "source": source_quick,
            "backup": backup_quick,
            "deferred_full_quick_check": not full_integrity_check,
        },
    }


def _inspect(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _objects(conn, "table")
    indexes = _objects(conn, "index")
    missing_base = sorted(set(REQUIRED_BASE_TABLES) - set(tables))
    incompatible_tables = sorted(
        name for name in TABLE_NAMES
        if name in tables and _canonical_sql(tables[name]) != _canonical_sql(TABLE_DDL[name])
    )
    incompatible_indexes = sorted(
        name for name in INDEX_NAMES
        if name in indexes and _canonical_sql(indexes[name]) != _canonical_sql(INDEX_DDL[name])
    )
    missing_tables = [name for name in TABLE_NAMES if name not in tables]
    missing_indexes = [name for name in INDEX_NAMES if name not in indexes]
    blockers: list[str] = []
    if "v2_shadow_runtime_manifest" in tables:
        blockers.append("production_cutover_refuses_shadow_runtime_manifest")
    if missing_base:
        blockers.append("production_cutover_base_tables_missing:" + ",".join(missing_base))
    if incompatible_tables:
        blockers.append("production_cutover_tables_incompatible:" + ",".join(incompatible_tables))
    if incompatible_indexes:
        blockers.append("production_cutover_indexes_incompatible:" + ",".join(incompatible_indexes))
    return {
        "ok": not blockers and not missing_tables and not missing_indexes,
        "blockers": blockers,
        "missing_tables": missing_tables,
        "missing_indexes": missing_indexes,
        "incompatible_tables": incompatible_tables,
        "incompatible_indexes": incompatible_indexes,
        "required_base_tables_present": not missing_base,
    }


def build_cutover_plan(db_path: Path | str) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    blockers: list[str] = []
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            inspection = _inspect(conn)
    except sqlite3.Error as exc:
        inspection = {}
        blockers.append(f"production_archive_unreadable:{type(exc).__name__}")
    blockers.extend(inspection.get("blockers") or [])
    actions = [
        *({"action": "create_table", "name": name} for name in inspection.get("missing_tables") or []),
        *({"action": "create_index", "name": name} for name in inspection.get("missing_indexes") or []),
    ]
    return {
        "schema": SCHEMA_VERSION,
        "migration_id": MIGRATION_ID,
        "ok": not blockers,
        "state": "already_applied" if not blockers and not actions else "ready_to_apply" if not blockers else "blocked",
        "db": str(db),
        "actions": actions,
        "blockers": sorted(set(blockers)),
        "inspection": inspection,
        "confirmation_phrase_required": CONFIRMATION,
        "writer_quiescent_required": True,
        "read_only": True,
        "boundary": {
            "additive_schema_only": True,
            "training_samples_not_required": True,
            "updates_existing_rows": False,
            "activates_decision_writer": False,
            "activates_live_submitter": False,
            "places_orders": False,
            "restarts_services": False,
        },
    }


def apply_cutover(
    *,
    db_path: Path | str,
    backup_path: Path | str | None,
    confirm: str,
    writer_quiescent: bool,
    full_integrity_check: bool = False,
) -> dict[str, Any]:
    plan = build_cutover_plan(db_path)
    blockers = list(plan.get("blockers") or [])
    if _text(confirm) != CONFIRMATION:
        blockers.append("confirmation_phrase_required")
    if not writer_quiescent:
        blockers.append("writer_quiescent_assertion_required")
    db = Path(db_path).expanduser().resolve()
    backup = _verify_fresh_backup(
        db,
        Path(backup_path) if backup_path is not None else None,
        full_integrity_check=full_integrity_check,
    )
    blockers.extend(backup.get("blockers") or [])
    if blockers:
        return {**plan, "ok": False, "state": "blocked", "blockers": sorted(set(blockers)), "backup": backup}
    if not plan.get("actions"):
        return {**plan, "ok": True, "state": "already_applied", "read_only": False, "applied": [], "backup": backup}

    conn = sqlite3.connect(db, timeout=30)
    applied: list[dict[str, str]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = _inspect(conn)
        if before.get("blockers"):
            raise RuntimeError("dynamic_three_leg_execution_schema_precondition_drift")
        for name in TABLE_NAMES:
            if name in before["missing_tables"]:
                conn.execute(TABLE_DDL[name])
                applied.append({"action": "create_table", "name": name})
        for name in INDEX_NAMES:
            if name in before["missing_indexes"]:
                conn.execute(INDEX_DDL[name])
                applied.append({"action": "create_index", "name": name})
        after = _inspect(conn)
        if after.get("ok") is not True:
            raise RuntimeError("dynamic_three_leg_execution_schema_post_verify_failed")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {
            **plan,
            "ok": False,
            "state": "rolled_back",
            "blockers": [f"dynamic_three_leg_execution_schema_apply_failed:{type(exc).__name__}"],
            "applied": [],
        }
    finally:
        conn.close()
    return {
        **plan,
        "ok": True,
        "state": "applied",
        "read_only": False,
        "applied": applied,
        "backup": backup,
        "inspection_after": after,
        "boundary": {**plan["boundary"], "writes_archive_db_schema": True},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument(
        "--full-integrity-check",
        action="store_true",
        help="run full PRAGMA quick_check on source and backup; use as an offline audit, not the short maintenance path",
    )
    args = parser.parse_args(argv)
    result = (
        apply_cutover(
            db_path=args.db,
            backup_path=args.backup,
            confirm=args.confirm,
            writer_quiescent=args.writer_quiescent,
            full_integrity_check=args.full_integrity_check,
        )
        if args.apply
        else build_cutover_plan(args.db)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
