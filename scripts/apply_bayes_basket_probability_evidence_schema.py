#!/usr/bin/env python3
"""Narrow controlled migration for P0-history/P1-trajectory evidence tables."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    resolve_target_database,
)
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    apply_probability_evidence_schema,
)


CONFIRMATION = "REPORTED_TRAJECTORY_NO_STOPPED"
MIN_FREE_BYTES = 1 << 30
MAX_DB_BYTES = 256 << 20
MAX_WAL_BYTES = 64 << 20


def apply(database: Path, *, confirm: str) -> dict[str, object]:
    if confirm != CONFIRMATION:
        raise RuntimeError("probability_evidence_writer_stop_confirmation_missing")
    db = resolve_target_database(database)
    free_before = shutil.disk_usage(db.parent).free
    db_bytes_before = db.stat().st_size if db.exists() else 0
    wal = Path(str(db) + "-wal")
    wal_bytes_before = wal.stat().st_size if wal.exists() else 0
    if (
        free_before < MIN_FREE_BYTES
        or db_bytes_before > MAX_DB_BYTES
        or wal_bytes_before > MAX_WAL_BYTES
    ):
        raise RuntimeError("probability_evidence_resource_gate_failed")
    with sqlite3.connect(db, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = apply_probability_evidence_schema(
                conn, commit=False
            )
            quick_check = str(
                conn.execute("PRAGMA quick_check").fetchone()[0]
            )
            if quick_check != "ok":
                raise RuntimeError(
                    "probability_evidence_quick_check_failed"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        **result,
        "state": "migrated",
        "database": str(db),
        "writer_confirmed_stopped": True,
        "quick_check": quick_check,
        "free_bytes_before": free_before,
        "db_bytes_before": db_bytes_before,
        "wal_bytes_before": wal_bytes_before,
        "production_target": True,
        "execution_mode": "production_no_live",
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-reported-trajectory-no-stopped",
        action="store_true",
    )
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({
            "state": "dry_run",
            "database": str(args.db),
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }, sort_keys=True))
        return 0
    try:
        result = apply(
            args.db,
            confirm=(
                CONFIRMATION
                if args.confirm_reported_trajectory_no_stopped
                else ""
            ),
        )
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({
            "state": "blocked_fail_closed",
            "reason": f"{type(exc).__name__}:{exc}",
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
