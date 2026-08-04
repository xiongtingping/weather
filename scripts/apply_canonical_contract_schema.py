#!/usr/bin/env python3
"""Controlled migration for the canonical contract fact table.

The resident daemon never installs DDL.  This one-shot command only creates
the append-only table/triggers required by ``canonical_ingress``; it does not
touch calibration rows, artifacts, markets or orders.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from bayes_basket_target.canonical_ssot_schema import (
    inspect_canonical_ssot_schema,
)

DDL = """
CREATE TABLE IF NOT EXISTS canonical_ssot_fact (
  fact_id TEXT PRIMARY KEY,
  fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation','activation_supersession','ensemble_member','native_arrival','ensemble_member_arrival_binding','ensemble_member_receipt_manifest','republisher_oos_artifact')),
  scope_key TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  fact_json TEXT NOT NULL,
  lineage_json TEXT NOT NULL,
  ingested_at_utc TEXT NOT NULL,
  UNIQUE(fact_type, content_sha256),
  UNIQUE(fact_type, scope_key)
);
CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_no_update
BEFORE UPDATE ON canonical_ssot_fact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_no_delete
BEFORE DELETE ON canonical_ssot_fact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_ingress_only
BEFORE INSERT ON canonical_ssot_fact
WHEN canonical_ingress_authorized()!=1
BEGIN SELECT RAISE(ABORT,'canonical_ingress_only'); END;
"""


def migrate(db: Path, *, apply: bool) -> dict[str, object]:
    supplied_path = db.expanduser()
    if supplied_path.is_symlink():
        raise RuntimeError("canonical_contract_schema_symlink_forbidden")
    database = supplied_path.resolve(strict=True)
    with sqlite3.connect(database, timeout=60.0, isolation_level=None) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        before = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='canonical_ssot_fact'"
        ).fetchone()[0]
        before_inspection = inspect_canonical_ssot_schema(conn)
        before_current = (
            before_inspection["ok"] is True
            and before_inspection["state"] == "V7"
        )
        if apply and before_inspection["state"] not in {"ABSENT", "V7"}:
            raise RuntimeError("canonical_contract_schema_v7_migration_required")
        if apply:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS canonical_ssot_fact ("
                    "fact_id TEXT PRIMARY KEY,"
                    "fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation','activation_supersession','ensemble_member','native_arrival','ensemble_member_arrival_binding','ensemble_member_receipt_manifest','republisher_oos_artifact'))," 
                    "scope_key TEXT NOT NULL,"
                    "content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),"
                    "fact_json TEXT NOT NULL,"
                    "lineage_json TEXT NOT NULL,"
                    "ingested_at_utc TEXT NOT NULL,"
                    "UNIQUE(fact_type, content_sha256),"
                    "UNIQUE(fact_type, scope_key))"
                )
                conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_no_update "
                    "BEFORE UPDATE ON canonical_ssot_fact BEGIN SELECT RAISE(ABORT,'append_only'); END"
                )
                conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_no_delete "
                    "BEFORE DELETE ON canonical_ssot_fact BEGIN SELECT RAISE(ABORT,'append_only'); END"
                )
                conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS canonical_ssot_fact_ingress_only "
                    "BEFORE INSERT ON canonical_ssot_fact "
                    "WHEN canonical_ingress_authorized()!=1 "
                    "BEGIN SELECT RAISE(ABORT,'canonical_ingress_only'); END"
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        after = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='canonical_ssot_fact'"
        ).fetchone()[0]
        after_inspection = inspect_canonical_ssot_schema(conn)
        after_current = (
            after_inspection["ok"] is True
            and after_inspection["state"] == "V7"
        )
    return {
        "schema": "bayes_basket_target_canonical_contract_schema_migration_v1",
        "state": (
            "APPLIED"
            if apply and after_current
            else "CURRENT_NO_WRITE"
            if after_current
            else "MIGRATION_REQUIRED"
        ),
        "database": str(database),
        "before_present": bool(before),
        "after_present": bool(after),
        "canonical_contract_schema_ok": after_current,
        "canonical_contract_schema_errors": after_inspection["errors"],
        "rows_preserved": True,
        "writes_orders": False,
        "calls_order_gateway": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/bayes_basket_target_ssot.db"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, apply=bool(args.apply)), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
