"""Decisive split: inside 'YES on a neighbour of the market mode', is their edge over unpicked lookalikes explained
by the market's own skew (the dearer neighbour), or is there residual private information?  Then: is 'buy the
dearer neighbour at the ask' a market rule for everyone (neutral baseline, all station-days)?"""
import json, gzip, math, collections
import numpy as np
rows=[json.loads(l) for l in gzip.open('choice_rows.jsonl.gz','rt')]
def mean(x): return sum(x)/len(x) if x else float('nan')
def eqret(L): return (sum(w for w,p in L)-sum(p for w,p in L))/sum(p for w,p in L) if L else float('nan')
def win(r,a): return 1.0 if ((a['ord']==r['truth_ord']) if a['side']=='Yes' else (a['ord']!=r['truth_ord'])) else 0.0
print("== A. YES on mode±1 priced 0.1–0.5, split by whether the bucket is the DEARER neighbour (market skew side) ==")
cells=collections.defaultdict(list)
for r in rows:
    ch=next(j for j,a in enumerate(r['alts']) if a['chosen'])
    yes={x['d_mode']:x for x in r['alts'] if x['side']=='Yes'}
    if 1 not in yes or -1 not in yes: continue
    dearer=1 if yes[1]['q']>yes[-1]['q'] else -1 if yes[-1]['q']>yes[1]['q'] else 0
    for j,a in enumerate(r['alts']):
        if a['side']=='Yes' and abs(a['d_mode'])==1 and 0.1<=a['price']<0.5:
            grp='dearer' if a['d_mode']==dearer else ('cheaper' if dearer!=0 else 'tie')
            cells[(grp, 'picked' if j==ch else 'not')].append((win(r,a),a['price']))
for grp in ('dearer','cheaper'):
    for pk in ('picked','not'):
        L=cells[(grp,pk)]; print(f"  {grp:8s} {pk:7s} n={len(L):4d} hit {mean([w for w,p in L]):.3f} price {mean([p for w,p in L]):.3f} equal-dollar return {eqret(L)*100:+.1f}%")
print("\n== B. same events: the pair of neighbours when they bought one of them ==")
pairs=[]
for r in rows:
    ch=next(j for j,a in enumerate(r['alts']) if a['chosen']); a=r['alts'][ch]
    if not (a['side']=='Yes' and abs(a['d_mode'])==1): continue
    yes={x['d_mode']:x for x in r['alts'] if x['side']=='Yes'}
    if 1 not in yes or -1 not in yes: continue
    d=1 if yes[1]['q']>yes[-1]['q'] else -1
    pairs.append(dict(dearer=(win(r,yes[d]),yes[d]['price']),cheaper=(win(r,yes[-d]),yes[-d]['price']),picked_dearer=(a['d_mode']==d),pick=(win(r,a),a['price'])))
print(f"  n={len(pairs)} | dearer neighbour at ask: hit {mean([p['dearer'][0] for p in pairs]):.3f} price {mean([p['dearer'][1] for p in pairs]):.3f} ret {eqret([p['dearer'] for p in pairs])*100:+.1f}% | cheaper: hit {mean([p['cheaper'][0] for p in pairs]):.3f} price {mean([p['cheaper'][1] for p in pairs]):.3f} ret {eqret([p['cheaper'] for p in pairs])*100:+.1f}% | they picked the dearer {mean([p['picked_dearer'] for p in pairs]):.2f}")
for lab,sel in (('picked dearer',True),('picked cheaper',False)):
    P=[p for p in pairs if p['picked_dearer']==sel]
    print(f"   {lab:14s} n={len(P)} their pick hit {mean([p['pick'][0] for p in P]):.3f} @ {mean([p['pick'][1] for p in P]):.3f} ret {eqret([p['pick'] for p in P])*100:+.1f}% | the other neighbour hit {mean([(p['cheaper'] if sel else p['dearer'])[0] for p in P]):.3f}")
print("\n== C. neutral baseline (1,480 station-days x moments with quotes): 'buy the dearer neighbour of the market mode at the ask' for everyone ==")
B=json.load(open('baseline3.json')); E=json.load(open('events_enriched.json')); traded={(e['station'],e['event_date']) for e in E}
res=collections.defaultdict(list)
for key,v in B.items():
    st,ed=key.split('|'); tb=v.get('settled_bucket')
    if not tb: continue
    for mom,m in v.items():
        if not isinstance(m,dict) or not m.get('quotes') or not m.get('market_mode'): continue
        Q=m['quotes']; mode=m['market_mode']; mo=Q[mode]['ord']
        nb={d:next((b for b,x in Q.items() if x['ord']==mo+d),None) for d in (1,-1)}
        if not nb[1] or not nb[-1]: continue
        a1,a2=Q[nb[1]],Q[nb[-1]]
        if a1.get('ask') is None or a2.get('ask') is None or a1.get('bid') is None or a2.get('bid') is None: continue
        m1=(a1['bid']+a1['ask'])/2; m2=(a2['bid']+a2['ask'])/2
        if m1==m2: continue
        d=1 if m1>m2 else -1; dear=Q[nb[d]]; cheap=Q[nb[-d]]
        hitd=1.0 if nb[d]==tb else 0.0; hitc=1.0 if nb[-d]==tb else 0.0
        for tag in ('ALL',mom,'traded' if (st,ed) in traded else 'untouched'):
            res[tag].append((hitd,dear['ask'],hitc,cheap['ask'],1.0 if mode==tb else 0.0,Q[mode]['ask']))
for tag in ['ALL','D-2 12:00','D-1 06:00','D-1 12:00','D-1 18:00','D0 02:00','D0 08:00','traded','untouched']:
    L=res.get(tag,[])
    if len(L)<30: continue
    print(f"  {tag:10s} n={len(L):4d} | dearer nb: hit {mean([x[0] for x in L]):.3f} ask {mean([x[1] for x in L]):.3f} ret {eqret([(x[0],x[1]) for x in L])*100:+.1f}% | cheaper nb: hit {mean([x[2] for x in L]):.3f} ask {mean([x[3] for x in L]):.3f} ret {eqret([(x[2],x[3]) for x in L])*100:+.1f}% | mode: hit {mean([x[4] for x in L]):.3f} ask {mean([x[5] for x in L]):.3f} ret {eqret([(x[4],x[5]) for x in L])*100:+.1f}%")
