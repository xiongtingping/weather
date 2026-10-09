import sys, json, gzip, math, collections, time
import numpy as np
sys.path.insert(0, '/Users/xiong/weather')
from bayes_basket_target.center_width_calibration import apply_peak_ceiling, apply_tail_mix, served_bucket_axis, TAIL_MIX_SCALE, TAIL_MIX_WEIGHT_GRID
from bayes_basket_target.center_width_store import _served_scores, _proper_score_gate
from datetime import date, timedelta
t0=time.time()
rows=[json.loads(l) for l in gzip.open('rows.jsonl.gz','rt')]
for r in rows:
    r['centers'], r['edges'] = served_bucket_axis(r['intervals'], r['keys'])
    r['prob']={k:v for k,v in zip(r['keys'],r['p'])}
    r['excluded']=tuple(k for k,v in zip(r['keys'],r['p']) if v==0.0)
    r['top']=max(r['p']); r['top_key']=r['keys'][int(np.argmax(r['p']))]
    r['far']=r['hours']>36.0
    r['base']=_served_scores(r['prob'],ordered_keys=r['keys'],settled_bucket=r['truth'],excluded_buckets=r['excluded'],key_centers_c=r['centers'])
    ti=r['keys'].index(r['truth']); mi=r['keys'].index(r['top_key']); r['d']=ti-mi
    r['tail_said']=sum(v for k,v in zip(r['keys'],r['p']) if abs(r['keys'].index(k)-mi)>=2)
N=len(rows); days=sorted({r['event_date'] for r in rows}); print('rows',N,'days',len(days), round(time.time()-t0,1),'s')
def score(vec,r): return _served_scores(vec,ordered_keys=r['keys'],settled_bucket=r['truth'],excluded_buckets=r['excluded'],key_centers_c=r['centers'])
def ceil_vec(r,c):
    if c is None or r['top']<=c: return r['prob']
    v,_=apply_peak_ceiling(r['prob'],ceiling=c,ordered_keys=r['keys'],excluded_buckets=r['excluded']); return v
def mix_vec(r,eps):
    if eps==0.0: return r['prob']
    v,_=apply_tail_mix(r['prob'],weight=eps,scale=TAIL_MIX_SCALE,ordered_keys=r['keys'],key_centers_c=r['centers'],excluded_buckets=r['excluded'],open_edge_bounds_c=r['edges']); return v
CGRID=[round(0.40+0.01*i,2) for i in range(31)]
# per-row scores for every ceiling (None = no ceiling)
LLc=np.zeros((N,len(CGRID))); SCc={}
for i,r in enumerate(rows):
    for j,c in enumerate(CGRID):
        s=r['base'] if r['top']<=c else score(ceil_vec(r,c),r)
        LLc[i,j]=s[0]; SCc[(i,j)]=s
print('ceiling grid scored', round(time.time()-t0,1),'s')
AGRID=[0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0]; K=0.40
LLa=np.zeros((N,len(AGRID))); SCa={}
for i,r in enumerate(rows):
    for j,a in enumerate(AGRID):
        c=min(0.90, K+a*(r['top']-K)) if r['top']>K else None
        s=r['base'] if (c is None or a==1.0) else score(ceil_vec(r,c),r)
        LLa[i,j]=s[0]; SCa[(i,j)]=s
print('knee grid scored', round(time.time()-t0,1),'s')
EGRID=list(TAIL_MIX_WEIGHT_GRID)
LLe=np.zeros((N,len(EGRID))); SCe={}
for i,r in enumerate(rows):
    for j,e in enumerate(EGRID):
        s=r['base'] if e==0.0 else score(mix_vec(r,e),r)
        LLe[i,j]=s[0]; SCe[(i,j)]=s
print('tail grid scored', round(time.time()-t0,1),'s')
ed=np.array([r['event_date'] for r in rows]); ka=np.array([r['known_at'] for r in rows]); base_ll=np.array([r['base'][0] for r in rows])
top=np.array([r['top'] for r in rows]); hit=np.array([r['d']==0 for r in rows],dtype=float)
stage=np.array([r['stage'] for r in rows]); far=np.array([r['far'] for r in rows]); unit=np.array([r['unit'] for r in rows])
def prefix_mask(d, group):
    dm=(date.fromisoformat(d)-timedelta(days=2)).isoformat()
    return group & (ed<=dm) & (ka<=d+'T00:00:00Z')
def walk(group_masks, choose):
    """choose(prefix_idx) -> param index or None; returns per-row chosen param (or -1) for scoring days with >=8 prefix days."""
    chosen=np.full(N,-2)  # -2 = not scored (no prefix), -1 = identity
    for g in group_masks:
        for d in days:
            pm=prefix_mask(d,g); idx=np.where(pm)[0]
            if len({ed[i] for i in idx})<8: continue
            sel=np.where(g&(ed==d))[0]
            if not len(sel): continue
            j=choose(idx); chosen[sel]= -1 if j is None else j
    return chosen
def groups(by_far=True):
    out=[]
    for s in ('P0','P0-M','P1'):
        if by_far:
            out.append((stage==s)&far); out.append((stage==s)&~far)
        else: out.append(stage==s)
    return out
def choose_cal(idx):
    best=None; bestgap=9
    for j,c in enumerate(CGRID):
        m=top[idx]>=c
        if m.sum()<100: continue
        h=hit[idx][m].mean()
        if h>c: continue
        gap=abs(h-c)
        if gap<bestgap: bestgap=gap; best=j
    return best
def choose_ll(LL):
    def f(idx):
        means=LL[idx].mean(axis=0); j=int(np.argmin(means))
        if means[j]>=base_ll[idx].mean()-1e-12: return None
        return j
    return f
def gate(mask, SC, chosen, label):
    sel=np.where(mask&(chosen>=-1))[0]
    if len(sel)<30: return None
    base=[rows[i]['base'] for i in sel]; cand=[SC[(i,chosen[i])] if chosen[i]>=0 else rows[i]['base'] for i in sel]
    g=_proper_score_gate(base,cand,[(rows[i]['station'],rows[i]['event_date']) for i in sel])
    touched=float(np.mean([chosen[i]>=0 for i in sel]))
    return dict(label=label,cells=g['cells'],days=g['station_days'],d_log=g['d_logscore'],ci_log=g['ci_logscore'],d_crps=g['d_crps'],ci_crps=g['ci_crps'],d_brier=g['d_brier'],ci_brier=g['ci_brier'],lower=g['lower'],touched=touched)
def fmt(g):
    if not g: return '  (n<30)'
    f=lambda v,ci: f"{v:+.4f} [{ci[0]:+.4f}, {ci[1]:+.4f}]" if (v is not None and ci) else '—'
    return f"  {g['label']:34s} n={g['cells']:5d}/{g['days']:4d}d touched {g['touched']:.2f} | ΔLog {f(g['d_log'],g['ci_log'])} | ΔCRPS {f(g['d_crps'],g['ci_crps'])} | ΔBrier {f(g['d_brier'],g['ci_brier'])} | {'PASS' if g['lower'] else 'no'}"
half=days[len(days)//2]
def report(name, SC, chosen, scope_mask):
    print(f"\n== {name} ==")
    out=[]
    for lab,m in (('scope, walk-forward rows',scope_mask),('  first half',scope_mask&(ed<half)),('  second half',scope_mask&(ed>=half)),('  current segment ≥10-05',scope_mask&(ed>='2026-10-05')),('  P0',scope_mask&(stage=='P0')),('  P0-M',scope_mask&(stage=='P0-M')),('  P1',scope_mask&(stage=='P1')),('  >36h',scope_mask&far),('  ≤36h',scope_mask&~far),('  unit C',scope_mask&(unit=='C')),('  unit F',scope_mask&(unit=='F')),('ALL rows (outside scope = identity)',np.ones(N,bool))):
        g=gate(m,SC,chosen,lab); print(fmt(g)); out.append(g)
    return out
def peak_diag(SC, chosen, scope_mask, name):
    sel=np.where(scope_mask&(chosen>=-1))[0]
    print(f"  top-bucket said→landed before / after ({name}):")
    for lo,hi in ((0.4,0.5),(0.5,0.6),(0.6,1.01)):
        m=[i for i in sel if lo<=top[i]<hi]
        if len(m)<30: continue
        after=[]
        for i in m:
            v = ceil_vec(rows[i], CGRID[chosen[i]]) if (SC is SCc and chosen[i]>=0) else (ceil_vec(rows[i], min(0.90, K+AGRID[chosen[i]]*(top[i]-K))) if (SC is SCa and chosen[i]>=0) else rows[i]['prob'])
            after.append(v[rows[i]['top_key']])
        print(f"    p_top {lo:.1f}-{hi:.1f}: n={len(m)} said {top[m].mean():.3f} → {np.mean(after):.3f}, landed {hit[m].mean():.3f}")
def tail_diag(chosen, scope_mask, name):
    sel=np.where(scope_mask&(chosen>=-1))[0]
    if len(sel)<30: return
    before=np.mean([rows[i]['tail_said'] for i in sel]); landed=np.mean([abs(rows[i]['d'])>=2 for i in sel])
    after=[]
    for i in sel:
        v=mix_vec(rows[i],EGRID[chosen[i]]) if chosen[i]>=0 else rows[i]['prob']; mi=rows[i]['keys'].index(rows[i]['top_key'])
        after.append(sum(x for k,x in v.items() if abs(rows[i]['keys'].index(k)-mi)>=2))
    eps_used=collections.Counter(EGRID[chosen[i]] if chosen[i]>=0 else 0.0 for i in sel)
    print(f"  tail |d|≥2 said→landed ({name}): said {before:.3f} → {np.mean(after):.3f}, landed {landed:.3f}; ε chosen {dict(sorted(eps_used.items()))}")
results={}
allmask=np.ones(N,bool)
ch_cal=walk(groups(True),choose_cal); results['K_cal']=report('K_cal: ceiling by calibration match, per stage × {>36h,≤36h}',SCc,ch_cal,allmask); peak_diag(SCc,ch_cal,allmask,'K_cal')
print('  c chosen (distribution):',dict(sorted(collections.Counter(CGRID[j] if j>=0 else None for j in ch_cal if j>=-1).items(),key=lambda kv:(kv[0] is None, kv[0]))))
ch_ll=walk(groups(True),choose_ll(LLc)); results['K_ll']=report('K_ll: ceiling by prefix LogScore, per stage × {>36h,≤36h}',SCc,ch_ll,allmask); peak_diag(SCc,ch_ll,allmask,'K_ll')
print('  c chosen:',dict(sorted(collections.Counter(CGRID[j] if j>=0 else None for j in ch_ll if j>=-1).items(),key=lambda kv:(kv[0] is None, kv[0]))))
ch_a=walk(groups(True),choose_ll(LLa)); results['S_knee']=report('S_knee: ceiling = 0.40 + α(p_top−0.40), α by prefix LogScore',SCa,ch_a,allmask); peak_diag(SCa,ch_a,allmask,'S_knee')
print('  α chosen:',dict(sorted(collections.Counter(AGRID[j] if j>=0 else None for j in ch_a if j>=-1).items(),key=lambda kv:(kv[0] is None, kv[0]))))
for scope_name,scope in (('P0',stage=='P0'),('F stations (all stages)',unit=='F'),('P0 & >36h',(stage=='P0')&far),('P0-M',stage=='P0-M'),('P1',stage=='P1'),('P0 & F',(stage=='P0')&(unit=='F')),('P0 & C',(stage=='P0')&(unit=='C'))):
    ch=walk([scope],choose_ll(LLe)); results[f'T_eps[{scope_name}]']=report(f'T_ε on {scope_name}: tail mix κ=1.5, ε by prefix LogScore',SCe,ch,scope); tail_diag(ch,scope,scope_name)
# combined on P0: K_ll then tail
print("\n== KT on P0: K_ll ceiling then T_ε (each with its own walk-forward choice) ==")
chK=ch_ll; scope=stage=='P0'; chT=walk([scope],choose_ll(LLe))
sel=np.where(scope&(chK>=-1)&(chT>=-1))[0]; base=[]; cand=[]; cl=[]
for i in sel:
    r=rows[i]; v=r['prob']
    if chK[i]>=0: v=ceil_vec(r,CGRID[chK[i]])
    if chT[i]>=0:
        if chK[i]>=0:
            v,_=apply_tail_mix(v,weight=EGRID[chT[i]],scale=TAIL_MIX_SCALE,ordered_keys=r['keys'],key_centers_c=r['centers'],excluded_buckets=r['excluded'],open_edge_bounds_c=r['edges'])
        else: v=mix_vec(r,EGRID[chT[i]])
    base.append(r['base']); cand.append(score(v,r)); cl.append((r['station'],r['event_date']))
g=_proper_score_gate(base,cand,cl); print(fmt(dict(label='P0 rows',cells=g['cells'],days=g['station_days'],d_log=g['d_logscore'],ci_log=g['ci_logscore'],d_crps=g['d_crps'],ci_crps=g['ci_crps'],d_brier=g['d_brier'],ci_brier=g['ci_brier'],lower=g['lower'],touched=float(np.mean([(chK[i]>=0) or (chT[i]>=0) for i in sel])))))
json.dump({k:[g for g in v if g] for k,v in results.items()},open('results.json','w'),ensure_ascii=False,indent=1)
print('done', round(time.time()-t0,1),'s')
