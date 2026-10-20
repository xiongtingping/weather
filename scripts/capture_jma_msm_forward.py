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
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import sqlite_busy
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.network_retry import (
    is_network_error, note_network_failure,
)
from bayes_basket_target.pass_deadline import (
    announce_write_blocked,
)
from bayes_basket_target.jma_msm_archive import (
    CAPTURE_STATIONS, PROVIDER_ID, SYSTEM_ID, TRANSPORT_PROVIDER_ID,
    JmaMsmPassDeadlineExceeded,
    close_client, fetch_day_maximum, file_url, set_pass_deadline,
)
from bayes_basket_target.native_archive_lead import capture_lead_bucket

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
EXTRACTION_VERSION = "jma_msm_surface_nearest_cell_v1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
MEMBER_ID = "MSM_CONTROL"
MEMBER_ROLE = "CONTROL"
# The roster is the archive module's, so the board that reports which
# pairs were ruled in reads the same tuple this capture walks.
STATIONS = CAPTURE_STATIONS
# MSM publishes eight runs a day; these two align with the fleet's six-hourly
# ladder, so a scope formed here has peers to pair with rather than sitting
# alone in a cycle nobody else fits.
RUN_CYCLES = (0, 12)
P1_MAX_RUNS_PER_PASS = 1
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
# ...and a wall-clock bound beside the run count, for the reason its sibling
# lane already carries: the run count alone does not bound the pass.  Four
# attempts at "about 86 seconds" reads as six minutes; measured 2026-09-10 a
# successful pass took 17m00s wall at 0% CPU -- entirely ranged reads off a
# mirror the source comment already calls one that "sometimes times out" --
# against a 1,800s subprocess budget.  A mirror half that speed spends the
# whole budget, is killed, writes nothing, and has held the exclusive SSOT
# write domain for thirty minutes in which the production cycle cannot start.
#
# ``capture_noaa_conus_forward`` was given exactly this on 2026-09-09 after
# three days of being killed; ``regional_point_lane.capture_p1`` was bounded
# in the same pass.  This lane got the P1 budget and not the loop one, so it
# was the sibling still holding a run count where the seconds are what the
# writer domain rations.
#
# One clock, not two.  Since 2026-09-11 the P1 pass and the loop share this
# budget and so do the fetches themselves (``jma_msm_archive.pass_deadline``),
# so the old arithmetic of "900 for the loop plus 240 for P1" no longer
# describes anything.
#
# 900 was too tight once they shared it.  Measured 2026-09-11 at about
# 36 KB/s: a 900-second pass stopped on time (15m00.66s, clean exit) and
# wrote ZERO rows -- P1 spent the whole clock on two runs and the loop got no
# turn at all.  A lane that never makes progress is not cheaper than a slow
# one, it is just quieter.  1200 buys a third more working time and still
# leaves ten minutes under the 1,800-second subprocess timeout, which is far
# more than the one in-flight range that can still be running when the
# deadline fires (measured overshoot: 0.66s).
BUDGET_SEC_PER_PASS = 1200.0
# Seconds of writer occupancy this lane's P1 pass may spend.  The resident
# admits one writer at a time, so a capture that runs to its subprocess
# timeout is a production cycle that cannot start for that long.  The run
# cap counts productive runs only, so an unpublished-but-slow run is
# unbounded work; see the budget note in regional_point_lane.capture_p1.
P1_BUDGET_SEC = 240.0


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
    # Strip SQL comments first, then walk to the balanced close of the
    # IN(...) list.  The naive ``[^)]*`` version was defeated by its own
    # schema on 2026-08-28: a comment inside the parentheses carried a
    # ')' of its own, the parse truncated there, and the transport this
    # guard exists to admit read as absent right after the migration that
    # admitted it.
    ddl = re.sub(r"--[^\n]*", "", str(row[0]))
    match = re.search(r"transport_provider_id[^(]*IN\s*\(", ddl, re.S)
    if not match:
        # No parseable allowlist means no constraint to fail, so let the
        # INSERT itself be the judge rather than refusing on a guess.
        return True, ""
    start = match.end()
    depth = 1
    end = start
    while end < len(ddl) and depth:
        if ddl[end] == "(":
            depth += 1
        elif ddl[end] == ")":
            depth -= 1
        end += 1
    allowed = {
        part.strip().strip("'")
        for part in ddl[start:end - 1].split(",")
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
) -> set[str]:
    """Lead buckets already stored for this station-day.

    Keyed the way the table's UNIQUE is -- on the lead bucket -- and not on
    (run_cycle, init) as it first was.  Those name the run, but the table
    holds one row per (station, day, bucket): the first pass stored the
    00Z run into h12_24, the second pass computed the same bucket from the
    06Z run, found its (cycle, init) unheld, fetched 8MB, and died on the
    UNIQUE constraint it was always going to hit.  The pre-check has to ask
    the question the INSERT will ask.
    """

    return {
        str(row[0])
        for row in conn.execute(
            "SELECT archive_lead_bucket"
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
    # One wall clock for the whole pass -- the P1 capture below and the loop
    # after it -- and the same clock the fetches themselves check.  Bounding
    # only the gaps between fetches left a single station-day free to outlast
    # the subprocess budget; see ``jma_msm_archive.pass_deadline``.
    pass_deadline_mono = time.monotonic() + BUDGET_SEC_PER_PASS
    set_pass_deadline(BUDGET_SEC_PER_PASS)
    conn = sqlite3.connect(str(DB), timeout=120.0)
    # Charter 95 (re-ruled 2026-09-06): the P1 lane -- every MSM cycle.
    # ``prepare`` registers the system and ensures the schema; both write, so
    # a locked file is answered here rather than after the first 86-second
    # ranged read.
    from bayes_basket_target.regional_point_lane import (  # noqa: PLC0415
        capture_p1, prepare,
    )
    try:
        prepare(conn)
    except sqlite_busy.WriteBlocked as exc:
        announce_write_blocked(f"建表/注册 {exc}")
        conn.close()
        return 0
    _receipt = capture_p1(
        conn, product="MSM", now=now, stations=STATIONS,
        max_runs=P1_MAX_RUNS_PER_PASS, budget_sec=P1_BUDGET_SEC,
        dry_run=args.dry_run,
    )
    print(
        f"P1 MSM: 计划 {_receipt['runs_planned']} run，"
        f"取 {_receipt['runs_attempted']} run"
        + ("（预算用尽）" if _receipt["budget_exhausted"] else "")
        + f"，写入 {_receipt['written']}，"
        f"已有 {_receipt['existed']}，空 {_receipt['empty']}，失败 {len(_receipt['failed'])}"
    )
    for _line in _receipt["failed"][:4]:
        print(f"  {_line}")
    # A P1 pass that stopped on the write lock has already answered the
    # question the loop below would spend 86 seconds a station-day asking:
    # the file is not taking writes.  Report and stop.
    if _receipt.get("write_blocked"):
        announce_write_blocked(_receipt["write_blocked"])
        conn.close()
        return 0
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
    deadline = pass_deadline_mono
    budget_exhausted = False
    write_blocked = ""

    try:
        for station, latitude, longitude, zone_name in STATIONS:
            zone = ZoneInfo(zone_name)
            for offset in range(-BACKFILL_DAYS, LOOKAHEAD_DAYS + 1):
                target = (now.astimezone(zone).date()
                          + timedelta(days=offset))
                day_start_local = datetime.combine(
                    target, datetime.min.time(), tzinfo=zone
                )
                day_start = day_start_local.astimezone(timezone.utc)
                # The zone's signed offset, read BEFORE the conversion to
                # UTC zeroes it.  The original expression took utcoffset()
                # of the converted instant -- always zero -- and negated
                # it, so every capture used the UTC day's hours: near
                # harmless for a daily maximum (the afternoon sits inside
                # both windows) but the wrong window, found while building
                # the CONUS lane against stations seven hours west.
                offset_hours = int(
                    day_start_local.utcoffset().total_seconds() // 3600
                )
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
                        if bucket in held:
                            skipped += 1
                            continue
                        if attempted >= MAX_RUNS_PER_PASS:
                            continue
                        # The cheap check, before a fetch is started at
                        # all.  The fetch carries the same deadline and
                        # stops between ranges if it runs out mid-run --
                        # either way the write happens only after a whole
                        # station-day is in hand, so nothing is left half
                        # written and what is unreached is still missing
                        # next pass.
                        if time.monotonic() >= deadline:
                            budget_exhausted = True
                            continue
                        attempted += 1
                        try:
                            captured = fetch_day_maximum(
                                run_init=run, event_date=target,
                                utc_offset_hours=offset_hours,
                                latitude=latitude, longitude=longitude,
                            )
                        except JmaMsmPassDeadlineExceeded:
                            # Not a fault.  The pass ran out of clock inside
                            # the fetch, which is the bound doing its job;
                            # reporting it beside network faults would make a
                            # healthy lane read as a failing one.
                            budget_exhausted = True
                            break
                        except Exception as exc:  # noqa: BLE001
                            # Network faults and archive gaps both land here
                            # and both mean the same thing to this pass: try
                            # again next time.  Neither is a reason to stop
                            # capturing the runs that follow.
                            failed.append(
                                f"{station} {target} {cycle}"
                                f" {type(exc).__name__}: {str(exc)[:60]}"
                            )
                            if is_network_error(exc):
                                note_network_failure(failed[-1])
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
                        # Committed row by row, and a contended file stops the
                        # pass rather than ending it: see ``sqlite_busy``.
                        sqlite_busy.write_row(
                            conn,
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
                        # The pre-loop snapshot does not know about this
                        # row; without this, a same-pass run landing in the
                        # same bucket walks straight into the UNIQUE.
                        held.add(bucket)
                        written += 1
    except sqlite_busy.WriteBlocked as exc:
        # Every row before this one is committed; this run is not, and the
        # next pass plans it again.  Stopping here rather than fetching on
        # is the point: the lock is held by somebody whose round outlasts
        # this connection's busy_timeout, so the runs that follow would cost
        # the same minutes of mirror reads and end at the same INSERT.
        write_blocked = str(exc)
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
    elif budget_exhausted:
        print(
            f"  本轮用尽 {BUDGET_SEC_PER_PASS:.0f} 秒预算，"
            "剩余的下一轮继续"
        )
    for line in failed[:8]:
        print(f"  {line}")
    if write_blocked:
        announce_write_blocked(write_blocked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
