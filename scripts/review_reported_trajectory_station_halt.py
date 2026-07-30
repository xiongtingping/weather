#!/usr/bin/env python3
"""Append one explicit correction review and update only the derived halt cache."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.correction_review import (
    CLEAR_REFERENCE_ONLY,
    DECISIONS,
    QUARANTINE_STATION_DAY,
    append_correction_review,
    assert_station_day_corrections_resolved,
)
from bayes_basket_target.settlement_time import settlement_day_window
from reported_trajectory_no_schema import (
    reported_trajectory_no_schema_status,
)


DEFAULT_DB = ROOT / "data" / "archive.db"
MAX_ARCHIVE_WAL_BYTES = 512 * 1024 * 1024
MIN_ARCHIVE_FREE_BYTES = 1024 * 1024 * 1024


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _storage_guard(path: Path) -> dict[str, int | bool]:
    wal = Path(str(path) + "-wal")
    wal_bytes = int(wal.stat().st_size) if wal.exists() else 0
    free_bytes = int(shutil.disk_usage(path.parent).free)
    if wal_bytes > MAX_ARCHIVE_WAL_BYTES:
        raise ContractError("correction_review_archive_wal_limit_exceeded")
    if free_bytes < MIN_ARCHIVE_FREE_BYTES:
        raise ContractError("correction_review_archive_free_space_too_low")
    return {
        "ok": True,
        "wal_bytes": wal_bytes,
        "free_bytes": free_bytes,
    }


def _registry_window(
    conn: sqlite3.Connection,
    *,
    source_lane: str,
    station_id: str,
    event_date: str,
) -> dict[str, str]:
    rows = conn.execute(
        "SELECT DISTINCT settlement_timezone,source_lane "
        "FROM reported_trajectory_market_registry "
        "WHERE station_id=? AND event_date=? AND source_eligible=1",
        (station_id, event_date),
    ).fetchall()
    scopes = {
        (
            str(row[0] or "").strip(),
            str(row[1] or "").strip().lower(),
        )
        for row in rows
    }
    if len(scopes) != 1:
        raise ContractError("correction_review_registry_scope_not_unique")
    timezone_name, registry_lane = next(iter(scopes))
    if registry_lane != source_lane:
        raise ContractError("correction_review_registry_source_lane_mismatch")
    window = settlement_day_window(event_date, timezone_name)
    return {
        "settlement_timezone": timezone_name,
        "start_utc": str(window["target_day_start_utc"]),
        "end_utc": str(window["target_day_end_utc"]),
    }


def apply_review(
    *,
    db_path: Path,
    source_lane: str,
    station_id: str,
    event_date: str,
    triggering_observation_id: str,
    decision: str,
    reviewer: str,
    rationale: str,
    reviewed_at: str,
) -> dict:
    requested_database = db_path.expanduser()
    if requested_database.is_symlink():
        raise ContractError("correction_review_database_symlink_forbidden")
    database = requested_database.resolve(strict=True)
    storage = _storage_guard(database)
    conn = sqlite3.connect(
        database,
        timeout=30.0,
        isolation_level=None,
    )
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    try:
        status = reported_trajectory_no_schema_status(conn)
        if status.get("ok") is not True:
            raise ContractError(
                "correction_review_schema_not_migrated:"
                + ",".join(status.get("missing_tables") or [])
            )
        window = _registry_window(
            conn,
            source_lane=source_lane,
            station_id=station_id,
            event_date=event_date,
        )
        conn.execute("BEGIN IMMEDIATE")
        receipt = append_correction_review(
            conn,
            source_lane=source_lane,
            station_id=station_id,
            event_date=event_date,
            triggering_observation_id=triggering_observation_id,
            decision=decision,
            reviewer=reviewer,
            rationale=rationale,
            reviewed_at=reviewed_at,
        )
        if decision == CLEAR_REFERENCE_ONLY:
            resolution = assert_station_day_corrections_resolved(
                conn,
                source_lane=source_lane,
                station_id=station_id,
                event_date=event_date,
                window_start_utc=window["start_utc"],
                window_end_utc=window["end_utc"],
                as_of_utc=reviewed_at,
            )
            conn.execute(
                "UPDATE reported_trajectory_station_halts "
                "SET state='cleared',"
                "reason_code='manual_review_clear_reference_only',"
                "cleared_at=? "
                "WHERE source_lane=? AND station_id=? AND event_date=?",
                (reviewed_at, source_lane, station_id, event_date),
            )
        else:
            resolution = {
                "state": "QUARANTINED_STATION_DAY",
                "review_ids": [receipt["review_id"]],
            }
            conn.execute(
                """
                INSERT INTO reported_trajectory_station_halts(
                  source_lane,station_id,event_date,state,reason_code,
                  triggering_observation_id,created_at,cleared_at
                ) VALUES(?,?,?,'active','manual_review_quarantine_station_day',?,?,NULL)
                ON CONFLICT(source_lane,station_id,event_date) DO UPDATE SET
                  state='active',
                  reason_code='manual_review_quarantine_station_day',
                  triggering_observation_id=excluded.triggering_observation_id,
                  created_at=excluded.created_at,
                  cleared_at=NULL
                """,
                (
                    source_lane, station_id, event_date,
                    triggering_observation_id, reviewed_at,
                ),
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "schema": "reported_trajectory_station_halt_review_apply_v1",
        "state": "committed",
        "database": str(database),
        "station_id": station_id,
        "event_date": event_date,
        "source_lane": source_lane,
        "decision": decision,
        "review": receipt,
        "resolution": resolution,
        "storage": storage,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--source-lane", choices=("amsc", "awc"), required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--triggering-observation-id", required=True)
    parser.add_argument("--decision", choices=sorted(DECISIONS), required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--reviewed-at", default="")
    parser.add_argument(
        "--confirm-review",
        action="store_true",
        help="Required explicit confirmation; no review is written without it.",
    )
    args = parser.parse_args()
    if not args.confirm_review:
        raise SystemExit("correction_review_confirmation_required")
    result = apply_review(
        db_path=args.db,
        source_lane=args.source_lane,
        station_id=args.station_id.strip().upper(),
        event_date=args.event_date.strip(),
        triggering_observation_id=args.triggering_observation_id.strip(),
        decision=args.decision,
        reviewer=args.reviewer.strip(),
        rationale=args.rationale.strip(),
        reviewed_at=args.reviewed_at.strip() or _utc_now(),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
