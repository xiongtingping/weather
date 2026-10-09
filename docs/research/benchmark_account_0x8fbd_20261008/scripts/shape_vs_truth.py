"""Neutral baseline (1,480 station-days x 6 local moments): what our served vector says at each
position relative to its own mode vs what landed; the market's q at the same positions; centre
bias in bucket units; split by moment/stage/unit and by cluster-traded vs untouched station-days."""
import json, collections, math
B=json.load(open('baseline3.json')); E=json.load(open('events_enriched.json'))
traded={(e['station'],e['event_date']) for e in E}
def mean(xs): return sum(xs)/len(xs) if xs else float('nan')
recs=[]
for key,v in B.items():
    st,ed=key.split('|'); tb=v.get('settled_bucket')
    if not tb: continue
    for mom,m in v.items():
        if not isinstance(m,dict) or not m.get('ours'): continue
        o=m['ours']; p=o['p']; ordn=o['ord']
        if tb not in ordn: continue
        tr=ordn[tb]; mode=o['mode']; mo=ordn[mode]
        # mean ordinal (centre) from the vector
        mu=sum(ordn[b]*pv for b,pv in p.items()); 
        byrel=collections.defaultdict(float)
        for b,pv in p.items(): byrel[max(-3,min(3,ordn[b]-mo))]+=pv
        q=m.get('market_q'); qrel=None; qmode=None
        if q and sum(q.values())>0:
            s=sum(q.values()); qn={b:x/s for b,x in q.items() if b in ordn}
            qrel=collections.defaultdict(float)
            for b,x in qn.items(): qrel[max(-3,min(3,ordn[b]-mo))]+=x
            qmode=m.get('market_mode')
        unit='F' if '°F' in tb else 'C'
        recs.append(dict(st=st,ed=ed,mom=mom,stage=o['stage'],mode=mo,tr=tr,mu=mu,rel=max(-3,min(3,tr-mo)),said=byrel,qsaid=qrel,
                         qmode_ord=ordn.get(qmode) if qmode in ordn else None,unit=unit,traded=(st,ed) in traded,n=len(p)))
print('station-day moments with our vector:',len(recs),'| with market:',sum(1 for r in recs if r['qsaid']),'| cluster-traded station-days:',len({(r['st'],r['ed']) for r in recs if r['traded']}),'of',len({(r['st'],r['ed']) for r in recs}))
def shape(R,tag):
    if len(R)<30: return
    line=f"  {tag:28s} n={len(R):4d} | centre bias (truth−mean, buckets) {mean([r['tr']-r['mu'] for r in R]):+.3f} | mode hit: said {mean([r['said'][0] for r in R]):.3f} landed {mean([r['rel']==0 for r in R]):.3f} |"
    for rel,lab in ((-2,'−2'),(-1,'−1'),(1,'+1'),(2,'+2')):
        line+=f" {lab}: said {mean([r['said'][rel] for r in R]):.3f} landed {mean([r['rel']==rel for r in R]):.3f} |"
    line+=f" |d|≥3: said {mean([r['said'][3]+r['said'][-3] for r in R]):.3f} landed {mean([abs(r['rel'])>=3 for r in R]):.3f}"
    print(line)
    Q=[r for r in R if r['qsaid']]
    if len(Q)>=30:
        line=f"  {'  market at same positions':28s} n={len(Q):4d} | mkt mode hit: landed {mean([r['tr']==r['qmode_ord'] for r in Q if r['qmode_ord'] is not None]):.3f} | at OUR mode: mkt said {mean([r['qsaid'][0] for r in Q]):.3f} ours {mean([r['said'][0] for r in Q]):.3f} landed {mean([r['rel']==0 for r in Q]):.3f} |"
        for rel,lab in ((-2,'−2'),(-1,'−1'),(1,'+1'),(2,'+2')):
            line+=f" {lab}: mkt {mean([r['qsaid'][rel] for r in Q]):.3f} ours {mean([r['said'][rel] for r in Q]):.3f} landed {mean([r['rel']==rel for r in Q]):.3f} |"
        print(line)
print("\n== G. Shape vs truth on the neutral baseline (position relative to OUR mode; said = our mass, landed = frequency) ==")
shape(recs,'ALL moments')
for mom in ('D-2 12:00','D-1 06:00','D-1 12:00','D-1 18:00','D0 02:00','D0 08:00'): shape([r for r in recs if r['mom']==mom],mom)
for stg in ('P0','P0-M','P1'): shape([r for r in recs if r['stage']==stg],f'stage {stg}')
for u in ('C','F'): shape([r for r in recs if r['unit']==u],f'unit {u}')
shape([r for r in recs if r['traded']],'cluster-traded station-days'); shape([r for r in recs if not r['traded']],'untouched station-days')
for stg in ('P0','P0-M','P1'):
    shape([r for r in recs if r['traded'] and r['stage']==stg],f'  traded & {stg}'); shape([r for r in recs if not r['traded'] and r['stage']==stg],f'  untouched & {stg}')
print("\n== H. Direction of truth vs our mode (warm/at/cold) and the claimed warm/cold mass ==")
for tag,R in (('ALL',recs),('P0',[r for r in recs if r['stage']=='P0']),('P0-M',[r for r in recs if r['stage']=='P0-M']),('P1',[r for r in recs if r['stage']=='P1']),('C',[r for r in recs if r['unit']=='C']),('F',[r for r in recs if r['unit']=='F'])):
    warm_said=mean([sum(v for k,v in r['said'].items() if k>0) for r in R]); cold_said=mean([sum(v for k,v in r['said'].items() if k<0) for r in R])
    print(f"  {tag:5s} n={len(R):4d} truth warm of mode {mean([r['rel']>0 for r in R]):.3f} (said {warm_said:.3f}) | at mode {mean([r['rel']==0 for r in R]):.3f} (said {mean([r['said'][0] for r in R]):.3f}) | cold {mean([r['rel']<0 for r in R]):.3f} (said {cold_said:.3f}) | mean truth−mean {mean([r['tr']-r['mu'] for r in R]):+.3f} buckets")
print("\n== I. Our mode's claimed probability vs its hit, by claimed bin (baseline) ==")
for lo,hi in ((0,.3),(.3,.4),(.4,.5),(.5,.6),(.6,1.01)):
    R=[r for r in recs if lo<=r['said'][0]<hi]
    if len(R)>=30: print(f"  mode p {lo:.1f}-{hi:.1f}: n={len(R):4d} said {mean([r['said'][0] for r in R]):.3f} landed {mean([r['rel']==0 for r in R]):.3f} | +1 said {mean([r['said'][1] for r in R]):.3f} landed {mean([r['rel']==1 for r in R]):.3f} | −1 said {mean([r['said'][-1] for r in R]):.3f} landed {mean([r['rel']==-1 for r in R]):.3f}")
