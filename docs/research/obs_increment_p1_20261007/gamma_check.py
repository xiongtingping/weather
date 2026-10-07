"""Hold the walk-forward theta of each test day, force the sky strength gamma
(a priori, not fitted) and score the test day -- does a strong sky increment
help or hurt out of sample?"""
import gzip, json, random
from collections import defaultdict
import numpy as np
import fit as F
rows=[json.loads(l) for l in gzip.open('rows.jsonl.gz','rt')]
data=F.prepare(rows)
wf=json.load(open('walk_forward.json'))
fitted=wf['fitted']
by_day=defaultdict(list)
for x in data: by_day[x['day']].append(x)
out={}
for g, r0 in [(0.0,0.0),(0.5,0.0),(1.0,0.0),(2.0,0.0),(1.0,0.2),(2.0,0.2)]:
    sd=defaultdict(list)
    for d,th in fitted.items():
        th=list(th); th[1]=g; th[2]=r0
        for x in by_day[d]:
            if not (12<=x['hour']<18): continue
            b=F.score(x['p'],x['k']); n=F.score(F.updated(x,th),x['k'])
            sd[(d,x['station'])].append((n[0]-b[0], n[1]-b[1], x['r']))
    vals=[(k[0],np.mean([v[0] for v in vs]),np.mean([v[1] for v in vs])) for k,vs in sd.items()]
    days=sorted({v[0] for v in vals}); byd=defaultdict(list)
    for v in vals: byd[v[0]].append(v)
    rng=random.Random(3); bl=[]
    for _ in range(1000):
        pick=[s for d in [rng.choice(days) for _ in days] for s in byd[d]]; bl.append(np.mean([s[1] for s in pick]))
    dark=[v for k,vs in sd.items() for v in vs if v[2]<-0.5]
    print(f"gamma={g} r0={r0}: 12-18h ΔLog {np.mean([v[1] for v in vals]):+.4f} [{np.percentile(bl,2.5):+.4f},{np.percentile(bl,97.5):+.4f}] ΔBrier {np.mean([v[2] for v in vals]):+.4f} | 比预报暗的小时 ΔLog {np.mean([v[0] for v in dark]):+.4f} (n={len(dark)})")
