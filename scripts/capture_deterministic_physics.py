#!/usr/bin/env python3
"""Capture the F systems' own physics at every roster station (charter 267 补记八).

Forward (default): every run of IFS HRES, AIFS Single, GFS, AIGFS, GDPS and
ICON global whose 2 m temperature rows the deterministic lane already holds
for today-1 .. today+3 and that has no physics row yet, newest run first.
GDPS and ICON keep only their newest runs online, so a run this lane misses
is gone; the other four are filled in by ``--backfill``.

``--backfill``: the publishers that keep their history (IFS HRES, AIFS, GFS,
AIGFS), between ``--since`` and ``--until``.  With ``--listing-only`` (the
default for a backfill) only the run each station-day's F read at its listing
instant (D-2 05:00Z, ``listing_forecast_f.system_maxima``) -- the run the
research asks about -- not every run the lane holds.

Same shape as every capture lane: plans with reads only, fetches one run,
writes it, commits, and only then fetches the next; a lock it cannot take
stops the pass.  ``--max-seconds`` bounds the pass between runs.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import deterministic_point_physics as P  # noqa: E402
from bayes_basket_target import sqlite_busy  # noqa: E402
from bayes_basket_target.pass_deadline import announce_write_blocked  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
FORWARD_DAYS = (-1, 3)
FORWARD_MAX_SECONDS = 3000.0
# Runs a forward pass takes per product: the newest, and one it missed.
FORWARD_MAX_RUNS = 2
BACKFILL_WAIT_SECONDS = 120.0


def listing_keys(conn: sqlite3.Connection, products, since: date, until: date, roster) -> set:
    """(system, station, date, run) of the run F read at each listing instant."""

    from bayes_basket_target import listing_forecast_f as L  # noqa: PLC0415

    systems = [P.SYSTEM_BY_PRODUCT[p] for p in products]
    keys = set()
    day = since
    while day <= until:
        for station, _lat, _lon, zone in roster:
            maxima = L.system_maxima(
                conn, station_id=station, event_date=day, as_of_utc=L.listing_instant(day),
                settlement_timezone=zone, systems=systems,
            )
            for system, entry in maxima.items():
                keys.add((system, station, day.isoformat(), entry["run_init_utc"]))
        day += timedelta(days=1)
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--since", type=date.fromisoformat)
    parser.add_argument("--until", type=date.fromisoformat)
    parser.add_argument("--products")
    parser.add_argument("--all-runs", action="store_true", help="backfill every held run, not only the listing run")
    parser.add_argument("--max-seconds", type=float, default=FORWARD_MAX_SECONDS)
    parser.add_argument("--max-runs-per-product", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: PLC0415
    from bayes_basket_target.native_deterministic_point_archive import (  # noqa: PLC0415
        HEAVY_PRODUCTS, production_network_strained,
    )

    now = datetime.now(timezone.utc)
    today = now.date()
    if args.backfill:
        products = [p for p in (args.products or ",".join(P.BACKFILLABLE)).split(",") if p]
        refused = [p for p in products if p not in P.BACKFILLABLE]
        if refused:
            print(f"无历史可回补：{','.join(refused)}")
            return 2
        since = args.since or today - timedelta(days=90)
        until = args.until or today - timedelta(days=1)
    else:
        products = [p for p in (args.products or ",".join(P.PRODUCTS)).split(",") if p]
        since, until = today + timedelta(days=FORWARD_DAYS[0]), today + timedelta(days=FORWARD_DAYS[1])
    conn = sqlite3.connect(str(args.db), timeout=120.0)
    try:
        roster = global_roster(conn)
        stations = {station: (lat, lon) for station, lat, lon, _zone in roster}
        try:
            with sqlite_busy.blocked_when_busy():
                P.ensure_schema(conn)
                conn.commit()
        except sqlite_busy.WriteBlocked as exc:
            announce_write_blocked(f"建表 {exc}")
            return 0
        only = None
        if args.backfill and not args.all_runs:
            only = listing_keys(conn, products, since, until, roster)
            print(f"挂牌时刻读的轮次：{len(only)} 个站日×系统")
        items = P.plan(conn, products=products, since=since, until=until, stations=stations, only=only)
        cap = args.max_runs_per_product or (None if args.backfill else FORWARD_MAX_RUNS)
        if cap:
            taken: dict[str, int] = {}
            kept = []
            for item in items:
                if taken.get(item["product"], 0) < cap:
                    taken[item["product"]] = taken.get(item["product"], 0) + 1
                    kept.append(item)
            items = kept
        print(f"{'回补' if args.backfill else '前向'} {since}..{until}：待取 {len(items)} 轮，"
              f"{sum(len(i['targets']) for i in items)} 站日")
        if args.dry_run or not items:
            for item in items[:20]:
                print(f"  {item['product']} {item['run_init']:%Y-%m-%dT%HZ} 站日 {len(item['targets'])}")
            return 0
        conn.execute("PRAGMA busy_timeout=60000")

        deadline = time.monotonic() + float(args.max_seconds)

        def strained(product: str) -> str:
            if not args.backfill:
                return production_network_strained() if product in HEAVY_PRODUCTS else ""
            # A backfill is never urgent: it waits out production's strained
            # egress (every product of it), and resumes where it stopped --
            # skipping would end the pass with the history unread.
            while (reason := production_network_strained()) and time.monotonic() < deadline:
                print(f"生产出口 {reason}，回补等待 {BACKFILL_WAIT_SECONDS:.0f}s")
                time.sleep(BACKFILL_WAIT_SECONDS)
            return reason

        sys.stdout.reconfigure(line_buffering=True)
        receipt = P.capture(
            conn, items, now=now, capture_mode="backfill" if args.backfill else "forward",
            deadline=deadline, should_yield=strained,
        )
        print(f"完成 {receipt['runs']} 轮，写入 {receipt['written']} 行，失败 {len(receipt['failed'])}"
              + (f"，停在 {receipt['stopped']}" if receipt["stopped"] else ""))
        for line in receipt["failed"][:10]:
            print(f"  {line}")
        if receipt["stopped"].startswith("write_blocked"):
            announce_write_blocked(receipt["stopped"])
        return 0
    finally:
        from bayes_basket_target.aifs_single_point_archive import close_client as close_aifs  # noqa: PLC0415
        from bayes_basket_target.native_deterministic_point_archive import close_client as close_native  # noqa: PLC0415

        close_aifs()
        close_native()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
