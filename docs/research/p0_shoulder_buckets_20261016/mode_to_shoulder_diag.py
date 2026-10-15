"""Diagnostic (not pre-registered): move a share alpha of the served P0's mode mass to its two shoulders, split by the served shoulder
ratio (A) or by the member fraction (B: warm share = 0.5 + frac, clipped 0.1..0.9).  alpha walk-forward per lead from {0,.05,...,.30}
on earlier days (>= 10).  Scored with the 1e-3 floor; station-day equal weight, day-block bootstrap."""
import gzip, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
GRID = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
def move(v, m, a, warm_share=None):
    v = np.array(v, float); take = a * v[m]; v[m] -= take
    lo, hi = v[m - 1] if m > 0 else 0, v[m + 1] if m + 1 < len(v) else 0
    if warm_share is None: warm_share = hi / (lo + hi) if lo + hi > 0 else 0.5
    if m == 0: warm_share = 1.0
    if m + 1 == len(v): warm_share = 0.0
    if m > 0: v[m - 1] += take * (1 - warm_share)
    if m + 1 < len(v): v[m + 1] += take * warm_share
    v = np.maximum(v, 1e-3); return v / v.sum()
def ls(v, ti): return -math.log(max(v[ti], 1e-12))
out = {}
for K in (0, 1, 2):
    R = json.load(gzip.open(f'post_pool_rows_d{K}.json.gz', 'rt'))
    days = np.array([x['day'] for x in R]); sd = np.array([x['st'] + x['day'] for x in R])
    base = np.array([ls(np.maximum(np.array(x['stages']['served']), 1e-3) / np.maximum(np.array(x['stages']['served']), 1e-3).sum(), x['ti']) for x in R])
    arms = {}
    for lab in ('A_served_ratio', 'B_member_fraction'):
        grid = {}
        for a in GRID:
            grid[a] = np.array([ls(move(x['stages']['served'], x['m'], a, None if lab.startswith('A') or x['frac'] is None else min(max(0.5 + x['frac'], 0.1), 0.9)), x['ti']) for x in R]) - base
        wf = np.full(len(R), np.nan); chosen = {}
        for d in sorted(set(days)):
            tr = days < d
            if len(set(days[tr])) < 10: continue
            a = min(GRID, key=lambda g: grid[g][tr].mean()); chosen[d] = a; wf[days == d] = grid[a][days == d]
        ok = ~np.isnan(wf); uu = sorted(set(days[ok])); rng = np.random.default_rng(7)
        # station-day already one row per station-day here
        ib = {u: np.where((days == u) & ok)[0] for u in uu}
        bs = [wf[np.concatenate([ib[u] for u in rng.choice(uu, len(uu))])].mean() for _ in range(1000)]
        arms[lab] = {'wf_dlog': float(wf[ok].mean()), 'lo': float(np.quantile(bs, .025)), 'hi': float(np.quantile(bs, .975)), 'n': int(ok.sum()),
                     'alpha_last': chosen[max(chosen)] if chosen else None, 'fixed': {str(a): float(grid[a].mean()) for a in GRID}}
        print(f"D-{K} {lab:18s} walk-forward dLogScore {wf[ok].mean():+.4f} [{np.quantile(bs,.025):+.4f},{np.quantile(bs,.975):+.4f}] n{ok.sum()} alpha(last) {arms[lab]['alpha_last']}  fixed: " +
              ' '.join(f'{a:.2f}:{grid[a].mean():+.4f}' for a in GRID))
    out[f'D{K}'] = arms
json.dump(out, open('readings_mode_to_shoulder_diag.json', 'w'), indent=1, default=float)
