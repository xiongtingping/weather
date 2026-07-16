#!/usr/bin/env python3
"""Read-only compatibility proof for the additive P1 schema migration.

The production archive is opened read-only but is never copied: it is nearly
10 GB in the current workspace.  Instead the tool reconstructs only the P1
object namespace already present in the archive inside an in-memory SQLite
database, then applies P1 DDL twice to that ephemeral schema skeleton.  This
proves name/DDL compatibility and idempotency without a confirmation phrase,
writer quiescence, a multi-GB RAM copy, or any production write.
"""

from __future__ import annotations

import argparse
import hashlib
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


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "sequential_bayes_schema_staging_v1"


def _p1_namespace_objects(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Read only contract-owned objects that can collide with additive P1 DDL."""

    from sequential_bayes_schema_contract import INDEX_DDL, TABLE_DDL

    objects: list[dict[str, str]] = []
    for object_type, names in (("table", TABLE_DDL), ("index", INDEX_DDL)):
        for name in names:
            row = conn.execute(
                "SELECT COALESCE(sql, '') FROM sqlite_master WHERE type=? AND name=?",
                (object_type, name),
            ).fetchone()
            if row is not None:
                objects.append(
                    {"type": object_type, "name": name, "sql": str(row[0] or "")}
                )
    return objects


def _p1_namespace_fingerprint(
    conn: sqlite3.Connection,
    *,
    objects: list[dict[str, str]],
) -> dict[str, Any]:
    """Fingerprint existing P1 objects only; legacy data is never copied."""

    rows: list[dict[str, Any]] = []
    for object_item in objects:
        object_type = str(object_item["type"])
        name = str(object_item["name"])
        schema_row = conn.execute(
            "SELECT COALESCE(sql, '') FROM sqlite_master WHERE type=? AND name=?",
            (object_type, name),
        ).fetchone()
        schema_sql = str(schema_row[0] if schema_row else "")
        rows.append(
            {
                "type": object_type,
                "name": name,
                "schema_sha256": hashlib.sha256(schema_sql.encode("utf-8")).hexdigest(),
            }
        )
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "tables": rows,
        "fingerprint_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def build_in_memory_p1_staging_proof(db_path: Path | str = DEFAULT_DB) -> dict[str, Any]:
    """Prove P1 DDL compatibility and idempotency without changing ``db_path``."""

    db = Path(db_path).expanduser().resolve()
    try:
        plan = build_forward_schema_plan(db)
        source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        staging = sqlite3.connect(":memory:")
        try:
            source_schema_version_before = int(source.execute("PRAGMA schema_version").fetchone()[0])
            preexisting_p1_objects = _p1_namespace_objects(source)
            for object_type in ("table", "index"):
                for object_item in preexisting_p1_objects:
                    if object_item["type"] == object_type:
                        staging.execute(object_item["sql"])
            before = _p1_namespace_fingerprint(staging, objects=preexisting_p1_objects)
            staging.execute("BEGIN IMMEDIATE")
            first_apply = apply_forward_schema(staging)
            first_verification = verify_schema(staging)
            after_first = _p1_namespace_fingerprint(staging, objects=preexisting_p1_objects)
            second_apply = apply_forward_schema(staging)
            second_verification = verify_schema(staging)
            after_second = _p1_namespace_fingerprint(staging, objects=preexisting_p1_objects)
            staging.rollback()
            source_schema_version_after = int(source.execute("PRAGMA schema_version").fetchone()[0])
        finally:
            staging.close()
            source.close()
    except (OSError, sqlite3.Error) as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "db": str(db),
            "error": f"{type(exc).__name__}:{exc}",
            "guards": {
                "reads_archive_db": True,
                "writes_archive_db": False,
                "writes_staging_db": False,
                "restarts_launchd": False,
                "places_orders": False,
            },
        }
    preexisting_p1_surface_unchanged = (
        before["fingerprint_sha256"] == after_first["fingerprint_sha256"] == after_second["fingerprint_sha256"]
    )
    idempotent = not list(second_apply.get("applied") or [])
    source_schema_unchanged = source_schema_version_before == source_schema_version_after
    ok = (
        bool(first_verification.get("ok"))
        and bool(second_verification.get("ok"))
        and idempotent
        and preexisting_p1_surface_unchanged
        and source_schema_unchanged
    )
    return {
        "schema": SCHEMA_VERSION,
        "migration_id": MIGRATION_ID,
        "ok": ok,
        "state": "compatible" if ok else "blocked",
        "db": str(db),
        "plan_action_count": len(list(plan.get("actions") or [])),
        "first_apply": first_apply,
        "first_verification": first_verification,
        "second_apply": second_apply,
        "second_verification": second_verification,
        "preexisting_p1_surface_unchanged": preexisting_p1_surface_unchanged,
        "preexisting_p1_surface_fingerprint_sha256": before["fingerprint_sha256"],
        "preexisting_p1_object_count": len(preexisting_p1_objects),
        "source_schema_unchanged": source_schema_unchanged,
        "staging_backend": "sqlite_memory_p1_namespace_skeleton_only",
        "guards": {
            "reads_archive_db": True,
            "writes_archive_db": False,
            "writes_staging_db": False,
            "historical_backfill": False,
            "updates_existing_rows": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "places_orders": False,
        },
        "next_boundary": "explicit_writer_quiescence_and_confirmation_before_production_apply",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    result = build_in_memory_p1_staging_proof(Path(args.db))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
