"""Which post-pool layer of P0 drops the shoulder information at D-1 / D-2?

For P0s decided before local 09:00 of D-K (K = 0, 1, 2), rebuild after the pool with production's own functions:
  pool      = sum(w_s x stored system vector)                      (lineage weights)
  +bridge   = cross_consensus.bridged_cross_probability            (when the P0's lineage carries a bridge receipt)
  +radiation= native_ensemble_p0._shift_probability_c by the receipt's shift_c
  served    = the stored P0 (= +radiation through the table layers: width lambda / thin-far slice tail mix / F far tail / far-peak ceiling, then the 1e-3 floor)
Reference bucket m = the served P0's mode; truth in m-1 / m+1.  For each stage: which-shoulder AUC, and the walk-forward log-loss gain of the
member fraction (certificate-debiased members above the centre of m) on top of that stage.  Also: max bucket probability of each stage
(the ceiling caps at 0.45) and how often the ceiling binds.
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
import analyze as A  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as NP  # noqa: E402
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from pipeline_audit import auc  # noqa: E402

SSOT = ROOT / 'data/bayes_basket_target_ssot.db'


def main(K):
    inp = sqlite3.connect(ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db')
    tzs = dict(inp.execute('select station_id, settlement_timezone from tz'))
    truth = {}
    for st, d, t, rev in inp.execute('select station_id, local_date, official_tmax_c, revision_no from truth order by revision_no'): truth[(st, d)] = float(t)
    by = defaultdict(list)
    for st, d, dec in inp.execute("select station_id, event_date, decision_at from root where stage='P0' and event_date between '2026-09-01' and '2026-10-14'"):
        by[(st, d)].append(dec)
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
        row = c.execute("select lineage_json, probabilities_json from basket_probability_envelope where station_id=? and event_date=? and stage='P0'"
                        " and decision_at_utc=? and state='COMMITTED' limit 1", (st, d, dec)).fetchone()
        if row is None: continue
        lin = json.loads(row[0]); served = json.loads(row[1])
        iv = json.loads(lin.get('bucket_intervals_c_json') or 'null')
        sysp = json.loads(lin.get('native_system_probabilities_json') or '{}')
        wts = json.loads(lin.get('system_weights_json') or '{}')
        cen = json.loads(lin.get('native_calibrated_system_centers_c_json') or '{}')
        if not iv or not sysp or not wts: continue
        ordered = NP._ordered_buckets(iv)
        if any(set(v) != set(ordered) for v in sysp.values()): stats['support'] += 1; continue
        T = truth[(st, d)]
        ti = next(i for i, b in enumerate(ordered) if (iv[b][0] is None or iv[b][0] <= T) and (iv[b][1] is None or T < iv[b][1]))
        sv = np.array([float(served[b]) for b in ordered]); sv /= sv.sum(); m = int(np.argmax(sv))
        systems = [s for s in sysp if float(wts.get(s, 0)) > 0]
        tw = sum(float(wts[s]) for s in systems)
        pool = {b: sum(float(wts[s]) * float(sysp[s][b]) for s in systems) / tw for b in ordered}
        stages = {'pool': pool}
        if lin.get('consensus_bridge_json') and all(s in cen for s in systems):
            br, _ = bridged_cross_probability(system_probabilities={s: sysp[s] for s in systems}, system_weights={s: float(wts[s]) / tw for s in systems},
                                              system_centers_c={s: float(cen[s]) for s in systems}, ordered_keys=ordered, key_centers_c=NP._bucket_centers(iv, ordered))
            stats['bridge'] += 1
        else:
            br = pool
        stages['bridge'] = br
        rad = json.loads(lin.get('synoptic_cooling_json') or '{}') if lin.get('synoptic_cooling_json') else {}
        shift = float(rad.get('shift_c') or 0.0) if rad.get('state') == 'RESOLVED' else 0.0
        if abs(shift) > 1e-9:
            shifted = [v + shift for v in NP._bucket_centers(iv, ordered)]
            stages['radiation'] = NP._shift_probability_c(br, ordered=ordered, bucket_centers_c=shifted, intervals=iv)
            stats['radiation'] += 1
        else:
            stages['radiation'] = br
        stages['served'] = {b: float(sv[i]) for i, b in enumerate(ordered)}
        # member fraction around the served mode centre
        order_c, cc, w = A.centres(iv)
        if list(order_c) != list(ordered): stats['order_mismatch'] += 1; continue
        runs = json.loads(lin.get('run_init_utc_by_system_json') or '{}'); arts = json.loads(lin.get('system_artifact_shas_json') or '{}')
        fr = []
        for s in systems:
            if s not in runs or s not in arts: continue
            vals = [float(v) for (v,) in c.execute(
                "select raw_tmax_c from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date=?"
                " and native_run_init_utc=? and archive_query_at_utc<=? group by member_id", (s, st, d, runs[s], dec))]
            if len(vals) < 2: continue
            if arts[s] not in art_cache:
                a = c.execute('select bias_parameters_json from calibration_native_ensemble_system_artifact where content_sha256=?', (arts[s],)).fetchone()
                art_cache[arts[s]] = json.loads(a[0]) if a else None
            if art_cache[arts[s]] is None: continue
            bias = NP._resolved_bias_c(sum(vals) / len(vals), art_cache[arts[s]])
            fr.append(np.mean([v + bias > cc[m] for v in vals]) - 0.5)
        rec = {'st': st, 'day': d, 'm': m, 'ti': ti, 'frac': float(np.mean(fr)) if len(fr) >= 3 else None,
               'stages': {k: [float(v[b]) for b in ordered] for k, v in stages.items()}}
        recs.append(rec)
    print(f'D-{K}: P0s {len(recs)}', dict(stats))
    json.dump(recs, gzip.open(HERE / f'post_pool_rows_d{K}.json.gz', 'wt'))
    sh = [x for x in recs if abs(x['ti'] - x['m']) == 1 and 0 < x['m'] < len(x['stages']['served']) - 1 and x['frac'] is not None]
    y = np.array([float(x['ti'] == x['m'] + 1) for x in sh]); d_arr = np.array([x['day'] for x in sh])

    def lsplit(v, m):
        q = min(max(v[m + 1] / (v[m + 1] + v[m - 1] + 1e-12), 1e-3), 1 - 1e-3); return math.log(q / (1 - q))

    def fit(X, yy, ridge=1.0):
        b = np.zeros(X.shape[1]); R = np.diag(np.r_[0, np.ones(X.shape[1] - 1)]) * ridge
        for _ in range(60):
            pr = 1 / (1 + np.exp(-X @ b)); b += np.linalg.solve((X * (pr * (1 - pr))[:, None]).T @ X + R + 1e-9 * np.eye(len(b)), X.T @ (yy - pr) - R @ b)
        return b
    out = {'K': K, 'n_p0': len(recs), 'n_shoulder': len(sh), 'stages': {}}
    fr = np.array([x['frac'] for x in sh])
    for k in ('pool', 'bridge', 'radiation', 'served'):
        z = np.array([lsplit(x['stages'][k], x['m']) for x in sh])
        X0 = np.c_[np.ones(len(sh)), z]; X1 = np.c_[X0, fr]
        l0 = np.full(len(sh), np.nan); l1 = l0.copy()
        for dd in sorted(set(d_arr)):
            tr = d_arr < dd; te = d_arr == dd
            if len(set(d_arr[tr])) < 10: continue
            for X, l in ((X0, l0), (X1, l1)):
                b = fit(X[tr], y[tr]); pr = 1 / (1 + np.exp(-X[te] @ b)); l[te] = -(y[te] * np.log(pr) + (1 - y[te]) * np.log(1 - pr))
        ok = ~np.isnan(l0); dl = (l1 - l0)[ok]; dk = d_arr[ok]; uu = np.unique(dk); rng = np.random.default_rng(7)
        ib = {u: np.where(dk == u)[0] for u in uu}; bs = [dl[np.concatenate([ib[u] for u in rng.choice(uu, len(uu))])].mean() for _ in range(500)]
        mx = np.array([max(x['stages'][k]) for x in recs]); pm = np.array([x['stages'][k][x['m']] for x in recs])
        mode_hit = np.mean([x['ti'] == x['m'] for x in recs])
        ls = np.mean([-math.log(max(x['stages'][k][x['ti']], 1e-3)) for x in recs])
        out['stages'][k] = {'auc': auc(z, y), 'frac_dll': float(dl.mean()), 'lo': float(np.quantile(bs, .025)), 'hi': float(np.quantile(bs, .975)),
                            'mean_max_bucket': float(mx.mean()), 'share_max_ge_0.449': float((mx >= 0.449).mean()), 'p_mode': float(pm.mean()), 'logscore': float(ls)}
        print(f"  {k:10s} which-shoulder AUC {auc(z, y):.3f}  +frac dLogLoss {dl.mean():+.4f} [{np.quantile(bs, .025):+.4f},{np.quantile(bs, .975):+.4f}]"
              f"  max bucket {mx.mean():.3f} (>=0.449 on {(mx >= 0.449).mean():.1%})  p(served mode) {pm.mean():.3f} realised {mode_hit:.3f}  LogScore {ls:.3f}")
    json.dump(out, open(HERE / f'readings_post_pool_d{K}.json', 'w'), indent=1, default=float)


if __name__ == '__main__':
    main(int(sys.argv[1]))
