#!/usr/bin/env python3
"""The market's modal bucket and normalised vector, hour by hour, per station-day.

For every station-day in [FIRST, LAST] and every UTC hour from the first
committed WS snapshot to the event's local day end: the newest COMMITTED
snapshot observed at or before the top of that hour, read by production's
``market_reference_v1`` and ``market_mode_bucket``.  An incomplete book is an
hour with no mode (never stepped back past).  Hot SSOT rows and the cold tier
(charter 238) together; the hot copy wins a duplicate.  Read-only.

    python3 build_market_modes.py --stations KLAX,EGLC
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import time
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.proper_bet_lane import market_mode_bucket  # noqa: E402
from ssot_cold_offload import cold_path, cold_tier_root  # noqa: E402

FIRST, LAST = "2026-07-28", "2026-10-07"
SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
OUT = HERE / "market_modes"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations", required=True)
    args = parser.parse_args()
    stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()]
    OUT.mkdir(exist_ok=True)
    todo = [s for s in stations if not (OUT / f"{s}.json.gz").exists()]
    if not todo:
        return 0
    hot = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
    zones = dict(hot.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall())
    cold = sqlite3.connect(f"file:{cold_path(cold_tier_root(), 'basket_official_ws_snapshot')}?mode=ro", uri=True)
    t0 = time.time()
    # (station, day) -> {observed_at: ("hot"|"cold", rowid)}; hot inserted last so it wins
    stamps: dict[tuple[str, str], dict[str, tuple[str, int]]] = defaultdict(dict)
    wanted = set(todo)
    for rowid, station, day, observed in cold.execute(
        "SELECT rowid, station_id, event_date, observed_at FROM basket_official_ws_snapshot"
        " WHERE event_date BETWEEN ? AND ?", (FIRST, LAST)):
        if station in wanted:
            stamps[(station, day)][str(observed)] = ("cold", int(rowid))
    print(f"cold index read {time.time() - t0:.0f}s", flush=True)
    for station in todo:
        for rowid, day, observed in hot.execute(
            "SELECT rowid, event_date, observed_at FROM basket_official_ws_snapshot"
            " WHERE station_id=? AND event_date BETWEEN ? AND ? AND state='COMMITTED'", (station, FIRST, LAST)):
            stamps[(station, day)][str(observed)] = ("hot", int(rowid))
    print(f"hot index read {time.time() - t0:.0f}s", flush=True)

    def fetch(where: tuple[str, int]):
        db = hot if where[0] == "hot" else cold
        row = db.execute("SELECT buckets_json, state FROM basket_official_ws_snapshot WHERE rowid=?",
                         (where[1],)).fetchone()
        return (row[0], str(row[1])) if row else (None, None)

    for station in todo:
        zone = ZoneInfo(zones[station])
        out: dict[str, list] = {}
        fetched = 0
        for (st, day), by_time in sorted(stamps.items()):
            if st != station:
                continue
            times = sorted(by_time, key=_utc)
            moments = [_utc(t) for t in times]
            day_end = datetime.fromisoformat(day).replace(tzinfo=zone) + timedelta(days=1)
            hour = moments[0].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            series = []
            memo: dict[str, tuple] = {}
            while hour <= day_end:
                index = bisect_right(moments, hour) - 1
                if index >= 0:
                    observed = times[index]
                    if observed not in memo:
                        # a non-committed cold row: step back to the newest committed one
                        state, raw, k = None, None, index
                        while k >= 0:
                            raw, state = fetch(by_time[times[k]])
                            fetched += 1
                            if state == "COMMITTED":
                                break
                            k -= 1
                        entry = (None, None, None)
                        if state == "COMMITTED" and raw:
                            buckets = json.loads(raw)
                            market = market_reference_v1(buckets)
                            if market.get("state") == "complete":
                                ordinal = {str(b.get("bucket")): int(b.get("ordinal", 0)) for b in buckets}
                                q = {str(k2): float(v) for k2, v in market["vector"].items()}
                                order = sorted(q, key=lambda b: ordinal.get(b, 0))
                                entry = (market_mode_bucket(q, order), {b: round(q[b], 6) for b in order},
                                         times[k])
                            else:
                                entry = (None, market.get("reason"), times[k])
                        memo[observed] = entry
                    mode, q, seen = memo[observed]
                    series.append([_iso(hour), mode, q, seen])
                hour += timedelta(hours=1)
            out[day] = series
        path = OUT / f"{station}.json.gz"
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as handle:
            json.dump(out, handle)
        tmp.rename(path)
        complete = sum(1 for s in out.values() for e in s if e[1])
        total = sum(len(s) for s in out.values())
        print(f"{station}: {len(out)} days, {total} hours, {complete} with a mode, {fetched} fetches,"
              f" {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
