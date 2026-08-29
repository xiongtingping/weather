"""Does the band factor's basis split by bucket position?

Reuses the store's own serving-lane collector (first decision per
station-day, pre-band claims reconstructed from receipts) -- no rule is
reimplemented.  Within each band cell, buckets are split by signed
distance from the snapshot's argmax bucket; realized/claimed per group is
the factor that group alone would want.  A by-day bootstrap on the ratio
difference says whether the split is noise.
"""
import json
import random
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.band_reliability_store import (
    BAND_TABLE_CELLS,
    _truth,
    serving_lane_by_station_day,
)

DB = "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"
conn = sqlite3.connect(DB, uri=True, timeout=60)
truth = _truth(conn)

random.seed(20260828)


def cell_of(p):
    for lo, hi in BAND_TABLE_CELLS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def dist_group(d):
    a = abs(d)
    if a == 0:
        return "centre"
    if a == 1:
        return "+-1"
    if a == 2:
        return "+-2"
    return ">=3"


for stage in ("P0", "P1"):
    lane = serving_lane_by_station_day(conn, truth, stage=stage, pre_band=True)
    # rows: (day, cell, group, side, claim, hit)
    rows = []
    n_snaps = 0
    for (station, event), snaps in lane.items():
        for entries in snaps:
            n_snaps += 1
            argmax_i = max(range(len(entries)), key=lambda i: entries[i][0])
            for i, (claim, hit, _lower) in enumerate(entries):
                cell = cell_of(claim)
                if cell is None:
                    continue
                d = i - argmax_i
                side = "cool" if d < 0 else ("warm" if d > 0 else "ctr")
                rows.append((event, cell, dist_group(d), side, claim, hit))
    print(f"\n===== stage {stage}: {len(lane)} station-days, "
          f"{n_snaps} snapshots, {len(rows)} bucket-claims =====")

    def agg(keyfn):
        out = defaultdict(lambda: [0.0, 0, 0, set()])
        for event, cell, grp, side, claim, hit in rows:
            k = keyfn(cell, grp, side)
            if k is None:
                continue
            acc = out[k]
            acc[0] += claim
            acc[1] += hit
            acc[2] += 1
            acc[3].add(event)
        return out

    print("--- band cell x |distance| (ratio = realized/claimed; "
          "band factor basis) ---")
    by_cd = agg(lambda cell, grp, side: (cell, grp))
    for (cell, grp) in sorted(by_cd, key=lambda k: (k[0][0], k[1])):
        claimed, hits, n, days = by_cd[(cell, grp)]
        if claimed <= 0:
            continue
        print(f"  [{cell[0]:.2f},{cell[1]:.2f}) {grp:>6}: n={n:5d} "
              f"days={len(days):3d} claimed={claimed:8.2f} hit={hits:4d} "
              f"ratio={hits / claimed:5.2f}")

    print("--- band cell x side ---")
    by_cs = agg(
        lambda cell, grp, side: (cell, side) if side != "ctr" else None
    )
    for (cell, side) in sorted(by_cs, key=lambda k: (k[0][0], k[1])):
        claimed, hits, n, days = by_cs[(cell, side)]
        if claimed <= 0:
            continue
        print(f"  [{cell[0]:.2f},{cell[1]:.2f}) {side:>4}: n={n:5d} "
              f"days={len(days):3d} claimed={claimed:8.2f} hit={hits:4d} "
              f"ratio={hits / claimed:5.2f}")

    # By-day bootstrap on near (|d|<=1) vs far (|d|>=2) ratio difference
    # inside each cell with enough sample on both arms.
    print("--- near(|d|<=1) vs far(|d|>=2) split, by-day bootstrap ---")
    by_day = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    for event, cell, grp, side, claim, hit in rows:
        arm = "near" if grp in ("centre", "+-1") else "far"
        acc = by_day[event][(cell, arm)]
        acc[0] += claim
        acc[1] += hit
    days = sorted(by_day)
    for cell in sorted({c for c, _ in BAND_TABLE_CELLS and []} | {
        c for e in by_day for (c, a) in by_day[e]
    }, key=lambda c: c[0]):
        def ratio_diff(sample_days):
            tot = {"near": [0.0, 0], "far": [0.0, 0]}
            for d in sample_days:
                for arm in ("near", "far"):
                    c, h = by_day[d].get((cell, arm), (0.0, 0))
                    tot[arm][0] += c
                    tot[arm][1] += h
            if tot["near"][0] <= 0 or tot["far"][0] <= 0:
                return None
            return (
                tot["near"][1] / tot["near"][0],
                tot["far"][1] / tot["far"][0],
            )
        point = ratio_diff(days)
        if point is None:
            continue
        diffs = []
        for _ in range(600):
            sample = [random.choice(days) for _ in days]
            r = ratio_diff(sample)
            if r is not None:
                diffs.append(r[0] - r[1])
        if len(diffs) < 300:
            continue
        diffs.sort()
        lo = diffs[int(0.025 * len(diffs))]
        hi = diffs[int(0.975 * len(diffs))]
        sig = "SPLIT" if (lo > 0 or hi < 0) else "no"
        print(f"  [{cell[0]:.2f},{cell[1]:.2f}) near={point[0]:5.2f} "
              f"far={point[1]:5.2f} diff_CI=[{lo:+.2f},{hi:+.2f}] -> {sig}")
conn.close()
