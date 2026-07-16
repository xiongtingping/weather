#!/usr/bin/env python3
"""Apply the additive reported-X checkpoint columns in a maintenance window.

Default mode is read-only.  Apply requires a fresh SQLite backup, an explicit
writer-quiescent assertion, and the exact confirmation phrase.  It changes no
existing checkpoint row and activates no runtime or order path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reported_x_successor_schema_contract import (  # noqa: E402
    COLUMNS,
    MIGRATION_ID,
    SCHEMA_VERSION as CONTRACT_SCHEMA,
    build_forward_plan,
    verify_schema,
)
from scripts.activate_dynamic_three_leg_execution_schema import _verify_fresh_backup  # noqa: E402


SCHEMA_VERSION = "activate_reported_x_successor_schema_v1"
CONFIRMATION = "ACTIVATE_REPORTED_X_SUCCESSOR_SCHEMA"


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_plan(db_path: Path | str) -> dict[str, Any]:
    forward = build_forward_plan(db_path)
    blockers = list(forward.get("block_reasons") or [])
    return {
        "schema": SCHEMA_VERSION,
        "contract_schema": CONTRACT_SCHEMA,
        "migration_id": MIGRATION_ID,
        "ok": not blockers,
        "state": (
            "already_applied"
            if forward.get("state") == "already_applied" and not blockers
            else "ready_to_apply" if not blockers else "blocked"
        ),
        "db": _text(forward.get("db")),
        "actions": list(forward.get("actions") or []),
        "blockers": blockers,
        "confirmation_phrase_required": CONFIRMATION,
        "writer_quiescent_required": True,
        "read_only": True,
        "boundary": {
            "additive_columns_only": True,
            "updates_existing_rows": False,
            "changes_activation": False,
            "publishes_ssot": False,
            "places_orders": False,
            "restarts_services": False,
        },
    }


def apply_plan(
    *,
    db_path: Path | str,
    backup_path: Path | str | None,
    confirm: str,
    writer_quiescent: bool,
) -> dict[str, Any]:
    plan = build_plan(db_path)
    blockers = list(plan.get("blockers") or [])
    if _text(confirm) != CONFIRMATION:
        blockers.append("confirmation_phrase_required")
    if not writer_quiescent:
        blockers.append("writer_quiescent_assertion_required")
    db = Path(db_path).expanduser().resolve()
    backup = _verify_fresh_backup(
        db,
        Path(backup_path).expanduser().resolve() if backup_path else None,
    )
    blockers.extend(backup.get("blockers") or [])
    if blockers:
        return {
            **plan,
            "ok": False,
            "state": "blocked",
            "blockers": sorted(set(blockers)),
            "backup": backup,
        }
    if not plan.get("actions"):
        return {
            **plan,
            "ok": True,
            "state": "already_applied",
            "read_only": False,
            "applied": [],
            "backup": backup,
        }
    applied: list[str] = []
    with sqlite3.connect(db, timeout=30) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = {
                _text(row[1])
                for row in conn.execute("PRAGMA table_info(posterior_checkpoints)")
            }
            for column, sql_type in COLUMNS.items():
                if column not in existing:
                    conn.execute(
                        f"ALTER TABLE posterior_checkpoints ADD COLUMN {column} {sql_type}"
                    )
                    applied.append(column)
            verified = verify_schema(conn)
            if verified.get("ok") is not True:
                raise RuntimeError("reported_x_successor_schema_post_verify_failed")
            conn.commit()
        except Exception as exc:
            conn.rollback()
            return {
                **plan,
                "ok": False,
                "state": "rolled_back",
                "blockers": [
                    f"reported_x_successor_schema_apply_failed:{type(exc).__name__}"
                ],
                "applied": [],
                "backup": backup,
            }
    return {
        **build_plan(db),
        "ok": True,
        "state": "applied",
        "read_only": False,
        "applied": applied,
        "backup": backup,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--backup", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = (
        apply_plan(
            db_path=args.db,
            backup_path=args.backup or None,
            confirm=args.confirm,
            writer_quiescent=args.writer_quiescent,
        )
        if args.apply
        else build_plan(args.db)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
