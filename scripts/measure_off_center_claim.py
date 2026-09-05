"""Re-measure the off-centre over-claim on today's served (post-DB-band) claims.

Read-only.  Run from the repo root: ``.venv/bin/python scripts/measure_off_center_claim.py``.
First run 2026-09-05 (user asked for the 08-14 premise to be re-checked):
centre-holding windows realise 1.10-1.30 of their claim, not 1.01.

Same statistic as commit c92a2b8d (2026-08-14): realised containment over
claimed mass, contiguous windows, split by whether the window holds the
model's centre, by claimed-mass band.  Sample and truth come from the band
store's own helpers (first decision per station-day, official Tmax truth);
nothing here re-implements a production rule except the window enumeration.
"""
import random, sqlite3, sys
from collections import defaultdict
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bayes_basket_target.band_reliability_store import _truth, serving_lane_by_station_day
from bayes_basket_target.basket_kernel.delta_v2 import (
    OFF_CENTER_CLAIM_BANDS, off_center_claim_factor,
)
from bayes_basket_target.basket_kernel.support import mean_center_bucket_v1

BANDS = ((0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.55), (0.55, 1.01))
conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA busy_timeout=60000")
truth = _truth(conn)

def band_of(claim):
    for lo, hi in BANDS:
        if lo <= claim < hi:
            return (lo, hi)
    return None

def ratio(rows):
    c = sum(r[0] for r in rows); h = sum(r[1] for r in rows)
    return (h / c if c > 0 else float("nan")), len(rows)

def bootstrap(rows_by_day, draws=1000, seed=20260905):
    rng = random.Random(seed); days = list(rows_by_day)
    if not days: return (float("nan"), float("nan"))
    out = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        c = sum(r[0] for d in pick for r in rows_by_day[d]); h = sum(r[1] for d in pick for r in rows_by_day[d])
        out.append(h / c if c > 0 else float("nan"))
    out.sort(); return out[int(0.025 * draws)], out[int(0.975 * draws)]

for stage in ("P0", "P0-M", "P1"):
    sample = serving_lane_by_station_day(conn, truth, stage=stage, pre_band=False)
    days = sorted({e for (_s, e) in sample})
    print(f"\n===== {stage}: {len(sample)} station-days, {len(days)} days, {days[0] if days else '-'}..{days[-1] if days else '-'}")
    for width in (4, 3):
        cells = defaultdict(lambda: defaultdict(list))  # (centre_def, holds, band) -> day -> rows
        for (station, event), snaps in sample.items():
            entries = snaps[0]
            claims = [c for c, _h, _l in entries]; hits = [h for _c, h, _l in entries]
            n = len(claims)
            if n < width or sum(claims) <= 0: continue
            keys = [str(i) for i in range(n)]
            probs = {k: claims[i] for i, k in enumerate(keys)}
            ords = {k: i for i, k in enumerate(keys)}
            argmax = max(range(n), key=lambda i: (claims[i], -i))
            mean_key = mean_center_bucket_v1(probs, ords, admitted=[k for k in keys if probs[k] > 0])
            mean_c = ords[mean_key] if mean_key is not None else argmax
            for start in range(0, n - width + 1):
                w = range(start, start + width)
                claim = sum(claims[i] for i in w); hit = 1 if any(hits[i] for i in w) else 0
                b = band_of(claim)
                if b is None: continue
                for cdef, centre in (("argmax", argmax), ("mean", mean_c)):
                    holds = centre in w
                    cells[(cdef, holds, b)][event].append((claim, hit))
                    if not holds:
                        f = off_center_claim_factor(claim, gap_buckets=min(abs(i - centre) for i in w))
                        cells[(cdef, "corrected", b)][event].append((claim * f, hit))
        print(f"\n--- {width}-bucket windows ---")
        print(f"{'centre':7} {'band':11} | {'holds centre':>26} | {'excludes centre':>26} | {'excl. after table':>18}")
        for cdef in ("argmax", "mean"):
            for b in BANDS:
                def fmt(key, ci=True):
                    rows_by_day = cells.get(key, {})
                    rows = [r for d in rows_by_day for r in rows_by_day[d]]
                    if not rows: return f"{'--':>26}" if ci else f"{'--':>18}"
                    r, n = ratio(rows)
                    if ci:
                        lo, hi = bootstrap(rows_by_day)
                        return f"{r:5.2f} [{lo:4.2f},{hi:4.2f}] n={n:<6}"
                    return f"{r:5.2f} n={n:<6}"
                print(f"{cdef:7} {b[0]:.2f}-{b[1]:.2f} | {fmt((cdef, True, b))} | {fmt((cdef, False, b))} | {fmt((cdef, 'corrected', b), ci=False)}")
print("\ntable in code:", [(hi, f) for hi, f in OFF_CENTER_CLAIM_BANDS])
