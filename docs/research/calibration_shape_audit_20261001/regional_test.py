"""Do regional / high-resolution deterministic point sources carry INCREMENTAL information about the P0 centre
error at P0 time?  (user: 「信息源：区域模型是否有增量信息？」; market excluded by charter.)
Panel: direction_data.json station-days (last pre-midnight P0).  Features via production regional lane
(runs_for / feature_at / replay_curve+resolve_correction, debias fitted only on earlier event days).
Reads: RMSE vs truth; corr(d,e) with d = (R+b) - c, e = T - c; direction (3-class, +-0.5C) LODO alone and on top of
the six-system structure F1; centre shift by charter 201's walk-forward k and by LODO ridge, 211 gate."""
import json, math, pathlib, collections, sys, datetime as dt, sqlite3
import numpy as np
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / 'docs/research/model_market_gap_decomposition_20260926'))
from direction_test import features, cls, evaluate, FAMILIES, rng
from analyze import axis
import decompose as D
from bayes_basket_target import deterministic_forecast_family as DFF, regional_point_debias as RPD
from bayes_basket_target.regional_point_lane import runs_for
from bayes_basket_target.deterministic_feature_correction import CAP_C, MIN_K_EVENT_DAYS
REGIONAL = {'NCEP_HRRR_POINT': 'regional', 'NCEP_NBM_POINT': 'regional(blend)', 'DWD_ICON_EU_POINT': 'regional', 'JMA_MSM_POINT': 'regional',
            'ECMWF_IFS_9KM_RELAY_POINT': 'global-hires', 'UKMO_GLOBAL_10KM_RELAY_POINT': 'global-hires', 'CMA_GRAPES_GLOBAL_POINT': 'global-det', 'ECMWF_AIFS_SINGLE_POINT': 'global-AI-det'}
def inst(s): return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
def main():
    c = sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=2); c.execute('pragma query_only=ON')
    zones = dict(c.execute('select station_id,settlement_timezone from calibration_station_market_timezone_binding'))
    known = {(r[0], r[1]): r[2] for r in c.execute('select station_id,event_date,min(known_at_utc) from basket_station_day_settlement_label group by 1,2')}
    rows = json.load(open(HERE / 'direction_data.json')); cap = {(r['station'], r['date']): r for r in json.load(open(HERE / 'capture.json'))['rows']}
    data = []; counts = collections.Counter(); curves = {}
    for r in rows:
        f = features(r, False)
        if f is None: continue
        e = cap[(r['station'], r['date'])]['last']; keys, lo, hi, rung = axis(e['inputs']['bucket_intervals_c_json'])
        centers = [(l + h) / 2 if l is not None and h is not None else (h - 0.5 if l is None else l + 0.5) for l, h in zip(lo, hi)]; oe = (hi[0] if lo[0] is None else None, lo[-1] if hi[-1] is None else None)
        p = D.as_vec(e['probability'], keys); hit = next(j for j, (l, h) in enumerate(zip(lo, hi)) if (l is None or r['truth'] >= l) and (h is None or r['truth'] < h))
        d = dict(station=r['station'], date=r['date'], at=e['at'], e=r['e'], y=cls(r['e']), f=f, mu=r['mu'], truth=r['truth'], keys=keys, centers=centers, oe=oe, p=p, hit=hit, ver=int(r['version'].split('_v')[-1]), regional={}, regional_raw={})
        runs = runs_for(c, station_id=r['station'], event_date=r['date'], as_of_utc=e['at'])
        if runs:
            if r['date'] not in curves: curves[r['date']] = RPD.replay_curve(r['date'])
            feats = DFF.feature_at(c, station_id=r['station'], event_date=r['date'], as_of_utc=e['at'], settlement_timezone=zones[r['station']], rows=runs)
            for sysid, ft in feats.items():
                d['regional_raw'][sysid] = float(ft['day_max_c']); counts[f'{sysid}:raw'] += 1
                corr = RPD.resolve_correction(curves[r['date']], system_id=sysid, station_id=r['station'], run_init_utc=ft['run_init_utc'], lead_hours_to_local_day_start=ft['lead_hours_to_local_day_start'], forecast_level_c=ft['day_max_c'])
                if corr is None: counts[f'{sysid}:no_debias'] += 1; continue
                d['regional'][sysid] = float(ft['day_max_c']) + float(corr['bias_c']); counts[f'{sysid}:debiased'] += 1
        data.append(d)
    print('coverage:', dict(sorted(counts.items())))
    dates_all = sorted({d['date'] for d in data})
    def boot_ci(vals, groups):
        vals = np.asarray(vals, float); u = sorted(set(groups)); idx = {g: [i for i, x in enumerate(groups) if x == g] for g in u}; out = []
        for _ in range(2000):
            pick = rng.choice(len(u), len(u), replace=True); sel = np.concatenate([idx[u[j]] for j in pick]); out.append(vals[sel].mean())
        return [float(vals.mean()), float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))]
    def shift_scores(sub, deltas):
        dB = []; dL = []
        for d, dm in zip(sub, deltas):
            q = D.as_vec(D.shift_tail(dict(zip(d['keys'], d['p'])), delta_c=float(dm), keys=d['keys'], centers=d['centers'], open_edges=d['oe']), d['keys']) if abs(dm) > 1e-9 else d['p']
            l1, b1 = D.score(q, d['keys'], d['hit']); l0, b0 = D.score(d['p'], d['keys'], d['hit']); dB.append(b1 - b0); dL.append(l1 - l0)
        g = [d['date'] for d in sub]; b = boot_ci(dB, g); l = boot_ci(dL, g)
        return dict(d_brier=b, d_logscore=l, verdict='PASS' if b[2] < 0 and l[2] < 0 else 'FAIL', mean_abs_shift=float(np.mean(np.abs(deltas))))
    report = {}
    F1 = [k for k in data[0]['f'] if not k.startswith('_') and FAMILIES['F1 structure (positions, pairs, camps, dispersion)'](k)]
    for sysid, kind in REGIONAL.items():
        sub = [d for d in data if sysid in d['regional']]
        if len(sub) < 40: report[sysid] = {'kind': kind, 'n': len(sub)}; continue
        dvec = np.array([d['regional'][sysid] - d['mu'] for d in sub]); e = np.array([d['e'] for d in sub]); T = np.array([d['truth'] for d in sub]); R = np.array([d['regional'][sysid] for d in sub]); mu = np.array([d['mu'] for d in sub])
        g = [d['date'] for d in sub]
        out = dict(kind=kind, n=len(sub), n_dates=len(set(g)), n_stations=len({d['station'] for d in sub}),
                   rmse_regional=float(np.sqrt(np.mean((R - T)**2))), rmse_p0=float(np.sqrt(np.mean((mu - T)**2))), rmse_avg=float(np.sqrt(np.mean(((R + mu) / 2 - T)**2))),
                   corr_d_e=float(np.corrcoef(dvec, e)[0, 1]) if dvec.std() > 0 else None, k_insample=float((dvec * e).sum() / (dvec**2).sum()),
                   mean_abs_d=float(np.abs(dvec).mean()), regional_closer_frac=boot_ci((np.abs(R - T) < np.abs(mu - T)).astype(float), g))
        nn = np.abs(e) > 0.5; big = nn & (np.abs(dvec) >= 0.5)
        out['direction_agree_on_|e|>0.5_and_|d|>=0.5'] = dict(n=int(big.sum()), frac=boot_ci((np.sign(dvec[big]) == np.sign(e[big])).astype(float), [x for x, m in zip(g, big) if m])) if big.sum() >= 15 else {'n': int(big.sum())}
        # direction classification: regional alone, F1 alone (same rows), F1 + regional
        for d_, dv in zip(sub, dvec): d_['f']['_reg'] = float(dv)
        for d_ in sub: d_['f']['reg_d'] = d_['f']['_reg']
        if len(set(g)) >= 8:
            ev_reg = evaluate(sub, ['reg_d'], 'reg'); ev_f1 = evaluate(sub, F1, 'F1'); ev_both = evaluate(sub, F1 + ['reg_d'], 'F1+reg')
            out['direction_lodo'] = dict(regional_alone=ev_reg['d_logloss'], F1_alone=ev_f1['d_logloss'], F1_plus_regional=ev_both['d_logloss'],
                                         increment_over_F1=[ev_both['logloss_model'] - ev_f1['logloss_model']], dir_acc=dict(reg=ev_reg['direction_acc_on_warm_cold'], F1=ev_f1['direction_acc_on_warm_cold'], both=ev_both['direction_acc_on_warm_cold']))
        # charter 201 walk-forward k per station (earlier event days whose truth was knowable at decision)
        deltas = []; ks = []
        for d_ in sub:
            prior = [x for x in sub if x['station'] == d_['station'] and x['date'] < d_['date'] and (x['station'], x['date']) in known and inst(known[(x['station'], x['date'])]) <= inst(d_['at'])]
            k = 0.0
            if len({x['date'] for x in prior}) >= MIN_K_EVENT_DAYS:
                num = sum((x['e']) * (x['regional'][sysid] - x['mu']) for x in prior); den = sum((x['regional'][sysid] - x['mu'])**2 for x in prior); k = min(1.0, max(0.0, num / den)) if den > 0 else 0.0
            ks.append(k); deltas.append(max(-CAP_C, min(CAP_C, k * (d_['regional'][sysid] - d_['mu']))))
        out['shift_201_walkforward'] = shift_scores(sub, np.array(deltas)) | {'frac_k_positive': float(np.mean(np.array(ks) > 0)), 'mean_k': float(np.mean(ks))}
        # LODO pooled k (one k across stations, fit on other dates), scaled 1.0 and 0.5
        pred = np.zeros(len(sub))
        for dte in sorted(set(g)):
            te = np.array([x == dte for x in g]); tr = ~te; den = (dvec[tr]**2).sum(); k = min(1.0, max(0.0, (dvec[tr] * e[tr]).sum() / den)) if den > 0 else 0.0; pred[te] = k * dvec[te]
        out['shift_lodo_pooled_k_x1'] = shift_scores(sub, np.clip(pred, -CAP_C, CAP_C)); out['shift_lodo_pooled_k_x0.5'] = shift_scores(sub, np.clip(0.5 * pred, -CAP_C, CAP_C))
        out['shift_fixed_k0.25'] = shift_scores(sub, np.clip(0.25 * dvec, -CAP_C, CAP_C))
        v19 = [d_ for d_ in sub if d_['ver'] >= 19]
        if len(v19) >= 30:
            dv = np.array([d_['regional'][sysid] - d_['mu'] for d_ in v19]); out['v19plus'] = dict(n=len(v19), n_dates=len({d_['date'] for d_ in v19}), corr_d_e=float(np.corrcoef(dv, [d_['e'] for d_ in v19])[0, 1]), shift_fixed_k0_25=shift_scores(v19, np.clip(0.25 * dv, -CAP_C, CAP_C)))
        report[sysid] = out
    # all regional-class sources combined as one "best available regional" feature: mean of available debiased regional (strict regional only)
    strict = [s for s, k in REGIONAL.items() if k.startswith('regional')]
    sub = [d for d in data if any(s in d['regional'] for s in strict)]
    for d_ in sub: d_['f']['reg_d'] = float(np.mean([d_['regional'][s] for s in strict if s in d_['regional']]) - d_['mu'])
    dvec = np.array([d_['f']['reg_d'] for d_ in sub]); e = np.array([d_['e'] for d_ in sub]); g = [d_['date'] for d_ in sub]
    comb = dict(n=len(sub), n_dates=len(set(g)), n_stations=len({d_['station'] for d_ in sub}), corr_d_e=float(np.corrcoef(dvec, e)[0, 1]), k_insample=float((dvec * e).sum() / (dvec**2).sum()))
    ev_reg = evaluate(sub, ['reg_d'], 'reg'); ev_f1 = evaluate(sub, F1, 'F1'); ev_both = evaluate(sub, F1 + ['reg_d'], 'F1+reg')
    comb['direction_lodo'] = dict(regional_alone=ev_reg['d_logloss'], F1_alone=ev_f1['d_logloss'], F1_plus_regional=ev_both['d_logloss'], dir_acc=dict(reg=ev_reg['direction_acc_on_warm_cold'], F1=ev_f1['direction_acc_on_warm_cold'], both=ev_both['direction_acc_on_warm_cold']))
    pred = np.zeros(len(sub))
    for dte in sorted(set(g)):
        te = np.array([x == dte for x in g]); tr = ~te; den = (dvec[tr]**2).sum(); k = min(1.0, max(0.0, (dvec[tr] * e[tr]).sum() / den)) if den > 0 else 0.0; pred[te] = k * dvec[te]
    comb['shift_lodo_pooled_k_x1'] = shift_scores(sub, np.clip(pred, -CAP_C, CAP_C)); comb['shift_lodo_pooled_k_x0.5'] = shift_scores(sub, np.clip(0.5 * pred, -CAP_C, CAP_C)); comb['shift_fixed_k0.25'] = shift_scores(sub, np.clip(0.25 * dvec, -CAP_C, CAP_C))
    report['COMBINED_strict_regional_mean'] = comb
    json.dump(report, open(HERE / 'results_regional.json', 'w'), indent=1, default=float)
    f3 = lambda v: '%+.4f [%+.4f,%+.4f]' % tuple(v)
    for s, o in report.items():
        print('##', s, o.get('kind', ''), 'n', o['n'], 'dates', o.get('n_dates'), 'stations', o.get('n_stations'))
        if 'corr_d_e' not in o: continue
        if 'rmse_regional' in o: print('   RMSE regional %.2f  P0 %.2f  avg %.2f | regional closer %s | corr(d,e) %+.3f  k* %+.2f  mean|d| %.2f | dir agree on big |e|,|d|: %s' % (o['rmse_regional'], o['rmse_p0'], o['rmse_avg'], f3(o['regional_closer_frac']), o['corr_d_e'], o['k_insample'], o['mean_abs_d'], o['direction_agree_on_|e|>0.5_and_|d|>=0.5']))
        else: print('   corr(d,e) %+.3f k* %+.2f' % (o['corr_d_e'], o['k_insample']))
        if 'direction_lodo' in o:
            dl = o['direction_lodo']; print('   direction LODO dLogLoss: regional alone %s | F1 alone %s | F1+regional %s | dir.acc reg %.2f F1 %.2f both %.2f' % (f3(dl['regional_alone']), f3(dl['F1_alone']), f3(dl['F1_plus_regional']), dl['dir_acc']['reg'], dl['dir_acc']['F1'], dl['dir_acc']['both']))
        for k in ('shift_201_walkforward', 'shift_lodo_pooled_k_x1', 'shift_lodo_pooled_k_x0.5', 'shift_fixed_k0.25'):
            if k in o: v = o[k]; print('   %-26s dBrier %s dLog %s -> %s  |shift| %.2f %s' % (k, f3(v['d_brier']), f3(v['d_logscore']), v['verdict'], v['mean_abs_shift'], ('k>0 %.0f%% mean k %.2f' % (100 * v['frac_k_positive'], v['mean_k'])) if 'mean_k' in v else ''))
        if 'v19plus' in o: v = o['v19plus']; print('   v19+: n %d dates %d corr(d,e) %+.3f  fixed k0.25: dBrier %s dLog %s -> %s' % (v['n'], v['n_dates'], v['corr_d_e'], f3(v['shift_fixed_k0_25']['d_brier']), f3(v['shift_fixed_k0_25']['d_logscore']), v['shift_fixed_k0_25']['verdict']))
if __name__ == '__main__': main()
