#!/usr/bin/env python3
"""One-shot rebuild: admit the SETTLEMENT_SOURCE lane into the truth table.

Charter 225 (user 2026-09-25, 「全部按结算」): a settled station-day whose AWC
truth is not in the bucket the market paid is re-settled from that market's
own official settlement page, as a revision of its own lane.  SQLite cannot
alter a CHECK in place, and the live ``calibration_station_daily_high_truth``
pins ``source_lane IN ('AMSC','AWC')``.  This rebuilds the table with the
widened constraint, preserves every row byte-exactly, keeps the replaced table
as an immutable backup, and re-creates the append-only triggers -- the same
procedure as ``widen_observation_fact_lanes_rebuild.py``.

The rename runs in legacy mode so the foreign keys of
``calibration_model_station_residual`` and
``calibration_republisher_historical_oos_contract`` and the body of
``calibration_republisher_historical_oos_contract_truth_guard`` keep naming
the live table rather than following it into the backup; the receipt checks
that they still do.  Old code writes only AWC rows, which the widened table
admits, so the rebuild is safe under a running resident; BEGIN IMMEDIATE
waits for its writer and fails closed after the busy timeout.
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


TABLE = "calibration_station_daily_high_truth"
BACKUP = "calibration_station_daily_high_truth_pre_settlement_source"
NEW = "calibration_station_daily_high_truth_widened_new"
CONFIRMATION = "widen-truth-lane-settlement-source"
WIDENED_CHECK = "CHECK(source_lane IN ('AMSC','AWC','SETTLEMENT_SOURCE'))"
COLUMNS = (
    "truth_id", "station_id", "local_date", "settlement_timezone",
    "official_tmax_c", "source_lane", "settlement_definition", "revision_no",
    "published_at_utc", "received_at_utc", "canonical_committed_at_utc",
    "timestamps_complete", "qc_status", "source_role",
    "source_payload_sha256", "observation_ids_json", "authority_scope",
    "content_sha256",
)
NEW_DDL = f"""
CREATE TABLE {NEW} (
  truth_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL REFERENCES calibration_station_registry(station_id),
  local_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  official_tmax_c REAL NOT NULL,
  source_lane TEXT NOT NULL
    {WIDENED_CHECK},
  settlement_definition TEXT NOT NULL,
  revision_no INTEGER NOT NULL CHECK(revision_no>=0),
  published_at_utc TEXT,
  received_at_utc TEXT,
  canonical_committed_at_utc TEXT,
  timestamps_complete INTEGER NOT NULL CHECK(timestamps_complete IN (0,1)),
  qc_status TEXT NOT NULL CHECK(qc_status IN ('ACCEPTED','QUARANTINED')),
  source_role TEXT NOT NULL,
  source_payload_sha256 TEXT NOT NULL CHECK(length(source_payload_sha256)=64),
  observation_ids_json TEXT NOT NULL,
  authority_scope TEXT NOT NULL CHECK(authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  UNIQUE(station_id,local_date,settlement_definition,revision_no)
)
""".strip()
LIVE_TRIGGERS = (
    (
        "calibration_truth_no_update",
        "CREATE TRIGGER calibration_truth_no_update BEFORE UPDATE ON "
        f"{TABLE} BEGIN SELECT RAISE(ABORT,'append_only'); END",
    ),
    (
        "calibration_truth_no_delete",
        "CREATE TRIGGER calibration_truth_no_delete BEFORE DELETE ON "
        f"{TABLE} BEGIN SELECT RAISE(ABORT,'append_only'); END",
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
# Objects outside the table whose SQL names it; each must still name the live
# table (and never the backup) after the swap.
REFERRERS = (
    "calibration_model_station_residual",
    "calibration_republisher_historical_oos_contract",
    "calibration_republisher_historical_oos_contract_truth_guard",
)


def already_widened(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    return row is not None and "SETTLEMENT_SOURCE" in str(row[0] or "")


def _referrer_state(conn: sqlite3.Connection) -> dict[str, str]:
    state: dict[str, str] = {}
    for name in REFERRERS:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            state[name] = "absent"
            continue
        sql = str(row[0] or "")
        if BACKUP in sql:
            state[name] = "points_at_backup"
        elif TABLE in sql:
            state[name] = "points_at_live"
        else:
            state[name] = "no_reference"
    return state


def widen(conn: sqlite3.Connection, *, apply: bool) -> dict[str, object]:
    """Widen the lane CHECK; the receipt says what was found and done."""

    receipt: dict[str, object] = {
        "schema": "calibration_truth_settlement_source_lane_widening_receipt_v1",
        "applied": bool(apply),
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    if already_widened(conn):
        receipt["state"] = "ALREADY_WIDENED"
        receipt["referrers"] = _referrer_state(conn)
        return receipt
    existing = sorted(
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name IN (?,?)",
            (BACKUP, NEW),
        )
    )
    if existing:
        receipt["state"] = "REFUSED_DIRTY"
        receipt["existing_objects"] = existing
        return receipt
    live_columns = tuple(
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({TABLE})")
    )
    if live_columns != COLUMNS:
        receipt["state"] = "REFUSED_COLUMN_ORDER_MISMATCH"
        receipt["live_columns"] = list(live_columns)
        return receipt
    before = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
    receipt["row_count_before"] = before
    receipt["rows_by_lane_before"] = dict(conn.execute(
        f"SELECT source_lane,COUNT(*) FROM {TABLE} GROUP BY source_lane"
    ).fetchall())
    receipt["referrers_before"] = _referrer_state(conn)
    if not apply:
        receipt["state"] = "DRY_RUN"
        return receipt
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(NEW_DDL)
        column_list = ",".join(COLUMNS)
        conn.execute(
            f"INSERT INTO {NEW}({column_list}) SELECT {column_list} FROM {TABLE}"
        )
        copied = int(conn.execute(f"SELECT COUNT(*) FROM {NEW}").fetchone()[0])
        if copied != before:
            raise RuntimeError(f"copy_count_mismatch:{copied}!={before}")
        mismatched = conn.execute(
            f"SELECT COUNT(*) FROM {TABLE} AS a LEFT JOIN {NEW} AS b "
            "ON a.truth_id=b.truth_id WHERE b.truth_id IS NULL OR "
            + " OR ".join(f"a.{col} IS NOT b.{col}" for col in COLUMNS[1:])
        ).fetchone()[0]
        if int(mismatched):
            raise RuntimeError(f"copy_content_mismatch:{mismatched}")
        for name, _ddl in LIVE_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        # Legacy rename: references elsewhere stay textual, so after the swap
        # they resolve to the widened table under the old name.
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute(f"ALTER TABLE {TABLE} RENAME TO {BACKUP}")
        conn.execute(f"ALTER TABLE {NEW} RENAME TO {TABLE}")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        for ddl in BACKUP_TRIGGERS:
            conn.execute(ddl)
        for _name, ddl in LIVE_TRIGGERS:
            conn.execute(ddl)
        referrers = _referrer_state(conn)
        if any(state == "points_at_backup" for state in referrers.values()):
            raise RuntimeError(f"referrer_moved_to_backup:{referrers}")
        final = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
        if final != before or not already_widened(conn):
            raise RuntimeError(f"post_swap_state_invalid:{final}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        raise
    receipt["row_count_after"] = final
    receipt["referrers_after"] = referrers
    receipt["state"] = "REBUILT"
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    database = args.database.expanduser().resolve(strict=True)
    conn = sqlite3.connect(str(database), timeout=600.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=600000")
        receipt = widen(conn, apply=args.apply)
    finally:
        conn.close()
    receipt["database"] = str(database)
    receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    write_bounded_json(args.receipt_output.expanduser().resolve(), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["state"] in {"DRY_RUN", "REBUILT", "ALREADY_WIDENED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
