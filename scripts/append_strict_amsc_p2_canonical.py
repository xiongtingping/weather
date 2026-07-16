#!/usr/bin/env python3
"""Append current-day strict primary AMSC reports to the P2 canonical log.

Default mode is read-only planning.  Apply mode is intentionally restricted
to today's Asia/Shanghai physical station day, requires an explicit phrase,
and uses the shared archive writer gate.  It never writes posterior/fact/
outbox/decision/order state.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_db_write_queue import connect_archive_db_rw  # noqa: E402
from p2_canonical_amsc_forward_writer import (  # noqa: E402
    apply_p2_canonical_amsc_forward_append,
    build_p2_canonical_amsc_forward_append_plan,
)


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_RAW_ARCHIVE = ROOT / "data" / "amsc_raw_history"
APPLY_CONFIRMATION = "APPEND_STRICT_AMSC_P2_CANONICAL"


def _now_iso() -> str:
    return datetime.now(ZoneInfo("UTC")).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _today_physical_station_day() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _compact(result: dict) -> dict:
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else result
    return {
        "schema": result.get("schema") or plan.get("schema"),
        "ok": result.get("ok") is True,
        "state": result.get("state") or plan.get("state"),
        "blockers": plan.get("blockers") or [],
        "event_date": plan.get("event_date"),
        "station_id": plan.get("station_id"),
        "target_identity": plan.get("target_identity"),
        "strict_source_read": plan.get("strict_source_read"),
        "proposal_summary": plan.get("proposal_summary"),
        "persistence": result.get("persistence"),
        "writes": result.get("writes"),
        "write_scope": plan.get("write_scope"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--raw-archive-dir", default=str(DEFAULT_RAW_ARCHIVE))
    parser.add_argument("--event-date", default=_today_physical_station_day())
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--target-identity", default="")
    parser.add_argument("--decision-as-of", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--forward-capture",
        action="store_true",
        help="attest this is the current AMSC forward capture lane, not historical backfill",
    )
    args = parser.parse_args(argv)

    decision_as_of = str(args.decision_as_of or _now_iso())
    event_date = str(args.event_date or "")[:10]
    if not args.apply:
        try:
            conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
            try:
                result = build_p2_canonical_amsc_forward_append_plan(
                    conn,
                    event_date=event_date,
                    station_id=args.station_id,
                    raw_archive_dir=args.raw_archive_dir,
                    decision_as_of=decision_as_of,
                    target_identity=args.target_identity,
                )
            finally:
                conn.close()
        except Exception as exc:
            result = {"schema": "p2_canonical_amsc_forward_writer_v1", "ok": False, "state": "blocked", "blockers": [f"p2_canonical_plan_failed:{exc.__class__.__name__}"]}
        print(json.dumps(_compact(result), ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") is True else 1

    blockers: list[str] = []
    if args.confirm != APPLY_CONFIRMATION:
        blockers.append("p2_canonical_apply_confirmation_missing_or_invalid")
    if args.forward_capture is not True:
        blockers.append("p2_canonical_forward_capture_attestation_required")
    if event_date != _today_physical_station_day():
        blockers.append("p2_canonical_noncurrent_event_date_forbidden")
    if blockers:
        print(json.dumps({"schema": "p2_canonical_amsc_forward_writer_v1", "ok": False, "state": "blocked", "blockers": blockers}, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        with connect_archive_db_rw(Path(args.db), owner="p2_canonical_amsc_forward_writer") as conn:
            result = apply_p2_canonical_amsc_forward_append(
                conn,
                event_date=event_date,
                station_id=args.station_id,
                raw_archive_dir=args.raw_archive_dir,
                decision_as_of=decision_as_of,
                target_identity=args.target_identity,
            )
    except Exception as exc:
        result = {
            "schema": "p2_canonical_amsc_forward_writer_v1",
            "ok": False,
            "state": "blocked",
            "blockers": [f"p2_canonical_apply_failed:{exc.__class__.__name__}"],
        }
    print(json.dumps(_compact(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
