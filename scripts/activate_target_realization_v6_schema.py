#!/usr/bin/env python3
"""Inspect or atomically install the inert Target Realization v6 schema."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dynamic_three_leg_v6_schema_contract import INDEX_DDL, TABLE_DDL  # noqa: E402


SCHEMA_VERSION = "activate_target_realization_v6_schema_v1"
CONFIRMATION = "INSTALL_INERT_TARGET_REALIZATION_V6_SCHEMA"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_sql(value: Any) -> str:
    text = " ".join(_text(value).lower().split()).rstrip(";")
    text = text.replace("create table if not exists ", "create table ", 1)
    text = text.replace("create index if not exists ", "create index ", 1)
    return text


def _objects(conn: sqlite3.Connection, kind: str) -> dict[str, str]:
    return {
        _text(row[0]): _text(row[1])
        for row in conn.execute("SELECT name,COALESCE(sql,'') FROM sqlite_master WHERE type=?", (kind,))
    }


def inspect(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _objects(conn, "table")
    indexes = _objects(conn, "index")
    incompatible_tables = sorted(
        name for name, ddl in TABLE_DDL.items()
        if name in tables and _canonical_sql(tables[name]) != _canonical_sql(ddl)
    )
    incompatible_indexes = sorted(
        name for name, ddl in INDEX_DDL.items()
        if name in indexes and _canonical_sql(indexes[name]) != _canonical_sql(ddl)
    )
    missing_tables = sorted(set(TABLE_DDL) - set(tables))
    missing_indexes = sorted(set(INDEX_DDL) - set(indexes))
    blockers = [
        *[f"v6_schema_table_incompatible:{name}" for name in incompatible_tables],
        *[f"v6_schema_index_incompatible:{name}" for name in incompatible_indexes],
    ]
    return {
        "ok": not blockers and not missing_tables and not missing_indexes,
        "state": "installed" if not blockers and not missing_tables and not missing_indexes else "installable" if not blockers else "blocked",
        "missing_tables": missing_tables,
        "missing_indexes": missing_indexes,
        "blockers": blockers,
    }


def run(*, db: Path, apply: bool, confirm: str, writer_quiescent: bool) -> dict[str, Any]:
    db = db.expanduser().resolve()
    if not apply:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            result = inspect(conn)
        return {
            "schema": SCHEMA_VERSION,
            **result,
            "db": str(db),
            "read_only": True,
            "places_orders": False,
            "external_disk_dependency": False,
        }
    blockers: list[str] = []
    if _text(confirm) != CONFIRMATION:
        blockers.append("v6_schema_confirmation_required")
    if not writer_quiescent:
        blockers.append("v6_schema_writer_quiescent_assertion_required")
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": blockers, "db": str(db), "places_orders": False}
    conn = sqlite3.connect(db, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = inspect(conn)
        if before["blockers"]:
            raise RuntimeError("v6_schema_precondition_drift")
        for name in before["missing_tables"]:
            conn.execute(TABLE_DDL[name])
        for name in before["missing_indexes"]:
            conn.execute(INDEX_DDL[name])
        after = inspect(conn)
        if after["ok"] is not True:
            raise RuntimeError("v6_schema_postcondition_failed")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "rolled_back", "blockers": [f"v6_schema_apply_failed:{type(exc).__name__}"], "db": str(db), "places_orders": False}
    finally:
        conn.close()
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "installed",
        "db": str(db),
        "read_only": False,
        "installed_tables": sorted(TABLE_DDL),
        "installed_indexes": sorted(INDEX_DDL),
        "external_disk_dependency": False,
        "activates_v6": False,
        "places_orders": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-quiescent", action="store_true")
    args = parser.parse_args(argv)
    result = run(db=args.db, apply=args.apply, confirm=args.confirm, writer_quiescent=args.writer_quiescent)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True or result.get("state") == "installable" else 2


if __name__ == "__main__":
    raise SystemExit(main())
