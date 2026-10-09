"""Inside their favourite cells: picked vs not picked on ladder-shape features (dip below neighbours, ask vs recent mid,
spread, depth, ladder mid-sum), with equal-dollar returns (sum(win-price)/sum(price)) instead of mean ratios."""
import json, gzip, math, collections
import numpy as np
rows=[json.loads(l) for l in gzip.open('choice_rows.jsonl.gz','rt')]
def mean(x): return sum(x)/len(x) if x else float('nan')
def eqret(L): return (sum(w for w,p in L)-sum(p for w,p in L))/sum(p for w,p in L) if L else float('nan')
cells={'YES mode±1 @0.1–0.5':lambda a:a['side']=='Yes' and abs(a['d_mode'])==1 and 0.1<=a['price']<0.5,
       'NO mode/−1 @0.3–0.7':lambda a:a['side']=='No' and a['d_mode'] in (0,-1) and 0.3<=a['price']<0.7,
       'YES mode @0.2–0.6':lambda a:a['side']=='Yes' and a['d_mode']==0 and 0.2<=a['price']<0.6}
def win(r,a): return 1.0 if ((a['ord']==r['truth_ord']) if a['side']=='Yes' else (a['ord']!=r['truth_ord'])) else 0.0
print("== equal-dollar returns (Σ(win−price)/Σprice) picked vs not picked, by cell and by price band ==")
for cname,cond in cells.items():
    for lo,hi in ((0.1,0.2),(0.2,0.3),(0.3,0.4),(0.4,0.5),(0.5,0.6),(0.6,0.7)):
        t=[];o=[]
        for r in rows:
            ch=next(j for j,a in enumerate(r['alts']) if a['chosen'])
            for j,a in enumerate(r['alts']):
                if cond(a) and lo<=a['price']<hi: (t if j==ch else o).append((win(r,a),a['price']))
        if len(t)>=25 and len(o)>=40:
            print(f"  {cname:22s} price {lo:.1f}–{hi:.1f}: picked n={len(t):3d} hit {mean([w for w,p in t]):.3f} ret {eqret(t)*100:+.1f}% | not picked n={len(o):4d} hit {mean([w for w,p in o]):.3f} ret {eqret(o)*100:+.1f}%")
print("\n== ladder-shape features inside the cells: picked vs not picked (means) ==")
def feats(r,a):
    q={x['ord']:x['q'] for x in r['alts'] if x['side']=='Yes'}; o=a['ord']
    nb=[q[k] for k in (o-1,o+1) if k in q]; dip=(a['q']-mean(nb)) if len(nb)==2 else None
    msum=sum(q.values())
    return dict(dip=dip, half_spread=a['spread']/2, rel_ask=(a['price']/a['q']-1) if a['side']=='Yes' else ((1-a['price'])/a['q']-1), depth=a['depth'] or 0.0, msum=msum, mom=a['mom'])
for cname,cond in cells.items():
    T=collections.defaultdict(list); O=collections.defaultdict(list)
    for r in rows:
        ch=next(j for j,a in enumerate(r['alts']) if a['chosen'])
        for j,a in enumerate(r['alts']):
            if not cond(a): continue
            f=feats(r,a)
            for k,v in f.items():
                if v is not None: (T if j==ch else O)[k].append(v)
    print(f"  {cname}:")
    for k in ('dip','half_spread','rel_ask','depth','msum','mom'):
        if T[k] and O[k]: print(f"     {k:12s} picked {np.mean(T[k]):+.4f} (median {np.median(T[k]):+.4f}, n={len(T[k])}) | not picked {np.mean(O[k]):+.4f} (median {np.median(O[k]):+.4f}, n={len(O[k])})")
print("\n== does the ladder dip (bucket mid below its neighbours' mean) carry information for everyone? YES mode±1 @0.1–0.5, by dip tercile ==")
L=[]
for r in rows:
    ch=next(j for j,a in enumerate(r['alts']) if a['chosen'])
    for j,a in enumerate(r['alts']):
        if cells['YES mode±1 @0.1–0.5'](a):
            f=feats(r,a)
            if f['dip'] is not None: L.append((f['dip'],win(r,a),a['price'],j==ch))
L.sort(key=lambda x:x[0]); n=len(L)
for lab,part in (('most dipped third',L[:n//3]),('middle',L[n//3:2*n//3]),('least dipped third',L[2*n//3:])):
    pk=[(w,p) for d,w,p,c in part if c]; np_=[(w,p) for d,w,p,c in part if not c]
    print(f"  {lab:20s} dip range [{part[0][0]:+.3f},{part[-1][0]:+.3f}] picked n={len(pk)} hit {mean([w for w,p in pk]):.3f} ret {eqret(pk)*100:+.1f}% | not picked n={len(np_)} hit {mean([w for w,p in np_]):.3f} ret {eqret(np_)*100:+.1f}%")
print("\n== same-event comparison: when they bought YES at mode±1, how did the OTHER neighbour (unpicked, same event) do? ==")
pairs=[]
for r in rows:
    ch=next(j for j,a in enumerate(r['alts']) if a['chosen']); a=r['alts'][ch]
    if not (a['side']=='Yes' and abs(a['d_mode'])==1): continue
    other=next((x for x in r['alts'] if x['side']=='Yes' and x['d_mode']==-a['d_mode']),None)
    if other: pairs.append((win(r,a),a['price'],win(r,other),other['price'],a['d_mode']))
print(f"  n={len(pairs)} | picked neighbour: hit {mean([x[0] for x in pairs]):.3f} price {mean([x[1] for x in pairs]):.3f} ret {eqret([(x[0],x[1]) for x in pairs])*100:+.1f}% | the other neighbour: hit {mean([x[2] for x in pairs]):.3f} price {mean([x[3] for x in pairs]):.3f} ret {eqret([(x[2],x[3]) for x in pairs])*100:+.1f}% | picked the warm side {mean([x[4]>0 for x in pairs]):.2f}")
for side,lab in ((1,'picked warm (+1)'),(-1,'picked cold (−1)')):
    P=[x for x in pairs if x[4]==side]
    print(f"   {lab}: n={len(P)} picked hit {mean([x[0] for x in P]):.3f} @ {mean([x[1] for x in P]):.3f} | other hit {mean([x[2] for x in P]):.3f} @ {mean([x[3] for x in P]):.3f}")
