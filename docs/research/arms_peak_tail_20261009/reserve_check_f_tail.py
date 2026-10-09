"""PROTOCOL 落地前复算: the F-station >36h tail mix re-served from the real envelope receipts' recorded input
with production's own functions, in production's order (width pair -> tail mix -> peak ceiling), judged by the 240 gate."""
import sys, json, math, collections, time, sqlite3
import numpy as np
sys.path.insert(0,'/Users/xiong/weather')
from bayes_basket_target import center_width_store as CW
from bayes_basket_target.center_width_calibration import apply_tail_mix, apply_peak_ceiling, TAIL_MIX_SCALE
from bayes_basket_target.center_width_store import _served_scores, _proper_score_gate
from datetime import date, timedelta
t0=time.time()
conn=sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=60); conn.execute('PRAGMA query_only=1')
F_ST={'KATL','KAUS','KBKF','KDAL','KHOU','KLAX','KLGA','KMIA','KORD','KSEA','KSFO'}
truth={k:v for k,v in CW._truth(conn).items() if k[0] in F_ST}
print('F station-days with truth', len(truth))
by_day=CW.serving_cells_by_day(conn, truth, stage='P0', min_decision_at_utc='2026-09-05T00:00:00Z')
cells=[c for day in sorted(by_day) for c in by_day[day]]
print('P0 cells (F stations)', len(cells), 'with recorded input', sum(1 for c in cells if c.get('reserved_under_current_width')), round(time.time()-t0),'s')
far=[c for c in cells if c.get('reserved_under_current_width') and (c.get('hours_to_settlement') or 0)>36.0]
print('>36h cells', len(far), '| with receipt ceiling', sum(1 for c in far if (c.get('center_width_receipt') or {}).get('peak_ceiling')), '| with slice tail weight', sum(1 for c in far if ((c.get('center_width_receipt') or {}).get('width_slice_tail_weight') or 0)>0))
def serve(c, eps):
    rec=c.get('center_width_receipt') or {}; v=c['probabilities']
    w=float(rec.get('width_slice_tail_weight') or 0.0)
    weight=eps if eps is not None else w
    if weight>0:
        v,_=apply_tail_mix(v,weight=weight,scale=float(rec.get('width_slice_tail_scale') or TAIL_MIX_SCALE),ordered_keys=c['ordered_keys'],key_centers_c=c['key_centers_c'],excluded_buckets=c['excluded_buckets'],open_edge_bounds_c=c.get('open_edge_bounds_c'))
    ceil=rec.get('peak_ceiling')
    if ceil: v,_=apply_peak_ceiling(v,ceiling=float(ceil),ordered_keys=c['ordered_keys'],excluded_buckets=c['excluded_buckets'])
    return v
def sc(c,v): return _served_scores(v,ordered_keys=c['ordered_keys'],settled_bucket=c['settled_bucket'],excluded_buckets=c['excluded_buckets'],key_centers_c=c['key_centers_c'])
EG=[None,0.05,0.10,0.15,0.20,0.30]
S=np.zeros((len(far),len(EG),3))
for i,c in enumerate(far):
    for j,e in enumerate(EG): S[i,j]=sc(c,serve(c,e))
# parity: the re-served vector (eps=None) vs what was actually served -- a sanity read of production order
print('scored', round(time.time()-t0),'s')
ed=np.array([c['event_date'] for c in far]); days=sorted(set(ed)); cl=[(c['station_id'],c['event_date']) for c in far]
f=lambda v,ci: f"{v:+.4f} [{ci[0]:+.4f},{ci[1]:+.4f}]{'*' if ci and (ci[0]>0 or ci[1]<0) else ''}" if ci else '—'
def gate(sel, cand, label):
    sel=np.asarray(sel)
    if len(sel)<30: print(f"  {label}: n={len(sel)} <30"); return
    g=_proper_score_gate([tuple(S[i,0]) for i in sel],[tuple(S[i,cand[i]]) for i in sel],[cl[i] for i in sel])
    print(f"  {label:40s} n={len(sel):4d}/{g['station_days']}d ΔLog {f(g['d_logscore'],g['ci_logscore'])} ΔCRPS {f(g['d_crps'],g['ci_crps'])} ΔBrier {f(g['d_brier'],g['ci_brier'])} -> {'PASS' if g['lower'] else 'no'}")
print("== F stations, P0, >36h: tail mix in production order, from recorded inputs ==")
for e in (0.10,0.15,0.20):
    j=EG.index(e); gate(np.arange(len(far)),np.full(len(far),j),f'fixed ε={e}')
chosen=np.full(len(far),-1)
# walk-forward on event_date (labels known by day d: cells' days are settled; the truth map is the standing one)
for d in days:
    dm=(date.fromisoformat(d)-timedelta(days=2)).isoformat(); idx=np.where(ed<=dm)[0]
    if len({ed[i] for i in idx})<8: continue
    sel=np.where(ed==d)[0]; chosen[sel]=int(np.argmin(S[idx][:,1:,0].mean(axis=0)))+1
sel=np.where(chosen>=0)[0]; gate(sel,chosen,f"walk-forward ε (chosen {dict(sorted(collections.Counter(EG[chosen[i]] for i in sel).items()))})")
half=days[len(days)//2]
for lab,m in (('first half',ed<half),('second half',ed>=half),('current segment ≥10-05',ed>='2026-10-05')):
    sel=np.where(m)[0]; gate(sel,np.full(len(far),EG.index(0.15)),f'fixed ε=0.15 {lab}')
# tail mass diagnostic
said0=[];said1=[];landed=[]
for i,c in enumerate(far):
    v0=serve(c,None); v1=serve(c,0.15); keys=c['ordered_keys']; mi=max(range(len(keys)),key=lambda k:v0[keys[k]]); ti=keys.index(c['settled_bucket'])
    said0.append(sum(v0[keys[k]] for k in range(len(keys)) if abs(k-mi)>=2)); said1.append(sum(v1[keys[k]] for k in range(len(keys)) if abs(k-mi)>=2)); landed.append(abs(ti-mi)>=2)
print(f"  tail |d|≥2: served says {np.mean(said0):.3f} → ε=0.15 says {np.mean(said1):.3f}, landed {np.mean(landed):.3f}")
print('done', round(time.time()-t0),'s')
