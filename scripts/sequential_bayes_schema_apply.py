#!/usr/bin/env python3
"""Controlled additive P1 schema migration; defaults to a read-only plan."""

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

from sequential_bayes_schema_contract import (  # noqa: E402
    MIGRATION_ID,
    apply_forward_schema,
    build_forward_schema_plan,
    verify_schema,
)


CONFIRMATION_PHRASE = "APPLY_SEQUENTIAL_BAYES_P1_SCHEMA"
DEFAULT_DB = ROOT / "data" / "archive.db"


def build_apply_contract(
    db_path: Path | str,
    *,
    apply: bool = False,
    confirm: str = "",
    writer_quiescent: bool = False,
) -> dict[str, Any]:
    plan = build_forward_schema_plan(db_path)
    actions = list(plan.get("actions") or [])
    blockers: list[str] = []
    if actions and not apply:
        blockers.append("apply_flag_required")
    if actions and str(confirm or "") != CONFIRMATION_PHRASE:
        blockers.append("confirmation_phrase_required")
    if actions and not writer_quiescent:
        blockers.append("writer_quiescent_assertion_required")
    return {
        "schema": "sequential_bayes_schema_apply_contract_v1",
        "migration_id": MIGRATION_ID,
        "db": str(Path(db_path).expanduser().resolve()),
        "read_only": not apply,
        "dry_run": not apply,
        "actions": actions,
        "apply_blockers": blockers,
        "confirmation_phrase_required": CONFIRMATION_PHRASE,
        "guards": {
            "writes_archive_db": False,
            "historical_backfill": False,
            "updates_existing_rows": False,
            "restarts_launchd": False,
            "places_orders": False,
            "changes_parameters": False,
        },
        "post_apply_verification": [
            "python3 scripts/sequential_bayes_schema_apply.py --db data/archive.db",
            "python3 -m pytest -q tests/test_sequential_bayes_schema_contract.py",
        ],
    }


def apply_contract(
    db_path: Path | str,
    *,
    confirm: str,
    writer_quiescent: bool,
) -> dict[str, Any]:
    contract = build_apply_contract(
        db_path,
        apply=True,
        confirm=confirm,
        writer_quiescent=writer_quiescent,
    )
    if contract["apply_blockers"]:
        contract["execution_mode"] = "blocked"
        return contract
    db = Path(db_path).expanduser().resolve()
    with sqlite3.connect(db, timeout=30) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = apply_forward_schema(conn)
        verification = verify_schema(conn)
        if not verification["ok"]:
            conn.rollback()
            contract["execution_mode"] = "rolled_back_verification_failed"
            contract["verification"] = verification
            return contract
        conn.commit()
    contract.update(
        {
            "read_only": False,
            "dry_run": False,
            "execution_mode": "applied",
            "apply_result": result,
            "verification": verification,
            "guards": {
                **dict(contract["guards"]),
                "writes_archive_db": True,
            },
        }
    )
    return contract


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-quiescent", action="store_true")
    args = parser.parse_args()
    if args.apply:
        result = apply_contract(
            args.db,
            confirm=args.confirm,
            writer_quiescent=bool(args.writer_quiescent),
        )
    else:
        result = build_apply_contract(args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not args.apply or not result.get("apply_blockers") else 2


if __name__ == "__main__":
    raise SystemExit(main())
