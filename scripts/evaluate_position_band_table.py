"""Would a position-aware band table close the residual charter 81 patches?

Read-only.  Pre-band claims (band store's own sample, first decision per
station-day, official truth) re-served two ways: the active pooled table
(``apply_band_reliability``, the serving function) and the same table with
the shadow layer's PROMOTED position cells replacing the pooled factor for
their (position, band) -- replacement semantics, the shadow's backoff rule.
Scored per bucket (NLL, Brier, reliability near/far) and per window
(containment over claim, holds the mean centre or not).
"""
import json, math, random, sqlite3, sys
from collections import defaultdict
sys.path.insert(0, ".")
from bayes_basket_target.band_reliability_store import _truth, serving_lane_by_station_day, load_band_table_with_axis
from bayes_basket_target.cross_consensus import apply_band_reliability
from bayes_basket_target.basket_kernel.support import mean_center_bucket_v1

conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA busy_timeout=60000")
truth = _truth(conn)
BANDS = ((0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.55), (0.55, 1.01))

def group_of(d):
    return "cool_far" if d <= -2 else "cool_near" if d == -1 else "centre" if d == 0 else "warm_near" if d == 1 else "warm_far"

def promoted_cells(stage):
    row = conn.execute("SELECT evidence_json FROM calibration_band_shadow_journal WHERE stage=? ORDER BY fitted_at_utc DESC LIMIT 1", (stage,)).fetchone()
    out = {}
    for key, factor in (json.loads(row[0]).get("promoted") or {}).items():
        station, group, band = eval(key)  # "['*', 'centre', (0.2, 0.25)]"
        if station == "*":
            out[(group, float(band[0]), float(band[1]))] = float(factor)
    return out

def pooled_factor(table, claim):
    for (lo, hi), f in table:
        if lo <= claim < hi: return f
    return 1.0

def serve_position(claims, table, cells):
    n = len(claims); am = max(range(n), key=lambda i: (claims[i], -i))
    fac = []
    for i, c in enumerate(claims):
        g = group_of(i - am); f = pooled_factor(table, c)
        for (gg, lo, hi), pf in cells.items():
            if gg == g and lo <= c < hi: f = pf; break
        fac.append(f)
    adj = [c * f for c, f in zip(claims, fac)]; t = sum(adj)
    return [a / t for a in adj]

def ratio(rows):
    c = sum(r[0] for r in rows); h = sum(r[1] for r in rows); return (h / c if c else float("nan")), len(rows)

def boot(rows_by_day, draws=600, seed=7):
    rng = random.Random(seed); days = list(rows_by_day)
    if not days: return (float("nan"), float("nan"))
    out = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        c = sum(r[0] for d in pick for r in rows_by_day[d]); h = sum(r[1] for d in pick for r in rows_by_day[d])
        out.append(h / c if c else float("nan"))
    out.sort(); return out[int(0.025 * draws)], out[int(0.975 * draws)]

for stage in ("P0", "P0-M", "P1"):
    pre = serving_lane_by_station_day(conn, truth, stage=stage, pre_band=True)
    post = serving_lane_by_station_day(conn, truth, stage=stage, pre_band=False)
    table, sha, axis = load_band_table_with_axis(conn, stage=stage)
    # This study predates charter 105 and its lookup (``pooled_factor``,
    # ``serve_position``) keys cells by the CLAIM.  P0 moved to the
    # distance axis on 2026-09-07, where every claim under 50% falls in
    # the first cell -- re-running it there would print a full scorecard
    # off one factor.  Stop instead of scoring the wrong lookup.
    assert axis == "claim", f"{stage} serves a {axis} table; this study is claim-keyed"
    cells = promoted_cells(stage)
    print(f"\n##### {stage}: {len(pre)} station-days, active pooled table {sha[:8]}, promoted position cells {len(cells)}")
    matched = total = 0
    score = {"raw": [0.0, 0.0, 0], "pooled": [0.0, 0.0, 0], "position": [0.0, 0.0, 0]}
    rel = defaultdict(lambda: defaultdict(list))     # (variant, near/far, band) -> day -> rows
    win = defaultdict(lambda: defaultdict(list))     # (variant, width, holds, band) -> day -> rows
    for key, snaps in pre.items():
        entries = snaps[0]; claims = [c for c, _h, _l in entries]; hits = [h for _c, h, _l in entries]
        n = len(claims)
        if n < 3 or sum(claims) <= 0: continue
        keys = [str(i) for i in range(n)]
        pooled, _ = apply_band_reliability({k: c for k, c in zip(keys, claims)}, factors=table)
        pooled_v = [pooled[k] for k in keys]
        if key in post:
            rec = [c for c, _h, _l in post[key][0]]
            if len(rec) == n:
                total += 1
                if max(abs(a - b) for a, b in zip(rec, pooled_v)) < 1e-6: matched += 1
        pos_v = serve_position(claims, table, cells)
        am = max(range(n), key=lambda i: (claims[i], -i))
        for name, vec in (("raw", claims), ("pooled", pooled_v), ("position", pos_v)):
            hit_i = hits.index(1) if 1 in hits else None
            if hit_i is not None:
                score[name][0] += -math.log(max(vec[hit_i], 1e-9))
                score[name][1] += sum((v - (1 if i == hit_i else 0)) ** 2 for i, v in enumerate(vec))
                score[name][2] += 1
            for i, v in enumerate(vec):
                b = next((bb for bb in BANDS if bb[0] <= v < bb[1]), None)
                if b is None: continue
                near = "near" if abs(i - am) <= 1 else "far"
                rel[(name, near, b)][key[1]].append((v, hits[i]))
            probs = {k: vec[i] for i, k in enumerate(keys)}
            mc = mean_center_bucket_v1(probs, {k: i for i, k in enumerate(keys)}, admitted=[k for k in keys if probs[k] > 0])
            centre = int(mc) if mc is not None else am
            for width in (4, 3):
                for s in range(0, n - width + 1):
                    w = range(s, s + width); claim = sum(vec[i] for i in w); hit = 1 if any(hits[i] for i in w) else 0
                    b = next((bb for bb in BANDS if bb[0] <= claim < bb[1]), None)
                    if b is None: continue
                    win[(name, width, centre in w, b)][key[1]].append((claim, hit))
    print(f"my pooled re-serve == recorded served on {matched}/{total} station-days (rest pinned an older table)")
    print("per-bucket score (lower better):", {k: f"NLL {v[0]/v[2]:.4f} Brier {v[1]/v[2]:.4f} n={v[2]}" for k, v in score.items()})
    print("\nreliability realised/claimed by band, near(|d|<=1) vs far:")
    print(f"{'band':9} | {'raw near':>10} {'raw far':>10} | {'pooled near':>12} {'pooled far':>11} | {'posit. near':>12} {'posit. far':>11}")
    for b in BANDS:
        cells_out = []
        for name in ("raw", "pooled", "position"):
            for nf in ("near", "far"):
                rows_by_day = rel.get((name, nf, b), {}); rows = [r for d in rows_by_day for r in rows_by_day[d]]
                r, k = ratio(rows); cells_out.append(f"{r:5.2f}({k})" if rows else "   --")
        print(f"{b[0]:.2f}-{b[1]:.2f} | {cells_out[0]:>10} {cells_out[1]:>10} | {cells_out[2]:>12} {cells_out[3]:>11} | {cells_out[4]:>12} {cells_out[5]:>11}")
    for width in (4, 3):
        print(f"\n{width}-bucket windows, containment/claim (mean centre):")
        print(f"{'band':9} | {'pooled holds':>22} {'pooled excl':>14} | {'position holds':>22} {'position excl':>14}")
        for b in BANDS:
            def cell(name, holds, ci):
                rows_by_day = win.get((name, width, holds, b), {}); rows = [r for d in rows_by_day for r in rows_by_day[d]]
                if not rows: return "--"
                r, k = ratio(rows)
                if ci:
                    lo, hi = boot(rows_by_day); return f"{r:4.2f}[{lo:4.2f},{hi:4.2f}] n={k}"
                return f"{r:4.2f} n={k}"
            print(f"{b[0]:.2f}-{b[1]:.2f} | {cell('pooled', True, True):>22} {cell('pooled', False, False):>14} | {cell('position', True, True):>22} {cell('position', False, False):>14}")
