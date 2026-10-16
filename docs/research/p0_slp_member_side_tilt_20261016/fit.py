"""PROTOCOL.md: mode-keeping side tilt from the members' above/below fraction, after the live SLP.

usage: python3 fit.py members   pull each row's seated members (same query as the row build) into members.json.gz
       python3 fit.py           score
"""
from __future__ import annotations

import gzip, json, math, sqlite3, sys
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
from bayes_basket_target.center_width_calibration import mode_shoulder_bin_for  # noqa: E402
sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
import analyze as A  # noqa: E402

ROWS = ROOT / 'docs/research/p0_slp_per_system_c_20261016'
BETAS = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0]
MIN_TRAIN_DAYS = 10
BINS = ('h0_24', 'h24_48', 'h48_plus')
CLIP = (0.02, 0.98)


def load_rows():
    rows = []
    for K in (0, 1, 2):
        for l in gzip.open(ROWS / f'rows_d{K}.json.gz', 'rt'):
            r = json.loads(l); r['K'] = K; rows.append(r)
    return rows


def pull_members():
    c = sqlite3.connect(f'file:{RN.SSOT}?mode=ro', uri=True, timeout=5)
    out = {}
    for r in load_rows():
        lin = json.loads(c.execute("select lineage_json from basket_probability_envelope where station_id=? and event_date=? and stage='P0'"
                                   " and decision_at_utc=? and state='COMMITTED' limit 1", (r['st'], r['day'], r['dec'])).fetchone()[0])
        runs = json.loads(lin.get('run_init_utc_by_system_json') or '{}')
        out[f"{r['K']}|{r['st']}|{r['day']}"] = {s: [float(v) for (v,) in c.execute(
            "select raw_tmax_c from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date=?"
            " and native_run_init_utc=? and archive_query_at_utc<=? group by member_id", (s, r['st'], r['day'], runs[s], r['dec']))] for s in r['w']}
    json.dump(out, gzip.open(HERE / 'members.json.gz', 'wt'))
    print('members for', len(out), 'rows')


def slp_vector(r):
    ordered = r['ordered']; cf = slp_shrink_for(r['hours'])
    vecs = {s: dict(zip(ordered, r['vecs'][s][str(cf)])) for s in r['w']}
    pooled, _ = bridged_cross_probability(system_probabilities=vecs, system_weights=r['w'], system_centers_c=r['cen'],
                                          ordered_keys=ordered, key_centers_c=r['centers_key'])
    if r['shift_sigma'] is not None:
        shift = float(r['shift_sigma']) * NP._distribution_sigma_c(pooled, r['centers_key'])
        if abs(shift) > 1e-9:
            pooled = NP._shift_probability_c(pooled, ordered=ordered, bucket_centers_c=[v + shift for v in r['centers_key']], intervals=r['iv'])
    return np.array([float(pooled[b]) for b in ordered])


def logit(x):
    x = min(CLIP[1], max(CLIP[0], x)); return math.log(x / (1 - x))


def side_z(p, r, members):
    m = int(np.argmax(p)); n = len(p)
    if m == 0 or m == n - 1:
        return m, None
    t = float(r['centers_key'][m])
    f_mem = 0.0
    for s, w in r['w'].items():
        vals = members[s]; mu = sum(vals) / len(vals); shift = r['cen'][s] - mu
        f_mem += w * sum(1 for v in vals if v + shift > t) / len(vals)
    f_p = float(p[m + 1:].sum() + 0.5 * p[m])
    return m, logit(f_mem) - logit(f_p)


def tilt(p, m, z, beta):
    if z is None or beta == 0: return p
    q = p.copy(); n = len(p)
    q[:m] *= math.exp(-beta * z / 2); q[m + 1:] *= math.exp(beta * z / 2)
    rest = 1.0 - p[m]; got = q.sum() - q[m]
    q[np.arange(n) != m] *= rest / got
    return q


def measures(p, r, m):
    ti = r['ti']; n = len(p)
    cdf = np.cumsum(p)[:-1]
    sh = [i for i in (m - 1, m + 1) if 0 <= i < n]; far = [i for i in range(n) if abs(i - m) >= 2]
    return [-math.log(max(p[ti], 1e-12)), float(((p - (np.arange(n) == ti)) ** 2).sum()),
            float((((cdf - (ti <= np.arange(n - 1))) ** 2) * np.asarray(r['widths'])).sum()),
            p[m], float(ti == m), p[sh].sum(), float(ti in sh), p[far].sum(), float(abs(ti - m) >= 2),
            float(p[m + 1:].sum()), float(ti > m), float(p[:m].sum()), float(ti < m)]


def main():
    rows = load_rows()
    mem = json.load(gzip.open(HERE / 'members.json.gz', 'rt'))
    T = np.zeros((len(rows), len(BETAS), 13)); zs = np.full(len(rows), np.nan)
    for i, r in enumerate(rows):
        p = slp_vector(r)
        m, z = side_z(p, r, mem[f"{r['K']}|{r['st']}|{r['day']}"])
        if z is not None: zs[i] = z
        for j, b in enumerate(BETAS):
            q = np.array(RN.floored(dict(zip(r['ordered'], tilt(p, m, z, b))), r['ordered']))
            T[i, j] = measures(q, r, m)
    bins = np.array([mode_shoulder_bin_for(r['hours']) for r in rows])
    dd = np.array([r['day'] for r in rows]); st = np.array([r['st'] for r in rows])
    print(f'z: rows with a tilt {int((~np.isnan(zs)).sum())} of {len(rows)}; |z| median {np.nanmedian(np.abs(zs)):.3f}, mean z {np.nanmean(zs):+.3f}')
    result = {}
    for b in BINS:
        idx = bins == b
        days = sorted(set(dd[idx])); di = {d: k for k, d in enumerate(days)}
        cur = np.full((len(rows), 13), np.nan); arm = np.full((len(rows), 13), np.nan); chosen = {}
        for d in days:
            tr = idx & (dd < d)
            if len(set(dd[tr])) < MIN_TRAIN_DAYS: continue
            te = idx & (dd == d)
            j = int(np.argmin(T[tr, :, 0].mean(axis=0)))
            chosen[d] = BETAS[j]; cur[te] = T[te, 0]; arm[te] = T[te, j]
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

        res = {'rows': int(idx.sum()), 'tested_rows': int(tested.sum()), 'evaluated_days': len(set(dd[tested])), 'chosen': chosen}
        r3 = {k: boot(arm[:, i] - cur[:, i], tested) for i, k in enumerate(('dlog', 'dbrier', 'dcrps'))}
        res['cmp'] = r3
        print(f"== {b}: rows {int(idx.sum())} tested {int(tested.sum())} evaluated days {len(set(dd[tested]))}  chosen beta {Counter(chosen.values()).most_common()}")
        print(f"  SIDE_TILT_WF - CURRENT_SLP  dLog {r3['dlog'][0]:+.4f} [{r3['dlog'][1]:+.4f},{r3['dlog'][2]:+.4f}]  "
              f"dCRPS {r3['dcrps'][0]:+.4f} [{r3['dcrps'][1]:+.4f},{r3['dcrps'][2]:+.4f}]  dBrier {r3['dbrier'][0]:+.4f} [{r3['dbrier'][1]:+.4f},{r3['dbrier'][2]:+.4f}]")
        # fixed beta, in-sample over the bin's tested rows (diagnostic)
        print('  fixed beta (tested rows, dLog):', ' '.join(f'{bt}:{(T[tested, j, 0] - T[tested, 0, 0]).mean():+.4f}' for j, bt in enumerate(BETAS)))
        per = {}
        for s in sorted(set(st[idx])):
            bb = boot(arm[:, 0] - cur[:, 0], tested & (st == s))
            if bb and bb[4] >= 10: per[s] = bb
        passed = A.bh({s: v[3] for s, v in per.items()})
        res['better_bh'] = sorted(str(s) for s in passed if per[s][0] < 0); res['worse_bh'] = sorted(str(s) for s in passed if per[s][0] > 0)
        res['stations'] = {str(s): {'dlog': v, 'bh': s in passed} for s, v in per.items()}
        print(f"  per station: BH better {res['better_bh']}  BH worse {res['worse_bh']}  (stations {len(per)})")
        for name, x in (('CURRENT_SLP', cur[tested]), ('SIDE_TILT', arm[tested])):
            k = x[:, 3:].mean(axis=0)
            print(f"    {name:11s} LS {x[:, 0].mean():.4f}  众数 说{k[0]:.3f}/实{k[1]:.3f}  两肩 说{k[2]:.3f}/实{k[3]:.3f}  更远 说{k[4]:.3f}/实{k[5]:.3f}"
                  f"  上方 说{k[6]:.3f}/实{k[7]:.3f}  下方 说{k[8]:.3f}/实{k[9]:.3f}")
        # side resolution: within the tested rows, does z sort the truth's side?
        zz = zs[tested]; up = cur[tested, 10]; ok = ~np.isnan(zz)
        if ok.sum() > 20:
            q = np.quantile(zz[ok], [1 / 3, 2 / 3])
            for name, mask in (('z low', zz[ok] <= q[0]), ('z mid', (zz[ok] > q[0]) & (zz[ok] <= q[1])), ('z high', zz[ok] > q[1])):
                xa = cur[tested][ok][mask]; xb = arm[tested][ok][mask]
                print(f"    {name:6s} n={int(mask.sum())}  上方 现行说{xa[:, 9].mean():.3f} 倾斜说{xb[:, 9].mean():.3f} 实{xa[:, 10].mean():.3f}")
        result[b] = res
    json.dump(result, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'members':
        pull_members()
    else:
        main()
