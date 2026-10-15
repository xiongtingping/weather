"""Where is the member-fraction shoulder information lost inside P0's construction?

P0s decided before local 09:00 of D-K (K from argv), 09-01..10-14, and the same production rebuild as pipeline_audit.py.  For each P0 and each seated system:
  frac_debiased  = share of (raw member + certificate bias) above the centre of the served P0's mode m
  frac_stretched = share of the spread-stretched members (centre + spread x (member - mean)) above that centre
Equal mean over systems, minus 0.5.  Walk-forward logistic (>= 10 earlier days): y ~ logit(split at stage S) [+ frac].
The first stage after which the fraction starts adding information is where it was lost.
"""
from __future__ import annotations

import gzip, json, math, sqlite3, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'docs/research/obs_minus_forecast_station_direction_20261015'))
import analyze as A  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402
from pipeline_audit import auc  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'
STAGES = ['B_raw_kernel', 'C_bias', 'D_bias_spread', 'E_system']


def main():
    c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=5)
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    K = int(sys.argv[1])  # 1 = P0 decided before local 09:00 of D-1, 2 = of D-2
    inp = sqlite3.connect(ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db')
    tzs = dict(inp.execute('select station_id, settlement_timezone from tz'))
    truth = {}
    for st, d, t, rev in inp.execute('select station_id, local_date, official_tmax_c, revision_no from truth order by revision_no'): truth[(st, d)] = float(t)
    by = defaultdict(list)
    for st, d, dec in inp.execute("select station_id, event_date, decision_at from root where stage='P0' and event_date between '2026-09-01' and '2026-10-14'"):
        by[(st, d)].append(dec)
    seen = {}
    for (st, d), decs in by.items():
        if (st, d) not in truth or st not in tzs: continue
        cut = (datetime.fromisoformat(d).replace(hour=9, tzinfo=ZoneInfo(tzs[st])) - timedelta(days=K)).astimezone(timezone.utc)
        cs = cut.strftime('%Y-%m-%dT%H:%M:%S.') + f'{cut.microsecond // 1000:03d}Z'
        ok = sorted(x for x in decs if x <= cs)
        if ok: seen[(st, d, ok[-1])] = {'truth': truth[(st, d)]}
    art_cache = {}
    recs = []
    for (st, d, dec), r in seen.items():
        row = c.execute("select lineage_json, probabilities_json from basket_probability_envelope where station_id=? and event_date=? and stage='P0'"
                        " and decision_at_utc=? and state='COMMITTED' limit 1", (st, d, dec)).fetchone()
        if row is None: continue
        lin = json.loads(row[0]); served = json.loads(row[1])
        iv = json.loads(lin.get('bucket_intervals_c_json') or 'null')
        if not iv: continue
        order, cc, w = A.centres(iv)
        p = np.array([float(served[b]) for b in order]); p /= p.sum(); m = int(np.argmax(p))
        T = r['truth']; ti = next(i for i, b in enumerate(order) if (iv[b][0] is None or iv[b][0] <= T) and (iv[b][1] is None or T < iv[b][1]))
        if abs(ti - m) != 1 or not (0 < m < len(order) - 1): continue
        stored = json.loads(lin.get('native_system_probabilities_json') or '{}')
        runs = json.loads(lin.get('run_init_utc_by_system_json') or '{}'); arts = json.loads(lin.get('system_artifact_shas_json') or '{}')
        weights = json.loads(lin.get('system_weights_json') or '{}')
        ivs = {b: iv[b] for b in order}
        fd, fs, vecs, wts = [], [], defaultdict(dict), {}
        for s in stored:
            if s not in runs or s not in arts: continue
            vals = [float(v) for (v,) in c.execute(
                "select raw_tmax_c from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date=?"
                " and native_run_init_utc=? and archive_query_at_utc<=? group by member_id", (s, st, d, runs[s], dec))]
            if len(vals) < 2: continue
            if arts[s] not in art_cache:
                a = c.execute("select bias_parameters_json, spread_parameters_json, reliability_parameters_json from calibration_native_ensemble_system_artifact where content_sha256=?", (arts[s],)).fetchone()
                art_cache[arts[s]] = None if a is None else tuple(json.loads(x) for x in a)
            art = art_cache[arts[s]]
            if art is None: continue
            bias_p, spread_p, rel_p = art
            mean = sum(vals) / len(vals); bias = NP._resolved_bias_c(mean, bias_p); spread = float(spread_p.get('spread_factor', 1.0))
            fd.append(np.mean([v + bias > cc[m] for v in vals]) - 0.5)
            fs.append(np.mean([mean + bias + spread * (v - mean) > cc[m] for v in vals]) - 0.5)
            hist_b = NP._interval_probability(vals, ivs)
            vecs['B_raw_kernel'][s] = np.array([hist_b[b] for b in order])
            vc = NP._interval_probability([v + bias for v in vals], ivs); vecs['C_bias'][s] = np.array([vc[b] for b in order])
            vdd = NP._calibrated_system_probability(values=vals, intervals=ivs, bias_parameters=bias_p, spread_parameters=spread_p, reliability_parameters={**rel_p, 'reliability_weight': 1.0})
            vecs['D_bias_spread'][s] = np.array([vdd[b] for b in order])
            vecs['E_system'][s] = np.array([float(stored[s][b]) for b in order])
            wts[s] = float(weights.get(s, 0.0))
        if len(fd) < 3: continue
        tw = sum(wts.values()) or 1.0

        def lsplit(v):
            q = min(max(v[m + 1] / (v[m + 1] + v[m - 1] + 1e-12), 1e-3), 1 - 1e-3); return math.log(q / (1 - q))
        rec = {'day': d, 'st': st, 'y': float(ti == m + 1), 'frac_debiased': float(np.mean(fd)), 'frac_stretched': float(np.mean(fs)), 'served': lsplit(p)}
        for k in STAGES:
            rec[k] = lsplit(sum(wts[s] * vecs[k][s] for s in wts) / tw)
            # equal-weight system-average split of this stage (per-system split averaged in logit)
            rec[k + '_sysavg'] = float(np.mean([lsplit(vecs[k][s]) for s in vecs[k]]))
        recs.append(rec)
    print('shoulder rows', len(recs))
    json.dump(recs, gzip.open(HERE / f'localize_rows_d{K}.json.gz', 'wt'))

    def fit(X, y, ridge=1.0):
        b = np.zeros(X.shape[1]); R = np.diag(np.r_[0, np.ones(X.shape[1] - 1)]) * ridge
        for _ in range(60):
            pr = 1 / (1 + np.exp(-X @ b)); b += np.linalg.solve((X * (pr * (1 - pr))[:, None]).T @ X + R + 1e-9 * np.eye(len(b)), X.T @ (y - pr) - R @ b)
        return b

    def incr(base, add):
        d = np.array([x['day'] for x in recs]); y = np.array([x['y'] for x in recs])
        X0 = np.c_[np.ones(len(recs)), [x[base] for x in recs]]; X1 = np.c_[X0, [x[add] for x in recs]]
        l0 = np.full(len(recs), np.nan); l1 = l0.copy(); q0 = l0.copy(); q1 = l0.copy()
        for dd in sorted(set(d)):
            tr = d < dd; te = d == dd
            if len(set(d[tr])) < 10: continue
            for X, l, q in ((X0, l0, q0), (X1, l1, q1)):
                b = fit(X[tr], y[tr]); pr = 1 / (1 + np.exp(-X[te] @ b)); q[te] = pr; l[te] = -(y[te] * np.log(pr) + (1 - y[te]) * np.log(1 - pr))
        ok = ~np.isnan(l0); dl = (l1 - l0)[ok]; dk = d[ok]; uu = np.unique(dk); rng = np.random.default_rng(7)
        ib = {u: np.where(dk == u)[0] for u in uu}; bs = [dl[np.concatenate([ib[u] for u in rng.choice(uu, len(uu))])].mean() for _ in range(500)]
        return {'n': int(ok.sum()), 'auc_base': auc(q0[ok], y[ok]), 'auc_with': auc(q1[ok], y[ok]), 'dll': float(dl.mean()), 'lo': float(np.quantile(bs, .025)), 'hi': float(np.quantile(bs, .975))}

    y = [x['y'] for x in recs]
    print('alone AUC: frac_debiased', round(auc([x['frac_debiased'] for x in recs], y), 3), ' frac_stretched', round(auc([x['frac_stretched'] for x in recs], y), 3))
    out = {}
    for base in STAGES + [k + '_sysavg' for k in STAGES] + ['served']:
        out[base] = {}
        line = f"{base:22s} alone AUC {auc([x[base] for x in recs], y):.3f}"
        for add in ('frac_debiased', 'frac_stretched'):
            r = incr(base, add); out[base][add] = r
            flag = 'ADDS' if r['hi'] < 0 else ''
            line += f" | +{add}: dLogLoss {r['dll']:+.4f} [{r['lo']:+.4f},{r['hi']:+.4f}] {flag}"
        print(line)
    json.dump(out, open(HERE / f'readings_localize_d{K}.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main()
