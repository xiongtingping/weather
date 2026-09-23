"""Charter 199 before go-live: the serving function under production training density."""
import json, math, sqlite3, sys, collections
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import fusion_blend as FB
EDGES = np.array([0, .01, .025, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.])
research = {(x['station'], x['day']) for x in json.load(open(HERE.parent / 'joint_objective/scores.json'))}
conn = sqlite3.connect(f"file:{FB.FUSION_BLEND_TRAINING_DB}?mode=ro", uri=True); conn.row_factory = sqlite3.Row
facts = [dict(r) for r in conn.execute("select * from fusion_blend_training_fact order by station_id, event_date, truth_revision")]
latest = {}
for r in facts: latest[(r['station_id'], r['event_date'])] = r
rows = []; states = collections.Counter()
for (station, day), r in sorted(latest.items()):
    if day < '2026-08-28' or r['probability_source'].startswith('NONE'): continue
    seat = json.loads(r['seat_weights_json']); probs = json.loads(r['probabilities_json'])
    if set(seat) - set(probs): continue
    served, receipt = FB.overlay_system_weights(station_id=station, event_date=day, decision_at_utc=r['root_decision_at_utc'],
        timezone_name=r['settlement_timezone'], system_weights=seat, family_by_system=json.loads(r['family_by_system_json']),
        family_caps=json.loads(r['family_caps_json']), lead_by_system=json.loads(r['lead_by_system_json']), mode=FB.FUSION_BLEND_MODE_BLEND)
    states[receipt['state']] += 1
    e = json.loads(r['center_error_json']); a = json.loads(r['crps_a_json']); B = json.loads(r['crps_b_json']); hit = r['truth_bucket_index']
    out = {'station': station, 'day': day, 'group': 'A' if (station, day) in research else ('C' if day >= '2026-09-20' else 'B'), 'state': receipt['state'], 'arms': {}}
    keys = sorted(seat)
    for arm, w in (('seat', seat), ('blend', served)):
        v = np.array([w[k] for k in keys]); P = np.array([probs[k] for k in keys]); q = v @ P
        y = np.zeros(len(q)); y[hit] = 1
        err = float(sum(w[k] * e[k] for k in keys))
        crps = float(sum(w[k] * a[k] for k in keys) - .5 * sum(w[i] * w[j] * B[i][j] for i in keys for j in keys))
        out['arms'][arm] = {'se': err * err, 'crps': crps, 'nll': -math.log(q[hit]) if q[hit] > 0 else None, 'brier': float(np.sum((q - y) ** 2)), 'q': q.tolist(), 'y': y.tolist(),
                            'move': 0.5 * sum(abs(w[k] - seat[k]) for k in keys)}
    rows.append(out)

def murphy(sub, arm):
    st = np.zeros((len(EDGES) - 1, 5))
    for r in sub:
        p = np.array(r['arms'][arm]['q']); y = np.array(r['arms'][arm]['y']); b = np.clip(np.searchsorted(EDGES, p, side='right') - 1, 0, len(EDGES) - 2); w = np.full(len(p), 1 / len(p))
        for c, z in enumerate([w, w * p, w * y, w * p * p, w * p * y]): st[:, c] += np.bincount(b, weights=z, minlength=len(EDGES) - 1)
    W, P, Y, P2, PY = st.T; n = W.sum(); m = W > 0; base = Y.sum() / n
    return float(np.sum((P[m] - Y[m]) ** 2 / W[m]) / n), float(np.sum(W[m] * (Y[m] / W[m] - base) ** 2) / n)

def summary(sub):
    out = {'n': len(sub), 'days': len({r['day'] for r in sub}), 'stations': len({r['station'] for r in sub}),
           'applied_share': float(np.mean([r['state'] == 'APPLIED' for r in sub])), 'move_median': float(np.median([r['arms']['blend']['move'] for r in sub]))}
    for arm in ('seat', 'blend'):
        a = [r['arms'][arm] for r in sub]
        zero = sum(x['nll'] is None for x in a)
        rel, res = murphy(sub, arm)
        out[arm] = {'rmse': math.sqrt(np.mean([x['se'] for x in a])), 'crps': float(np.mean([x['crps'] for x in a])), 'nll': float(np.mean([x['nll'] for x in a])) if not zero else None,
                    'zero_hits': zero, 'brier': float(np.mean([x['brier'] for x in a])), 'reliability': rel, 'resolution': res}
    out['change_pct'] = {k: 100 * (out['blend'][k] / out['seat'][k] - 1) for k in ('rmse', 'crps', 'nll', 'brier', 'reliability', 'resolution') if out['seat'][k]}
    days = sorted({r['day'] for r in sub}); n = len(days); rng = np.random.default_rng(230923)
    daily = np.array([[sum(r['arms'][arm][k] for r in sub if r['day'] == d) for arm in ('blend', 'seat') for k in ('se', 'crps', 'nll', 'brier')] + [sum(r['day'] == d for r in sub)] for d in days])
    draws = []
    for _ in range(4000):
        ix = [min(s + j, n - 1) for s in rng.integers(0, max(1, n - 2), math.ceil(n / 3)) for j in range(3)][:n]
        t = daily[ix].sum(axis=0); c = t[-1]
        draws.append([math.sqrt(t[0] / c) - math.sqrt(t[4] / c), (t[1] - t[5]) / c, (t[2] - t[6]) / c, (t[3] - t[7]) / c])
    d = np.array(draws)
    out['delta_ci95'] = dict(zip(['rmse', 'crps', 'nll', 'brier'], np.quantile(d, [.025, .975], axis=0).T.tolist()))
    out['all_three_lower_share'] = float(np.mean(np.all(d[:, :3] < 0, axis=1)))
    return out

res = {'states': dict(states), 'groups': {}}
for g, sub in [('ALL', rows)] + [(g, [r for r in rows if r['group'] == g]) for g in 'ABC']:
    if sub: res['groups'][g] = summary(sub)
(HERE / 'results.json').write_text(json.dumps(res, indent=1, sort_keys=True))
print(json.dumps(res['states']))
for g, s in res['groups'].items():
    print(g, 'n', s['n'], 'days', s['days'], 'applied', round(s['applied_share'], 3), 'move', round(s['move_median'], 3),
          '| change%', {k: round(v, 3) for k, v in s['change_pct'].items()}, '| zero', s['seat']['zero_hits'], s['blend']['zero_hits'], '| all3<0', round(s['all_three_lower_share'], 3))
    print('   ci', {k: [round(x, 5) for x in v] for k, v in s['delta_ci95'].items()})
