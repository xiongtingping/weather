"""Follow-ups: outliers, far-lead by version, disagreement days, spread-skill slope,
sigma-tier extra-lambda diagnostic (calls the production width function, leave-one-date-out)."""
import json, math, pathlib, collections, sys
import numpy as np
from scipy import stats
sys.path.insert(0, str(pathlib.Path(__file__).parents[3]))
from analyze import collect, shape_report, block_boot, wmean, wfrac, floor_ls, rng
from bayes_basket_target.center_width_calibration import apply_center_width_calibration, served_bucket_axis
from bayes_basket_target.materialization import floor_probabilities
OUT = pathlib.Path(__file__).parent
D = json.load(open(OUT / 'capture.json'))
raw = {(r['station'], r['date']): r for r in D['rows']}
rep = {}
last = collect('last'); first = [r for r in collect('first') if r['hours'] > 36]

# A. outliers
def outliers(rows, tag):
    z = np.array([(r['truth'] - r['mu']) / r['sd'] for r in rows]); zs = (z - z.mean()) / z.std()
    out = []
    for r, zz in zip(rows, zs):
        if abs(zz) > 3 or r['p_truth'] < 1e-3:
            e = raw[(r['station'], r['date'])][tag]; mem = e.get('members') or {}
            vals = [np.array(m['values']) for k, m in mem.items() if m and r['weights'].get(k, 0) > 0]
            allm = np.concatenate(vals) if vals else np.array([])
            out.append(dict(station=r['station'], date=r['date'], truth=r['truth'], mu=round(r['mu'], 2), sd=round(r['sd'], 2), zs=round(float(zz), 2),
                            p_truth=float(f"{r['p_truth']:.2e}"), version=r['version'].split('_')[-1], truth_rev=raw[(r['station'], r['date'])]['truth_revision'],
                            lane=raw[(r['station'], r['date'])]['truth_lane'], hours=round(r['hours'], 1),
                            members_min=float(allm.min()) if len(allm) else None, members_max=float(allm.max()) if len(allm) else None,
                            frac_members_beyond_truth=float(np.mean(allm >= r['truth']) if r['truth'] > r['mu'] else np.mean(allm <= r['truth'])) if len(allm) else None,
                            centers={k: round(v, 1) for k, v in r['centers'].items() if r['weights'].get(k, 0) > 0}))
    return sorted(out, key=lambda x: -abs(x['zs']))
rep['outliers_last'] = outliers(last, 'last'); rep['outliers_first_gt36h'] = outliers(first, 'first')

# B. far lead by version family / by hours
fam = lambda r: 'v2-v7' if int(r['version'].split('_v')[-1]) <= 7 else 'v8-v15' if int(r['version'].split('_v')[-1]) <= 15 else 'v19+'
def brief(rows, label):
    if len(rows) < 25: return {'n': len(rows)}
    s = shape_report(rows, label)
    return {k: s[k] for k in ('n', 'n_dates', 'n_stations', 'bias_c', 'z_meansq', 'pit_var_ratio', 'cover_50', 'cover_90', 'z_skew', 'z_excess_kurt', 'frac_|zs|>3', 'lowprob<0.01_realised_vs_served', 'logscore_floor1e-3')}
rep['first_gt36h_by_version'] = {k: brief([r for r in first if fam(r) == k], k) for k in ('v2-v7', 'v8-v15', 'v19+')}
rep['first_gt36h_by_hours'] = {k: brief([r for r in first if lo <= r['hours'] < hi], k) for k, lo, hi in (('36-48h', 36, 48), ('48-60h', 48, 60), ('>=60h', 60, 999))}
rep['first_gt36h_by_nsystems'] = {k: brief([r for r in first if (sum(1 for w in r['weights'].values() if w > 0) >= 4) == (k == '>=4 systems')], k) for k in ('<=3 systems', '>=4 systems')}
rep['last_recent_v19plus_by_unit'] = {u: brief([r for r in last if fam(r) == 'v19+' and r['unit'] == u], u) for u in ('C', 'F')}

# C. disagreement days: warm vs cold system centre
def disagreement(rows, label):
    out = {}; bins = collections.defaultdict(list); warmwins = collections.Counter(); warmsys = collections.Counter(); coldsys = collections.Counter()
    for r in rows:
        cs = {k: v for k, v in r['centers'].items() if r['weights'].get(k, 0) > 0}
        if len(cs) < 2: continue
        gap = max(cs.values()) - min(cs.values()); wk = max(cs, key=cs.get); ck = min(cs, key=cs.get)
        b = '<1C' if gap < 1 else '1-2C' if gap < 2 else '>=2C'
        bins[b].append(r)
        if gap >= 1.0:
            dw = abs(r['truth'] - cs[wk]); dc = abs(r['truth'] - cs[ck])
            warmwins[b, 'warm' if dw < dc else 'cold'] += 1; warmsys[wk] += 1; coldsys[ck] += 1
            if dw < dc: warmsys[wk, 'win'] += 1
            else: coldsys[ck, 'win'] += 1
    for b, rr in bins.items():
        s = shape_report(rr, b) if len(rr) >= 25 else None
        out[b] = dict(n=len(rr), bias_c=s['bias_c'] if s else None, z_meansq=s['z_meansq'] if s else None, logscore=s['logscore_floor1e-3'] if s else None,
                      warm_centre_closer=warmwins[b, 'warm'], cold_centre_closer=warmwins[b, 'cold'])
    out['warmest_system_counts'] = {k: v for k, v in warmsys.items() if isinstance(k, str)}
    out['warmest_system_wins'] = {k[0]: v for k, v in warmsys.items() if isinstance(k, tuple)}
    out['coldest_system_counts'] = {k: v for k, v in coldsys.items() if isinstance(k, str)}
    out['coldest_system_wins'] = {k[0]: v for k, v in coldsys.items() if isinstance(k, tuple)}
    return out
rep['disagreement_last'] = disagreement(last, 'last'); rep['disagreement_first_gt36h'] = disagreement(first, 'first')

# D. spread-skill slope & sigma-tier extra-lambda diagnostic
def spread_skill(rows, label):
    sd2 = np.array([r['sd']**2 - r['rung']**2 / 12 for r in rows]); res2 = np.array([(r['truth'] - r['mu'])**2 for r in rows]); dates = [r['date'] for r in rows]
    def slope(w):
        W = np.asarray(w, float); X = np.vstack([np.ones_like(sd2), sd2]).T * np.sqrt(W)[:, None]; y = res2 * np.sqrt(W)
        b = np.linalg.lstsq(X, y, rcond=None)[0]; return float(b[1])
    def intercept(w):
        W = np.asarray(w, float); X = np.vstack([np.ones_like(sd2), sd2]).T * np.sqrt(W)[:, None]; y = res2 * np.sqrt(W)
        return float(np.linalg.lstsq(X, y, rcond=None)[0][0])
    out = dict(n=len(rows), slope_res2_on_sd2=block_boot(dates, slope), intercept_c2=block_boot(dates, intercept))
    qs = np.percentile(np.sqrt(np.maximum(sd2, 0)), [0, 25, 50, 75, 100]); rows_bins = []
    for a, b in zip(qs[:-1], qs[1:]):
        m = (np.sqrt(np.maximum(sd2, 0)) >= a) & (np.sqrt(np.maximum(sd2, 0)) <= b)
        rows_bins.append(dict(served_sd_range=[round(float(a), 2), round(float(b), 2)], n=int(m.sum()), mean_served_sd=float(np.sqrt(sd2[m].mean())), rms_res=float(np.sqrt(res2[m].mean())),
                              ratio_rms_over_sd=float(np.sqrt(res2[m].mean() / sd2[m].mean()))))
    out['quartiles'] = rows_bins
    return out
rep['spread_skill_last'] = spread_skill(last, 'last'); rep['spread_skill_first_gt36h'] = spread_skill(first, 'first')

def tier_diagnostic(rows, label, boundaries=(1.44, 2.0), grid=np.round(np.arange(0.6, 1.41, 0.05), 2)):
    """Extra scale lambda' applied on top of the served vector, per served-sigma tier; leave-one-date-out.
    Pure diagnostic read of which direction each tier wants; not the lane's fit."""
    pre = []
    for r in rows:
        e = raw[(r['station'], r['date'])]['last' if label == 'last' else 'first']; iv = e['inputs']['bucket_intervals_c_json']
        keys = r['keys']; centers, edges = served_bucket_axis(iv, keys); p = {k: float(v) for k, v in zip(keys, r['pv'])}
        sig_rungs = math.sqrt(max(r['sd']**2 - r['rung']**2 / 12, 0)) / r['rung']
        tier = 0 if sig_rungs < boundaries[0] else 1 if sig_rungs < boundaries[1] else 2
        ls = {}; br = {}
        for lam in grid:
            try:
                q, _ = apply_center_width_calibration(p, delta_c=0.0, width_lambda=float(lam), ordered_keys=keys, key_centers_c=centers, open_edge_bounds_c=edges)
            except ValueError:
                continue
            qf, _ = floor_probabilities(q); tot = sum(qf.values()); qv = np.array([qf[k] / tot for k in keys])
            ls[float(lam)] = -math.log(qv[r['idx']]); y = np.zeros(len(keys)); y[r['idx']] = 1; br[float(lam)] = float(((qv - y)**2).sum())
        pre.append(dict(date=r['date'], tier=tier, ls=ls, br=br))
    out = {}; dates = sorted({x['date'] for x in pre})
    for t, name in enumerate(('narrow', 'mid', 'wide')):
        sub = [x for x in pre if x['tier'] == t]
        if len(sub) < 30: out[name] = {'n': len(sub)}; continue
        chosen = []; dls = []; dbr = []; dd = []
        for d in dates:
            train = [x for x in sub if x['date'] != d]; test = [x for x in sub if x['date'] == d]
            if not test or not train: continue
            lam = min(grid, key=lambda l: np.mean([x['ls'][float(l)] for x in train if float(l) in x['ls']]) if all(float(l) in x['ls'] for x in train) else 9)
            chosen.append(float(lam))
            for x in test: dls.append(x['ls'][float(lam)] - x['ls'][1.0]); dbr.append(x['br'][float(lam)] - x['br'][1.0]); dd.append(d)
        dls = np.array(dls); dbr = np.array(dbr)
        insample = min(grid, key=lambda l: np.mean([x['ls'][float(l)] for x in sub]))
        out[name] = dict(n=len(sub), n_dates=len(set(dd)), insample_best_extra_lambda=float(insample), loo_chosen_lambda_range=[min(chosen), max(chosen)],
                         loo_delta_logscore=block_boot(dd, lambda w: wmean(dls, w)), loo_delta_brier=block_boot(dd, lambda w: wmean(dbr, w)),
                         mean_sigma_rungs_boundaries=list(boundaries))
    return out
rep['extra_lambda_by_sigma_tier_last'] = tier_diagnostic(last, 'last'); rep['extra_lambda_by_sigma_tier_first_gt36h'] = tier_diagnostic(first, 'first')
# whole-sample extra lambda (sanity: should be ~1.0 if the lane's 0.75 is right on this sample)
rep['extra_lambda_all_last'] = tier_diagnostic(last, 'last', boundaries=(0.0, 0.0))['wide']
rep['extra_lambda_all_first_gt36h'] = tier_diagnostic(first, 'first', boundaries=(0.0, 0.0))['wide']
(OUT / 'results2.json').write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=float))
def fmt(v):
    if isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v): return f"{v[0]:+.3f} [{v[1]:+.3f},{v[2]:+.3f}]"
    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], list): return fmt(v[0]) + f" vs ref {v[1]:.4f}"
    return json.dumps(v, ensure_ascii=False, default=float)
for k, v in rep.items():
    print('##', k)
    if isinstance(v, list):
        for x in v: print('  ', json.dumps(x, ensure_ascii=False, default=float))
    elif isinstance(v, dict) and all(isinstance(x, dict) for x in v.values()):
        for g, d in v.items():
            print('  --', g)
            for kk, vv in d.items(): print(f'     {kk}: {fmt(vv)}')
    else:
        for kk, vv in v.items(): print(f'   {kk}: {fmt(vv)}')
