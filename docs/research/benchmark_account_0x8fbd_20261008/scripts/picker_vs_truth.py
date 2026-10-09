"""Three pickers against settled truth on the same station-days: the cluster's bucket,
the market's modal bucket, our served modal bucket -- hit rates, what each said vs what
landed, and where ours is wrong (position, direction, stage, lead, unit, station group)."""
import json, collections, statistics as st, math
E=json.load(open('events_enriched.json'))
FM=json.load(open('fills_model.json'))
fills = FM if isinstance(FM,list) else next(v for v in FM.values() if isinstance(v,list))
model_by=( {} )
for f in fills:
    model_by[(f['cid'], f['ts'], f.get('taker'))]=f['model']
HI={'ZHHH','LIMC','EGLC','RCSS','RKSI','LTAC'}
def mean(xs): return sum(xs)/len(xs) if xs else float('nan')
def ll(p,h): p=min(.999,max(.001,p)); return -(h*math.log(p)+(1-h)*math.log(1-p))
rows=[]
for e in E:
    if not (e.get('market') and e.get('p0') and e['p0'].get('p_my') is not None): continue
    if not e['first_taker'] or e.get('truth_ord') is None: continue
    m=model_by.get((e['cid'],e['ts'],True)) or model_by.get((e['cid'],e['ts'],None)) or {}
    stg=e['p0']['stage']; mm=m.get(stg) or {}
    our_mode_ord=mm.get('mode_ord'); our_mode_p=mm.get('mode_p')
    rows.append(dict(st=e['station'],ed=e['event_date'],side=e['outcome'],my=e['my_ord'],tr=e['truth_ord'],
        mk=e['market']['mode'],d_mk=e['market']['d'],q_my=e['market']['q_my'],q_mode=e['market']['q_mode'],
        p_my=e['p0']['p_my'],p_mode_mkt=e['p0']['p_mode_mkt'],stage=stg,our_mode=our_mode_ord,our_mode_p=our_mode_p,
        lead=e['lead_h'],unit=e['unit'],usdc=e['usdc'],hi=e['station'] in HI,n2=e['market']['n2']))
print('first-taker events with market+our p:',len(rows),'| with our mode:',sum(1 for r in rows if r['our_mode'] is not None))
yes=[r for r in rows if r['side']=='Yes']; no=[r for r in rows if r['side']=='No']
print('YES',len(yes),'NO',len(no))
def pick_table(R,tag):
    R=[r for r in R if r['our_mode'] is not None]
    if len(R)<15: return
    hit_their=mean([r['tr']==r['my'] for r in R]); hit_mk=mean([r['tr']==r['mk'] for r in R]); hit_our=mean([r['tr']==r['our_mode'] for r in R])
    said_their_p=mean([r['p_my'] for r in R]); said_their_q=mean([r['q_my'] for r in R])
    said_our_mode=mean([r['our_mode_p'] for r in R]); said_mk_mode=mean([r['q_mode'] for r in R])
    same=mean([r['our_mode']==r['mk'] for r in R]); same_their=mean([r['my']==r['mk'] for r in R]); same_ours=mean([r['my']==r['our_mode'] for r in R])
    print(f"  {tag:30s} n={len(R):4d} | hit: their {hit_their:.3f} mkt-mode {hit_mk:.3f} our-mode {hit_our:.3f} | said on their bucket: ours {said_their_p:.3f} mkt {said_their_q:.3f} | said on own mode: ours {said_our_mode:.3f} mkt {said_mk_mode:.3f} | our mode=mkt mode {same:.2f}, their=mkt {same_their:.2f}, their=ours {same_ours:.2f}")
print("\n== A. YES first-taker buys: three pickers on the same events ==")
pick_table(yes,'ALL YES')
for stg in ('P0','P0-M','P1'): pick_table([r for r in yes if r['stage']==stg],f'stage {stg}')
for u in ('C','F'): pick_table([r for r in yes if r['unit']==u],f'unit {u}')
pick_table([r for r in yes if r['hi']],'their high-info stations'); pick_table([r for r in yes if not r['hi']],'other stations')
for lo,hi in ((0,12),(12,24),(24,48),(48,200)): pick_table([r for r in yes if lo<=r['lead']<hi],f'lead {lo}-{hi}h')
q=sorted(r['usdc'] for r in yes); t1,t2=q[len(q)//3],q[2*len(q)//3]
pick_table([r for r in yes if r['usdc']<t1],'size small'); pick_table([r for r in yes if t1<=r['usdc']<t2],'size mid'); pick_table([r for r in yes if r['usdc']>=t2],'size large')
print("\n== B. Their YES bucket relative to OUR mode (ordinal, + = warmer) ==")
print("  rel   n   landed  our p   mkt q   their-bucket share | truth rel to our mode: mean, P(warm) P(at) P(cold)")
for rel in (-3,-2,-1,0,1,2,3):
    R=[r for r in yes if r['our_mode'] is not None and max(-3,min(3,r['my']-r['our_mode']))==rel]
    if len(R)<10: continue
    tr_rel=[r['tr']-r['our_mode'] for r in R]
    print(f"  {rel:+d}   {len(R):4d}  {mean([r['tr']==r['my'] for r in R]):.3f}  {mean([r['p_my'] for r in R]):.3f}  {mean([r['q_my'] for r in R]):.3f}  {len(R)/len(yes):.2f} | {mean(tr_rel):+.2f}, {mean([t>0 for t in tr_rel]):.2f} {mean([t==0 for t in tr_rel]):.2f} {mean([t<0 for t in tr_rel]):.2f}")
print("\n== C. Their YES bucket relative to MARKET mode ==")
for rel in (-3,-2,-1,0,1,2,3):
    R=[r for r in yes if max(-3,min(3,r['d_mk']))==rel]
    if len(R)<10: continue
    print(f"  {rel:+d}   n={len(R):4d}  landed {mean([r['tr']==r['my'] for r in R]):.3f}  our p {mean([r['p_my'] for r in R]):.3f}  mkt q {mean([r['q_my'] for r in R]):.3f}  LL ours {mean([ll(r['p_my'],r['tr']==r['my']) for r in R]):.3f} mkt {mean([ll(r['q_my'],r['tr']==r['my']) for r in R]):.3f}")
print("\n== D. Reliability of OUR p on their YES buckets vs on OUR own mode ==")
bins=[(0,.05),(.05,.1),(.1,.2),(.2,.3),(.3,.4),(.4,.5),(.5,.7),(.7,1.01)]
print("  our p bin | on THEIR bucket: n said landed (mkt said) | on OUR mode: n said landed (mkt said)")
for lo,hi in bins:
    A=[r for r in yes if lo<=r['p_my']<hi]; B=[r for r in yes if r['our_mode_p'] is not None and lo<=r['our_mode_p']<hi]
    a=f"{len(A):4d} {mean([r['p_my'] for r in A]):.3f} {mean([r['tr']==r['my'] for r in A]):.3f} ({mean([r['q_my'] for r in A]):.3f})" if A else '   -'
    b=f"{len(B):4d} {mean([r['our_mode_p'] for r in B]):.3f} {mean([r['tr']==r['our_mode'] for r in B]):.3f} ({mean([r['q_mode'] for r in B]):.3f})" if B else '   -'
    print(f"  {lo:.2f}-{hi:.2f} | {a} | {b}")
print("\n== E. When they and we disagree on the mode (their bucket != our mode): who is right ==")
D=[r for r in yes if r['our_mode'] is not None and r['my']!=r['our_mode']]
print(f"  n={len(D)} their bucket landed {mean([r['tr']==r['my'] for r in D]):.3f} | our mode landed {mean([r['tr']==r['our_mode'] for r in D]):.3f} | neither {mean([r['tr']!=r['my'] and r['tr']!=r['our_mode'] for r in D]):.3f} | truth on their side of our mode {mean([(r['tr']-r['our_mode'])*(r['my']-r['our_mode'])>0 for r in D]):.3f}")
for stg in ('P0','P0-M','P1'):
    Ds=[r for r in D if r['stage']==stg]
    if len(Ds)>=15: print(f"   {stg}: n={len(Ds)} their {mean([r['tr']==r['my'] for r in Ds]):.3f} ours {mean([r['tr']==r['our_mode'] for r in Ds]):.3f} truth-their-side {mean([(r['tr']-r['our_mode'])*(r['my']-r['our_mode'])>0 for r in Ds]):.3f}")
for grp,Dg in (('hi-info',[r for r in D if r['hi']]),('others',[r for r in D if not r['hi']])):
    if len(Dg)>=15: print(f"   {grp}: n={len(Dg)} their {mean([r['tr']==r['my'] for r in Dg]):.3f} ours {mean([r['tr']==r['our_mode'] for r in Dg]):.3f} truth-their-side {mean([(r['tr']-r['our_mode'])*(r['my']-r['our_mode'])>0 for r in Dg]):.3f}")
print("\n== F. NO first-taker buys (they bet the bucket does NOT land) ==")
N=[r for r in no if r['our_mode'] is not None]
print(f"  n={len(N)} bucket landed {mean([r['tr']==r['my'] for r in N]):.3f} | our p on it {mean([r['p_my'] for r in N]):.3f} mkt q {mean([r['q_my'] for r in N]):.3f} | share where bucket==mkt mode {mean([r['my']==r['mk'] for r in N]):.2f}, ==our mode {mean([r['my']==r['our_mode'] for r in N]):.2f}")
for rel in (-2,-1,0,1,2):
    R=[r for r in N if max(-2,min(2,r['my']-r['our_mode']))==rel]
    if len(R)>=10: print(f"   rel to our mode {rel:+d}: n={len(R)} landed {mean([r['tr']==r['my'] for r in R]):.3f} our p {mean([r['p_my'] for r in R]):.3f} mkt q {mean([r['q_my'] for r in R]):.3f}")
