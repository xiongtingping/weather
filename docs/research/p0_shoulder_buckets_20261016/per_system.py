"""Same-mode morning rows with truth in a shoulder: warm/cold-shoulder AUC of each native system's own vector and calibrated centre,
of the pooled P0, of the equal-weight pool, and of the market.  Per-system vectors from the P0's lineage (native_system_probabilities_json)."""
import gzip, json, sys, sqlite3, numpy as np, collections
sys.path.insert(0, '../obs_minus_forecast_station_direction_20261015'); import analyze as A
c = sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=5)
def auc(s, y):
    s = np.asarray(s); y = np.asarray(y); o = np.argsort(s, kind='mergesort'); r = np.empty(len(s)); r[o] = np.arange(1, len(s) + 1)
    n1 = y.sum(); n0 = len(y) - n1; return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)
cache = {}
rows = []
for l in gzip.open('../p0m_morning_side_20261016/joined.jsonl.gz', 'rt'):
    r = json.loads(l)
    if not r.get('p0') or not (7 <= r['local_hour'] < 13): continue
    order, cc, w = A.centres(r['intervals'])
    if set(r['p0']['p']) != set(order) or set(r['market']['0']['q']) != set(order): continue
    p = np.array([float(r['p0']['p'][b]) for b in order]); p /= p.sum()
    q = np.array([float(r['market']['0']['q'][b]) for b in order]); q /= q.sum()
    m = int(np.argmax(p))
    if m != int(np.argmax(q)) or m == 0 or m == len(p) - 1: continue
    T = r['truth']; ti = next(i for i, b in enumerate(order) if (r['intervals'][b][0] is None or r['intervals'][b][0] <= T) and (r['intervals'][b][1] is None or T < r['intervals'][b][1]))
    if abs(ti - m) != 1: continue
    # find the P0 checkpoint via the P1 envelope's lineage (joined rows carry the P0 vector; re-read its lineage)
    key = (r['station'], r['p0']['decided_at'])
    if key not in cache:
        row = c.execute("select lineage_json from basket_probability_envelope where station_id=? and event_date=? and stage='P0' and decision_at_utc=? and state='COMMITTED' limit 1",
                        (r['station'], r['day'], r['p0']['decided_at'])).fetchone()
        cache[key] = json.loads(row[0]) if row else None
    lin = cache[key]
    if not lin: continue
    sysp = json.loads(lin.get('native_system_probabilities_json') or '{}')
    cen = json.loads(lin.get('native_calibrated_system_centers_c_json') or '{}')
    wts = json.loads(lin.get('system_weights_json') or '{}')
    rec = dict(day=r['day'], st=r['station'], y=float(ti == m + 1), pool=p[m + 1] / (p[m + 1] + p[m - 1]), mk=q[m + 1] / (q[m + 1] + q[m - 1]),
               mk_off=((q * cc).sum() - cc[m]) / w, sys={}, n_sys=len(sysp), spread=None)
    vs = []
    for s, v in sysp.items():
        if set(v) != set(order): continue
        x = np.array([float(v[b]) for b in order]); x /= x.sum()
        rec['sys'][s] = dict(split=(x[m + 1] + 1e-9) / (x[m + 1] + x[m - 1] + 2e-9), off=(cen[s] - cc[m]) / w if s in cen else None, wt=wts.get(s))
        vs.append(x)
    if vs:
        eq = np.mean(vs, 0); rec['eq'] = eq[m + 1] / (eq[m + 1] + eq[m - 1])
        offs = [v['off'] for v in rec['sys'].values() if v['off'] is not None]
        rec['cen_mean_off'] = float(np.mean(offs)) if offs else None
        rec['cen_median_off'] = float(np.median(offs)) if offs else None
        rec['spread'] = float(np.std(offs)) * w if offs else None
    rows.append(rec)
y = np.array([x['y'] for x in rows]); print('rows', len(rows), 'P0 lineages read', len(cache))
print(f"pooled P0 split AUC {auc([x['pool'] for x in rows], y):.3f}   market split {auc([x['mk'] for x in rows], y):.3f}")
e = [x for x in rows if 'eq' in x]; print(f"equal-weight pool split AUC {auc([x['eq'] for x in e], [x['y'] for x in e]):.3f} (n{len(e)})")
for k in ('cen_mean_off', 'cen_median_off'):
    e = [x for x in rows if x.get(k) is not None]; print(f"{k} AUC {auc([x[k] for x in e], [x['y'] for x in e]):.3f} (n{len(e)})")
for s in sorted({s for x in rows for s in x['sys']}):
    e = [x for x in rows if s in x['sys']]
    a1 = auc([x['sys'][s]['split'] for x in e], [x['y'] for x in e])
    eo = [x for x in e if x['sys'][s]['off'] is not None]
    a2 = auc([x['sys'][s]['off'] for x in eo], [x['y'] for x in eo]) if eo else float('nan')
    am = auc([x['mk'] for x in e], [x['y'] for x in e])
    print(f"  {s:26s} n{len(e):4d} split AUC {a1:.3f}  centre AUC {a2:.3f}  (market on same rows {am:.3f})  mean weight {np.mean([x['sys'][s]['wt'] or 0 for x in e]):.2f}")
# spread: do systems disagreeing explain the flat split?
sp = np.array([x['spread'] or np.nan for x in rows]); ok = ~np.isnan(sp); med = np.nanmedian(sp)
for lab, sel in (('low spread', ok & (sp <= med)), ('high spread', ok & (sp > med))):
    idx = np.where(sel)[0]
    print(f"{lab} (sd of system centres {'<=' if lab=='low spread' else '>'} {med:.2f}C): n{len(idx)} pool AUC {auc([rows[i]['pool'] for i in idx], y[idx]):.3f} market {auc([rows[i]['mk'] for i in idx], y[idx]):.3f}")
json.dump([{k: v for k, v in x.items()} for x in rows], gzip.open('per_system_rows.json.gz', 'wt'), default=float)
