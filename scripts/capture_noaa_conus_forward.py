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
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.noaa_conus_archive import (
    CAPTURE_STATIONS, PROVIDER_IDS, SYSTEM_IDS, TRANSPORTS,
    NoaaConusPassDeadlineExceeded,
    close_client, fetch_day_maximum, run_covers_peak, run_directory_url,
)
from bayes_basket_target.pass_deadline import set_pass_deadline
from bayes_basket_target.native_archive_lead import capture_lead_bucket

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
EXTRACTION_VERSIONS = {
    "HRRR": "ncep_hrrr_surface_nearest_cell_v1",
    "NBM": "ncep_nbm_core_nearest_cell_v1",
}
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
MEMBER_IDS = {"HRRR": "HRRR_CONTROL", "NBM": "NBM_BLEND"}
MEMBER_ROLE = "CONTROL"
STATIONS = CAPTURE_STATIONS
# Aligned with the fleet's six-hourly ladder for MSM's reason: a scope
# formed here has peers to pair with.  The peak pre-filter prunes what a
# product cannot serve (NBM's 00Z never covers a US afternoon).
RUN_CYCLES = (0, 12)
P1_MAX_RUNS_PER_PASS = 3
BACKFILL_DAYS = 3
LOOKAHEAD_DAYS = 2
# An NBM station-day is about 50 MB of ranged reads and an HRRR one about
# 30 MB; six runs keeps a pass under a few minutes of serialized writer
# time while still outrunning the ~30 new runs a day the fleet produces.
MAX_RUNS_PER_PASS = 6
# ...and a wall-clock bound beside the run count, because the run count alone
# does not bound the pass (2026-09-09).  Six ranged S3 reads at 30-50 MB each
# take as long as the archive takes; the hosted budget is 1,800s and this pass
# had been consuming all of it and being ``timeout_killed`` since 2026-09-06.
# A killed pass never advances its completion clock, so it was maximally
# overdue at every restart, took the exclusive SSOT write domain again, and
# starved six other SSOT members 12+ hours behind it on a 3-hour interval.
#
# A sharded lane's contract is "do a bounded amount and RETURN"; a lane that
# does an unbounded amount and gets killed is not sharded, it is a hostage.
# 900s leaves the P1 lane's 480s and a margin under the 1,800s budget.
BUDGET_SEC_PER_PASS = 900.0
# Seconds of writer occupancy this lane may spend on the P1 pass, per
# product.  The sweep's subprocess timeout is 1800s and the resident admits
# one writer at a time, so every second here is a second the production
# cycle cannot start.  Measured 2026-09-09: the P1 pass reached its
# 1800s timeout inside NBM and was killed -- the third consecutive day
# without a completed run (last clock 2026-09-06T08:12Z) -- and the
# production cycle went 35 to 53 minutes between rounds against a 300s
# target.  240s a product leaves the P0 pass, which is bounded by attempts
# rather than by productive runs, the rest of the timeout.
P1_BUDGET_SEC_PER_PRODUCT = 240.0


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


def _plan(
    conn: sqlite3.Connection, now: datetime
) -> tuple[list[dict[str, Any]], int]:
    """Every run worth capturing this pass, before any of them is fetched.

    Planned first and interleaved by product second, because a pass cap
    applied to a product-major walk starves the later product outright:
    the first pass spent all six of its runs on HRRR and the NBM lane sat
    at zero rows with nothing wrong with it.  A cap has to be shared by
    rotation, not by sort order -- the same lesson the resident's single
    writer learned.  Costs only indexed reads and arithmetic.
    """

    by_product: dict[str, list[dict[str, Any]]] = {}
    skipped = 0
    for product in ("HRRR", "NBM"):
        items: list[dict[str, Any]] = []
        system_id = SYSTEM_IDS[product]
        for station, latitude, longitude, zone_name in STATIONS[product]:
            zone = ZoneInfo(zone_name)
            for offset in range(-BACKFILL_DAYS, LOOKAHEAD_DAYS + 1):
                target = now.astimezone(zone).date() + timedelta(days=offset)
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
                        # S3 publication lags the run by about two hours;
                        # the f48 tail arrives later still.
                        if run > now - timedelta(hours=4):
                            continue
                        if not run_covers_peak(
                            product=product, run_init=run,
                            event_date=target,
                            utc_offset_hours=offset_hours,
                        ):
                            continue
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
                        # Two runs of the same day can land in one bucket;
                        # the table holds one row per bucket, so the
                        # second would fetch 30 MB to die on the UNIQUE.
                        held.add(bucket)
                        items.append({
                            "product": product, "system_id": system_id,
                            "station": station, "latitude": latitude,
                            "longitude": longitude, "target": target,
                            "offset_hours": offset_hours, "run": run,
                            "cycle": f"{run:%H}Z",
                            "init": run.isoformat().replace("+00:00", "Z"),
                            "bucket": bucket,
                        })
        # Newest event day first, across all stations, before any station's
        # backfill.  Station-major order starves the far end of the roster
        # of exactly the runs that matter: a station whose newest run is
        # never reached contributes nothing to the live race no matter how
        # complete its history gets.  Sorting by day rotates the stations
        # for free, since each contributes about two items per day.
        items.sort(key=lambda i: (
            -i["target"].toordinal(), i["station"], i["run"].isoformat()
        ))
        by_product[product] = items
    # Round-robin: one run from each product in turn, so a cap reached
    # mid-list leaves both lanes equally far along.
    plan: list[dict[str, Any]] = []
    for index in range(max((len(v) for v in by_product.values()), default=0)):
        for product in ("HRRR", "NBM"):
            if index < len(by_product[product]):
                plan.append(by_product[product][index])
    return plan, skipped


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(DB), timeout=120.0)
    # Charter 95: the registry row the compiler resolves this system from.
    from bayes_basket_target.native_ensemble_training_facts import (  # noqa: PLC0415
        register_regional_point_systems,
    )
    register_regional_point_systems(conn)
    conn.commit()
    # Charter 95 (re-ruled 2026-09-06): the P1 lane -- every HRRR and NBM
    # cycle, run-scoped, into the lane's own table.  Runs before the P0
    # pass so a slow archive cannot starve it.
    from bayes_basket_target.regional_point_lane import (  # noqa: PLC0415
        capture_p1, ensure_schema,
    )
    ensure_schema(conn)
    for _product in ("HRRR", "NBM"):
        # Each phase arms its own clock, the same seconds its own budget
        # already names, so the requests inside a run stop where the run
        # loop would have: the budget above is checked between runs, and one
        # run is ninety-odd requests.  See ``pass_deadline``.
        set_pass_deadline(P1_BUDGET_SEC_PER_PRODUCT)
        _receipt = capture_p1(
            conn, product=_product, now=now,
            stations=STATIONS[_product], max_runs=P1_MAX_RUNS_PER_PASS,
            budget_sec=P1_BUDGET_SEC_PER_PRODUCT,
            dry_run=args.dry_run,
        )
        print(
            f"P1 {_product}: 计划 {_receipt['runs_planned']} run，"
            f"取 {_receipt['runs_attempted']} run，写入 {_receipt['written']}，"
            f"已有 {_receipt['existed']}，空 {_receipt['empty']}，失败 {len(_receipt['failed'])}"
            + ("，预算用尽" if _receipt["budget_exhausted"] else "")
        )
        for _line in _receipt["failed"][:4]:
            print(f"  {_line}")
    conn.execute("PRAGMA busy_timeout=60000")

    written = 0
    attempted = 0
    failed: list[str] = []
    blocked = {
        product for product in ("HRRR", "NBM")
        if not _transport_is_writable(conn, TRANSPORTS[product])[0]
    }
    if blocked and not args.dry_run:
        for product in sorted(blocked):
            reason = _transport_is_writable(conn, TRANSPORTS[product])[1]
            print(f"  {product} 采集已就绪但无法落库：{reason}")

    try:
        plan, skipped = _plan(conn, now)
        deadline = time.monotonic() + BUDGET_SEC_PER_PASS
        set_pass_deadline(BUDGET_SEC_PER_PASS)
        budget_exhausted = False
        for item in plan:
            product = item["product"]
            if product in blocked and not args.dry_run:
                continue
            if attempted >= MAX_RUNS_PER_PASS:
                break
            if time.monotonic() >= deadline:
                budget_exhausted = True
                break
            attempted += 1
            station, target = item["station"], item["target"]
            cycle, bucket = item["cycle"], item["bucket"]
            try:
                captured = fetch_day_maximum(
                    product=product, run_init=item["run"],
                    event_date=target,
                    utc_offset_hours=item["offset_hours"],
                    latitude=item["latitude"], longitude=item["longitude"],
                )
            except NoaaConusPassDeadlineExceeded:
                # The clock ran out inside the fetch: the bound doing its
                # job, not a fault, and the same stop the check above makes.
                budget_exhausted = True
                break
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    f"{product} {station} {target} {cycle}"
                    f" {type(exc).__name__}: {str(exc)[:60]}"
                )
                continue
            if not captured["complete"]:
                failed.append(
                    f"{product} {station} {target} {cycle} 时次不全"
                    f" {len(captured['hours_covered'])}"
                    f"/{len(captured['hours_wanted'])}"
                )
                continue
            identity = {
                "ensemble_system_id": item["system_id"],
                "station_id": station,
                "target_local_date": target.isoformat(),
                "run_cycle": cycle,
                "native_run_init_utc": item["init"],
                "member_id": MEMBER_IDS[product],
                "raw_tmax_c": round(captured["forecast_tmax_c"], 6),
                "extraction_version": EXTRACTION_VERSIONS[product],
            }
            digest = content_sha256(identity)
            print(
                f"  {product} {station} {target} {cycle} {bucket}"
                f" {captured['forecast_tmax_c']:.2f}°C"
                f" ({captured['bytes_downloaded']/1048576:.1f} MB)"
                + ("  [dry-run]" if args.dry_run else "")
            )
            if args.dry_run:
                continue
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
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{product.lower()}-{digest[:32]}",
                    item["system_id"], station,
                    target.isoformat(), bucket, cycle,
                    MEMBER_IDS[product], MEMBER_ROLE,
                    float(captured["forecast_tmax_c"]),
                    SETTLEMENT_DEFINITION,
                    EXTRACTION_VERSIONS[product],
                    PROVIDER_IDS[product],
                    TRANSPORTS[product], item["init"],
                    1,
                    now.isoformat().replace("+00:00", "Z"),
                    run_directory_url(product, item["run"]),
                    captured["grid_geometry"], digest,
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
    elif budget_exhausted:
        print(
            f"  本轮 {BUDGET_SEC_PER_PASS:.0f}s 时间预算用尽，剩余的下一轮继续"
            "（正常返回，不占用托管写域直到被杀）"
        )
    for line in failed[:8]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
