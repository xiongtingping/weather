"""PROTOCOL.md: mode-to-shoulder share after the live SLP, by horizon bin, walk-forward.

usage: python3 fit.py
"""
from __future__ import annotations

import gzip, json, math, sys
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'docs/research/p0_spread_adjusted_pool_20261016'))
import run as RN  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from bayes_basket_target.spread_adjusted_pool import slp_shrink_for  # noqa: E402
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    MODE_SHOULDER_SHARE_GRID, apply_mode_shoulder, mode_shoulder_bin_for,
)
sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
import analyze as A  # noqa: E402

ROWS = ROOT / 'docs/research/p0_slp_per_system_c_20261016'
GRID = list(MODE_SHOULDER_SHARE_GRID)
MIN_TRAIN_DAYS = 10
BINS = ('h0_24', 'h24_48', 'h48_plus')


def slp_vector(r):
    ordered = r['ordered']; cf = slp_shrink_for(r['hours'])
    vecs = {s: dict(zip(ordered, r['vecs'][s][str(cf)])) for s in r['w']}
    pooled, _ = bridged_cross_probability(system_probabilities=vecs, system_weights=r['w'], system_centers_c=r['cen'],
                                          ordered_keys=ordered, key_centers_c=r['centers_key'])
    if r['shift_sigma'] is not None:
        shift = float(r['shift_sigma']) * NP._distribution_sigma_c(pooled, r['centers_key'])
        if abs(shift) > 1e-9:
            pooled = NP._shift_probability_c(pooled, ordered=ordered, bucket_centers_c=[v + shift for v in r['centers_key']], intervals=r['iv'])
    return pooled


def measures(p, r):
    ti = r['ti']; n = len(p)
    cdf = np.cumsum(p)[:-1]
    m = int(np.argmax(p)); sh = [i for i in (m - 1, m + 1) if 0 <= i < n]; far = [i for i in range(n) if abs(i - m) >= 2]
    return [-math.log(max(p[ti], 1e-12)), float(((p - (np.arange(n) == ti)) ** 2).sum()),
            float((((cdf - (ti <= np.arange(n - 1))) ** 2) * np.asarray(r['widths'])).sum()),
            p[m], float(ti == m), p[sh].sum(), float(ti in sh), p[far].sum(), float(abs(ti - m) >= 2)]


def main():
    rows = []
    for K in (0, 1, 2):
        rows += [json.loads(l) for l in gzip.open(ROWS / f'rows_d{K}.json.gz', 'rt')]
    T = np.zeros((len(rows), len(GRID), 9)); disp = np.zeros(len(rows))
    for i, r in enumerate(rows):
        base = slp_vector(r)
        for j, s in enumerate(GRID):
            v = base if s == 0 else apply_mode_shoulder(base, share=s, ordered_keys=r['ordered'])[0]
            T[i, j] = measures(np.array(RN.floored(v, r['ordered'])), r)
        w = r['w']; c = r['cen']; mu = sum(w[x] * c[x] for x in w)
        disp[i] = math.sqrt(sum(w[x] * (c[x] - mu) ** 2 for x in w)) / float(np.mean(r['widths']))
    bins = np.array([mode_shoulder_bin_for(r['hours']) for r in rows])
    dd = np.array([r['day'] for r in rows]); st = np.array([r['st'] for r in rows])
    result = {}
    for b in BINS:
        idx = bins == b
        days = sorted(set(dd[idx])); di = {d: k for k, d in enumerate(days)}
        cur = np.full((len(rows), 9), np.nan); arm = np.full((len(rows), 9), np.nan); chosen = {}
        for d in days:
            tr = idx & (dd < d)
            if len(set(dd[tr])) < MIN_TRAIN_DAYS: continue
            te = idx & (dd == d)
            j = int(np.argmin(T[tr, :, 0].mean(axis=0)))
            chosen[d] = GRID[j]
            cur[te] = T[te, 0]; arm[te] = T[te, j]
        tested = ~np.isnan(arm[:, 0])
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

        res = {'rows': int(idx.sum()), 'tested_rows': int(tested.sum()), 'evaluated_days': len(set(dd[tested])), 'chosen': chosen, 'blocks': {}}
        r3 = {m: boot(arm[:, k] - cur[:, k], tested) for k, m in enumerate(('dlog', 'dbrier', 'dcrps'))}
        res['cmp'] = r3
        print(f"== {b}: rows {int(idx.sum())} tested {int(tested.sum())} evaluated days {len(set(dd[tested]))}  chosen s {Counter(chosen.values()).most_common()}")
        print(f"  SLP_SHOULDER_WF - CURRENT_SLP  dLog {r3['dlog'][0]:+.4f} [{r3['dlog'][1]:+.4f},{r3['dlog'][2]:+.4f}]  "
              f"dCRPS {r3['dcrps'][0]:+.4f} [{r3['dcrps'][1]:+.4f},{r3['dcrps'][2]:+.4f}]  dBrier {r3['dbrier'][0]:+.4f} [{r3['dbrier'][1]:+.4f},{r3['dbrier'][2]:+.4f}]")
        per = {}
        for s in sorted(set(st[idx])):
            bb = boot(arm[:, 0] - cur[:, 0], tested & (st == s))
            if bb and bb[4] >= 10: per[s] = bb
        passed = A.bh({s: v[3] for s, v in per.items()})
        res['stations'] = {str(s): {'dlog': v, 'bh': s in passed} for s, v in per.items()}
        res['better_bh'] = sorted(str(s) for s in passed if per[s][0] < 0); res['worse_bh'] = sorted(str(s) for s in passed if per[s][0] > 0)
        print(f"  per station: BH better {res['better_bh']}  BH worse {res['worse_bh']}  (stations {len(per)})")
        if tested.any():
            q = np.quantile(disp[tested], [1 / 3, 2 / 3])
            for seg, mask in (('all', tested), ('low', tested & (disp <= q[0])), ('mid', tested & (disp > q[0]) & (disp <= q[1])), ('high', tested & (disp > q[1]))):
                for name, x in (('CURRENT_SLP', cur[mask]), ('SLP_SHOULDER', arm[mask])):
                    blk = [float(v) for v in x[:, 3:].mean(axis=0)]
                    res['blocks'][f'{seg}|{name}'] = blk
                    print(f"    {seg:4s} {name:12s} n={int(mask.sum()):4d} LS {x[:, 0].mean():.4f}  众数 说{blk[0]:.3f}/实{blk[1]:.3f}  两肩 说{blk[2]:.3f}/实{blk[3]:.3f}  更远 说{blk[4]:.3f}/实{blk[5]:.3f}")
        result[b] = res
    json.dump(result, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
