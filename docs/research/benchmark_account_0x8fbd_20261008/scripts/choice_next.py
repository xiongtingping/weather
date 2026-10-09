"""(1) Does their side call predict where the NEXT served model decision moves (mean shift, next P0/P0-M after +3h, +9h)?
(2) Are their same-day calls coherent within a region more than truth itself is?  (3) Per-station within-cell gap."""
import json, gzip, math, re, collections, sqlite3
import numpy as np
from datetime import datetime, timezone, timedelta
rows=[json.loads(l) for l in gzip.open('choice_rows.jsonl.gz','rt')]
hot=sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=30); hot.execute('PRAGMA query_only=1')
def iso(ts): return datetime.fromtimestamp(ts,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
def mean(x): return sum(x)/len(x) if x else float('nan')
def served_mean(st,ed,after_ts,stage_set=('P0','P0-M')):
    h=hot.execute("select history_id, stage, decision_at_utc from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc>=? and stage in (?,?) order by decision_at_utc asc limit 1",(st,ed,iso(after_ts),*stage_set)).fetchone()
    if not h: return None
    P={o:p for o,p in hot.execute("select ordinal,probability from basket_probability_bridge_bucket_probability where history_id=?",(h[0],))}
    s=sum(P.values()); return (sum(o*p for o,p in P.items())/s if s>0 else None, h[1], h[2])
pairs=[]
for r in rows:
    ch=next(j for j,a in enumerate(r['alts']) if a['chosen']); a=r['alts'][ch]
    if not (a['side']=='Yes' and abs(a['d_mode'])==1): continue
    yes={x['d_mode']:x for x in r['alts'] if x['side']=='Yes'}
    if 1 not in yes or -1 not in yes: continue
    P={x['ord']:x['p'] for x in yes.values()}; s=sum(P.values()); m0=sum(o*p for o,p in P.items())/s
    skew=1 if yes[1]['q']>yes[-1]['q'] else -1
    pairs.append(dict(st=r['station'],ed=r['event_date'],ts=r['ts'],side=a['d_mode'],truth_side=int(np.sign(r['truth_ord']-r['mode_ord'])),skew=skew,m0=m0,mode=r['mode_ord'],lead=r['lead_h'],usdc=r['usdc'],hit=1.0 if a['ord']==r['truth_ord'] else 0.0,stage=r['stage']))
print('pairs',len(pairs))
print("\n== (1) next served model decision after their fill: does the mean move toward their side? ==")
for gap_h,label in ((3,'first P0/P0-M decision ≥3h later'),(9,'first ≥9h later'),(20,'first ≥20h later')):
    rec=[]
    for p in pairs:
        nxt=served_mean(p['st'],p['ed'],p['ts']+gap_h*3600)
        if not nxt or nxt[0] is None: continue
        d=nxt[0]-p['m0']; rec.append((p,d))
    if len(rec)<40: continue
    tow=[np.sign(d)==p['side'] for p,d in rec if abs(d)>0.05]; away=[np.sign(d)==-p['side'] for p,d in rec if abs(d)>0.05]
    tow_sk=[np.sign(d)==p['skew'] for p,d in rec if abs(d)>0.05]; tow_tr=[np.sign(d)==p['truth_side'] for p,d in rec if abs(d)>0.05 and p['truth_side']!=0]
    mag=[d*p['side'] for p,d in rec]
    print(f"  {label:34s} n={len(rec)} (|Δmean|>0.05 bucket: {len(tow)}) | model moves toward their side {mean(tow):.3f} vs away {mean(away):.3f} | toward market-skew side {mean(tow_sk):.3f} | toward truth side {mean(tow_tr):.3f} | mean signed shift toward their side {mean(mag):+.3f} buckets")
    big=[(p,d) for p,d in rec if p['usdc']>=50]
    if len(big)>=30:
        tw=[np.sign(d)==p['side'] for p,d in big if abs(d)>0.05]; aw=[np.sign(d)==-p['side'] for p,d in big if abs(d)>0.05]
        print(f"      ≥50 U legs: toward {mean(tw):.3f} vs away {mean(aw):.3f} (n={len(tw)})")
print("\n== (2) regional coherence of same-day direction calls (region = ICAO prefix) ==")
def region(st):
    c=st[0]
    return {'K':'US','C':'Canada','E':'N.Europe','L':'S.Europe','Z':'China','R':'Japan/Korea/Taiwan/Phil','V':'S.Asia','W':'SE Asia','Y':'Australia','N':'NZ/Pacific','S':'S.America','F':'Africa','O':'Mid-East','U':'Russia','M':'Mexico/CAm'}.get(c,c)
byday=collections.defaultdict(list)
for p in pairs: byday[(region(p['st']),p['ed'])].append(p)
agree_calls=[];agree_truth=[];n_groups=0
for key,P in byday.items():
    sts={}
    for p in P: sts.setdefault(p['st'],p)  # one call per station-day (first)
    L=list(sts.values())
    if len(L)<2: continue
    n_groups+=1
    for i in range(len(L)):
        for j in range(i+1,len(L)):
            agree_calls.append(L[i]['side']==L[j]['side'])
            if L[i]['truth_side']!=0 and L[j]['truth_side']!=0: agree_truth.append(L[i]['truth_side']==L[j]['truth_side'])
print(f"  region-days with ≥2 stations called: {n_groups} | pairwise same-side rate of their calls {mean(agree_calls):.3f} (n={len(agree_calls)}; independence with 52% warm → {0.52**2+0.48**2:.3f}) | pairwise same-side rate of TRUTH (off-mode days) {mean(agree_truth):.3f} (n={len(agree_truth)})")
# permutation null for their calls: shuffle sides across pairs
rng=np.random.default_rng(1); sides=[p['side'] for p in pairs]; nulls=[]
for _ in range(500):
    perm=rng.permutation(sides); k=0; agree=[]
    for key,P in byday.items():
        sts={}
        for p in P: sts.setdefault(p['st'],p)
        L=list(sts.values())
        if len(L)<2: continue
        ids=[pairs.index(p) for p in L]
        for i in range(len(ids)):
            for j in range(i+1,len(ids)): agree.append(perm[ids[i]]==perm[ids[j]])
    nulls.append(mean(agree))
print(f"  permutation null for their calls: {np.mean(nulls):.3f} [{np.percentile(nulls,2.5):.3f}, {np.percentile(nulls,97.5):.3f}]")
print("\n== (3) per-station: direction-call accuracy (their side vs other side) and within-cell gap, n≥15 ==")
bst=collections.defaultdict(list)
for p in pairs: bst[p['st']].append(p)
cell=collections.defaultdict(lambda:{'picked':[],'not':[]})
for r in rows:
    ch=next(j for j,a in enumerate(r['alts']) if a['chosen'])
    for j,a in enumerate(r['alts']):
        if a['side']=='Yes' and abs(a['d_mode'])==1 and 0.1<=a['price']<0.5:
            cell[r['station']]['picked' if j==ch else 'not'].append(1.0 if a['ord']==r['truth_ord'] else 0.0)
tbl=[]
for st,P in bst.items():
    if len(P)<15: continue
    tbl.append((st,len(P),mean([p['truth_side']==p['side'] for p in P]),mean([p['truth_side']==-p['side'] for p in P]),mean(cell[st]['picked']) if cell[st]['picked'] else float('nan'),mean(cell[st]['not']) if cell[st]['not'] else float('nan'),len(cell[st]['picked'])))
for st,n,a,b,pk,nt,npk in sorted(tbl,key=lambda t:-(t[2]-t[3])):
    print(f"  {st} {region(st):24s} calls n={n:3d} their side {a:.2f} vs other {b:.2f} (edge {a-b:+.2f}) | cell YES±1: picked hit {pk:.2f} (n={npk}) vs not {nt:.2f}")
