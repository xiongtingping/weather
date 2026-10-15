"""Where the morning wrong side comes from: run age, seated systems, stations."""
import gzip, json, numpy as np, collections
from datetime import datetime
def utc(s): return datetime.fromisoformat(s.replace('Z','+00:00'))
J = [json.loads(l) for l in gzip.open('joined.jsonl.gz','rt')]
R = json.load(gzip.open('decomposed.json.gz','rt'))
assert len(J) == len(R)
rows = []
for j, x in zip(J, R):
    if not (7 <= x['lh'] < 13) or not x['s_p0'] or not x['s_mk'] or x['truth_side'] == 0 or not j['p0']: continue
    runs = json.loads(j['p0']['run_init_by_system'] or '{}')
    if not runs: continue
    dec = utc(j['decided_at'])
    ages = {k: (dec - utc(v)).total_seconds()/3600 for k, v in runs.items()}
    rows.append({**x, 'n_sys': len(runs), 'newest_age': min(ages.values()), 'oldest_age': max(ages.values()), 'systems': sorted(runs)})
print('morning rows', len(rows))
def acc(sel, k): s=[r for r in sel if r[k]]; return np.mean([r[k]==r['truth_side'] for r in s]), len(s)
for lo, hi in ((0,12),(12,18),(18,24),(24,36),(36,99)):
    sel=[r for r in rows if lo<=r['newest_age']<hi]
    if sel: a,n=acc(sel,'s_p0'); b,_=acc(sel,'s_mk'); print(f'newest run age {lo}-{hi}h n{n} P0 {a:.3f} market {b:.3f} gap {b-a:+.3f}')
for k in sorted(set(r['n_sys'] for r in rows)):
    sel=[r for r in rows if r['n_sys']==k]; a,n=acc(sel,'s_p0'); b,_=acc(sel,'s_mk'); print(f'systems {k} n{n} P0 {a:.3f} market {b:.3f}')
print('seated', collections.Counter(s for r in rows for s in r['systems']).most_common())
# per station gap
per = {}
for st in sorted(set(r['st'] for r in rows)):
    sel=[r for r in rows if r['st']==st]
    a,n=acc(sel,'s_p0'); b,_=acc(sel,'s_mk'); c,_=acc(sel,'s_p1')
    per[st]=(n,a,c,b,b-a, np.mean([r['newest_age'] for r in sel]))
print('station  n   P0    P1    mkt   gap  newest_run_age_h')
for st,v in sorted(per.items(), key=lambda kv:-kv[1][4]): print(f'{st} {v[0]:4d} {v[1]:.2f} {v[2]:.2f} {v[3]:.2f} {v[4]:+.2f} {v[5]:5.1f}')
# region by UTC offset approx: morning local 07-13 => utc hour
json.dump({'per_station': per}, open('locate.json','w'), default=float, indent=1)
