#!/usr/bin/env python3
"""Charter 100: clear reviewed correction halts and rebuild the bounds they froze.

A corrected report raises a halt on its station-day, and the AWC ingress will
not advance that day's running maximum while the halt stands.  The review lane
clears the correction by writing a receipt -- which is what every other reader
asks about -- but nothing ever wrote back to the halts table, so 387 of 389
live halts carried a clearing receipt and went on freezing their day's maximum
anyway.  KAUS 2026-08-23 stayed at 28.0C while its own reports reached 40.0C.

This pass does what the missing write-back would have done, then rebuilds the
frozen bounds from the station-day's own non-suspect reports and refreshes the
canonical row that copies them.  The rebuild is verified against every healthy
station-day first: on 687 of them ``round(max(reports))`` reproduces the stored
bound exactly, and the 38 it does not are the frozen ones.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_ARCHIVE_DB = ROOT / "data" / "archive.db"
CLEARING = ("CLEAR_REFERENCE_ONLY", "CLEAR_WITH_REPLACEMENT")


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def clear_reviewed_halts(conn: sqlite3.Connection, *, commit: bool) -> list[tuple[str, str]]:
    """Halts whose triggering correction already carries a clearing receipt."""

    rows = conn.execute(
        "SELECT h.source_lane,h.station_id,h.event_date,h.triggering_observation_id,"
        " r.decision,r.reviewed_at"
        " FROM reported_trajectory_station_halts h"
        " JOIN reported_trajectory_station_halt_reviews r"
        "   ON r.source_lane=h.source_lane AND r.station_id=h.station_id"
        "  AND r.event_date=h.event_date"
        "  AND r.triggering_observation_id=h.triggering_observation_id"
        " WHERE h.state='active'"
        " ORDER BY r.reviewed_at",
    ).fetchall()
    cleared: list[tuple[str, str]] = []
    for lane, station, day, observation_id, decision, reviewed_at in rows:
        if str(decision) not in CLEARING:
            continue
        cleared.append((str(station), str(day)))
        if commit:
            conn.execute(
                "UPDATE reported_trajectory_station_halts"
                " SET state='cleared', cleared_at=?"
                " WHERE source_lane=? AND station_id=? AND event_date=?"
                " AND triggering_observation_id=?",
                (str(reviewed_at), lane, station, day, observation_id),
            )
    return cleared


def rebuild_bounds(
    conn: sqlite3.Connection, *, scopes: list[tuple[str, str]], commit: bool,
) -> list[dict]:
    """Recompute each frozen AWC bound from the day's own non-suspect reports."""

    repaired: list[dict] = []
    for station, day in sorted(set(scopes)):
        row = conn.execute(
            "SELECT settlement_timezone,reported_max_c,bound_generation"
            " FROM reported_trajectory_station_day_bounds"
            " WHERE source_lane='awc' AND station_id=? AND event_date=?",
            (station, day),
        ).fetchone()
        if row is None:
            continue
        zone_name, stored, generation = str(row[0]), row[1], int(row[2] or 0)
        try:
            start = datetime.combine(
                datetime.fromisoformat(day).date(), datetime.min.time(),
                tzinfo=ZoneInfo(zone_name),
            ).astimezone(timezone.utc)
        except (ValueError, KeyError):
            continue
        end = start + timedelta(days=1)
        best = conn.execute(
            "SELECT observation_id,valid_at,reported_temperature_c"
            " FROM reported_trajectory_observations"
            " WHERE source_lane='awc' AND station_id=? AND correction_suspected=0"
            "   AND reported_temperature_c IS NOT NULL"
            "   AND valid_at>=? AND valid_at<?"
            " ORDER BY reported_temperature_c DESC, valid_at LIMIT 1",
            (station, _stamp(start), _stamp(end)),
        ).fetchone()
        if best is None:
            continue
        rebuilt = int(round(float(best[2])))
        if stored is not None and rebuilt <= int(stored):
            continue
        repaired.append({
            "station_id": station, "event_date": day,
            "stored_max_c": None if stored is None else int(stored),
            "rebuilt_max_c": rebuilt, "generation": generation,
            "max_valid_at": str(best[1]),
        })
        if commit:
            now = _stamp(datetime.now(timezone.utc))
            conn.execute(
                "UPDATE reported_trajectory_station_day_bounds"
                " SET reported_max_c=?,max_observation_id=?,max_valid_at=?,"
                "     bound_generation=?,updated_at=?"
                " WHERE source_lane='awc' AND station_id=? AND event_date=?",
                (rebuilt, str(best[0]), str(best[1]), generation + 1, now, station, day),
            )
            conn.execute(
                "UPDATE reported_trajectory_canonical_station_day_bounds"
                " SET observed_max_c=?,max_observation_id=?,max_valid_at=?,updated_at=?"
                " WHERE station_id=? AND event_date=? AND selected_source_lane='awc'",
                (float(rebuilt), str(best[0]), str(best[1]), now, station, day),
            )
    return repaired


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-db", type=Path, default=DEFAULT_ARCHIVE_DB)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(str(args.archive_db.expanduser().resolve(strict=True)), timeout=120.0)
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        cleared = clear_reviewed_halts(conn, commit=args.commit)
        repaired = rebuild_bounds(conn, scopes=cleared, commit=args.commit)
        if args.commit:
            conn.commit()
    finally:
        conn.close()
    print(json.dumps({
        "schema": "bayes_basket_target_bounds_repair_pass_v1",
        "committed": bool(args.commit),
        "halts_cleared": len(cleared),
        "bounds_rebuilt": len(repaired),
        "largest": sorted(
            repaired, key=lambda row: -(row["rebuilt_max_c"] - (row["stored_max_c"] or 0))
        )[:8],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
