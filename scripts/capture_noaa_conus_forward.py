"""Capture HRRR and NBM daily maxima into the member-run corpus.

Same shape as the MSM lane: production's own lead bucket, the station's
local day deciding which hours matter, nothing captured for a day already
begun, idempotent by the table's own UNIQUE.  Two differences earn their
keep.  The run list is pre-filtered by ``run_covers_peak`` -- pure
arithmetic -- because these products have hard coverage cliffs (HRRR ends
at f48, the NBM goes three-hourly after f36) and a run that cannot reach a
station's afternoon would otherwise burn a fetch every pass to fail the
same way.  And the local-day offset is computed before the UTC conversion:
the MSM lane took ``utcoffset()`` of an already-converted instant, which
is always zero, and captured UTC days by accident -- near-harmless for a
daily maximum (the afternoon sits inside both windows) but wrong, and
fixed there in the same commit that adds this lane.

Station lists are the sweep's verdicts, not the roster: KLAX/HRRR measured
reversed (the served posterior beat it) and is excluded from HRRR while
staying in NBM; Toronto measured reversed and is in neither.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.noaa_conus_archive import (
    PROVIDER_IDS, SYSTEM_IDS, TRANSPORTS,
    close_client, fetch_day_maximum, run_covers_peak, run_directory_url,
)
from bayes_basket_target.native_archive_lead import capture_lead_bucket

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
EXTRACTION_VERSIONS = {
    "HRRR": "ncep_hrrr_surface_nearest_cell_v1",
    "NBM": "ncep_nbm_core_nearest_cell_v1",
}
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
MEMBER_IDS = {"HRRR": "HRRR_CONTROL", "NBM": "NBM_BLEND"}
MEMBER_ROLE = "CONTROL"
US_STATIONS = (
    ("KATL", 33.62972, -84.44223, "America/New_York"),
    ("KAUS", 30.1831, -97.68063, "America/Chicago"),
    ("KBKF", 39.713, -104.758, "America/Denver"),
    ("KDAL", 32.83836, -96.83584, "America/Chicago"),
    ("KHOU", 29.64582, -95.28214, "America/Chicago"),
    ("KLAX", 33.93817, -118.3866, "America/Los_Angeles"),
    ("KLGA", 40.77945, -73.88027, "America/New_York"),
    ("KMIA", 25.78806, -80.31692, "America/New_York"),
    ("KORD", 41.96017, -87.93161, "America/Chicago"),
    ("KSEA", 47.44467, -122.31442, "America/Los_Angeles"),
    ("KSFO", 37.61961, -122.36561, "America/Los_Angeles"),
)
STATIONS = {
    # KLAX/HRRR measured reversed in the 46-pair sweep; it stays in NBM,
    # where it did not.
    "HRRR": tuple(s for s in US_STATIONS if s[0] != "KLAX"),
    "NBM": US_STATIONS,
}
# Aligned with the fleet's six-hourly ladder for MSM's reason: a scope
# formed here has peers to pair with.  The peak pre-filter prunes what a
# product cannot serve (NBM's 00Z never covers a US afternoon).
RUN_CYCLES = (0, 12)
BACKFILL_DAYS = 3
LOOKAHEAD_DAYS = 2
# An NBM station-day is about 50 MB of ranged reads and an HRRR one about
# 30 MB; six runs keeps a pass under a few minutes of serialized writer
# time while still outrunning the ~30 new runs a day the fleet produces.
MAX_RUNS_PER_PASS = 6


def _transport_is_writable(
    conn: sqlite3.Connection, transport: str
) -> tuple[bool, str]:
    """Same pre-flight as the MSM lane, parameterized by transport."""

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        ("calibration_native_ensemble_historical_member_run",),
    ).fetchone()
    if not row or not row[0]:
        return False, "表不存在"
    ddl = re.sub(r"--[^\n]*", "", str(row[0]))
    match = re.search(r"transport_provider_id[^(]*IN\s*\(", ddl, re.S)
    if not match:
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
    if transport in allowed:
        return True, ""
    return False, f"transport {transport} 不在表的白名单里"


def _rows_for_day(
    conn: sqlite3.Connection, system_id: str, station: str, event_date: str
) -> set[str]:
    """Lead buckets already stored -- the question the UNIQUE will ask."""

    return {
        str(row[0])
        for row in conn.execute(
            "SELECT archive_lead_bucket"
            " FROM calibration_native_ensemble_historical_member_run"
            " WHERE ensemble_system_id=? AND station_id=?"
            " AND target_local_date=?",
            (system_id, station, event_date),
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(DB), timeout=120.0)
    conn.execute("PRAGMA busy_timeout=60000")

    written = 0
    skipped = 0
    attempted = 0
    failed: list[str] = []

    try:
        for product in ("HRRR", "NBM"):
            writable, reason = _transport_is_writable(
                conn, TRANSPORTS[product]
            )
            if not writable and not args.dry_run:
                print(f"  {product} 采集已就绪但无法落库：{reason}")
                continue
            system_id = SYSTEM_IDS[product]
            for station, latitude, longitude, zone_name in STATIONS[product]:
                zone = ZoneInfo(zone_name)
                for offset in range(-BACKFILL_DAYS, LOOKAHEAD_DAYS + 1):
                    target = (now.astimezone(zone).date()
                              + timedelta(days=offset))
                    day_start_local = datetime.combine(
                        target, datetime.min.time(), tzinfo=zone
                    )
                    day_start = day_start_local.astimezone(timezone.utc)
                    # The zone's signed UTC offset, taken BEFORE the
                    # conversion to UTC zeroes it.
                    offset_hours = int(
                        day_start_local.utcoffset().total_seconds() // 3600
                    )
                    held = _rows_for_day(
                        conn, system_id, station, target.isoformat()
                    )
                    for hour in RUN_CYCLES:
                        for back in (1, 2):
                            run = datetime(
                                target.year, target.month, target.day,
                                hour, tzinfo=timezone.utc
                            ) - timedelta(days=back)
                            if run >= day_start:
                                continue
                            # S3 publication lags the run by about two
                            # hours; the f48 tail arrives later still.
                            if run > now - timedelta(hours=4):
                                continue
                            if not run_covers_peak(
                                product=product, run_init=run,
                                event_date=target,
                                utc_offset_hours=offset_hours,
                            ):
                                continue
                            cycle = f"{run:%H}Z"
                            init = run.isoformat().replace("+00:00", "Z")
                            bucket = capture_lead_bucket(
                                event_date=target.isoformat(),
                                settlement_timezone=zone_name,
                                run_init=run,
                            )
                            if bucket not in ("h12_24", "h24_48"):
                                continue
                            if bucket in held:
                                skipped += 1
                                continue
                            if attempted >= MAX_RUNS_PER_PASS:
                                continue
                            attempted += 1
                            try:
                                captured = fetch_day_maximum(
                                    product=product, run_init=run,
                                    event_date=target,
                                    utc_offset_hours=offset_hours,
                                    latitude=latitude, longitude=longitude,
                                )
                            except Exception as exc:  # noqa: BLE001
                                failed.append(
                                    f"{product} {station} {target} {cycle}"
                                    f" {type(exc).__name__}:"
                                    f" {str(exc)[:60]}"
                                )
                                continue
                            if not captured["complete"]:
                                failed.append(
                                    f"{product} {station} {target} {cycle}"
                                    " 时次不全"
                                    f" {len(captured['hours_covered'])}"
                                    f"/{len(captured['hours_wanted'])}"
                                )
                                continue
                            identity = {
                                "ensemble_system_id": system_id,
                                "station_id": station,
                                "target_local_date": target.isoformat(),
                                "run_cycle": cycle,
                                "native_run_init_utc": init,
                                "member_id": MEMBER_IDS[product],
                                "raw_tmax_c": round(
                                    captured["forecast_tmax_c"], 6
                                ),
                                "extraction_version":
                                    EXTRACTION_VERSIONS[product],
                            }
                            digest = content_sha256(identity)
                            url = run_directory_url(product, run)
                            print(
                                f"  {product} {station} {target} {cycle}"
                                f" {bucket}"
                                f" {captured['forecast_tmax_c']:.2f}°C"
                                f" ({captured['bytes_downloaded']/1048576:.1f}"
                                " MB)"
                                + ("  [dry-run]" if args.dry_run else "")
                            )
                            if args.dry_run:
                                continue
                            conn.execute(
                                "INSERT INTO"
                                " calibration_native_ensemble_historical_member_run("
                                " historical_member_run_id,"
                                " ensemble_system_id,"
                                " station_id, target_local_date,"
                                " archive_lead_bucket, run_cycle, member_id,"
                                " member_role, raw_tmax_c,"
                                " settlement_definition,"
                                " extraction_version, provider_id,"
                                " transport_provider_id,"
                                " native_run_init_utc,"
                                " offline_oos_eligible, archive_query_at_utc,"
                                " source_url, raw_object_ref,"
                                " source_object_sha256, qc_status,"
                                " authority_scope, holdout_eligible,"
                                " content_sha256)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                                "?,?,?,?,?,?)",
                                (
                                    f"{product.lower()}-{digest[:32]}",
                                    system_id, station,
                                    target.isoformat(), bucket, cycle,
                                    MEMBER_IDS[product], MEMBER_ROLE,
                                    float(captured["forecast_tmax_c"]),
                                    SETTLEMENT_DEFINITION,
                                    EXTRACTION_VERSIONS[product],
                                    PROVIDER_IDS[product],
                                    TRANSPORTS[product], init,
                                    1,
                                    now.isoformat().replace("+00:00", "Z"),
                                    url, captured["grid_geometry"], digest,
                                    "ACCEPTED", "CALIBRATION_TRAIN_ONLY", 0,
                                    digest,
                                ),
                            )
                            conn.commit()
                            held.add(bucket)
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
