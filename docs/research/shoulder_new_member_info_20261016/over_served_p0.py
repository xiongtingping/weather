"""Increment over the SERVED P0: does P0's own shoulder split already carry the member-fraction and the deterministic-model information?
Unit as run.py.  Served P0 per unit = the newest committed P0 decided before local 09:00 of (event day - k), k = 0/1/2 for D0/D1/D2
(inputs.db root table, 08-31..).  P0 feature = logit of p(m+1)/(p(m+1)+p(m-1)) at the unit's reference bucket m."""
import gzip, json, math, sqlite3, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'docs/research/raw_member_shoulder_skill_20261016'))
from analyze import auc, auc_ci, to_unit, bucket

sys.path.insert(0, str(ROOT))
units = json.load(gzip.open(HERE / 'units.json.gz', 'rt'))
db = sqlite3.connect(ROOT / 'docs/research/p1_forward_physics_20261015/inputs.db')
tz = dict(db.execute('select station_id, settlement_timezone from tz'))
p0 = {}
for st, d, dec, pj in db.execute("select station_id, event_date, decision_at, probabilities_json from root where stage='P0'"):
    p0.setdefault((st, d), []).append((dec, pj))
for v in p0.values(): v.sort()
lad = {}
for st, d, ij in db.execute("select station_id, event_date, intervals_json from p1 where intervals_json is not null"):
    lad.setdefault((st, d), json.loads(ij))
def stamp(t): return t.strftime('%Y-%m-%dT%H:%M:%S.') + f'{t.microsecond // 1000:03d}Z'
rows = []
for x in units:
    st, d = x['st'], x['day']
    if (st, d) not in p0 or (st, d) not in lad or st not in tz: continue
    k = {'D0': 0, 'D1': 1, 'D2': 2}[x['cls']]
    cut = (datetime.fromisoformat(d).replace(hour=9, tzinfo=ZoneInfo(tz[st])) - timedelta(days=k)).astimezone(timezone.utc)
    cand = [pj for dec, pj in p0[(st, d)] if dec <= stamp(cut)]
    if not cand: continue
    p = json.loads(cand[-1]); iv = lad[(st, d)]
    u = 'F' if '°F' in next(iter(iv)) else 'C'
    # bucket index for each label via its centre in unit space
    cen = {}
    for b, (lo, hi) in iv.items():
        mid = ((lo if lo is not None else hi - 1.0) + (hi if hi is not None else lo + 1.0)) / 2
        cen[b] = bucket(to_unit(mid, u), u)[0]
    base_k = None
    # reconstruct reference bucket index from the unit: base offset is relative to bucket of base; recover via truth bucket and y
    # truth bucket index kt; m = kt - 1 if y==1 else kt + 1
    rows.append((x, p, cen))
# need truth bucket per unit -> recompute from truth table
c = sqlite3.connect(f"file:{ROOT/'data/bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=5)
truth = {}
for st, d, t, rev in c.execute("select station_id, local_date, official_tmax_c, revision_no from calibration_station_daily_high_truth where source_lane in ('AWC','SETTLEMENT_SOURCE') and qc_status='ACCEPTED' and local_date>='2026-08-31' order by revision_no"):
    truth[(st, d)] = float(t)
recs = []
for x, p, cen in rows:
    u = 'F' if any('°F' in b for b in cen) else 'C'
    kt = bucket(to_unit(truth[(x['st'], x['day'])], u), u)[0]
    m = kt - 1 if x['y'] == 1 else kt + 1
    up = sum(v for b, v in p.items() if cen.get(b) == m + 1); dn = sum(v for b, v in p.items() if cen.get(b) == m - 1)
    if up + dn <= 0: continue
    s = min(max(up / (up + dn), 1e-3), 1 - 1e-3)
    recs.append({**x, 'p0': math.log(s / (1 - s))})
print('units with a served P0', len(recs))
def fit(X, y, ridge=1.0):
    b = np.zeros(X.shape[1]); R = np.diag(np.r_[0, np.ones(X.shape[1] - 1)]) * ridge
    for _ in range(60):
        pr = 1 / (1 + np.exp(-X @ b)); b += np.linalg.solve((X * (pr * (1 - pr))[:, None]).T @ X + R + 1e-9 * np.eye(len(b)), X.T @ (y - pr) - R @ b)
    return b
def incr(sel, base_feats, add):
    sel = [x for x in sel if all(f in x for f in base_feats + add)]
    if len(sel) < 150: return None
    d = np.array([x['day'] for x in sel]); y = np.array([x['y'] for x in sel])
    X0 = np.c_[np.ones(len(sel)), [[x[f] for f in base_feats] for x in sel]]; X1 = np.c_[X0, [[x[f] for f in add] for x in sel]]
    l0 = np.full(len(sel), np.nan); l1 = l0.copy(); q0 = l0.copy(); q1 = l0.copy()
    for dd in sorted(set(d)):
        tr = d < dd; te = d == dd
        if len(set(d[tr])) < 12: continue
        for X, l, q in ((X0, l0, q0), (X1, l1, q1)):
            b = fit(X[tr], y[tr]); pr = 1 / (1 + np.exp(-X[te] @ b)); q[te] = pr; l[te] = -(y[te] * np.log(pr) + (1 - y[te]) * np.log(1 - pr))
    ok = ~np.isnan(l0); dl = (l1 - l0)[ok]; dk = d[ok]; uu = np.unique(dk); rng = np.random.default_rng(7)
    ib = {u: np.where(dk == u)[0] for u in uu}; bs = [dl[np.concatenate([ib[u] for u in rng.choice(uu, len(uu))])].mean() for _ in range(500)]
    return {'n': int(ok.sum()), 'auc0': auc(q0[ok], y[ok]), 'auc1': auc(q1[ok], y[ok]), 'dll': float(dl.mean()), 'lo': float(np.quantile(bs, .025)), 'hi': float(np.quantile(bs, .975))}
out = {}
for cls in ('ALL', 'D0', 'D1', 'D2'):
    sel = recs if cls == 'ALL' else [x for x in recs if x['cls'] == cls]
    a = auc([x['p0'] for x in sel], [x['y'] for x in sel])
    print(f'\n== {cls} units {len(sel)}  served-P0 split AUC alone {a:.3f}')
    out[cls] = {'p0_alone_auc': a}
    for add in (['shape_frac'], ['det_equal'], ['shape_frac', 'det_equal'], ['det_AIGFS_6h'], ['det_GFS_3h'], ['base']):
        r = incr(sel, ['p0'], add)
        if r is None: continue
        out[cls]['+'.join(add)] = r
        flag = 'GAIN' if r['hi'] < 0 else ('worse' if r['lo'] > 0 else '')
        print(f"  P0 + {'+'.join(add):24s} n{r['n']:5d} AUC {r['auc0']:.3f} -> {r['auc1']:.3f}  dLogLoss {r['dll']:+.4f} [{r['lo']:+.4f},{r['hi']:+.4f}] {flag}")
json.dump(out, open(HERE / 'readings_over_p0.json', 'w'), indent=1, default=float)
