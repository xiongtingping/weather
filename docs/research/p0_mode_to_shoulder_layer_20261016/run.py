"""PROTOCOL.md: mode-to-shoulder layer on the served P0, alpha per lead walked forward, charter 240 fleet + per station."""
from __future__ import annotations

import gzip, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
import analyze as A  # noqa: E402

GRID = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
MIN_TRAIN = 15


def move(v, m, a, s):
    v = np.array(v, float); take = a * v[m]; v[m] -= take
    if m == 0: s = 1.0
    if m + 1 == len(v): s = 0.0
    if m > 0: v[m - 1] += take * (1 - s)
    if m + 1 < len(v): v[m + 1] += take * s
    return v


def score(v, ti, w):
    pv = np.maximum(np.array(v, float), 0); pv /= pv.sum()
    f, _ = floor_probabilities(dict(zip(range(len(pv)), pv)), ()); pv = np.array([f[i] for i in range(len(pv))])
    ls = -math.log(pv[ti]); brier = float(((pv - (np.arange(len(pv)) == ti)) ** 2).sum())
    cdf = np.cumsum(pv)[:-1]; crps = float((((cdf - (ti <= np.arange(len(pv) - 1))) ** 2) * w).sum())
    return np.array([ls, crps, brier])


def main():
    out = {}
    for K in (0, 1, 2):
        R = json.load(gzip.open(ROOT / f'docs/research/p0_shoulder_buckets_20261016/post_pool_rows_d{K}.json.gz', 'rt'))
        R = [x for x in R if x['frac'] is not None]
        # bucket width from the served ladder is not stored; use 1.0 C for C stations, 1.111 for F (shoulder rows know unit via station registry in inputs)
        import sqlite3
        inp = sqlite3.connect(ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db')
        unit = {}
        for st, ij in inp.execute('select station_id, min(intervals_json) from p1 group by station_id'):
            unit[st] = 1.1111111 if '°F' in ij else 1.0
        days = np.array([x['day'] for x in R]); st = np.array([x['st'] for x in R]); ud = sorted(set(days))
        base = np.array([score(x['stages']['served'], x['ti'], unit.get(x['st'], 1.0)) for x in R])
        shares = {'A': [], 'B': [], 'C': []}
        for x in R:
            v = np.array(x['stages']['served']); m = x['m']
            lo = v[m - 1] if m > 0 else 0.0; hi = v[m + 1] if m + 1 < len(v) else 0.0
            a = hi / (lo + hi) if lo + hi > 0 else 0.5; b = min(max(0.5 + x['frac'], 0.1), 0.9)
            shares['A'].append(a); shares['B'].append(b); shares['C'].append((a + b) / 2)
        res = {'n': len(R), 'days': len(ud), 'arms': {}}
        rng = np.random.default_rng(7); draws = rng.multinomial(len(ud), np.full(len(ud), 1 / len(ud)), size=2000); didx = {d: i for i, d in enumerate(ud)}

        def boot(delta, mask=None):
            ds = np.zeros(len(ud)); dc = np.zeros(len(ud))
            for i, (x, v) in enumerate(zip(R, delta)):
                if mask is not None and not mask[i]: continue
                if np.isnan(v): continue
                ds[didx[x['day']]] += v; dc[didx[x['day']]] += 1
            if dc.sum() == 0: return None
            bs = (draws @ ds) / np.maximum(draws @ dc, 1e-9); valid = (draws @ dc) > 0
            pt = ds.sum() / dc.sum(); return [float(pt), float(np.quantile(bs[valid], .025)), float(np.quantile(bs[valid], .975)), int(dc.sum())]
        print(f'\n== D{K}: {len(R)} station-days, {len(ud)} days')
        for arm in ('A', 'B', 'C'):
            grid = {a: np.array([score(move(x['stages']['served'], x['m'], a, shares[arm][i]), x['ti'], unit.get(x['st'], 1.0)) for i, x in enumerate(R)]) - base for a in GRID}
            fixed = {str(a): {m_: boot(grid[a][:, k]) for k, m_ in enumerate(('dlog', 'dcrps', 'dbrier'))} for a in GRID}
            wf = np.full((len(R), 3), np.nan); chosen = {}
            for d in ud:
                tr = days < d
                if len(set(days[tr])) < MIN_TRAIN: continue
                a = min(GRID, key=lambda g: grid[g][tr, 0].mean()); chosen[d] = a; wf[days == d] = grid[a][days == d]
            fleet = {m_: boot(wf[:, k]) for k, m_ in enumerate(('dlog', 'dcrps', 'dbrier'))}
            per = {}
            for s in sorted(set(st)):
                b = boot(wf[:, 0], st == s)
                if b and b[3] >= 10: per[s] = b
            pv = {s: 2 * min(np.mean(np.array([v[0]]) >= 0), 1) for s, v in per.items()}  # placeholder, replaced below
            # per-station p from bootstrap: recompute sign share
            pvals = {}
            for s, b in per.items():
                mask = st == s; ds = np.zeros(len(ud)); dc = np.zeros(len(ud))
                for i, x in enumerate(R):
                    if mask[i] and not np.isnan(wf[i, 0]): ds[didx[x['day']]] += wf[i, 0]; dc[didx[x['day']]] += 1
                bs = (draws @ ds) / np.maximum(draws @ dc, 1e-9); valid = (draws @ dc) > 0; bs = bs[valid]
                pvals[s] = max(2 * min((bs >= 0).mean(), (bs <= 0).mean()), 1 / len(bs))
            passed = A.bh(pvals)
            res['arms'][arm] = {'fixed': fixed, 'walk_forward': fleet, 'alpha_by_day': chosen, 'stations': {s: {'dlog': b, 'p': pvals[s], 'bh': s in passed} for s, b in per.items()},
                                'better_bh': sorted(s for s in passed if per[s][0] < 0), 'worse_bh': sorted(s for s in passed if per[s][0] > 0)}
            f = fleet['dlog']
            if f:
                print(f"  arm {arm}: walk-forward dLog {f[0]:+.4f} [{f[1]:+.4f},{f[2]:+.4f}] CRPS {fleet['dcrps'][0]:+.4f} [{fleet['dcrps'][1]:+.4f},{fleet['dcrps'][2]:+.4f}] Brier {fleet['dbrier'][0]:+.4f} [{fleet['dbrier'][1]:+.4f},{fleet['dbrier'][2]:+.4f}]  n{f[3]} alpha last {chosen[max(chosen)] if chosen else None} (used {sorted(set(chosen.values()))})")
                print(f"       fixed alpha dLog: " + ' '.join(f"{a:.2f}:{fixed[str(a)]['dlog'][0]:+.4f}" for a in GRID))
                print(f"       stations BH better {res['arms'][arm]['better_bh']} worse {res['arms'][arm]['worse_bh']}")
            # calibration after the layer at walk-forward alpha
            if chosen:
                a_last = chosen[max(chosen)]
                pm = []; psh = []; ym = []; ysh = []
                for i, x in enumerate(R):
                    v = move(x['stages']['served'], x['m'], a_last, shares[arm][i]); v = np.maximum(v, 0); v /= v.sum(); m = x['m']
                    pm.append(v[m]); psh.append((v[m - 1] if m > 0 else 0) + (v[m + 1] if m + 1 < len(v) else 0)); ym.append(x['ti'] == m); ysh.append(abs(x['ti'] - m) == 1)
                res['arms'][arm]['calibration_after'] = {'alpha': a_last, 'p_mode': float(np.mean(pm)), 'mode_freq': float(np.mean(ym)), 'p_shoulders': float(np.mean(psh)), 'shoulder_freq': float(np.mean(ysh))}
                print(f"       after layer (alpha {a_last}): p(mode) {np.mean(pm):.3f} realised {np.mean(ym):.3f} | p(shoulders) {np.mean(psh):.3f} realised {np.mean(ysh):.3f}")
        out[f'D{K}'] = res
    json.dump(out, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
