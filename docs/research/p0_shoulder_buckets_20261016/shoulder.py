"""Where P0 loses to the market, bucket by bucket: mode, shoulders (mode +-1), tails.
Rows: p0m_morning_side_20261016/joined.jsonl.gz (served hourly P1 rows with their P0 and the market at the decision)."""
import gzip, json, sys, numpy as np, collections
sys.path.insert(0, '../obs_minus_forecast_station_direction_20261015'); import analyze as A
from bayes_basket_target.materialization import floor_probabilities
R = []
for l in gzip.open('../p0m_morning_side_20261016/joined.jsonl.gz', 'rt'):
    r = json.loads(l)
    if not r.get('p0'): continue
    order, c, w = A.centres(r['intervals'])
    def vec(p):
        if not p or set(p) != set(order): return None
        v = np.array([float(p[b]) for b in order]); v = v / v.sum()
        f, _ = floor_probabilities(dict(zip(order, v)), ()); v = np.array([f[b] for b in order]); return v
    p0 = vec(r['p0']['p']); q = vec(r['market']['0']['q']); p1 = vec(r['p1'])
    if p0 is None or q is None: continue
    T = r['truth']; ti = next(i for i, b in enumerate(order) if (r['intervals'][b][0] is None or r['intervals'][b][0] <= T) and (r['intervals'][b][1] is None or T < r['intervals'][b][1]))
    R.append(dict(st=r['station'], day=r['day'], lh=r['local_hour'], p0=p0, q=q, p1=p1, ti=ti, c=c, w=w, T=T,
                  m0=int(np.argmax(p0)), mq=int(np.argmax(q))))
print('rows', len(R))
def sd_mean(vals, keys):
    d = collections.defaultdict(list)
    for v, k in zip(vals, keys): d[k].append(v)
    return np.mean([np.mean(x) for x in d.values()])
for label, sel in (('all hours', R), ('morning 07-13', [x for x in R if 7 <= x['lh'] < 13])):
    print(f'\n=== {label}  n={len(sel)}')
    keys = [x['st'] + x['day'] for x in sel]
    same = np.array([x['m0'] == x['mq'] for x in sel])
    print(f"P0 mode == market mode: {same.mean():.3f}; |mode diff| = 1: {np.mean([abs(x['m0']-x['mq'])==1 for x in sel]):.3f}; >=2: {np.mean([abs(x['m0']-x['mq'])>=2 for x in sel]):.3f}")
    # 1. LogScore gap P0 - market by truth position relative to market mode
    print('LogScore P0 - market by truth position (rel. market mode):')
    tot = 0
    for k in (-3, -2, -1, 0, 1, 2, 3):
        s = [x for x in sel if (x['ti'] - x['mq'] if abs(x['ti'] - x['mq']) < 3 else 3 * np.sign(x['ti'] - x['mq'])) == k]
        if not s: continue
        g = np.mean([np.log(x['q'][x['ti']]) - np.log(x['p0'][x['ti']]) for x in s])
        share = len(s) / len(sel); tot += g * share
        print(f'   truth at mode{k:+d}{"(tail)" if abs(k)==3 else ""}: freq {share:.3f}  P0 p {np.mean([x["p0"][x["ti"]] for x in s]):.3f}  mkt p {np.mean([x["q"][x["ti"]] for x in s]):.3f}  gap/row {g:+.3f}  contribution {g*share:+.4f}')
    print(f'   total gap {tot:+.4f} nat')
    # 2. calibration by position relative to each forecaster's own mode
    print('Calibration at own-mode positions (mean prob -> realised freq):')
    for name, key, mk in (('P0', 'p0', 'm0'), ('market', 'q', 'mq')):
        line = []
        for k in (-2, -1, 0, 1, 2):
            idx = [(x[key][x[mk] + k], float(x['ti'] == x[mk] + k)) for x in sel if 0 <= x[mk] + k < len(x[key])]
            line.append(f"{k:+d}: {np.mean([a for a,_ in idx]):.3f}->{np.mean([b for _,b in idx]):.3f}")
        print('   ', name, '  '.join(line))
    # 3. shoulder split when both share the mode
    ss = [x for x in sel if x['m0'] == x['mq'] and 0 < x['m0'] < len(x['p0']) - 1]
    def split(v, m): a, b = v[m + 1], v[m - 1]; return a / (a + b)
    inshoulder = [x for x in ss if abs(x['ti'] - x['m0']) == 1]
    y = np.array([float(x['ti'] == x['m0'] + 1) for x in inshoulder])
    sp0 = np.array([split(x['p0'], x['m0']) for x in inshoulder]); sq = np.array([split(x['q'], x['m0']) for x in inshoulder])
    print(f'Same mode rows {len(ss)}; truth in a shoulder {len(inshoulder)}: warm-shoulder freq {y.mean():.3f}; P0 split {sp0.mean():.3f} market split {sq.mean():.3f}')
    print(f'   split Brier P0 {np.mean((sp0-y)**2):.4f}  market {np.mean((sq-y)**2):.4f}  ; side right P0 {np.mean((sp0>0.5)==(y==1)):.3f} market {np.mean((sq>0.5)==(y==1)):.3f}')
    bins = np.linspace(0, 1, 6)
    for name, s in (('P0', sp0), ('market', sq)):
        cal = []
        for lo, hi in zip(bins[:-1], bins[1:]):
            m = (s >= lo) & (s < hi if hi < 1 else s <= hi)
            if m.sum() > 20: cal.append(f'{lo:.1f}-{hi:.1f}: {s[m].mean():.2f}->{y[m].mean():.2f} (n{m.sum()})')
        print('   split calibration', name, ' | '.join(cal))
    # shoulder total mass
    sh0 = np.array([x['p0'][x['m0']-1] + x['p0'][x['m0']+1] for x in ss]); shq = np.array([x['q'][x['m0']-1] + x['q'][x['m0']+1] for x in ss])
    ys = np.array([float(abs(x['ti']-x['m0'])==1) for x in ss]); ym = np.array([float(x['ti']==x['m0']) for x in ss])
    print(f'   same-mode rows: mode prob P0 {np.mean([x["p0"][x["m0"]] for x in ss]):.3f} mkt {np.mean([x["q"][x["m0"]] for x in ss]):.3f} realised {ym.mean():.3f}; shoulders P0 {sh0.mean():.3f} mkt {shq.mean():.3f} realised {ys.mean():.3f}')
    # LogScore gap on same-mode rows vs different-mode rows
    for lab, s in (('same mode', [x for x in sel if x['m0']==x['mq']]), ('mode differs', [x for x in sel if x['m0']!=x['mq']])):
        g = sd_mean([np.log(x['q'][x['ti']]) - np.log(x['p0'][x['ti']]) for x in s], [x['st']+x['day'] for x in s])
        print(f'   {lab}: share {len(s)/len(sel):.3f}  gap (P0 worse by) {g:+.3f} nat/row (station-day mean)')
