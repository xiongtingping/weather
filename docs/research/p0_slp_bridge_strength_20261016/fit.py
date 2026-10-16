"""PROTOCOL.md: bridge (lambda_max, g0) and c fitted jointly by lead, walk-forward, vs the live SLP.

usage: python3 fit.py
"""
from __future__ import annotations

import gzip, itertools, json, math, sys, time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'docs/research/p0_spread_adjusted_pool_20261016'))
import run as RN  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402
from bayes_basket_target.cross_consensus import bridged_cross_probability, shift_probability  # noqa: E402
from bayes_basket_target.spread_adjusted_pool import slp_shrink_for  # noqa: E402
sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
import analyze as A  # noqa: E402

ROWS = ROOT / 'docs/research/p0_slp_per_system_c_20261016'
C_GRID = [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]
LMAX = [0.4, 0.6, 0.8, 1.0]
G0 = [0.0, 0.5, 1.0]
RAMP = 0.5
CFGS = list(itertools.product(LMAX, G0, C_GRID))
LIVE_BRIDGE = (0.4, 1.0)
MIN_TRAIN_DAYS = 10


def lam(gap, lmax, g0):
    if gap <= g0: return 0.0
    if gap >= g0 + RAMP: return lmax
    return lmax * (gap - g0) / RAMP


def finish(r, pooled):
    ordered = r['ordered']
    if r['shift_sigma'] is not None:
        shift = float(r['shift_sigma']) * NP._distribution_sigma_c(pooled, r['centers_key'])
        if abs(shift) > 1e-9:
            pooled = NP._shift_probability_c(pooled, ordered=ordered, bucket_centers_c=[v + shift for v in r['centers_key']], intervals=r['iv'])
    return np.array(RN.floored(pooled, ordered))


def measures(p, r):
    ti = r['ti']; n = len(p)
    cdf = np.cumsum(p)[:-1]
    m = int(np.argmax(p)); sh = [i for i in (m - 1, m + 1) if 0 <= i < n]; far = [i for i in range(n) if abs(i - m) >= 2]
    return [-math.log(max(p[ti], 1e-12)), float(((p - (np.arange(n) == ti)) ** 2).sum()),
            float((((cdf - (ti <= np.arange(n - 1))) ** 2) * np.asarray(r['widths'])).sum()),
            p[m], float(ti == m), p[sh].sum(), float(ti in sh), p[far].sum(), float(abs(ti - m) >= 2)]


def row_table(r):
    ordered = r['ordered']; keys = list(ordered)
    w = r['w']; cen = r['cen']
    gap = max(cen.values()) - min(cen.values())
    cons_c = sum(w[s] * cen[s] for s in w)
    out = np.zeros((len(CFGS), 9))
    for ci, cf in enumerate(C_GRID):
        vecs = {s: dict(zip(ordered, r['vecs'][s][str(cf)])) for s in w}
        mix = {k: sum(w[s] * vecs[s][k] for s in w) for k in keys}; t = sum(mix.values()); mix = {k: v / t for k, v in mix.items()}
        cons = {k: 0.0 for k in keys}
        for s in w:
            rc = shift_probability(vecs[s], delta_c=cons_c - cen[s], ordered_keys=keys, key_centers_c=r['centers_key'])
            for k in keys: cons[k] += w[s] * rc[k]
        t = sum(cons.values()); cons = {k: v / t for k, v in cons.items()}
        for li, lmax in enumerate(LMAX):
            for gi, g0 in enumerate(G0):
                b = lam(gap, lmax, g0)
                pooled = mix if b <= 0 else {k: (1 - b) * mix[k] + b * cons[k] for k in keys}
                out[CFGS.index((lmax, g0, cf))] = measures(finish(r, pooled), r)
        if ci == C_GRID.index(0.8):
            # the live bridge reproduces the production function exactly
            prod, _ = bridged_cross_probability(system_probabilities=vecs, system_weights=w, system_centers_c=cen, ordered_keys=keys, key_centers_c=r['centers_key'])
            b = lam(gap, *LIVE_BRIDGE)
            mine = mix if b <= 0 else {k: (1 - b) * mix[k] + b * cons[k] for k in keys}
            assert max(abs(prod[k] - mine[k]) for k in keys) < 1e-12, 'bridge reimplementation differs from production'
    mu = cons_c; sd = math.sqrt(sum(w[s] * (cen[s] - mu) ** 2 for s in w))
    return out, sd / float(np.mean(r['widths']))


def main():
    result = {}
    for K in (0, 1, 2):
        t0 = time.time()
        rows = [json.loads(l) for l in gzip.open(ROWS / f'rows_d{K}.json.gz', 'rt')]
        T = []; disp = []
        for r in rows:
            tab, dsp = row_table(r); T.append(tab); disp.append(dsp)
        T = np.array(T); disp = np.array(disp)
        print(f'D{K}: tables built {T.shape} in {time.time() - t0:.0f}s', flush=True)
        days = sorted({r['day'] for r in rows}); di = {d: i for i, d in enumerate(days)}
        dd = np.array([r['day'] for r in rows]); st = np.array([r['st'] for r in rows])
        live_idx = np.array([CFGS.index((*LIVE_BRIDGE, slp_shrink_for(r['hours']))) for r in rows])
        live_bridge_cfgs = [i for i, (l, g, c) in enumerate(CFGS) if (l, g) == LIVE_BRIDGE]
        arms = {a: np.full((len(rows), 9), np.nan) for a in ('CURRENT_SLP', 'SLP_C_WF', 'BRIDGE_C_WF')}
        chosen = {}
        for d in days:
            tr = dd < d
            if len(set(dd[tr])) < MIN_TRAIN_DAYS: continue
            te = dd == d
            mean_log = T[tr, :, 0].mean(axis=0)
            best = int(np.argmin(mean_log))
            best_c = live_bridge_cfgs[int(np.argmin(mean_log[live_bridge_cfgs]))]
            chosen[d] = {'bridge_c': CFGS[best], 'c_only': CFGS[best_c][2]}
            arms['BRIDGE_C_WF'][te] = T[te, best]
            arms['SLP_C_WF'][te] = T[te, best_c]
            arms['CURRENT_SLP'][te] = T[np.where(te)[0], live_idx[te]]
        tested = ~np.isnan(arms['BRIDGE_C_WF'][:, 0])
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

        res = {'rows': len(rows), 'tested_rows': int(tested.sum()), 'evaluated_days': len(set(dd[tested])),
               'chosen': {d: {'bridge_c': list(v['bridge_c']), 'c_only': v['c_only']} for d, v in chosen.items()}, 'cmp': {}, 'blocks': {}}
        print(f'== D{K}: rows {len(rows)} tested {int(tested.sum())} evaluated days {len(set(dd[tested]))}')
        for a, b in (('BRIDGE_C_WF', 'CURRENT_SLP'), ('BRIDGE_C_WF', 'SLP_C_WF'), ('SLP_C_WF', 'CURRENT_SLP')):
            r = {m: boot(arms[a][:, k] - arms[b][:, k], tested) for k, m in enumerate(('dlog', 'dbrier', 'dcrps'))}
            res['cmp'][f'{a} - {b}'] = r
            print(f"  {a:11s} - {b:11s}  dLog {r['dlog'][0]:+.4f} [{r['dlog'][1]:+.4f},{r['dlog'][2]:+.4f}]  "
                  f"dCRPS {r['dcrps'][0]:+.4f} [{r['dcrps'][1]:+.4f},{r['dcrps'][2]:+.4f}]  dBrier {r['dbrier'][0]:+.4f} [{r['dbrier'][1]:+.4f},{r['dbrier'][2]:+.4f}]")
        per = {}
        for s in sorted(set(st)):
            b = boot(arms['BRIDGE_C_WF'][:, 0] - arms['CURRENT_SLP'][:, 0], tested & (st == s))
            if b and b[4] >= 10: per[s] = b
        passed = A.bh({s: v[3] for s, v in per.items()})
        res['stations'] = {s: {'dlog': v, 'bh': s in passed} for s, v in per.items()}
        res['better_bh'] = sorted(str(s) for s in passed if per[s][0] < 0); res['worse_bh'] = sorted(str(s) for s in passed if per[s][0] > 0)
        print(f"  per station: BH better {res['better_bh']}  BH worse {res['worse_bh']}  (stations {len(per)})")
        q = np.quantile(disp[tested], [1 / 3, 2 / 3])
        segs = {'all': tested, 'low': tested & (disp <= q[0]), 'mid': tested & (disp > q[0]) & (disp <= q[1]), 'high': tested & (disp > q[1])}
        for seg, mask in segs.items():
            for a in arms:
                x = arms[a][mask]
                blk = [float(v) for v in x[:, 3:].mean(axis=0)]
                res['blocks'][f'{seg}|{a}'] = blk
                print(f"    {seg:4s} {a:11s} n={int(mask.sum()):4d} LS {x[:, 0].mean():.4f}  众数 说{blk[0]:.3f}/实{blk[1]:.3f}  两肩 说{blk[2]:.3f}/实{blk[3]:.3f}  更远 说{blk[4]:.3f}/实{blk[5]:.3f}")
        from collections import Counter
        print('  chosen (lmax,g0,c) counts:', Counter(tuple(v['bridge_c']) for v in chosen.values()).most_common(5),
              ' last', chosen[max(chosen)])
        result[f'D{K}'] = res
        json.dump(result, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
