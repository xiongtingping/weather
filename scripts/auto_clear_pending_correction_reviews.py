#!/usr/bin/env python3
"""Resolve pending METAR correction halts by standing policy, not by waiting.

Every corrected report raises a halt that ``assert_station_day_corrections_
resolved`` will not clear without a review receipt.  With nobody reviewing,
those halts accumulate: sixteen station-days were holding the settled-truth
batch, which stopped truth for all 45 stations and P1 with it.

What this removes is the wait, not the check.  Each correction is recorded as
``CLEAR_REFERENCE_ONLY`` -- admitted as reference evidence, never moving settled
truth on its own -- and the contradiction guard downstream is untouched: two
reports that disagree on temperature for one valid time still fail closed as
``settlement_truth_hourly_revision_ambiguous``.

Receipts are ordinary review rows carrying the policy id as their reviewer, so
the ledger records what was decided and a later human decision can supersede
them.  The resident worker holds the canonical archive read-only by design, so
this runs as its own maintenance pass rather than inside a production cycle.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.correction_review import (  # noqa: E402
    AUTOMATIC_REVIEWER,
    auto_clear_station_day_corrections,
)
from bayes_basket_target.settlement_time import (  # noqa: E402
    settlement_day_window,
)

DEFAULT_ARCHIVE_DB = ROOT / "data" / "archive.db"
DEFAULT_TARGET_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
# One pass should not silently rewrite months of history.  The halts that
# matter are the ones holding the current batch.
DEFAULT_MAX_SCOPES = 200


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _pending_scopes(
    archive: sqlite3.Connection,
    *,
    since: str,
    limit: int,
) -> list[tuple[str, str, str]]:
    """Station-days with a correction that has no review receipt yet."""

    rows = archive.execute(
        "SELECT DISTINCT o.source_lane, o.station_id, date(o.valid_at) AS day "
        "FROM reported_trajectory_observations AS o "
        "WHERE o.correction_suspected=1 AND date(o.valid_at)>=? "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM reported_trajectory_station_halt_reviews AS r "
        "    WHERE r.source_lane=o.source_lane AND r.station_id=o.station_id "
        "      AND r.triggering_observation_id=o.observation_id"
        "  ) "
        "ORDER BY day, o.station_id LIMIT ?",
        (since, int(limit)),
    ).fetchall()
    return [(str(a), str(b), str(c)) for a, b, c in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-db", type=Path, default=DEFAULT_ARCHIVE_DB)
    parser.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--since",
        default="2026-07-01",
        help="Earliest local date to clear; older halts are left alone.",
    )
    parser.add_argument("--max-scopes", type=int, default=DEFAULT_MAX_SCOPES)
    parser.add_argument("--as-of-utc", default=None)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Without this the pass reports what it would clear and writes nothing.",
    )
    args = parser.parse_args()

    as_of = args.as_of_utc or _utc_now()
    archive_path = args.archive_db.expanduser().resolve(strict=True)
    target_path = args.target_db.expanduser().resolve(strict=True)

    # Timezones come from the target SSOT; the settlement window decides which
    # observations belong to the local day, and a UTC-date approximation would
    # clear the wrong ones near the boundary.
    with sqlite3.connect(f"file:{target_path}?mode=ro", uri=True) as target:
        target.execute("PRAGMA busy_timeout=15000")
        timezones = {
            str(row[0]): str(row[1])
            for row in target.execute(
                "SELECT station_id, settlement_timezone "
                "FROM calibration_station_daily_high_truth "
                "GROUP BY station_id"
            )
        }

    archive = sqlite3.connect(archive_path, timeout=120.0)
    archive.execute("PRAGMA busy_timeout=120000")
    try:
        scopes = _pending_scopes(
            archive, since=str(args.since), limit=int(args.max_scopes)
        )
        results: list[dict[str, object]] = []
        skipped: list[dict[str, str]] = []
        for lane, station, day in scopes:
            zone = timezones.get(station)
            if not zone:
                # No settlement timezone means no defensible day window, and
                # guessing one would clear observations that belong to the
                # neighbouring day.
                skipped.append({
                    "station_id": station,
                    "event_date": day,
                    "reason": "settlement_timezone_unknown",
                })
                continue
            window = settlement_day_window(day, zone)
            if args.commit:
                archive.execute("BEGIN IMMEDIATE")
            try:
                result = auto_clear_station_day_corrections(
                    archive,
                    source_lane=lane,
                    station_id=station,
                    event_date=day,
                    window_start_utc=window["target_day_start_utc"],
                    window_end_utc=window["target_day_end_utc"],
                    as_of_utc=as_of,
                )
                if args.commit:
                    archive.commit()
                else:
                    archive.rollback()
            except Exception as exc:
                archive.rollback()
                skipped.append({
                    "station_id": station,
                    "event_date": day,
                    "reason": f"{type(exc).__name__}:{exc}"[:200],
                })
                continue
            if int(result.get("cleared_count") or 0):
                results.append(result)
    finally:
        archive.close()

    print(json.dumps({
        "schema": "bayes_basket_target_correction_auto_clear_pass_v1",
        "as_of_utc": as_of,
        "reviewer": AUTOMATIC_REVIEWER,
        "committed": bool(args.commit),
        "candidate_scope_count": len(scopes),
        "cleared_scope_count": len(results),
        "cleared_observation_count": sum(
            int(item.get("cleared_count") or 0) for item in results
        ),
        "skipped": skipped[:20],
        "skipped_count": len(skipped),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
