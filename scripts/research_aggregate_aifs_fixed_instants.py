#!/usr/bin/env python3
"""Research only: what each replay arm was serving, compared at fixed instants.

``research_aggregate_aifs_replay.py`` pairs the arms update for update, which
needs AIFS rows stamped with PF50's own arrival.  Under the user's ruling
(2026-09-22, every run, each at its own publication, newest used) AIFS
arrives on its own clock, so the arms update at different moments and there
is nothing to pair record for record.  Instead: on every whole UTC hour of a
station-day, take the record each arm had most recently served (latest
``at`` <= the hour) and score the two against the same settled truth.  An
hour where AIFS arrived first and the baseline was still serving an older
packet is exactly where earlier arrival is worth something, and it is counted
for as long as it lasted.

Hours run from the first hour both arms serve to the end of the local event
day.  Hours where only the AIFS arm serves (AIFS made the pool two families
before the baseline had them) are counted, not scored.  Hours where the arms'
gate tables differ are left out, as before: the replay memoises tables per
hour on the first update into it, so differing tables say when the arm
updated, not what AIFS did.

Weighting: each station-day's hours averaged, then station-days equally;
intervals resample whole event dates.  ``--first-event-date`` should sit
after the baseline members' stamps became real-time captures (runs from
about 2026-08-14; before that they are backfill times days after
publication, and AIFS at its publication time would look days early).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import research_aggregate_aifs_replay as paired  # noqa: E402
from bayes_basket_target.stable_pool import STABLE_POOL_KAPPA  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
AIFS = paired.AIFS
PF50 = "ECMWF_IFS_ENS_OPEN_PF50"
VIEWS = ("served", "noband", "raw")
HORIZONS = ((48.0, "h48_plus"), (24.0, "h24_48"), (0.0, "h00_24"), (float("-inf"), "in_day"))


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def horizon(hours_to_start: float) -> str:
    return next(name for floor, name in HORIZONS if hours_to_start >= floor)


def by_station_day(records: dict) -> dict[tuple[str, str], tuple[list[datetime], list[dict]]]:
    grouped = defaultdict(list)
    for (station, day, at), record in records.items():
        grouped[(station, day)].append((_utc(at), record))
    out = {}
    for key, rows in grouped.items():
        rows.sort(key=lambda r: r[0])
        out[key] = ([r[0] for r in rows], [r[1] for r in rows])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kappa", type=float, default=STABLE_POOL_KAPPA)
    parser.add_argument("--baseline-arm", default="baseline")
    parser.add_argument("--with-arm", default="with_aifs_allruns")
    parser.add_argument("--first-event-date", default="2026-08-16")
    parser.add_argument("--last-event-date", default="9999-12-31")
    args = parser.parse_args()
    scheme = f"C{args.kappa:g}"
    ssot = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    zones = dict(ssot.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ))
    base = by_station_day(paired.load(args.baseline_arm))
    with_ = by_station_day(paired.load(args.with_arm))
    keys = sorted(
        k for k in set(base) & set(with_)
        if args.first_event_date <= k[1] <= args.last_event_date and k[0] in zones
    )
    counts = Counter()
    unseated_drift = 0.0
    pairs = []
    for station, day in keys:
        b_at, b_rec = base[(station, day)]
        w_at, w_rec = with_[(station, day)]
        start = datetime.combine(date.fromisoformat(day), datetime.min.time(), ZoneInfo(zones[station]))
        start = start.astimezone(timezone.utc)
        end = start + timedelta(days=1)
        hour = max(b_at[0], w_at[0]).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        first_with = w_at[0].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        counts["hours_only_aifs_arm_serving"] += max(0, int((min(hour, end) - first_with).total_seconds() // 3600))
        while hour < end:
            b = b_rec[bisect_right(b_at, hour) - 1]
            w = w_rec[bisect_right(w_at, hour) - 1]
            hour_next = hour + timedelta(hours=1)
            bs, ws = b["schemes"].get(scheme), w["schemes"].get(scheme)
            if not bs or not ws:
                counts["hours_scheme_missing"] += 1
                hour = hour_next
                continue
            if (b.get("band_sha"), b.get("cw_sha")) != (w.get("band_sha"), w.get("cw_sha")):
                counts["hours_gate_table_mismatch"] += 1
                hour = hour_next
                continue
            seated = AIFS in w.get("pool", ())
            runs = w.get("runs", {})
            newer = seated and str(runs.get(AIFS, "")) > str(runs.get(PF50, ""))
            pair = {
                "station": station, "event_date": day,
                "lead": horizon((start - hour).total_seconds() / 3600.0),
                "seated": seated, "aifs_newer_than_pf50": newer,
            }
            for view in VIEWS:
                for i, metric in enumerate(paired.METRICS):
                    pair[f"base_{view}_{metric}"] = float(bs[view][i])
                    pair[f"with_{view}_{metric}"] = float(ws[view][i])
            if not seated:
                unseated_drift = max(unseated_drift, max(
                    abs(pair[f"with_{v}_{m}"] - pair[f"base_{v}_{m}"]) for v in VIEWS for m in paired.METRICS
                ))
            counts["hours_scored"] += 1
            counts["hours_seated"] += seated
            counts["hours_aifs_newer_than_pf50"] += newer
            pairs.append(pair)
            hour = hour_next

    report = {
        "scheme": scheme, "arms": [args.baseline_arm, args.with_arm],
        "window": [args.first_event_date, args.last_event_date],
        "station_days": len({(p["station"], p["event_date"]) for p in pairs}),
        "event_dates": len({p["event_date"] for p in pairs}),
        "stations": len({p["station"] for p in pairs}),
        "counts": dict(counts),
        "max_abs_delta_where_aifs_not_seated": round(unseated_drift, 6),
        "results": {},
    }
    subsets = (
        ("all_hours", pairs),
        ("hours_aifs_seated", [p for p in pairs if p["seated"]]),
        ("hours_aifs_newer_than_pf50", [p for p in pairs if p["aifs_newer_than_pf50"]]),
    )
    for name, rows in subsets:
        if not rows:
            continue
        # Station-day means within each horizon, so a horizon's rows are
        # still one per station-day and the headline is not dominated by
        # whichever horizon has the most hours.
        day_rows = paired.station_day_means(rows)
        block = {"hours": len(rows), "station_days": len(day_rows)}
        for view in VIEWS:
            for metric in paired.METRICS:
                base_mean = statistics.fmean(r[f"base_{view}_{metric}"] for r in day_rows)
                point, low, high = paired.bootstrap(
                    day_rows, lambda r, v=view, m=metric: r[f"with_{v}_{m}"] - r[f"base_{v}_{m}"]
                )
                block[f"{view}_{metric}"] = {
                    "baseline": round(base_mean, 4), "delta": round(point, 4),
                    "ci95": [round(low, 4), round(high, 4)],
                    "relative": round(point / base_mean, 4) if base_mean else None,
                }
        by_horizon = {}
        for _, name_h in HORIZONS:
            sub = paired.station_day_means([r for r in rows if r["lead"] == name_h])
            if not sub:
                continue
            point, low, high = paired.bootstrap(sub, lambda r: r["with_served_crps"] - r["base_served_crps"])
            base_mean = statistics.fmean(r["base_served_crps"] for r in sub)
            by_horizon[name_h] = {
                "station_days": len(sub), "baseline": round(base_mean, 4), "delta": round(point, 4),
                "ci95": [round(low, 4), round(high, 4)],
                "relative": round(point / base_mean, 4) if base_mean else None,
            }
        block["served_crps_by_hours_to_local_day_start"] = by_horizon
        per_station = defaultdict(list)
        for r in day_rows:
            per_station[r["station"]].append(r["with_served_crps"] - r["base_served_crps"])
        block["stations_better_with_aifs"] = sum(1 for v in per_station.values() if statistics.fmean(v) < 0)
        block["stations"] = len(per_station)
        report["results"][name] = block
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
