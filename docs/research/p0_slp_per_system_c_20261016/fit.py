"""PROTOCOL.md: PER_SYSTEM_WF vs GLOBAL_WF (and FIXED), walk-forward by event day, D0/D1/D2.

usage: python3 fit.py
"""
from __future__ import annotations

import gzip, json, math, sys, time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'docs/research/p0_spread_adjusted_pool_20261016'))
import run as RN  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from bayes_basket_target.spread_adjusted_pool import slp_shrink_for  # noqa: E402
sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
import analyze as A  # noqa: E402

GRID = [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]
MIN_TRAIN_DAYS = 10
MAX_PASSES = 3


class Rows:
    def __init__(self, rows):
        self.rows = rows
        self.cache = {}

    def score(self, i, cmap):
        r = self.rows[i]
        key = (i, tuple(cmap[s] for s in sorted(r['w'])))
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        ordered = r['ordered']
        vecs = {s: dict(zip(ordered, r['vecs'][s][str(cmap[s])])) for s in r['w']}
        pooled, _ = bridged_cross_probability(system_probabilities=vecs, system_weights=r['w'], system_centers_c=r['cen'],
                                              ordered_keys=ordered, key_centers_c=r['centers_key'])
        if r['shift_sigma'] is not None:
            shift = float(r['shift_sigma']) * NP._distribution_sigma_c(pooled, r['centers_key'])
            if abs(shift) > 1e-9:
                pooled = NP._shift_probability_c(pooled, ordered=ordered, bucket_centers_c=[v + shift for v in r['centers_key']], intervals=r['iv'])
        p = np.array(RN.floored(pooled, ordered)); ti = r['ti']
        cdf = np.cumsum(p)[:-1]
        out = (-math.log(max(p[ti], 1e-12)), float(((p - (np.arange(len(p)) == ti)) ** 2).sum()),
               float((((cdf - (ti <= np.arange(len(p) - 1))) ** 2) * np.asarray(r['widths'])).sum()))
        self.cache[key] = out
        return out

    def mean_log(self, idx, cmap_for):
        return sum(self.score(i, cmap_for(i))[0] for i in idx) / len(idx)


def fit_global(R, idx):
    return min(GRID, key=lambda g: R.mean_log(idx, lambda i: {s: g for s in R.rows[i]['w']}))


def fit_per_system(R, idx, start):
    systems = sorted({s for i in idx for s in R.rows[i]['w']})
    cs = {s: start for s in systems}
    cur = R.mean_log(idx, lambda i: {s: cs[s] for s in R.rows[i]['w']})
    for _ in range(MAX_PASSES):
        moved = False
        for s in systems:
            sub = [i for i in idx if s in R.rows[i]['w']]
            best, best_val = cs[s], None
            for g in GRID:
                trial = dict(cs); trial[s] = g
                val = R.mean_log(idx, lambda i: {x: trial[x] for x in R.rows[i]['w']})
                if best_val is None or val < best_val - 1e-12:
                    best, best_val = g, val
            if best != cs[s]:
                cs[s] = best; moved = True; cur = best_val
        if not moved:
            break
    return cs, cur


def main():
    result = {}
    for K in (0, 1, 2):
        t0 = time.time()
        rows = [json.loads(l) for l in gzip.open(HERE / f'rows_d{K}.json.gz', 'rt')]
        R = Rows(rows)
        days = sorted({r['day'] for r in rows}); di = {d: i for i, d in enumerate(days)}
        dd = np.array([r['day'] for r in rows]); st = np.array([r['st'] for r in rows])
        arms = {a: np.full((len(rows), 3), np.nan) for a in ('GLOBAL_WF', 'PER_SYSTEM_WF', 'FIXED')}
        chosen = {}
        for d in days:
            tr = [i for i in range(len(rows)) if dd[i] < d]
            if len(set(dd[tr])) < MIN_TRAIN_DAYS: continue
            te = [i for i in range(len(rows)) if dd[i] == d]
            g = fit_global(R, tr)
            cs, _ = fit_per_system(R, tr, g)
            chosen[d] = {'global': g, 'per_system': cs}
            for i in te:
                arms['GLOBAL_WF'][i] = R.score(i, {s: g for s in rows[i]['w']})
                arms['PER_SYSTEM_WF'][i] = R.score(i, {s: cs.get(s, g) for s in rows[i]['w']})
                fx = slp_shrink_for(rows[i]['hours'])
                arms['FIXED'][i] = R.score(i, {s: fx for s in rows[i]['w']})
            print(f'  D{K} {d} global {g} per-system {cs}  ({time.time() - t0:.0f}s)', flush=True)
        tested = ~np.isnan(arms['GLOBAL_WF'][:, 0])
        draws = np.random.default_rng(7).multinomial(len(days), np.full(len(days), 1 / len(days)), size=2000)

        def boot(delta, mask):
            ds = np.zeros(len(days)); dc = np.zeros(len(days))
            for i in np.where(mask)[0]:
                ds[di[dd[i]]] += delta[i]; dc[di[dd[i]]] += 1
            if dc.sum() == 0: return None
            ok = (draws @ dc) > 0
            bs = ((draws @ ds) / np.maximum(draws @ dc, 1e-9))[ok]
            return [float(ds.sum() / dc.sum()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975)),
                    float(max(2 * min((bs >= 0).mean(), (bs <= 0).mean()), 1 / len(bs))), int(len(set(dd[mask])))]

        res = {'rows': len(rows), 'tested_rows': int(tested.sum()), 'evaluated_days': len(set(dd[tested])), 'chosen': chosen, 'cmp': {}}
        print(f'== D{K}: rows {len(rows)} tested {int(tested.sum())} evaluated days {len(set(dd[tested]))}')
        for a, b in (('PER_SYSTEM_WF', 'GLOBAL_WF'), ('PER_SYSTEM_WF', 'FIXED'), ('GLOBAL_WF', 'FIXED')):
            r = {m: boot(arms[a][:, k] - arms[b][:, k], tested) for k, m in enumerate(('dlog', 'dbrier', 'dcrps'))}
            res['cmp'][f'{a} - {b}'] = r
            print(f"  {a:13s} - {b:9s}  dLog {r['dlog'][0]:+.4f} [{r['dlog'][1]:+.4f},{r['dlog'][2]:+.4f}]  "
                  f"dCRPS {r['dcrps'][0]:+.4f} [{r['dcrps'][1]:+.4f},{r['dcrps'][2]:+.4f}]  dBrier {r['dbrier'][0]:+.4f} [{r['dbrier'][1]:+.4f},{r['dbrier'][2]:+.4f}]")
        for a in arms:
            print(f'    {a:13s} mean LogScore {np.nanmean(arms[a][tested, 0]):.4f}')
        per = {}
        for s in sorted(set(st)):
            b = boot(arms['PER_SYSTEM_WF'][:, 0] - arms['GLOBAL_WF'][:, 0], tested & (st == s))
            if b and b[4] >= 10: per[s] = b
        passed = A.bh({s: v[3] for s, v in per.items()})
        res['stations'] = {s: {'dlog': v, 'bh': s in passed} for s, v in per.items()}
        res['better_bh'] = sorted(s for s in passed if per[s][0] < 0); res['worse_bh'] = sorted(s for s in passed if per[s][0] > 0)
        print(f"  per station: BH better {res['better_bh']}  BH worse {res['worse_bh']}  (stations {len(per)})")
        last = chosen[max(chosen)]
        print(f"  last fit: global {last['global']}  per-system {last['per_system']}")
        sf = {}
        for r in rows:
            for s, v in r['sf'].items(): sf.setdefault(s, []).append(v)
        print('  certificate spread_factor median by system:', {s: round(float(np.median(v)), 2) for s, v in sorted(sf.items())})
        result[f'D{K}'] = res
        json.dump(result, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
