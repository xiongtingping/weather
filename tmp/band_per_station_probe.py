"""Would per-station band tables have evidence to stand on?

Reuses the store's serving-lane collector.  Per station: sample size,
realized/claimed ratio in three coarse claim groups, and a joint by-day
bootstrap of (station ratio - fleet-without-station ratio) so shared
weather days don't fake independence.
"""
import random
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.band_reliability_store import (
    _truth,
    serving_lane_by_station_day,
)

DB = "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"
conn = sqlite3.connect(DB, uri=True, timeout=60)
truth = _truth(conn)
random.seed(20260828)

GROUPS = (
    ("tail<0.05", 0.00, 0.05),
    ("shldr0.05-0.20", 0.05, 0.20),
    ("core>=0.20", 0.20, 1.01),
)


def group_of(p):
    for name, lo, hi in GROUPS:
        if lo <= p < hi:
            return name
    return None


for stage in ("P0", "P1"):
    lane = serving_lane_by_station_day(conn, truth, stage=stage, pre_band=True)
    # per (station, day, group): [claimed, hits]
    cell = defaultdict(lambda: [0.0, 0])
    days_by_station = defaultdict(set)
    for (station, event), snaps in lane.items():
        for entries in snaps:
            days_by_station[station].add(event)
            for claim, hit, _lower in entries:
                g = group_of(claim)
                if g is None:
                    continue
                acc = cell[(station, event, g)]
                acc[0] += claim
                acc[1] += hit
    all_days = sorted({e for (_s, e, _g) in cell})
    stations = sorted(days_by_station)
    print(f"\n===== stage {stage}: {len(stations)} stations, "
          f"{sum(len(v) for v in days_by_station.values())} station-days, "
          f"{len(all_days)} distinct event days =====")
    print("station  days | " + " | ".join(
        f"{name}: ratio (diff vs fleet CI)" for name, _l, _h in GROUPS))

    def ratios(day_sample, station):
        mine = {g: [0.0, 0] for g, _l, _h in GROUPS}
        rest = {g: [0.0, 0] for g, _l, _h in GROUPS}
        for day in day_sample:
            for st in stations:
                for g, _l, _h in GROUPS:
                    c, h = cell.get((st, day, g), (0.0, 0))
                    tgt = mine if st == station else rest
                    tgt[g][0] += c
                    tgt[g][1] += h
        out = {}
        for g, _l, _h in GROUPS:
            m = mine[g][1] / mine[g][0] if mine[g][0] > 0 else None
            r = rest[g][1] / rest[g][0] if rest[g][0] > 0 else None
            out[g] = (m, r)
        return out

    flagged = []
    for station in stations:
        n_days = len(days_by_station[station])
        point = ratios(all_days, station)
        cols = []
        for g, _l, _h in GROUPS:
            m, r = point[g]
            if m is None or r is None:
                cols.append(f"{g}: --")
                continue
            diffs = []
            for _ in range(400):
                sample = [random.choice(all_days) for _ in all_days]
                bm, br = ratios(sample, station)[g]
                if bm is not None and br is not None:
                    diffs.append(bm - br)
            if len(diffs) < 200:
                cols.append(f"{g}: {m:4.2f} (thin)")
                continue
            diffs.sort()
            lo = diffs[int(0.025 * len(diffs))]
            hi = diffs[int(0.975 * len(diffs))]
            mark = ""
            if lo > 0 or hi < 0:
                mark = " *SPLIT*"
                flagged.append((station, g, m, r, lo, hi))
            cols.append(f"{g}: {m:4.2f} [{lo:+.2f},{hi:+.2f}]{mark}")
        print(f"{station:7s} {n_days:4d} | " + " | ".join(cols))
    print(f"-- stations significantly off fleet: {len(flagged)}")
    for station, g, m, r, lo, hi in flagged:
        print(f"   {station} {g}: station={m:.2f} fleet={r:.2f} "
              f"CI=[{lo:+.2f},{hi:+.2f}]")
conn.close()
