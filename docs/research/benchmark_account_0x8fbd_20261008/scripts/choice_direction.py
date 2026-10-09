"""Their YES neighbour pick is a direction call around the market mode (warm vs cold).  Which observable signals
agree with it, and does agreeing predict the hit?  Signals: our model's lean (served mean vs market mode), our mode
side, yesterday's truth side, today's running max side (D0), last-hour momentum of the two neighbours, which
neighbour is cheaper, which has more depth, the warm-side prior."""
import json, gzip, math, re, collections
import numpy as np
rows=[json.loads(l) for l in gzip.open('choice_rows.jsonl.gz','rt')]
def centre_c(label, unit):
    m=re.match(r'(-?\d+)-(-?\d+)°F',label)
    if m: return ((int(m.group(1))+int(m.group(2)))/2+0.5-32)*5/9
    m=re.match(r'(-?\d+)°F or (higher|below)',label)
    if m: v=int(m.group(1)); return ((v+1 if m.group(2)=='higher' else v-1)-32)*5/9
    m=re.match(r'(-?\d+)°C or (higher|below)',label)
    if m: v=int(m.group(1)); return float(v+1 if m.group(2)=='higher' else v-1)
    m=re.match(r'(-?\d+)°C',label); return float(m.group(1)) if m else None
def mean(x): return sum(x)/len(x) if x else float('nan')
pairs=[]
for r in rows:
    ch=next(j for j,a in enumerate(r['alts']) if a['chosen']); a=r['alts'][ch]
    if not (a['side']=='Yes' and abs(a['d_mode'])==1): continue
    yes={x['d_mode']:x for x in r['alts'] if x['side']=='Yes'}
    other=yes.get(-a['d_mode'])
    if not other: continue
    side=a['d_mode']  # +1 warm, -1 cold
    hit=1.0 if a['ord']==r['truth_ord'] else 0.0
    truth_side=np.sign(r['truth_ord']-r['mode_ord'])
    # our served lean: mean ordinal from p over YES alts
    P={x['ord']:x['p'] for x in yes.values()}; s=sum(P.values()); our_mean=sum(o*p for o,p in P.items())/s if s>0 else None
    sig={}
    sig['our mean vs mode']=np.sign(our_mean-r['mode_ord']) if our_mean is not None else 0
    sig['our mode vs mode']=np.sign(r['our_mode_ord']-r['mode_ord'])
    sig['our p: warm nb > cold nb']=np.sign(yes[1]['p']-yes[-1]['p'])
    sig['market q: warm nb > cold nb']=np.sign(yes[1]['q']-yes[-1]['q'])
    sig['cheaper neighbour']=np.sign(yes[-1]['price']-yes[1]['price'])  # +1 if warm is cheaper
    sig['deeper neighbour']=np.sign((yes[1]['depth'] or 0)-(yes[-1]['depth'] or 0))
    mw,mc=yes[1]['mom'],yes[-1]['mom']
    sig['neighbour whose mid FELL last hour']=np.sign((mc if mc is not None else 0)-(mw if mw is not None else 0)) if (mw is not None or mc is not None) else 0
    mode_c=next((centre_c(x['bucket'],r['unit']) for x in yes.values() if x['d_mode']==0),None)
    if r['ytruth_c'] is not None and mode_c is not None: sig["yesterday's truth side of mode"]=np.sign(r['ytruth_c']-mode_c)
    if r['is_d0'] and r['tmax_today'] is not None and mode_c is not None: sig["today's running max side (D0)"]=np.sign(r['tmax_today']-mode_c)
    if r['st_mean_c'] is not None and mode_c is not None: sig['station sample-mean side']=np.sign(r['st_mean_c']-mode_c)
    sig['always warm']=1
    pairs.append(dict(side=side,hit=hit,truth_side=truth_side,price=a['price'],sig=sig,usdc=r['usdc'],lead=r['lead_h'],stage=r['stage']))
print('pairs',len(pairs),'| their side right (truth on their side of mode):',round(mean([p['truth_side']==p['side'] for p in pairs]),3),'| truth on other side:',round(mean([p['truth_side']==-p['side'] for p in pairs]),3),'| truth at mode:',round(mean([p['truth_side']==0 for p in pairs]),3))
print("\n== agreement of their side with each signal, and their hit when the signal agrees / disagrees ==")
names=sorted({k for p in pairs for k in p['sig']})
for k in names:
    P=[p for p in pairs if k in p['sig'] and p['sig'][k]!=0]
    if len(P)<40: continue
    agree=[p for p in P if p['sig'][k]==p['side']]; dis=[p for p in P if p['sig'][k]==-p['side']]
    # the signal's own directional skill: does truth fall on the signal's side?
    skill=mean([p['truth_side']==p['sig'][k] for p in P]); anti=mean([p['truth_side']==-p['sig'][k] for p in P])
    print(f"  {k:36s} n={len(P):3d} agree {len(agree)/len(P):.2f} | their hit when agree {mean([p['hit'] for p in agree]):.3f} (n={len(agree)}) vs disagree {mean([p['hit'] for p in dis]):.3f} (n={len(dis)}) | signal alone: truth on its side {skill:.3f}, opposite {anti:.3f}")
print("\n== their direction call by size, lead and stage ==")
for lab,sel in (('size <20 U',lambda p:p['usdc']<20),('20–100 U',lambda p:20<=p['usdc']<100),('≥100 U',lambda p:p['usdc']>=100),('lead <12h',lambda p:p['lead']<12),('12–36h',lambda p:12<=p['lead']<36),('≥36h',lambda p:p['lead']>=36),('P0',lambda p:p['stage']=='P0'),('P0-M',lambda p:p['stage']=='P0-M'),('P1',lambda p:p['stage']=='P1')):
    P=[p for p in pairs if sel(p)]
    if len(P)>=30: print(f"  {lab:10s} n={len(P):3d} their side right {mean([p['truth_side']==p['side'] for p in P]):.3f} vs other side {mean([p['truth_side']==-p['side'] for p in P]):.3f} | hit {mean([p['hit'] for p in P]):.3f} @ {mean([p['price'] for p in P]):.3f}")
