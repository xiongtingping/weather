"""PROTOCOL.md: joint walk-forward fit of the SLP shrink c and per-system weight tilts (D1/D2).
usage: python3 run.py build K   (K = 1 or 2)  |  python3 run.py score
"""
from __future__ import annotations

import gzip, json, math, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
import analyze as A  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from bayes_basket_target.center_width_calibration import served_bucket_axis  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
GRID = [0.6, 0.7, 0.8, 0.9, 1.0]
SYSTEMS = ['DWD_ICON_GLOBAL_EPS', 'ECCC_GEM_GLOBAL_ENS', 'ECMWF_AIFS_ENS_GLOBAL', 'ECMWF_IFS_ENS_OPEN_PF50', 'NOAA_AIGEFS_025', 'NOAA_GEFS_05']
MIN_TRAIN_DAYS = 10
RIDGE = 1.0
FLOOR = 1e-3


def build(K):
    inp = sqlite3.connect(ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db')
    tzs = dict(inp.execute('select station_id, settlement_timezone from tz'))
    truth = {}
    for st, d, t, rev in inp.execute('select station_id, local_date, official_tmax_c, revision_no from truth order by revision_no'): truth[(st, d)] = float(t)
    by = defaultdict(list)
    for st, d, dec in inp.execute("select station_id, event_date, decision_at from root where stage='P0' and event_date between '2026-09-01' and '2026-10-14'"):
        by[(st, d)].append(dec)
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=5)
    art = {}; stats = defaultdict(int)
    out = gzip.open(HERE / f'rows_d{K}.json.gz', 'wt')
    for (st, d), decs in sorted(by.items()):
        if (st, d) not in truth or st not in tzs: continue
        cut = (datetime.fromisoformat(d).replace(hour=9, tzinfo=ZoneInfo(tzs[st])) - timedelta(days=K)).astimezone(timezone.utc)
        cs = cut.strftime('%Y-%m-%dT%H:%M:%S.') + f'{cut.microsecond // 1000:03d}Z'
        ok = sorted(x for x in decs if x <= cs)
        if not ok: continue
        dec = ok[-1]
        row = c.execute("select lineage_json, probabilities_json from basket_probability_envelope where station_id=? and event_date=? and stage='P0'"
                        " and decision_at_utc=? and state='COMMITTED' limit 1", (st, d, dec)).fetchone()
        if row is None: continue
        lin = json.loads(row[0]); served = json.loads(row[1])
        iv = json.loads(lin.get('bucket_intervals_c_json') or 'null')
        sysp = json.loads(lin.get('native_system_probabilities_json') or '{}'); wts = json.loads(lin.get('system_weights_json') or '{}')
        cen = json.loads(lin.get('native_calibrated_system_centers_c_json') or '{}')
        runs = json.loads(lin.get('run_init_utc_by_system_json') or '{}'); arts = json.loads(lin.get('system_artifact_shas_json') or '{}')
        rad = json.loads(lin.get('synoptic_cooling_json') or '{}') if lin.get('synoptic_cooling_json') else {}
        if not iv or not sysp:
            stats['incomplete'] += 1; continue
        ordered = NP._ordered_buckets(iv); ivs = {b: iv[b] for b in ordered}
        systems = [s for s in sysp if float(wts.get(s, 0)) > 0 and s in runs and s in arts and s in cen and s in SYSTEMS]
        vec = {}
        for s in systems:
            vals = [float(v) for (v,) in c.execute(
                "select raw_tmax_c from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date=?"
                " and native_run_init_utc=? and archive_query_at_utc<=? group by member_id", (s, st, d, runs[s], dec))]
            if len(vals) < 2: continue
            if arts[s] not in art:
                a = c.execute('select bias_parameters_json, spread_parameters_json, reliability_parameters_json from calibration_native_ensemble_system_artifact where content_sha256=?', (arts[s],)).fetchone()
                art[arts[s]] = None if a is None else tuple(json.loads(x) for x in a)
            if art[arts[s]] is None: continue
            bp, sp, rp = art[arts[s]]
            vec[s] = {}
            for cf in GRID:
                sp2 = dict(sp); sp2['spread_factor'] = min(5.0, max(0.25, float(sp['spread_factor']) * cf))
                v = NP._calibrated_system_probability(values=vals, intervals=ivs, bias_parameters=bp, spread_parameters=sp2, reliability_parameters=rp)
                vec[s][str(cf)] = [float(v[b]) for b in ordered]
        if set(vec) != set(systems) or len(systems) < 2:
            stats['members_missing'] += 1; continue
        T = truth[(st, d)]
        ti = next(i for i, b in enumerate(ordered) if (iv[b][0] is None or iv[b][0] <= T) and (iv[b][1] is None or T < iv[b][1]))
        f, _ = floor_probabilities({b: float(served[b]) for b in ordered})
        out.write(json.dumps({
            'st': st, 'day': d, 'ti': ti, 'ordered': ordered, 'intervals': iv, 'widths': list(np.diff(np.array(served_bucket_axis(iv, ordered)[0]))),
            'served': [f[b] for b in ordered], 'w': {s: float(wts[s]) for s in systems}, 'centers': {s: float(cen[s]) for s in systems},
            'shift_sigma': rad.get('shift_sigma') if rad.get('state') == 'RESOLVED' else None, 'vec': vec}) + '\n')
        stats['rows'] += 1
    out.close(); print(f'D-{K}', dict(stats))


def final_vector(r, cf, weights):
    """Linear mixture at the fitted weights -> consensus bridge -> radiation shift -> floor (production order, no table layer)."""
    ordered = r['ordered']; iv = r['intervals']
    systems = [s for s in r['vec'] if weights.get(s, 0) > 0]
    tw = sum(weights[s] for s in systems)
    probs = {s: dict(zip(ordered, r['vec'][s][str(cf)])) for s in systems}
    keyc = NP._bucket_centers(iv, ordered)
    pooled, _ = bridged_cross_probability(system_probabilities=probs, system_weights={s: weights[s] / tw for s in systems},
                                          system_centers_c={s: r['centers'][s] for s in systems}, ordered_keys=ordered, key_centers_c=keyc)
    if r['shift_sigma'] is not None:
        shift = float(r['shift_sigma']) * NP._distribution_sigma_c(pooled, keyc)
        if abs(shift) > 1e-9:
            pooled = NP._shift_probability_c(pooled, ordered=ordered, bucket_centers_c=[v + shift for v in keyc], intervals=iv)
    f, _ = floor_probabilities(pooled)
    return np.array([f[b] for b in ordered])


def scores(p, r):
    ti = r['ti']; cdf = np.cumsum(p)[:-1]
    return np.array([-math.log(max(p[ti], 1e-12)), float(((p - (np.arange(len(p)) == ti)) ** 2).sum()),
                     float((((cdf - (ti <= np.arange(len(p) - 1))) ** 2) * np.asarray(r['widths'])).sum())])


def fit(train, cf, use_served):
    """beta for one c on the training rows: mean mixture LogScore (floored) + ridge."""
    idx = {s: i for i, s in enumerate(SYSTEMS)}
    mats = []
    for r in train:
        S = [s for s in r['vec']]
        M = np.array([r['vec'][s][str(cf)] for s in S])          # sys x bucket
        base = np.array([r['w'][s] if use_served else 1.0 for s in S])
        mats.append((np.array([idx[s] for s in S]), M, base, r['ti']))

    def obj(beta):
        total = 0.0; grad = np.zeros(len(SYSTEMS))
        for ix, M, base, ti in mats:
            a = base * np.exp(beta[ix]); wn = a / a.sum()
            p = wn @ M
            p = np.maximum(p, 0); p = p / p.sum()
            pf = np.maximum(p, FLOOR); pf = pf / pf.sum()
            pt = pf[ti]
            total += -math.log(pt)
            # gradient through the mixture (floor treated as constant where it binds)
            if p[ti] > FLOOR:
                dp = (M[:, ti] - p[ti]) * wn          # d p_ti / d beta_ix
                np.add.at(grad, ix, -dp / max(p[ti], 1e-12))
        n = len(mats)
        return total / n + 0.5 * RIDGE * float(beta @ beta) / n, grad / n + RIDGE * beta / n
    res = minimize(obj, np.zeros(len(SYSTEMS)), jac=True, method='L-BFGS-B', options={'maxiter': 200})
    return res.x, float(res.fun)


def score():
    from importlib import import_module
    result = {}
    for K in (1, 2):
        R = [json.loads(l) for l in gzip.open(HERE / f'rows_d{K}.json.gz', 'rt')]
        days = sorted({r['day'] for r in R}); dd = np.array([r['day'] for r in R]); st = np.array([r['st'] for r in R])
        base = np.array([scores(np.array(r['served']), r) for r in R])
        arms = {'joint_served_tilt': np.full((len(R), 3), np.nan), 'joint_global': np.full((len(R), 3), np.nan), 'c_only': np.full((len(R), 3), np.nan)}
        fitted = {}
        for d in days:
            tr = [r for r in R if r['day'] < d]
            if len({r['day'] for r in tr}) < MIN_TRAIN_DAYS: continue
            te = [i for i, r in enumerate(R) if r['day'] == d]
            pick = {}
            for name, use_served in (('joint_served_tilt', True), ('joint_global', False)):
                best = None
                for cf in GRID:
                    beta, val = fit(tr, cf, use_served)
                    if best is None or val < best[2]: best = (cf, beta, val)
                pick[name] = best
            # c only (served weights, beta = 0), chosen on the same mixture objective
            best_c = min(GRID, key=lambda cf: np.mean([-math.log(max(np.maximum((np.array([r['w'][s] for s in r['vec']]) / sum(r['w'].values())) @ np.array([r['vec'][s][str(cf)] for s in r['vec']]), FLOOR)[r['ti']], 1e-12)) for r in tr]))
            pick['c_only'] = (best_c, np.zeros(len(SYSTEMS)), None)
            fitted[d] = {k: {'c': v[0], 'beta': [round(float(x), 3) for x in v[1]]} for k, v in pick.items()}
            for i in te:
                r = R[i]
                for name, (cf, beta, _) in pick.items():
                    use_served = name != 'joint_global'
                    wts = {s: (r['w'][s] if use_served else 1.0) * math.exp(beta[SYSTEMS.index(s)]) for s in r['vec']}
                    arms[name][i] = scores(final_vector(r, cf, wts), r)
            print(f'  D{K} {d} fitted', {k: (v['c'], v['beta']) for k, v in fitted[d].items() if k != 'c_only'}, flush=True)
        tested = ~np.isnan(arms['joint_served_tilt'][:, 0])
        di = {d: i for i, d in enumerate(days)}
        draws = np.random.default_rng(7).multinomial(len(days), np.full(len(days), 1 / len(days)), size=2000)

        def boot(delta, mask):
            ds = np.zeros(len(days)); dc = np.zeros(len(days))
            for i in np.where(mask & ~np.isnan(delta))[0]:
                ds[di[dd[i]]] += delta[i]; dc[di[dd[i]]] += 1
            if dc.sum() == 0: return None
            bs = (draws @ ds) / np.maximum(draws @ dc, 1e-9); bs = bs[(draws @ dc) > 0]
            return [float(ds.sum() / dc.sum()), float(np.quantile(bs, .025)), float(np.quantile(bs, .975)), float(max(2 * min((bs >= 0).mean(), (bs <= 0).mean()), 1 / len(bs)))]
        res = {'rows': len(R), 'tested': int(tested.sum()), 'fitted_by_day': fitted, 'arms': {}}
        print(f'\n== D{K}: rows {len(R)} tested {int(tested.sum())}')
        for name, arr in arms.items():
            a = {m: boot(arr[:, k] - base[:, k], tested) for k, m in enumerate(('dlog', 'dbrier', 'dcrps'))}
            per = {}
            for s in sorted(set(st)):
                b = boot(arr[:, 0] - base[:, 0], tested & (st == s))
                if b and len(set(dd[tested & (st == s)])) >= 10: per[s] = b
            passed = A.bh({s: v[3] for s, v in per.items()})
            a['better_bh'] = sorted(s for s in passed if per[s][0] < 0); a['worse_bh'] = sorted(s for s in passed if per[s][0] > 0)
            a['stations'] = per
            pm = []; ym = []; ps = []; ys = []
            for i in np.where(tested)[0]:
                r = R[i]
                cf, beta, _ = (fitted[r['day']][name]['c'], np.array(fitted[r['day']][name]['beta']), None)
                use_served = name != 'joint_global'
                wts = {s: (r['w'][s] if use_served else 1.0) * math.exp(beta[SYSTEMS.index(s)]) for s in r['vec']}
                p = final_vector(r, cf, wts); m = int(np.argmax(p))
                pm.append(p[m]); ym.append(r['ti'] == m); ps.append((p[m - 1] if m > 0 else 0) + (p[m + 1] if m + 1 < len(p) else 0)); ys.append(abs(r['ti'] - m) == 1)
            a['mode_p_real'] = [float(np.mean(pm)), float(np.mean(ym))]; a['shoulder_p_real'] = [float(np.mean(ps)), float(np.mean(ys))]
            res['arms'][name] = a
            print(f"  {name:18s} dLog {a['dlog'][0]:+.4f} [{a['dlog'][1]:+.4f},{a['dlog'][2]:+.4f}]  dCRPS {a['dcrps'][0]:+.4f} [{a['dcrps'][1]:+.4f},{a['dcrps'][2]:+.4f}]  dBrier {a['dbrier'][0]:+.4f} [{a['dbrier'][1]:+.4f},{a['dbrier'][2]:+.4f}]"
                  f"  BH+ {a['better_bh']} BH- {a['worse_bh']}  mode {a['mode_p_real'][0]:.3f}/{a['mode_p_real'][1]:.3f} shoulders {a['shoulder_p_real'][0]:.3f}/{a['shoulder_p_real'][1]:.3f}")
        sm = []; sy = []
        for i in np.where(tested)[0]:
            p = np.array(R[i]['served']); m = int(np.argmax(p)); sm.append(p[m]); sy.append(R[i]['ti'] == m)
        print(f"  served mode {np.mean(sm):.3f}/{np.mean(sy):.3f}")
        last = fitted[max(fitted)]
        print('  last fit:', {k: v for k, v in last.items()})
        result[f'D{K}'] = res
    json.dump(result, open(HERE / 'readings.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    if sys.argv[1] == 'build':
        build(int(sys.argv[2]))
    else:
        score()
