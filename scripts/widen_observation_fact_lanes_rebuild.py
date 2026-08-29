#!/usr/bin/env python3
"""One-shot rebuild: widen basket_target_observation_facts lane CHECKs.

SQLite cannot alter a CHECK in place.  The live table pins
``source_lane IN ('AMSC','AWC')`` / ``report_type IN ('METAR','SPECI')``,
which refuses the scheduled ASOS substitute carriers the atomic chain now
admits (user ruling 2026-08-28).  This rebuilds the table with the widened
constraints, preserves every row byte-exactly, keeps the replaced table as
an immutable backup, and re-creates the append-only triggers and the scope
index.  Run only with the resident service stopped; the BEGIN IMMEDIATE
fence fails closed against any live writer.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


TABLE = "basket_target_observation_facts"
BACKUP = "basket_target_observation_facts_amsc_awc_replaced"
NEW = "basket_target_observation_facts_widened_new"
CONFIRMATION = "widen-observation-fact-lanes-production-no-live"
COLUMNS = (
    "observation_fact_id", "observation_id", "station_id", "event_date",
    "source_lane", "report_type", "raw_sha256", "canonical_committed_at",
    "content_sha256", "created_at", "settlement_timezone",
    "truth_observation_revision_content_sha256", "observed_at_utc",
    "awc_source_receipt_sha256",
)
# Same column order as the live table (base columns then the ALTER-appended
# ones), so the copy is positionally exact and the backup diffable.
NEW_DDL = f"""
CREATE TABLE {NEW} (
  observation_fact_id TEXT PRIMARY KEY,
  observation_id TEXT NOT NULL UNIQUE,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  source_lane TEXT NOT NULL
    CHECK(source_lane IN ('AMSC','AWC','MADIS_HFMETAR','ASOS_1MIN')),
  report_type TEXT NOT NULL
    CHECK(report_type IN ('METAR','SPECI','ASOS_HF','ASOS_1MIN')),
  raw_sha256 TEXT NOT NULL CHECK(length(raw_sha256)=64),
  canonical_committed_at TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  created_at TEXT NOT NULL,
  settlement_timezone TEXT,
  truth_observation_revision_content_sha256 TEXT
    CHECK(length(truth_observation_revision_content_sha256)=64),
  observed_at_utc TEXT,
  awc_source_receipt_sha256 TEXT
    CHECK(awc_source_receipt_sha256 IS NULL
      OR length(awc_source_receipt_sha256)=64)
)
""".strip()
LIVE_TRIGGERS = (
    (
        "basket_target_observation_no_update",
        "CREATE TRIGGER basket_target_observation_no_update "
        f"BEFORE UPDATE ON {TABLE} "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END",
    ),
    (
        "basket_target_observation_no_delete",
        "CREATE TRIGGER basket_target_observation_no_delete "
        f"BEFORE DELETE ON {TABLE} "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END",
    ),
    (
        "basket_target_observation_canonical_insert_only",
        "CREATE TRIGGER basket_target_observation_canonical_insert_only "
        f"BEFORE INSERT ON {TABLE} "
        "WHEN target_canonical_projection_ingress_authorized()<>1 "
        "BEGIN SELECT RAISE(ABORT,'canonical_projection_ingress_required'); "
        "END",
    ),
)
BACKUP_TRIGGERS = (
    f"CREATE TRIGGER {BACKUP}_no_update BEFORE UPDATE ON {BACKUP} "
    "BEGIN SELECT RAISE(ABORT,'append_only'); END",
    f"CREATE TRIGGER {BACKUP}_no_delete BEFORE DELETE ON {BACKUP} "
    "BEGIN SELECT RAISE(ABORT,'append_only'); END",
    f"CREATE TRIGGER {BACKUP}_no_insert BEFORE INSERT ON {BACKUP} "
    "BEGIN SELECT RAISE(ABORT,'replaced_table_immutable'); END",
)
INDEX_DDL = (
    "CREATE INDEX basket_target_observation_facts_scope_idx "
    f"ON {TABLE}(station_id,event_date)"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    database = args.database.expanduser().resolve(strict=True)
    now = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    conn = sqlite3.connect(str(database), timeout=30.0)
    receipt: dict[str, object] = {
        "schema": "basket_target_observation_fact_lane_widening_receipt_v1",
        "database": str(database),
        "applied": bool(args.apply),
        "started_at_utc": now,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    try:
        existing = {
            str(row[0]): str(row[1] or "")
            for row in conn.execute(
                "SELECT name,sql FROM sqlite_master "
                "WHERE tbl_name IN (?,?,?)",
                (TABLE, BACKUP, NEW),
            )
        }
        if BACKUP in existing or NEW in existing:
            receipt["state"] = "ALREADY_REBUILT_OR_DIRTY"
            receipt["existing_objects"] = sorted(existing)
            return _finish(receipt, args.receipt_output, ok=BACKUP in existing)
        live_columns = [
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({TABLE})")
        ]
        if tuple(live_columns) != COLUMNS:
            receipt["state"] = "REFUSED_COLUMN_ORDER_MISMATCH"
            receipt["live_columns"] = live_columns
            return _finish(receipt, args.receipt_output, ok=False)
        before = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        by_lane = dict(conn.execute(
            f"SELECT source_lane, COUNT(*) FROM {TABLE} GROUP BY source_lane"
        ).fetchall())
        receipt["row_count_before"] = int(before)
        receipt["rows_by_lane_before"] = by_lane
        if not args.apply:
            receipt["state"] = "DRY_RUN"
            return _finish(receipt, args.receipt_output, ok=True)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(NEW_DDL)
        column_list = ",".join(COLUMNS)
        conn.execute(
            f"INSERT INTO {NEW}({column_list}) "
            f"SELECT {column_list} FROM {TABLE}"
        )
        after = conn.execute(f"SELECT COUNT(*) FROM {NEW}").fetchone()[0]
        if int(after) != int(before):
            conn.rollback()
            receipt["state"] = "REFUSED_COPY_COUNT_MISMATCH"
            return _finish(receipt, args.receipt_output, ok=False)
        for name, _ddl in LIVE_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        # The scope index would ride the rename into the backup and keep its
        # name, colliding with the re-create below; the immutable backup
        # needs no index.
        conn.execute(
            "DROP INDEX IF EXISTS basket_target_observation_facts_scope_idx"
        )
        # Legacy rename semantics: a modern RENAME rewrites every reference
        # to the old name -- including the FOREIGN KEY inside
        # basket_target_bound_facts -- which would re-point child rows at
        # the immutable backup.  Legacy mode leaves references textual, so
        # after the swap they resolve to the new table under the old name.
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute(f"ALTER TABLE {TABLE} RENAME TO {BACKUP}")
        conn.execute(f"ALTER TABLE {NEW} RENAME TO {TABLE}")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        for ddl in BACKUP_TRIGGERS:
            conn.execute(ddl)
        for _name, ddl in LIVE_TRIGGERS:
            conn.execute(ddl)
        conn.execute(INDEX_DDL)
        conn.commit()
        final = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        receipt["row_count_after"] = int(final)
        receipt["state"] = (
            "REBUILT" if int(final) == int(before) else "COUNT_DRIFT"
        )
        return _finish(
            receipt, args.receipt_output, ok=int(final) == int(before)
        )
    finally:
        conn.close()


def _finish(receipt: dict[str, object], path: Path, *, ok: bool) -> int:
    write_bounded_json(path.expanduser().resolve(), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
