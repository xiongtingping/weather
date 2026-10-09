"""Is their direction call a station-constant bias (the market systematically on the wrong side at some stations),
which would be learnable without them?  Per station: their warm-call share, truth's warm share on off-mode days,
within-day consistency of their calls, and a walk-forward 'constant side per station' rule for everyone."""
import json, gzip, math, collections
import numpy as np
rows=[json.loads(l) for l in gzip.open('choice_rows.jsonl.gz','rt')]
B=json.load(open('baseline3.json'))
def mean(x): return sum(x)/len(x) if x else float('nan')
def eqret(L): return (sum(w for w,p in L)-sum(p for w,p in L))/sum(p for w,p in L) if L else float('nan')
pairs=[]
for r in rows:
    ch=next(j for j,a in enumerate(r['alts']) if a['chosen']); a=r['alts'][ch]
    if not (a['side']=='Yes' and abs(a['d_mode'])==1): continue
    pairs.append(dict(st=r['station'],ed=r['event_date'],ts=r['ts'],side=a['d_mode'],truth_side=int(np.sign(r['truth_ord']-r['mode_ord'])),hit=1.0 if a['ord']==r['truth_ord'] else 0.0,price=a['price'],lead=r['lead_h']))
print("== per station: warm share of their calls vs warm share of truth (off-mode days), their edge, within-day consistency ==")
bst=collections.defaultdict(list)
for p in pairs: bst[p['st']].append(p)
# truth warm share from the neutral baseline (all station-days, D-1 12:00 or nearest moment with market mode)
truth_side_all=collections.defaultdict(list)
for key,v in B.items():
    st,ed=key.split('|'); tb=v.get('settled_bucket')
    if not tb: continue
    for mom in ('D-1 12:00','D-1 18:00','D0 02:00'):
        m=v.get(mom)
        if isinstance(m,dict) and m.get('market_mode') and m.get('ours'):
            o=m['ours']['ord']; 
            if tb in o and m['market_mode'] in o:
                truth_side_all[st].append(int(np.sign(o[tb]-o[m['market_mode']]))); break
print(f"  {'station':8s} {'calls':>5s} {'warm%':>6s} {'edge':>6s} | truth(off-mode) warm% all days  n | same-side within day")
tbl=[]
for st,P in sorted(bst.items(),key=lambda kv:-len(kv[1])):
    if len(P)<12: continue
    warm=mean([p['side']>0 for p in P]); edge=mean([p['truth_side']==p['side'] for p in P])-mean([p['truth_side']==-p['side'] for p in P])
    T=[t for t in truth_side_all.get(st,[]) if t!=0]; tw=mean([t>0 for t in T]) if T else float('nan')
    days=collections.defaultdict(list)
    for p in P: days[p['ed']].append(p['side'])
    cons=[len(set(v))==1 for v in days.values() if len(v)>=2]
    tbl.append((st,len(P),warm,edge,tw,len(T),mean(cons) if cons else float('nan'),len(cons)))
    print(f"  {st:8s} {len(P):5d} {warm*100:5.0f}% {edge:+.2f} | {tw*100:5.0f}% {len(T):3d} | {mean(cons) if cons else float('nan'):.2f} (days with ≥2 calls: {len(cons)})")
print("\n== how much of their edge is a station-constant side?  Predict their side by the station's majority side (leave-one-day-out) ==")
right=[];
for p in pairs:
    others=[q['side'] for q in bst[p['st']] if q['ed']!=p['ed']]
    if len(others)<8: continue
    maj=1 if mean([s>0 for s in others])>0.5 else -1
    right.append(p['side']==maj)
print(f"  their call equals the station majority side (other days): {mean(right):.3f} (n={len(right)})")
print("\n== a station-constant side rule for everyone: on the neutral baseline, buy YES on the neighbour the station's history favours (expanding window, first 10 days as burn-in) ==")
# build per station-day: truth side vs market mode and the two neighbours' asks at D-1 12:00 (fallback D-1 18:00)
recs=[]
for key,v in B.items():
    st,ed=key.split('|'); tb=v.get('settled_bucket')
    if not tb: continue
    for mom in ('D-1 12:00','D-1 18:00','D0 02:00'):
        m=v.get(mom)
        if not (isinstance(m,dict) and m.get('quotes') and m.get('market_mode')): continue
        Q=m['quotes']; mo=Q[m['market_mode']]['ord']
        nb={d:next((b for b,x in Q.items() if x['ord']==mo+d),None) for d in (1,-1)}
        if not nb[1] or not nb[-1] or Q[nb[1]].get('ask') is None or Q[nb[-1]].get('ask') is None: continue
        ts_side=int(np.sign(Q[tb]['ord']-mo)) if tb in Q else None
        if ts_side is None: continue
        recs.append(dict(st=st,ed=ed,truth_side=ts_side,ask={1:Q[nb[1]]['ask'],-1:Q[nb[-1]]['ask']},hit={1:1.0 if nb[1]==tb else 0.0,-1:1.0 if nb[-1]==tb else 0.0})); break
recs.sort(key=lambda r:r['ed']); days=sorted({r['ed'] for r in recs})
hist=collections.defaultdict(list); trades=[]; trades_strong=[]
for d in days:
    todays=[r for r in recs if r['ed']==d]
    for r in todays:
        H=[t for t in hist[r['st']] if t!=0]
        if len(H)>=8:
            w=mean([t>0 for t in H]); side=1 if w>0.5 else -1
            trades.append((r['hit'][side],r['ask'][side],abs(w-0.5)))
            if abs(w-0.5)>=0.2: trades_strong.append((r['hit'][side],r['ask'][side]))
    for r in todays: hist[r['st']].append(r['truth_side'])
print(f"  all stations with ≥8 history days: n={len(trades)} hit {mean([h for h,a,_ in trades]):.3f} ask {mean([a for h,a,_ in trades]):.3f} equal-dollar return {eqret([(h,a) for h,a,_ in trades])*100:+.1f}%")
print(f"  only stations whose history leans ≥70/30: n={len(trades_strong)} hit {mean([h for h,a in trades_strong]):.3f} ask {mean([a for h,a in trades_strong]):.3f} return {eqret(trades_strong)*100:+.1f}%")
print("\n== their calls by local hour-of-day and by lead: warm share and edge ==")
from zoneinfo import ZoneInfo
from datetime import datetime
reg=json.load(open('registry.json')); tz={r['station_id']:r['settlement_timezone'] for r in reg}
for lab,sel in (('local 0–6',lambda h:h<6),('6–12',lambda h:6<=h<12),('12–18',lambda h:12<=h<18),('18–24',lambda h:h>=18)):
    P=[p for p in pairs if sel(datetime.fromtimestamp(p['ts'],ZoneInfo(tz.get(p['st'],'UTC'))).hour)]
    if len(P)>=30: print(f"  {lab:10s} n={len(P):3d} warm {mean([p['side']>0 for p in P]):.2f} | their side {mean([p['truth_side']==p['side'] for p in P]):.3f} vs other {mean([p['truth_side']==-p['side'] for p in P]):.3f} | hit {mean([p['hit'] for p in P]):.3f} @ {mean([p['price'] for p in P]):.3f}")
