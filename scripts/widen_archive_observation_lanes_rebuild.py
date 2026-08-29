#!/usr/bin/env python3
"""One-shot rebuild: widen the canonical archive's observation lane CHECKs.

``reported_trajectory_observations`` and its origins table pin
``source_lane IN ('amsc','awc')`` (and METAR/SPECI report types), so every
SOFT_PATH carrier row the 2026-08-29 soft-in-hard-out ruling admits was
being silently swallowed by INSERT OR IGNORE -- the CHECK failure reads as
a duplicate.  This rebuilds both tables with the widened constraints,
preserves every row positionally, keeps the replaced tables as backups, and
re-creates the station/time index.  Run only with the resident service
stopped; the BEGIN IMMEDIATE fence fails closed against any live writer.
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


CONFIRMATION = "widen-archive-observation-lanes-production-no-live"
OBS = "reported_trajectory_observations"
ORI = "reported_trajectory_observation_origins"
OBS_COLUMNS = (
    "observation_id", "source_lane", "station_id", "report_type", "valid_at",
    "reported_temperature_c", "raw_sha256", "raw_text", "first_seen_at",
    "parsed_at", "canonical_committed_at", "fetch_started_at",
    "fetch_completed_at", "source_poll_id", "correction_suspected",
)
ORI_COLUMNS = (
    "observation_id", "source_lane", "station_id", "origin_source_id",
    "origin_country_code", "origin_authority", "origin_url",
    "origin_transport", "direct_country_official", "fallback_used",
    "fallback_reason", "preferred_origin_source_id",
    "preferred_origin_authority", "official_source_registry_sha256",
    "origin_json", "committed_at",
)
OBS_NEW_DDL = f"""
CREATE TABLE {OBS}_widened_new (
  observation_id TEXT PRIMARY KEY,
  source_lane TEXT NOT NULL
    CHECK(source_lane IN ('amsc','awc','asos_1min','madis_hfmetar')),
  station_id TEXT NOT NULL,
  report_type TEXT NOT NULL
    CHECK(report_type IN ('METAR','SPECI','ASOS_HF','ASOS_1MIN')),
  valid_at TEXT NOT NULL,
  reported_temperature_c INTEGER NOT NULL,
  raw_sha256 TEXT NOT NULL,
  raw_text TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  parsed_at TEXT NOT NULL,
  canonical_committed_at TEXT NOT NULL,
  fetch_started_at TEXT NOT NULL,
  fetch_completed_at TEXT NOT NULL,
  source_poll_id TEXT NOT NULL,
  correction_suspected INTEGER NOT NULL DEFAULT 0
    CHECK(correction_suspected IN (0,1)),
  UNIQUE(source_lane,station_id,valid_at,report_type,raw_sha256)
)
""".strip()
ORI_NEW_DDL = f"""
CREATE TABLE {ORI}_widened_new (
  observation_id TEXT PRIMARY KEY,
  source_lane TEXT NOT NULL
    CHECK(source_lane IN ('amsc','awc','asos_1min','madis_hfmetar')),
  station_id TEXT NOT NULL,
  origin_source_id TEXT NOT NULL,
  origin_country_code TEXT NOT NULL,
  origin_authority TEXT NOT NULL,
  origin_url TEXT NOT NULL,
  origin_transport TEXT NOT NULL,
  direct_country_official INTEGER NOT NULL
    CHECK(direct_country_official IN (0,1)),
  fallback_used INTEGER NOT NULL CHECK(fallback_used IN (0,1)),
  fallback_reason TEXT NOT NULL,
  preferred_origin_source_id TEXT NOT NULL,
  preferred_origin_authority TEXT NOT NULL,
  official_source_registry_sha256 TEXT NOT NULL,
  origin_json TEXT NOT NULL,
  committed_at TEXT NOT NULL
)
""".strip()
INDEX_DDL = (
    "CREATE INDEX idx_reported_trajectory_observations_station_time "
    f"ON {OBS}(station_id,valid_at)"
)


def _rebuild(conn: sqlite3.Connection, table: str, columns: tuple, new_ddl: str) -> int:
    before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.execute(new_ddl)
    column_list = ",".join(columns)
    conn.execute(
        f"INSERT INTO {table}_widened_new({column_list}) "
        f"SELECT {column_list} FROM {table}"
    )
    after = conn.execute(
        f"SELECT COUNT(*) FROM {table}_widened_new"
    ).fetchone()[0]
    if int(after) != int(before):
        raise RuntimeError(f"{table}_copy_count_mismatch")
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_amsc_awc_replaced")
    conn.execute(f"ALTER TABLE {table}_widened_new RENAME TO {table}")
    return int(before)


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
        "schema": "archive_observation_lane_widening_receipt_v1",
        "database": str(database),
        "applied": bool(args.apply),
        "started_at_utc": now,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    try:
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND (name LIKE '%_widened_new'"
                "  OR name LIKE '%_amsc_awc_replaced')"
            )
        }
        if existing:
            receipt["state"] = "ALREADY_REBUILT_OR_DIRTY"
            receipt["existing_objects"] = sorted(existing)
            return _finish(
                receipt, args.receipt_output,
                ok=any(name.endswith("_amsc_awc_replaced") for name in existing),
            )
        for table, columns in ((OBS, OBS_COLUMNS), (ORI, ORI_COLUMNS)):
            live = [
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            if tuple(live) != columns:
                receipt["state"] = f"REFUSED_COLUMN_ORDER_MISMATCH:{table}"
                receipt["live_columns"] = live
                return _finish(receipt, args.receipt_output, ok=False)
        if not args.apply:
            receipt["state"] = "DRY_RUN"
            receipt["row_counts"] = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (OBS, ORI)
            }
            return _finish(receipt, args.receipt_output, ok=True)
        conn.execute("BEGIN IMMEDIATE")
        # Legacy rename semantics: a modern RENAME would rewrite the
        # halt-review table's FOREIGN KEY to point at the replaced backup.
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute(
            "DROP INDEX IF EXISTS "
            "idx_reported_trajectory_observations_station_time"
        )
        counts = {
            OBS: _rebuild(conn, OBS, OBS_COLUMNS, OBS_NEW_DDL),
            ORI: _rebuild(conn, ORI, ORI_COLUMNS, ORI_NEW_DDL),
        }
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute(INDEX_DDL)
        conn.commit()
        receipt["row_counts"] = counts
        receipt["state"] = "REBUILT"
        return _finish(receipt, args.receipt_output, ok=True)
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        receipt["state"] = "FAILED"
        receipt["reason"] = f"{type(exc).__name__}:{str(exc)[:200]}"
        return _finish(receipt, args.receipt_output, ok=False)
    finally:
        conn.close()


def _finish(receipt: dict[str, object], path: Path, *, ok: bool) -> int:
    write_bounded_json(path.expanduser().resolve(), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
