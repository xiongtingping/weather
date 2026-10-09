"""Market age at the cluster's first taker buy, at scale: listing time = first committed WS snapshot of the
station-day (hot store for >=10-01, cold tier before); hit and equal-dollar return by age."""
import json, sqlite3, collections, time
import numpy as np
from datetime import datetime, timezone
E=json.load(open('events_enriched.json'))
ft=[e for e in E if e['first_taker'] and e.get('truth_ord') is not None and e['outcome']=='Yes']
hot=sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=30); hot.execute('PRAGMA query_only=1')
cold=sqlite3.connect('file:/Volumes/weather_data/weather_cold/ssot_rows/basket_official_ws_snapshot.sqlite?mode=ro&immutable=1',uri=True)
first={}; t0=time.time()
dates=sorted({e['event_date'] for e in ft})
for ed in dates:
    if ed>='2026-10-01':
        for st in {e['station'] for e in ft if e['event_date']==ed}:
            r=hot.execute("select min(observed_at) from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED'",(st,ed)).fetchone()
            if r and r[0]: first[(st,ed)]=r[0]
    else:
        for st,obs in cold.execute("select station_id, min(observed_at) from basket_official_ws_snapshot where event_date=? and state='COMMITTED' group by station_id",(ed,)):
            first[(st,ed)]=obs
print('station-days with a first snapshot', len(first), round(time.time()-t0),'s')
def ts(iso): return datetime.fromisoformat(iso.replace('Z','+00:00')).timestamp()
rows=[]
for e in ft:
    f=first.get((e['station'],e['event_date']))
    if not f: continue
    age=(e['ts']-ts(f))/3600; win=1.0 if e['truth_ord']==e['my_ord'] else 0.0
    rows.append((age,win,e['first_price'],e['usdc'],e['lead_h'],e['station']))
def mean(x): return sum(x)/len(x) if x else float('nan')
def eqret(L): return (sum(w for w,p in L)-sum(p for w,p in L))/sum(p for w,p in L) if L else float('nan')
print(f"events with age {len(rows)} | age median {np.median([r[0] for r in rows]):.1f}h, share within 1h of listing {mean([r[0]<=1 for r in rows]):.2f}, within 10 min {mean([r[0]<=1/6 for r in rows]):.2f}, before our first snapshot (age<0) {mean([r[0]<0 for r in rows]):.2f}")
print("age bin: n | hit | price | equal-dollar return | mean size")
for lo,hi,lab in ((-1e9,0,'before our first snapshot'),(0,1/6,'0-10 min'),(1/6,1,'10-60 min'),(1,6,'1-6 h'),(6,24,'6-24 h'),(24,1e9,'>24 h')):
    P=[r for r in rows if lo<=r[0]<hi]
    if len(P)>=15: print(f"  {lab:26s} n={len(P):4d} hit {mean([r[1] for r in P]):.3f} price {mean([r[2] for r in P]):.3f} ret {eqret([(r[1],r[2]) for r in P])*100:+.1f}% size {mean([r[3] for r in P]):.0f} U")
# bootstrap the 0-60 min return by station-day
import random
early=[r for r in rows if 0<=r[0]<1]; late=[r for r in rows if r[0]>=1]
def boot(P):
    keys=collections.defaultdict(list)
    for r in P: keys[(r[5],)].append(r)  # by station (days not in rows; use station blocks)
    blocks=list(keys.values()); rng=random.Random(1); out=[]
    for _ in range(1000):
        s=[x for _ in blocks for x in blocks[rng.randrange(len(blocks))]]; out.append(eqret([(x[1],x[2]) for x in s]))
    out.sort(); return out[25],out[975]
print(f"  within first hour: return {eqret([(r[1],r[2]) for r in early])*100:+.1f}% 95% by station blocks [{boot(early)[0]*100:+.1f}, {boot(early)[1]*100:+.1f}] (n={len(early)}) | later: {eqret([(r[1],r[2]) for r in late])*100:+.1f}% [{boot(late)[0]*100:+.1f}, {boot(late)[1]*100:+.1f}] (n={len(late)})")
