"""Score the strict replay (PROTOCOL.md): SLP with c walked forward vs the current chain, D1/D2, segments and stations."""
from __future__ import annotations

import glob, gzip, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
import analyze as A  # noqa: E402

GRID_C = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
MIN_TRAIN_DAYS = 10
TABLE_SINCE = '2026-09-04'


def scores(vec, actual):
    keys = sorted(vec, key=lambda k: int(k))
    p = np.array([vec[k] for k in keys]); p = p / p.sum()
    b = oos._temperature_bucket(actual)
    if b not in vec:
        ti = 0 if int(b) < int(keys[0]) else len(keys) - 1
    else:
        ti = keys.index(b)
    cdf = np.cumsum(p)[:-1]
    crps = float(((cdf - (ti <= np.arange(len(p) - 1))) ** 2).sum())
    m = int(np.argmax(p))
    return [-math.log(max(p[ti], 1e-12)), float(((p - (np.arange(len(p)) == ti)) ** 2).sum()), crps,
            p[m], float(ti == m), (p[m - 1] if m > 0 else 0) + (p[m + 1] if m + 1 < len(p) else 0), float(abs(ti - m) == 1)]


def main():
    R = [json.loads(l) for f in glob.glob(str(HERE / 'stations/*.jsonl.gz')) for l in gzip.open(f, 'rt')]
    result = {}
    for K in (1, 2):
        rows = [r for r in R if r['K'] == K]
        days = sorted({r['event_date'] for r in rows}); di = {d: i for i, d in enumerate(days)}
        dd = np.array([r['event_date'] for r in rows]); st = np.array([r['station'] for r in rows])
        S = {name: np.array([scores(r['arms'][name], r['actual']) for r in rows]) for name in rows[0]['arms']}
        wf = np.full((len(rows), 7), np.nan); chosen = {}
        for d in days:
            tr = dd < d
            if len(set(dd[tr])) < MIN_TRAIN_DAYS: continue
            cf = min(GRID_C, key=lambda g: S[f'slp_{g}'][tr, 0].mean()); chosen[d] = cf
            wf[dd == d] = S[f'slp_{cf}'][dd == d]
        tested = ~np.isnan(wf[:, 0])
        draws = np.random.default_rng(7).multinomial(len(days), np.full(len(days), 1 / len(days)), size=2000)

        def boot(delta, mask):
            ds = np.zeros(len(days)); dc = np.zeros(len(days))
            for i in np.where(mask & ~np.isnan(delta))[0]:
                ds[di[dd[i]]] += delta[i]; dc[di[dd[i]]] += 1
            if dc.sum() == 0: return None
            ok = (draws @ dc) > 0
            bs = ((draws @ ds) / np.maximum(draws @ dc, 1e-9))[ok]
            return [float(ds.sum() / dc.sum()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975)),
                    float(max(2 * min((bs >= 0).mean(), (bs <= 0).mean()), 1 / len(bs))), int(len(set(dd[mask & ~np.isnan(delta)])))]
        segs = {'all': tested, 'before_tables(<09-04)': tested & (dd < TABLE_SINCE), 'with_tables(>=09-04)': tested & (dd >= TABLE_SINCE)}
        res = {'rows': len(rows), 'event_days': len(days), 'tested_rows': int(tested.sum()), 'evaluated_days': len(set(dd[tested])),
               'c_by_day': chosen, 'cmp': {}}
        print(f"\n== D{K}: rows {len(rows)} event days {len(days)} tested rows {int(tested.sum())} evaluated days {len(set(dd[tested]))}  c chosen {sorted(set(chosen.values()))} (last {chosen[max(chosen)] if chosen else None})")
        for base in ('current_asof', 'current_today'):
            for seg, mask in segs.items():
                r = {m: boot(wf[:, k] - S[base][:, k], mask) for k, m in enumerate(('dlog', 'dbrier', 'dcrps'))}
                res['cmp'][f'SLP_wf vs {base} | {seg}'] = r
                if r['dlog'] is None: continue
                print(f"  SLP_wf - {base:13s} {seg:22s} days {r['dlog'][4]:3d}  dLog {r['dlog'][0]:+.4f} [{r['dlog'][1]:+.4f},{r['dlog'][2]:+.4f}]  "
                      f"dCRPS {r['dcrps'][0]:+.4f} [{r['dcrps'][1]:+.4f},{r['dcrps'][2]:+.4f}]  dBrier {r['dbrier'][0]:+.4f} [{r['dbrier'][1]:+.4f},{r['dbrier'][2]:+.4f}]")
        # fixed c, all tested rows, vs current_asof
        line = []
        for cf in GRID_C:
            b = boot(S[f'slp_{cf}'][:, 0] - S['current_asof'][:, 0], tested)
            line.append(f"c{cf}:{b[0]:+.4f}")
        print('  fixed c vs current_asof (LogScore):', ' '.join(line))
        # per station (main: vs current_asof, all)
        per = {}
        for s in sorted(set(st)):
            b = boot(wf[:, 0] - S['current_asof'][:, 0], tested & (st == s))
            if b and b[4] >= 10: per[s] = b
        passed = A.bh({s: v[3] for s, v in per.items()})
        res['stations'] = {s: {'dlog': v, 'bh': s in passed} for s, v in per.items()}
        res['better_bh'] = sorted(s for s in passed if per[s][0] < 0); res['worse_bh'] = sorted(s for s in passed if per[s][0] > 0)
        print(f"  per station (vs current_asof): BH better {res['better_bh']}  BH worse {res['worse_bh']}  (stations scored {len(per)})")
        for name, arr in (('current_asof', S['current_asof']), ('SLP_wf', wf)):
            for seg, mask in segs.items():
                a = arr[mask]
                if len(a):
                    print(f"    {name:12s} {seg:22s} mode p/real {a[:,3].mean():.3f}/{a[:,4].mean():.3f}  shoulders p/real {a[:,5].mean():.3f}/{a[:,6].mean():.3f}")
        result[f'D{K}'] = res
    json.dump(result, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
