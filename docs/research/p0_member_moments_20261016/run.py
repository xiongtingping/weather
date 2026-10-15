"""PROTOCOL.md: do the members' skewness / kurtosis matter?  N2 (moments 1-2 only) vs E (production: empirical members) vs E+ (Cornish-Fisher
amplified moments), rebuilt per system with production functions for the P0 decided before local 09:00 of D-K, pooled with the P0's weights.
Shoulder (which-shoulder AUC, feature increment over the served split) and full ladder (charter 240) verdicts.  usage: python3 run.py K
"""
from __future__ import annotations

import gzip, json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
sys.path.insert(0, str(ROOT / 'docs/research/p0_shoulder_buckets_20261016'))
import analyze as A  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402
from bayes_basket_target.native_ensemble_oos import _member_kernel_bandwidth_c  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from pipeline_audit import auc  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
KAPPAS = [0.5, 1.0, 1.5, 2.0]


def cornish_fisher(vals, extra_g1, extra_g2):
    v = np.asarray(vals, float); mu = v.mean(); sd = v.std()
    if sd <= 1e-9: return list(v)
    z = (v - mu) / sd
    w = z + extra_g1 / 6 * (z ** 2 - 1) + extra_g2 / 24 * (z ** 3 - 3 * z) - extra_g1 ** 2 / 36 * (2 * z ** 3 - 5 * z)
    w = (w - w.mean()) / (w.std() + 1e-12) * sd + mu
    return list(w)


def main(K):
    inp = sqlite3.connect(ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db')
    tzs = dict(inp.execute('select station_id, settlement_timezone from tz'))
    truth = {}
    for st, d, t, rev in inp.execute('select station_id, local_date, official_tmax_c, revision_no from truth order by revision_no'): truth[(st, d)] = float(t)
    by = defaultdict(list)
    for st, d, dec in inp.execute("select station_id, event_date, decision_at from root where stage='P0' and event_date between '2026-09-01' and '2026-10-14'"): by[(st, d)].append(dec)
    picks = []
    for (st, d), decs in by.items():
        if (st, d) not in truth or st not in tzs: continue
        cut = (datetime.fromisoformat(d).replace(hour=9, tzinfo=ZoneInfo(tzs[st])) - timedelta(days=K)).astimezone(timezone.utc)
        cs = cut.strftime('%Y-%m-%dT%H:%M:%S.') + f'{cut.microsecond // 1000:03d}Z'
        ok = sorted(x for x in decs if x <= cs)
        if ok: picks.append((st, d, ok[-1]))
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=5)
    art_cache = {}; recs = []; stats = defaultdict(int)
    for st, d, dec in picks:
        row = c.execute("select lineage_json, probabilities_json from basket_probability_envelope where station_id=? and event_date=? and stage='P0' and decision_at_utc=? and state='COMMITTED' limit 1", (st, d, dec)).fetchone()
        if row is None: continue
        lin = json.loads(row[0]); served = json.loads(row[1])
        iv = json.loads(lin.get('bucket_intervals_c_json') or 'null')
        stored = json.loads(lin.get('native_system_probabilities_json') or '{}'); runs = json.loads(lin.get('run_init_utc_by_system_json') or '{}')
        arts = json.loads(lin.get('system_artifact_shas_json') or '{}'); wts = json.loads(lin.get('system_weights_json') or '{}')
        if not iv or not stored: continue
        order, cc, w = A.centres(iv); ivs = {b: iv[b] for b in order}
        sv = np.array([float(served[b]) for b in order]); sv /= sv.sum(); m = int(np.argmax(sv))
        T = truth[(st, d)]; ti = next(i for i, b in enumerate(order) if (iv[b][0] is None or iv[b][0] <= T) and (iv[b][1] is None or T < iv[b][1]))
        systems = {}
        for s in stored:
            if s not in runs or s not in arts or float(wts.get(s, 0)) <= 0: continue
            vals = [float(v) for (v,) in c.execute("select raw_tmax_c from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date=? and native_run_init_utc=? and archive_query_at_utc<=? group by member_id", (s, st, d, runs[s], dec))]
            if len(vals) < 5: continue
            if arts[s] not in art_cache:
                a = c.execute('select bias_parameters_json, spread_parameters_json from calibration_native_ensemble_system_artifact where content_sha256=?', (arts[s],)).fetchone()
                art_cache[arts[s]] = None if a is None else (json.loads(a[0]), json.loads(a[1]))
            if art_cache[arts[s]] is None: continue
            bias_p, spread_p = art_cache[arts[s]]
            mean = sum(vals) / len(vals); bias = NP._resolved_bias_c(mean, bias_p); spread = float(spread_p['spread_factor'])
            bw = _member_kernel_bandwidth_c(vals, spread, spread_p)
            corrected = [mean + bias + spread * (v - mean) for v in vals]
            sd = float(np.std(corrected)); z = (np.array(corrected) - np.mean(corrected)) / (sd + 1e-12)
            g1 = float(np.mean(z ** 3)); g2 = float(np.mean(z ** 4) - 3)
            # N2: normal with the same mean and sd; realised as 2000 quantile points so the same kernel integrator applies
            from math import erf, sqrt
            qs = (np.arange(400) + 0.5) / 400
            normal_pts = list(np.mean(corrected) + sd * np.sqrt(2) * np.array([_erfinv(2 * q - 1) for q in qs]))
            vec = {}
            vec['N2'] = NP._interval_probability(normal_pts, ivs, bandwidth_c=bw)
            vec['E'] = NP._interval_probability(corrected, ivs, bandwidth_c=bw)
            for k in KAPPAS:
                if k == 1.0: vec[f'E+{k}'] = vec['E']; continue
                vec[f'E+{k}'] = NP._interval_probability(cornish_fisher(corrected, (k - 1) * g1, (k - 1) * g2), ivs, bandwidth_c=bw)
            systems[s] = {'w': float(wts[s]), 'g1': g1, 'g2': g2, 'frac': float(np.mean(np.array(corrected) > cc[m])) - 0.5,
                          'vec': {kk: np.array([v[b] for b in order]) for kk, v in vec.items()}}
        if len(systems) < 2: stats['few_systems'] += 1; continue
        tw = sum(x['w'] for x in systems.values())
        pools = {kk: sum(x['w'] * x['vec'][kk] for x in systems.values()) / tw for kk in systems[next(iter(systems))]['vec']}
        recs.append({'st': st, 'day': d, 'm': m, 'ti': ti, 'w': w, 'served': sv, 'pools': pools,
                     'g1': float(np.mean([x['g1'] for x in systems.values()])), 'g2': float(np.mean([x['g2'] for x in systems.values()])),
                     'frac': float(np.mean([x['frac'] for x in systems.values()]))})
    print(f'D-{K}: P0s {len(recs)} {dict(stats)}')

    def score(p, r):
        pv = np.array(p, float); pv = np.maximum(pv, 0); pv /= pv.sum()
        f, _ = floor_probabilities(dict(zip(range(len(pv)), pv)), ()); pv = np.array([f[i] for i in range(len(pv))])
        ls = -math.log(pv[r['ti']]); brier = float(((pv - (np.arange(len(pv)) == r['ti'])) ** 2).sum())
        cdf = np.cumsum(pv)[:-1]; crps = float((((cdf - (r['ti'] <= np.arange(len(pv) - 1))) ** 2) * r['w']).sum())
        return np.array([ls, crps, brier])
    days = np.array([r['day'] for r in recs]); ud = sorted(set(days)); rng = np.random.default_rng(7)
    draws = rng.multinomial(len(ud), np.full(len(ud), 1 / len(ud)), size=2000); didx = {d: i for i, d in enumerate(ud)}

    def boot(delta):
        ds = np.zeros(len(ud)); dc = np.zeros(len(ud))
        for r, v in zip(recs, delta): ds[didx[r['day']]] += v; dc[didx[r['day']]] += 1
        bs = (draws @ ds) / (draws @ dc); return float(delta.mean()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975))
    S = {kk: np.array([score(r['pools'][kk], r) for r in recs]) for kk in recs[0]['pools']}
    S['served'] = np.array([score(r['served'], r) for r in recs])
    out = {'K': K, 'n': len(recs), 'mean_g1': float(np.mean([r['g1'] for r in recs])), 'mean_g2': float(np.mean([r['g2'] for r in recs])), 'ladder': {}, 'shoulder': {}}
    print(f"  member skew g1 mean {out['mean_g1']:+.3f}  excess kurtosis g2 mean {out['mean_g2']:+.3f}")
    print('  full ladder (negative = better):')
    for a, b in (('N2', 'E'), ('E+0.5', 'E'), ('E+1.5', 'E'), ('E+2.0', 'E'), ('served', 'E')):
        res = {}
        for k, mname in enumerate(('dlog', 'dcrps', 'dbrier')): res[mname] = boot(S[a][:, k] - S[b][:, k])
        out['ladder'][f'{a}-{b}'] = res
        print(f"    {a:7s} - {b}: " + '  '.join(f"{mname} {v[0]:+.4f} [{v[1]:+.4f},{v[2]:+.4f}]" for mname, v in res.items()))
    # walk-forward kappa by LogScore
    wf = np.full(len(recs), np.nan); chosen = {}
    for d in ud:
        tr = days < d
        if len(set(days[tr])) < 10: continue
        k = min(KAPPAS, key=lambda kk: (S[f'E+{kk}'][tr, 0] - S['E'][tr, 0]).mean()); chosen[d] = k; wf[days == d] = (S[f'E+{k}'] - S['E'])[days == d, 0]
    ok = ~np.isnan(wf)
    if ok.any():
        sub = [r for r, o in zip(recs, ok) if o]; ds = np.zeros(len(ud)); dc = np.zeros(len(ud))
        for r, v in zip(sub, wf[ok]): ds[didx[r['day']]] += v; dc[didx[r['day']]] += 1
        bs = (draws @ ds) / np.maximum(draws @ dc, 1e-9); out['ladder']['E+wf-E'] = (float(wf[ok].mean()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975)), chosen[max(chosen)])
        print(f"    E+ walk-forward kappa - E: dlog {wf[ok].mean():+.4f} [{np.quantile(bs,.025):+.4f},{np.quantile(bs,.975):+.4f}]  kappa chosen last {chosen[max(chosen)]}  (kappas by day: {sorted(set(chosen.values()))})")
    # shoulder
    sh = [r for r in recs if abs(r['ti'] - r['m']) == 1 and 0 < r['m'] < len(r['served']) - 1]
    y = np.array([float(r['ti'] == r['m'] + 1) for r in sh]); dsh = np.array([r['day'] for r in sh])

    def lsplit(v, m):
        q = min(max(v[m + 1] / (v[m + 1] + v[m - 1] + 1e-12), 1e-3), 1 - 1e-3); return math.log(q / (1 - q))
    print(f'  shoulder rows {len(sh)}: which-shoulder AUC  ' + '  '.join(f"{kk} {auc([lsplit(r['pools'][kk], r['m']) for r in sh], y):.3f}" for kk in ('N2', 'E', 'E+1.5', 'E+2.0')) + f"  served {auc([lsplit(r['served'], r['m']) for r in sh], y):.3f}")
    out['shoulder']['auc'] = {kk: auc([lsplit(r['pools'][kk], r['m']) for r in sh], y) for kk in recs[0]['pools']}

    def fit(X, yy, ridge=1.0):
        b = np.zeros(X.shape[1]); R = np.diag(np.r_[0, np.ones(X.shape[1] - 1)]) * ridge
        for _ in range(60):
            pr = 1 / (1 + np.exp(-X @ b)); b += np.linalg.solve((X * (pr * (1 - pr))[:, None]).T @ X + R + 1e-9 * np.eye(len(b)), X.T @ (yy - pr) - R @ b)
        return b
    base = np.array([lsplit(r['served'], r['m']) for r in sh])
    for name, feats in (('g1', ['g1']), ('g2', ['g2']), ('g1+g2', ['g1', 'g2']), ('frac', ['frac']), ('frac+g1+g2', ['frac', 'g1', 'g2'])):
        X0 = np.c_[np.ones(len(sh)), base]; X1 = np.c_[X0, [[r[f] for f in feats] for r in sh]]
        l0 = np.full(len(sh), np.nan); l1 = l0.copy()
        for d in sorted(set(dsh)):
            tr = dsh < d; te = dsh == d
            if len(set(dsh[tr])) < 10: continue
            for X, l in ((X0, l0), (X1, l1)):
                b = fit(X[tr], y[tr]); pr = 1 / (1 + np.exp(-X[te] @ b)); l[te] = -(y[te] * np.log(pr) + (1 - y[te]) * np.log(1 - pr))
        o = ~np.isnan(l0); dl = (l1 - l0)[o]; dk = dsh[o]; uu = np.unique(dk); ib = {u: np.where(dk == u)[0] for u in uu}
        bs = [dl[np.concatenate([ib[u] for u in rng.choice(uu, len(uu))])].mean() for _ in range(500)]
        out['shoulder'][name] = (float(dl.mean()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975)), int(o.sum()))
        print(f"    served split + {name:11s}: dLogLoss {dl.mean():+.4f} [{np.quantile(bs,.025):+.4f},{np.quantile(bs,.975):+.4f}] n{o.sum()}")
    json.dump(out, open(HERE / f'readings_d{K}.json', 'w'), indent=1, default=float)


def _erfinv(x):
    # Giles' approximation, enough for quantile points
    a = 0.147; ln = math.log(1 - x * x); t = 2 / (math.pi * a) + ln / 2
    return math.copysign(math.sqrt(math.sqrt(t * t - ln / a) - t), x)


if __name__ == '__main__':
    main(int(sys.argv[1]))
