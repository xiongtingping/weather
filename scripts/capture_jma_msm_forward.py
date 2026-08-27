"""Capture JMA MSM for RCSS into the member-run corpus, run by run.

The archive module can read one grid cell out of a 75 MB file for 8 MB of
range requests.  This is what calls it on a schedule and lands the result
where training and admission can see it, so the source can start earning the
seat its history says it deserves.

Three things this deliberately does not decide for itself.

The lead bucket comes from ``capture_lead_bucket``, production's own, because
that is the key training groups are formed under and a capture stamped with a
guess would never pair with anything -- the calendar labels it replaces
overlap across eighteen to thirty-one hours, so a wrong stamp is not a near
miss but a different group.

The station's local day decides which forecast hours matter, so which runs
are worth capturing is derived from the day, not from a fixed offset.

And nothing is captured for a day that has already begun locally: a run
issued after its own day started is not a forecast of it, and admitting one
would let a source score itself on information the others did not have.

Idempotent by content hash: a run already stored is skipped, so the cadence
can be as generous as the archive's publication delay requires without
storing the same field twice.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.jma_msm_archive import (
    FAMILY_ID, PROVIDER_ID, SYSTEM_ID, TRANSPORT_PROVIDER_ID,
    close_client, fetch_day_maximum, file_url,
)
from bayes_basket_target.native_archive_lead import capture_lead_bucket

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
EXTRACTION_VERSION = "jma_msm_surface_nearest_cell_v1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
MEMBER_ID = "MSM_CONTROL"
MEMBER_ROLE = "CONTROL"
# The station this source was admitted for, and only it.  Widening the list
# is a ruling: the other twenty-five candidate pairs were measured and
# refused, and eleven of them were refused at this very model.
STATIONS = (
    ("RCSS", 25.0697, 121.5525, "Asia/Taipei"),
)
# MSM publishes eight runs a day; these two align with the fleet's six-hourly
# ladder, so a scope formed here has peers to pair with rather than sitting
# alone in a cycle nobody else fits.
RUN_CYCLES = (0, 12)
# How far back to keep filling.  The archive holds far more, but a capture
# lane that reaches back indefinitely re-reads a settled corpus every pass.
BACKFILL_DAYS = 3
LOOKAHEAD_DAYS = 2
# Work per pass, bounded.  A station-day costs about 86 seconds of ranged
# reads, so an unbounded first pass over the backfill window is twenty-odd
# minutes -- and sweeps are serialised, so that is twenty-odd minutes no
# other writer can run.  Capping the pass makes the backfill finish over
# several of them instead, which the idempotence already supports: whatever
# is not reached this time is simply still missing next time.
MAX_RUNS_PER_PASS = 4


def _transport_is_writable(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Can this transport be stored at all, before anything is fetched.

    The table's ``transport_provider_id`` carries a fixed allowlist, and a
    transport not on it is refused at INSERT -- after the fetch has already
    cost about 86 seconds a run.  An hourly sweep that discovers this at the
    end of every pass burns six minutes an hour to learn something a single
    indexed read already knows, so it is checked at the front.

    Parsed from the live schema rather than restated here: a copy of the
    allowlist in this file would go stale the moment the real one is widened,
    and would then block a capture the table would actually accept.
    """

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        ("calibration_native_ensemble_historical_member_run",),
    ).fetchone()
    if not row or not row[0]:
        return False, "表不存在"
    match = re.search(
        r"transport_provider_id IN \(([^)]*)\)", str(row[0]), re.S
    )
    if not match:
        # No parseable allowlist means no constraint to fail, so let the
        # INSERT itself be the judge rather than refusing on a guess.
        return True, ""
    allowed = {
        part.strip().strip("'")
        for part in match.group(1).split(",")
        if part.strip()
    }
    if TRANSPORT_PROVIDER_ID in allowed:
        return True, ""
    return False, (
        f"transport {TRANSPORT_PROVIDER_ID} 不在表的白名单里"
        f"（允许: {', '.join(sorted(allowed))}）"
    )


def _rows_for_day(
    conn: sqlite3.Connection, station: str, event_date: str
) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT run_cycle, native_run_init_utc"
            " FROM calibration_native_ensemble_historical_member_run"
            " WHERE ensemble_system_id=? AND station_id=?"
            " AND target_local_date=?",
            (SYSTEM_ID, station, event_date),
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(DB), timeout=120.0)
    conn.execute("PRAGMA busy_timeout=60000")
    writable, reason = _transport_is_writable(conn)
    if not writable and not args.dry_run:
        # Refuse before spending the network, and say what would fix it.
        print(f"  采集已就绪但无法落库：{reason}")
        print("  在 transport 白名单加宽之前不抓取（每轮省约 6 分钟）")
        conn.close()
        return 0

    written = 0
    skipped = 0
    attempted = 0
    failed: list[str] = []

    try:
        for station, latitude, longitude, zone_name in STATIONS:
            zone = ZoneInfo(zone_name)
            for offset in range(-BACKFILL_DAYS, LOOKAHEAD_DAYS + 1):
                target = (now.astimezone(zone).date()
                          + timedelta(days=offset))
                day_start = datetime.combine(
                    target, datetime.min.time(), tzinfo=zone
                ).astimezone(timezone.utc)
                held = _rows_for_day(conn, station, target.isoformat())
                for hour in RUN_CYCLES:
                    for back in (1, 2):
                        run = datetime(
                            target.year, target.month, target.day,
                            hour, tzinfo=timezone.utc
                        ) - timedelta(days=back)
                        # A run issued after its own day began is not a
                        # forecast of that day.
                        if run >= day_start:
                            continue
                        # And one the archive has not had time to publish is
                        # a gap to retry, not a fault.
                        if run > now - timedelta(hours=3):
                            continue
                        cycle = f"{run:%H}Z"
                        init = run.isoformat().replace("+00:00", "Z")
                        if (cycle, init) in held:
                            skipped += 1
                            continue
                        bucket = capture_lead_bucket(
                            event_date=target.isoformat(),
                            settlement_timezone=zone_name,
                            run_init=run,
                        )
                        if bucket not in ("h12_24", "h24_48"):
                            # Outside the two groups this station's peers
                            # actually form.  Capturing it would build a
                            # scope with no one to pair with.
                            continue
                        if attempted >= MAX_RUNS_PER_PASS:
                            continue
                        attempted += 1
                        try:
                            captured = fetch_day_maximum(
                                run_init=run, event_date=target,
                                utc_offset_hours=int(
                                    day_start.utcoffset().total_seconds()
                                    // 3600
                                ) * -1,
                                latitude=latitude, longitude=longitude,
                            )
                        except Exception as exc:  # noqa: BLE001
                            # Network faults and archive gaps both land here
                            # and both mean the same thing to this pass: try
                            # again next time.  Neither is a reason to stop
                            # capturing the runs that follow.
                            failed.append(
                                f"{station} {target} {cycle}"
                                f" {type(exc).__name__}: {str(exc)[:60]}"
                            )
                            continue
                        if not captured["complete"]:
                            failed.append(
                                f"{station} {target} {cycle}"
                                f" 时次不全 {len(captured['hours_covered'])}"
                                f"/{len(captured['hours_wanted'])}"
                            )
                            continue
                        identity = {
                            "ensemble_system_id": SYSTEM_ID,
                            "station_id": station,
                            "target_local_date": target.isoformat(),
                            "run_cycle": cycle,
                            "native_run_init_utc": init,
                            "member_id": MEMBER_ID,
                            "raw_tmax_c": round(
                                captured["forecast_tmax_c"], 6
                            ),
                            "extraction_version": EXTRACTION_VERSION,
                        }
                        digest = content_sha256(identity)
                        url = file_url(run, "FH16-33")
                        print(
                            f"  {station} {target} {cycle} {bucket}"
                            f" {captured['forecast_tmax_c']:.2f}°C"
                            f" ({captured['bytes_downloaded']/1024:.0f} KB)"
                            + ("  [dry-run]" if args.dry_run else "")
                        )
                        if args.dry_run:
                            continue
                        # Plain INSERT, not OR IGNORE.  The first version
                        # used OR IGNORE and reported four rows written
                        # while the table stayed empty: a CHECK constraint
                        # was refusing every row and OR IGNORE swallowed it.
                        # Duplicates are already excluded by the held-rows
                        # query above, so the only thing IGNORE could hide
                        # here is a genuine fault.
                        conn.execute(
                            "INSERT INTO"
                            " calibration_native_ensemble_historical_member_run("
                            " historical_member_run_id, ensemble_system_id,"
                            " station_id, target_local_date,"
                            " archive_lead_bucket, run_cycle, member_id,"
                            " member_role, raw_tmax_c, settlement_definition,"
                            " extraction_version, provider_id,"
                            " transport_provider_id, native_run_init_utc,"
                            " offline_oos_eligible, archive_query_at_utc,"
                            " source_url, raw_object_ref,"
                            " source_object_sha256, qc_status,"
                            " authority_scope, holdout_eligible,"
                            " content_sha256)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                            "?,?,?,?)",
                            (
                                f"msm-{digest[:32]}", SYSTEM_ID, station,
                                target.isoformat(), bucket, cycle,
                                MEMBER_ID, MEMBER_ROLE,
                                float(captured["forecast_tmax_c"]),
                                SETTLEMENT_DEFINITION, EXTRACTION_VERSION,
                                PROVIDER_ID, TRANSPORT_PROVIDER_ID, init,
                                1,
                                now.isoformat().replace("+00:00", "Z"),
                                url, captured["grid_geometry"], digest,
                                "ACCEPTED", "CALIBRATION_TRAIN_ONLY", 0,
                                digest,
                            ),
                        )
                        conn.commit()
                        written += 1
    finally:
        close_client()
        conn.close()

    print(
        f"\n写入 {written}，已有跳过 {skipped}，"
        f"本轮尝试 {attempted}/{MAX_RUNS_PER_PASS}，"
        f"失败/不全 {len(failed)}"
    )
    if attempted >= MAX_RUNS_PER_PASS:
        print("  达到本轮上限，剩余的下一轮继续")
    for line in failed[:8]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
