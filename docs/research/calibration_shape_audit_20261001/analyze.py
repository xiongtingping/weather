"""Five-step shape audit of the served P0 distribution (read-only, from capture.json).

1 mean bias  2 width  3 asymmetry after (mu,sigma)  4 tails after (mu,sigma)  5 member clusters.
Reference distribution: served bucket vector read as piecewise-uniform density (open edges:
exponential tail, scale = one rung).  Uncertainty: date-block bootstrap (dates are the clusters).
"""
import json, math, pathlib, collections, sys
import numpy as np
from scipy import stats
OUT = pathlib.Path(__file__).parent
rng = np.random.default_rng(20261001)
D = json.load(open(OUT / 'capture.json'))

def axis(intervals):
    items = sorted(intervals.items(), key=lambda kv: (-1e9 if kv[1][0] is None else kv[1][0]))
    keys = [k for k, _ in items]; lo = [v[0] for _, v in items]; hi = [v[1] for _, v in items]
    widths = [h - l for l, h in zip(lo, hi) if l is not None and h is not None]
    return keys, lo, hi, float(np.median(widths))

def cont(p, keys, lo, hi, rung, truth):
    """Mean, sigma, PIT, served p at truth, bucket index of truth, served skew of the distribution."""
    pv = np.array([max(float(p.get(k, 0.0)), 0.0) for k in keys]); pv = pv / pv.sum()
    m = np.zeros(len(keys)); v = np.zeros(len(keys))
    for i, (l, h) in enumerate(zip(lo, hi)):
        if l is None: m[i], v[i] = h - rung, rung**2
        elif h is None: m[i], v[i] = l + rung, rung**2
        else: m[i], v[i] = (l + h) / 2, (h - l)**2 / 12
    mu = float((pv * m).sum()); var = float((pv * (v + (m - mu)**2)).sum())
    # third central moment: uniform pieces contribute only via their centres; exponential tails add skew (small)
    m3 = float((pv * ((m - mu)**3 + 3 * (m - mu) * v)).sum())
    F = np.concatenate([[0.0], np.cumsum(pv)])
    idx = None
    for i, (l, h) in enumerate(zip(lo, hi)):
        if (l is None or truth >= l) and (h is None or truth < h): idx = i; break
    if idx is None: idx = len(keys) - 1
    l, h = lo[idx], hi[idx]
    if l is None: frac = math.exp((truth - h) / rung)
    elif h is None: frac = 1 - math.exp(-(truth - l) / rung)
    else: frac = (truth - l) / (h - l)
    u = float(F[idx] + pv[idx] * frac)
    return mu, math.sqrt(var), m3 / var**1.5 if var > 0 else 0.0, u, float(pv[idx]), idx, pv

def floor_ls(pv, idx, floor=1e-3):
    q = np.maximum(pv, floor); q = q / q.sum(); return -math.log(q[idx])

def block_boot(dates, fn, n=2000):
    """fn(mask)->stat ; resample dates with replacement."""
    ud = sorted(set(dates)); dates = np.array(dates); vals = []
    base = fn(np.ones(len(dates), bool))
    for _ in range(n):
        pick = rng.choice(ud, len(ud), replace=True)
        cnt = collections.Counter(pick); w = np.array([cnt.get(d, 0) for d in dates], float)
        vals.append(fn(w))
    lo, hi = np.percentile(vals, [2.5, 97.5]); return base, float(lo), float(hi)

def wmean(x, w): return float((x * w).sum() / w.sum())
def wvar(x, w): m = wmean(x, w); return float((w * (x - m)**2).sum() / w.sum())
def wskew(x, w): m = wmean(x, w); s = math.sqrt(wvar(x, w)); return float((w * ((x - m) / s)**3).sum() / w.sum())
def wkurt(x, w): m = wmean(x, w); s = math.sqrt(wvar(x, w)); return float((w * ((x - m) / s)**4).sum() / w.sum() - 3)
def wfrac(b, w): return float((b * w).sum() / w.sum())

def collect(tag, which='probability', require_cw=False):
    rows = []
    for r in D['rows']:
        e = r.get(tag)
        if not e or 'rejected' in e: continue
        inp = e['inputs']; cw = inp.get('center_width_json')
        if which == 'input':
            if not isinstance(cw, dict) or not cw.get('input_probability'): continue
            p = cw['input_probability']
        else:
            if require_cw and not isinstance(cw, dict): continue
            p = e['probability']
        keys, lo, hi, rung = axis(inp['bucket_intervals_c_json'])
        if set(keys) != set(p): continue
        mu, sd, sk, u, pt, idx, pv = cont(p, keys, lo, hi, rung, r['truth_c'])
        rows.append(dict(station=r['station'], date=r['date'], truth=r['truth_c'], mu=mu, sd=sd, served_skew=sk, u=u, p_truth=pt,
                         idx=idx, pv=pv, keys=keys, rung=rung, ls=floor_ls(pv, idx), hours=e['hours_to_midnight'],
                         unit='F' if rung < 1.2 and rung > 1.0 else ('C' if abs(rung - 1) < 1e-6 else '?'),
                         lam=(cw or {}).get('width_lambda') if isinstance(cw, dict) else None,
                         delta=(cw or {}).get('delta_c') if isinstance(cw, dict) else None,
                         weights=inp.get('system_weights_json') or {}, centers=inp.get('native_calibrated_system_centers_c_json') or {},
                         members=e.get('members') or {}, version=e['posterior_version'],
                         bridge=inp.get('consensus_bridge_json') or {}))
    return rows

def shape_report(rows, label):
    dates = [r['date'] for r in rows]; n = len(rows)
    res = np.array([r['truth'] - r['mu'] for r in rows]); sd = np.array([r['sd'] for r in rows]); rung = np.array([r['rung'] for r in rows])
    z = res / sd; u = np.array([r['u'] for r in rows]); ls = np.array([r['ls'] for r in rows])
    out = {'label': label, 'n': n, 'n_dates': len(set(dates)), 'n_stations': len({r['station'] for r in rows})}
    # 1 mean
    out['bias_c'] = block_boot(dates, lambda w: wmean(res, w))
    out['bias_rungs'] = block_boot(dates, lambda w: wmean(res / rung, w))
    out['bias_z'] = block_boot(dates, lambda w: wmean(z, w))
    out['pit_mean_minus_half'] = block_boot(dates, lambda w: wmean(u, w) - 0.5)
    out['median_res_c'] = float(np.median(res))
    # 2 width
    out['rms_res_c'] = float(np.sqrt(np.mean(res**2))); out['rms_sd_c'] = float(np.sqrt(np.mean(sd**2))); out['mean_sd_c'] = float(sd.mean())
    out['z_meansq'] = block_boot(dates, lambda w: wmean(z**2, w))          # 1.0 if width right
    out['z_sd_after_bias'] = block_boot(dates, lambda w: math.sqrt(wvar(z, w)))
    out['pit_var_ratio'] = block_boot(dates, lambda w: wvar(u, w) * 12)   # 1.0 if calibrated
    for c in (0.5, 0.8, 0.9, 0.95):
        out[f'cover_{int(c*100)}'] = block_boot(dates, lambda w, c=c: wfrac((u >= (1-c)/2) & (u <= 1-(1-c)/2), w))
    # 3 asymmetry after mu,sigma
    zs = (z - z.mean()) / z.std()
    out['z_skew'] = block_boot(dates, lambda w: wskew(z, w))
    out['z_bowley'] = float((np.percentile(zs, 75) + np.percentile(zs, 25) - 2 * np.median(zs)) / (np.percentile(zs, 75) - np.percentile(zs, 25)))
    out['z_lt_-1_vs_gt_1'] = [float((zs < -1).mean()), float((zs > 1).mean())]
    out['z_lt_-2_vs_gt_2'] = [float((zs < -2).mean()), float((zs > 2).mean())]
    out['pit_lt_0.1_vs_gt_0.9'] = [float((u < .1).mean()), float((u > .9).mean())]
    out['pit_lt_0.05_vs_gt_0.95'] = [float((u < .05).mean()), float((u > .95).mean())]
    out['served_skew_mean'] = float(np.mean([r['served_skew'] for r in rows]))
    out['served_skew_abs_gt_0.3'] = float(np.mean([abs(r['served_skew']) > 0.3 for r in rows]))
    # 4 tails after mu,sigma
    out['z_excess_kurt'] = block_boot(dates, lambda w: wkurt(z, w))
    for t in (2, 2.5, 3):
        out[f'frac_|zs|>{t}'] = (block_boot(dates, lambda w, t=t: wfrac(np.abs(zs) > t, w)), float(2 * stats.norm.sf(t)))
    out['pit_tail_0.025_each'] = [float((u < .025).mean()), float((u > .975).mean())]
    # low-probability bucket hits: realised vs served
    for thr in (1e-3, 1e-2, 5e-2):
        hit = np.array([r['p_truth'] < thr for r in rows]); exp = np.array([r['pv'][r['pv'] < thr].sum() for r in rows])
        out[f'lowprob<{thr}_realised_vs_served'] = (block_boot(dates, lambda w, hit=hit: wfrac(hit, w)), float(exp.mean()))
    out['logscore_floor1e-3'] = block_boot(dates, lambda w: wmean(ls, w))
    out['hit_truth_mode'] = float(np.mean([r['idx'] == int(np.argmax(r['pv'])) for r in rows]))
    return out

def by_group(rows, keyfn, label):
    g = collections.defaultdict(list)
    for r in rows: g[keyfn(r)].append(r)
    out = {}
    for k, v in sorted(g.items(), key=lambda kv: str(kv[0])):
        if len(v) < 30: continue
        s = shape_report(v, f'{label}={k}')
        out[str(k)] = {kk: s[kk] for kk in ('n', 'n_dates', 'bias_c', 'z_meansq', 'pit_var_ratio', 'cover_90', 'z_skew', 'z_excess_kurt', 'frac_|zs|>2', 'pit_lt_0.05_vs_gt_0.95', 'logscore_floor1e-3')}
    return out

def station_t(rows):
    g = collections.defaultdict(list)
    for r in rows: g[r['station']].append(r['truth'] - r['mu'])
    t = {s: (np.mean(v) / (np.std(v, ddof=1) / math.sqrt(len(v))), len(v), float(np.mean(v))) for s, v in g.items() if len(v) >= 10}
    big = {s: round(v[2], 2) for s, v in t.items() if abs(v[0]) > 2}
    return {'n_stations': len(t), 'n_abs_t_gt_2': len(big), 'expected_at_5pct': round(0.05 * len(t), 1), 'stations_abs_t_gt_2_bias_c': big,
            'station_bias_sd_c': float(np.std([v[2] for v in t.values()])), 'station_bias_mean_c': float(np.mean([v[2] for v in t.values()]))}

# ---------- step 5: clusters ----------
def gmm2_bic_gain(x):
    """BIC(1 gaussian) - BIC(2-gaussian mixture, EM). >0 favours two components."""
    x = np.asarray(x, float); n = len(x)
    if n < 10 or x.std() < 1e-6: return 0.0, None
    ll1 = stats.norm.logpdf(x, x.mean(), x.std()).sum(); bic1 = -2 * ll1 + 2 * math.log(n)
    best = -np.inf; bestp = None
    for init in (np.percentile(x, [25, 75]), np.percentile(x, [10, 90])):
        mu = init.astype(float).copy(); s = np.array([x.std() / 2, x.std() / 2]) + 1e-3; w = np.array([.5, .5])
        for _ in range(200):
            lp = np.log(w)[None, :] + stats.norm.logpdf(x[:, None], mu[None, :], s[None, :])
            lse = np.logaddexp(lp[:, 0], lp[:, 1]); r = np.exp(lp - lse[:, None])
            nk = r.sum(0) + 1e-9; w = nk / n; mu = (r * x[:, None]).sum(0) / nk
            s = np.sqrt((r * (x[:, None] - mu[None, :])**2).sum(0) / nk); s = np.maximum(s, 0.15)
        ll2 = lse.sum()
        if ll2 > best: best = ll2; bestp = (w.copy(), mu.copy(), s.copy())
    bic2 = -2 * best + 5 * math.log(n)
    return float(bic1 - bic2), bestp

def bimodality_coefficient(x):
    x = np.asarray(x, float); n = len(x)
    if n < 10 or x.std() < 1e-6: return 0.0
    g = stats.skew(x); k = stats.kurtosis(x)
    return float((g**2 + 1) / (k + 3 * (n - 1)**2 / ((n - 2) * (n - 3))))

def local_maxima(pv):
    pk = 0; v = pv
    for i in range(len(v)):
        if v[i] < 0.02: continue
        l = v[i-1] if i > 0 else -1; r = v[i+1] if i < len(v) - 1 else -1
        if v[i] > l and v[i] >= r: pk += 1
    return pk

def cluster_audit(rows):
    out = {}; per_sys = collections.defaultdict(list); pooled = []; sysgap = []; served_peaks = []; day_flags = []
    for r in rows:
        allv = []; wts = []
        for sysid, m in r['members'].items():
            if not m or m['n'] < 10: continue
            x = np.array(m['values']); gain, p = gmm2_bic_gain(x); bc = bimodality_coefficient(x)
            sep = None
            if p is not None:
                w, mu, s = p; sep = abs(mu[0] - mu[1]) / math.sqrt((s[0]**2 + s[1]**2) / 2); minw = w.min()
            else: minw = 0
            strong = gain > 6 and sep is not None and sep > 2 and minw > 0.15
            per_sys[sysid].append(dict(gain=gain, bc=bc, strong=strong, sep=sep, minw=minw, n=m['n'], sd=float(x.std())))
            wt = r['weights'].get(sysid, 0.0); allv.append(x); wts.append((wt, len(x)))
        if allv:
            # weighted pooled member sample (resample each system to its weight share)
            pool = np.concatenate([np.repeat(x, max(1, int(round(200 * wt / max(1, len(x)))))) if wt > 0 else x[:0] for x, (wt, _) in zip(allv, wts)])
            if len(pool) >= 20:
                gain, p = gmm2_bic_gain(pool); strong = False; sep = None; valley = None
                if p is not None:
                    w, mu, s = p; sep = abs(mu[0] - mu[1]) / math.sqrt((s[0]**2 + s[1]**2) / 2); strong = gain > 6 and sep > 2 and w.min() > 0.15
                    # truth position relative to the two modes (in units of pooled sd between modes)
                    a, b = sorted(mu); valley = (r['truth'] - a) / (b - a) if b > a else None
                pooled.append(dict(gain=gain, strong=strong, sep=sep, valley=valley, row=r))
        cs = [c for s, c in r['centers'].items() if r['weights'].get(s, 0) > 0]
        if len(cs) >= 2: sysgap.append((max(cs) - min(cs)) / r['sd'])
        served_peaks.append(local_maxima(r['pv']))
    out['systems'] = {s: dict(n=len(v), frac_strong_bimodal=float(np.mean([x['strong'] for x in v])), frac_bic_gain_gt6=float(np.mean([x['gain'] > 6 for x in v])),
                              frac_bc_gt_0p555=float(np.mean([x['bc'] > .555 for x in v])), mean_members=float(np.mean([x['n'] for x in v]))) for s, v in per_sys.items()}
    out['pooled'] = dict(n=len(pooled), frac_strong_bimodal=float(np.mean([x['strong'] for x in pooled])), frac_bic_gain_gt6=float(np.mean([x['gain'] > 6 for x in pooled])))
    out['system_centre_gap_over_served_sd'] = dict(n=len(sysgap), median=float(np.median(sysgap)), frac_gt_1=float(np.mean(np.array(sysgap) > 1)), frac_gt_2=float(np.mean(np.array(sysgap) > 2)))
    out['served_vector_local_maxima'] = dict(collections.Counter(served_peaks))
    # does it matter: compare strong-bimodal pooled days vs the rest
    strong = [x for x in pooled if x['strong']]; rest = [x for x in pooled if not x['strong']]
    def summ(xs):
        rr = [x['row'] for x in xs]
        if len(rr) < 15: return {'n': len(rr)}
        s = shape_report(rr, 'sub'); return {k: s[k] for k in ('n', 'n_dates', 'bias_c', 'z_meansq', 'pit_var_ratio', 'cover_90', 'z_excess_kurt', 'logscore_floor1e-3', 'hit_truth_mode')}
    out['bimodal_days'] = summ(strong); out['unimodal_days'] = summ(rest)
    vals = [x['valley'] for x in strong if x['valley'] is not None]
    if vals:
        v = np.array(vals); out['truth_position_between_modes_on_bimodal_days'] = dict(n=len(v), frac_in_valley_0p3_0p7=float(np.mean((v > .3) & (v < .7))),
            frac_near_lower_mode=float(np.mean(v <= .3)), frac_near_upper_mode=float(np.mean(v >= .7)), frac_outside=float(np.mean((v < -0.3) | (v > 1.3))))
    return out

def main():
    report = {}
    last = collect('last'); first = collect('first')
    last_cw = [r for r in last if r['lam'] is not None]
    inp = collect('last', which='input')
    report['counts'] = dict(last=len(last), last_with_center_width=len(last_cw), input_pre_width=len(inp), first=len(first),
                            first_gt36h=sum(1 for r in first if r['hours'] > 36), unit=dict(collections.Counter(r['unit'] for r in last)),
                            width_lambda=dict(collections.Counter(r['lam'] for r in last_cw)), delta_nonzero=sum(1 for r in last_cw if abs(r['delta'] or 0) > 1e-9),
                            bridge_lambda_nonzero=sum(1 for r in last if (r['bridge'] or {}).get('lambda', 0) not in (0, None)))
    report['served_last_p0'] = shape_report(last, 'served last pre-midnight P0')
    # paired: same rows, pre-width-layer input vs served
    keyed = {(r['station'], r['date']): r for r in last}
    inp_paired = [r for r in inp if (r['station'], r['date']) in keyed]; served_paired = [keyed[(r['station'], r['date'])] for r in inp_paired]
    report['pre_width_layer_input_paired'] = shape_report(inp_paired, 'input before width layer (paired)')
    report['served_paired_same_rows'] = shape_report(served_paired, 'served (same rows as paired input)')
    report['served_first_p0_gt36h'] = shape_report([r for r in first if r['hours'] > 36], 'served first P0, >36h')
    report['by_unit'] = by_group(last, lambda r: r['unit'], 'unit')
    report['by_hours_to_midnight'] = by_group(last, lambda r: '<6h' if r['hours'] < 6 else '6-12h' if r['hours'] < 12 else '12-24h' if r['hours'] < 24 else '>=24h', 'hours')
    sdq = np.percentile([r['sd'] for r in last], [33, 67])
    report['by_served_sigma_tercile'] = by_group(last, lambda r: 'narrow' if r['sd'] < sdq[0] else 'mid' if r['sd'] < sdq[1] else 'wide', 'sigma')
    report['by_version_family'] = by_group(last, lambda r: 'v2-v7' if int(r['version'].split('_v')[-1]) <= 7 else 'v8-v15' if int(r['version'].split('_v')[-1]) <= 15 else 'v19+', 'version')
    report['station_level_bias'] = station_t(last)
    # spread-skill: bin by served sd
    bins = np.percentile([r['sd'] for r in last], [0, 20, 40, 60, 80, 100]); ss = []
    for a, b in zip(bins[:-1], bins[1:]):
        sub = [r for r in last if a <= r['sd'] <= b]
        ss.append(dict(sd_range=[float(a), float(b)], n=len(sub), mean_served_sd=float(np.mean([r['sd'] for r in sub])), rms_residual=float(np.sqrt(np.mean([(r['truth'] - r['mu'])**2 for r in sub])))))
    report['spread_skill'] = ss
    # conditional asymmetry: residual sign vs served skew sign / vs system disagreement direction
    sk = np.array([r['served_skew'] for r in last]); res = np.array([r['truth'] - r['mu'] for r in last])
    report['residual_vs_served_skew'] = dict(corr=float(np.corrcoef(sk, res)[0, 1]), mean_res_when_served_skew_pos=float(res[sk > 0.1].mean()), n_pos=int((sk > .1).sum()),
                                             mean_res_when_served_skew_neg=float(res[sk < -0.1].mean()), n_neg=int((sk < -.1).sum()))
    report['clusters_last_p0'] = cluster_audit(last)
    (OUT / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=1, default=float))
    print(json.dumps(report, ensure_ascii=False, indent=1, default=float))
if __name__ == '__main__': main()
