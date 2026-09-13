"""Read-only: the calibrated TAF operator's own holdout rows, paired against the P0 it corrects."""
import json, glob, math, random, collections, pathlib
OUT = pathlib.Path(__file__).resolve().parent
base = '/Users/xiong/weather/data/runtime/bayes_basket_target_nwp/taf_revision_training'
reg = json.load(open('/Users/xiong/weather/config/artifacts/bayes_basket_target_taf_revision_active.json'))
EPS = 1e-6
def metrics(p, truth, buckets):
    q = max(p.get(truth, 0.0), EPS)
    nll = -math.log(q)
    brier = sum((p.get(b, 0.0) - (1.0 if b == truth else 0.0)) ** 2 for b in buckets)
    top = max(buckets, key=lambda b: p.get(b, 0.0))
    return nll, brier, top == truth
rows = {}
used_by = collections.defaultdict(list)
for a in reg['artifacts']:
    f = f"{base}/{a['station_id']}/{a['artifact_id']}_transport_oos.json"
    d = json.load(open(f))
    for r in d['evaluated_rows']:
        key = (r['station_id'], r['event_date'], r['decision_at_utc'], r.get('taf_available_at_utc'))
        used_by[key].append(a['artifact_id'])
        if key in rows:
            continue
        nb, bb, hb = metrics(r['base_probabilities'], r['truth_bucket'], r['buckets'])
        nt, bt, ht = metrics(r['taf_probabilities'], r['truth_bucket'], r['buckets'])
        rows[key] = dict(station=r['station_id'], day=r['event_date'], nll_b=nb, nll_t=nt, brier_b=bb, brier_t=bt,
                         hit_b=hb, hit_t=ht, ae_b=abs(r['base_mean_c'] - r['truth_tmax_c']), ae_t=abs(r['taf_mean_c'] - r['truth_tmax_c']),
                         shift=r['taf_mean_c'] - r['base_mean_c'], base_err=r['base_mean_c'] - r['truth_tmax_c'])
R = list(rows.values())
n = len(R)
def mean(k): return sum(x[k] for x in R) / n
def boot(k1, k2, reps=4000, seed=7):
    days = sorted({(x['station'], x['day']) for x in R})
    by = collections.defaultdict(list)
    for x in R: by[(x['station'], x['day'])].append(x)
    rnd = random.Random(seed); out = []
    for _ in range(reps):
        pick = [rnd.choice(days) for _ in days]
        xs = [x for d in pick for x in by[d]]
        out.append(sum(x[k2] - x[k1] for x in xs) / len(xs))
    out.sort(); return out[int(.05 * reps)], out[int(.95 * reps)]
summary = dict(
    artifacts=len(reg['artifacts']), unique_rows=n, row_references=sum(len(v) for v in used_by.values()),
    station_days=len({(x['station'], x['day']) for x in R}), stations=len({x['station'] for x in R}),
    nll=dict(base=mean('nll_b'), taf=mean('nll_t'), diff_90=boot('nll_b', 'nll_t')),
    brier=dict(base=mean('brier_b'), taf=mean('brier_t'), diff_90=boot('brier_b', 'brier_t')),
    hit=dict(base=mean('hit_b'), taf=mean('hit_t')),
    mae_c=dict(base=mean('ae_b'), taf=mean('ae_t'), diff_90=boot('ae_b', 'ae_t')),
    rows_nll_better=sum(x['nll_t'] < x['nll_b'] for x in R),
    rows_nll_much_worse=sum(x['nll_t'] - x['nll_b'] > 2.0 for x in R),
    shift_same_direction_as_needed=sum((x['shift'] > 0) == (x['base_err'] < 0) for x in R if abs(x['shift']) > 0.25),
    shift_rows=sum(1 for x in R if abs(x['shift']) > 0.25),
)
per = {}
for s in sorted({x['station'] for x in R}):
    xs = [x for x in R if x['station'] == s]
    per[s] = dict(n=len(xs), nll_b=sum(x['nll_b'] for x in xs)/len(xs), nll_t=sum(x['nll_t'] for x in xs)/len(xs),
                  mae_b=sum(x['ae_b'] for x in xs)/len(xs), mae_t=sum(x['ae_t'] for x in xs)/len(xs))
summary['per_station'] = per
(OUT / 'summary.json').write_text(json.dumps(summary, indent=1))
print(json.dumps({k: v for k, v in summary.items() if k != 'per_station'}, indent=1))
for s, v in per.items(): print(s, v['n'], 'NLL %.2f->%.2f' % (v['nll_b'], v['nll_t']), 'MAE %.2f->%.2f' % (v['mae_b'], v['mae_t']))
