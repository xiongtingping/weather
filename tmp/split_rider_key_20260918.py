"""Split riders: served per-system calibration keyed on the packet lead vs the member's own hour lead.
Measures, on settled station-days, what swapping to the own-lead exact fit would do to P0 RPS
(first-order: served distribution shifted by the weight-summed bias change), with a sign-flipped control."""
import sqlite3, json, sys, random, statistics as S, collections
sys.path.insert(0,'/Users/xiong/weather'); sys.path.insert(0,'/Users/xiong/weather/scripts')
from bayes_basket_target.label_knowability import knowable_labels
from bayes_basket_target.native_ensemble_oos import _resolved_bias_c
c=sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=120)
labels=knowable_labels(c)
rows=c.execute("""SELECT station_id, event_date, decision_at_utc, probabilities_json, bucket_ordinals_json,
 json_extract(lineage_json,'$.provisional_parameter_sources_json'), json_extract(lineage_json,'$.hour_bucket_split_riders_json'),
 json_extract(lineage_json,'$.hour_lead_bucket_by_system_json'), json_extract(lineage_json,'$.run_cycle_by_system_json'),
 json_extract(lineage_json,'$.season_bin'), json_extract(lineage_json,'$.system_weights_json'), json_extract(lineage_json,'$.training_lead_bucket'),
 json_extract(lineage_json,'$.native_calibrated_system_centers_c_json'), json_extract(lineage_json,'$.system_artifact_shas_json'), json_extract(lineage_json,'$.lead_bucket'), json_extract(lineage_json,'$.bucket_intervals_c_json')
 FROM basket_probability_envelope WHERE state='COMMITTED' AND event_date>=? AND stage='P0' AND decision_at_utc>=?""",(sys.argv[1],sys.argv[1])).fetchall()
cache={}
def fit(st,s,season,lead,cyc,at):
    k=(st,s,season,lead,cyc,at[:13])
    if k not in cache:
        r=c.execute("""SELECT f.bias_parameters_json, f.content_sha256 FROM calibration_native_ensemble_activation a JOIN calibration_native_ensemble_system_artifact f ON f.system_artifact_id=a.artifact_id AND f.ensemble_system_id=a.ensemble_system_id_or_star
          WHERE a.artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' AND a.station_id=? AND a.ensemble_system_id_or_star=? AND a.season_bin=? AND a.lead_bucket=? AND a.run_cycle=?
          AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' AND a.valid_from_utc<=? AND a.valid_until_utc>? ORDER BY a.generation DESC LIMIT 1""",(st,s,season,lead,cyc,at,at)).fetchone()
        cache[k]=(json.loads(r[0]),r[1]) if r else None
    return cache[k]
def shift(p, ords, d):
    keys=sorted(ords,key=ords.get); out={k:0.0 for k in keys}; n=len(keys)
    for i,k in enumerate(keys):
        m=p.get(k,0.0)
        if m<=0: continue
        t=i+d
        if t<=0: out[keys[0]]+=m; continue
        if t>=n-1: out[keys[-1]]+=m; continue
        lo=int(t); w=t-lo; out[keys[lo]]+=m*(1-w); out[keys[lo+1]]+=m*w
    s=sum(out.values()); return {k:v/s for k,v in out.items()}
def rps(p, ords, truth):
    keys=sorted(ords,key=ords.get); cp=0; ct=0; r=0
    for k in keys[:-1]:
        cp+=p.get(k,0); ct= 1.0 if ords[k]>=ords[truth] else 0.0
        r+=(cp-ct)**2
    return r
per_sd=collections.defaultdict(list); kinds=collections.Counter(); shifts=[]
for st,ev,at,pj,oj,pps,split,hb,cyc,season,w,tl,cal,shas,lead,iv in rows:
    iv=json.loads(iv or '{}'); widths=sorted(b[1]-b[0] for b in iv.values() if b[0] is not None and b[1] is not None); bw=widths[len(widths)//2] if widths else 1.0
    lab=labels.get((st,ev))
    if not lab: continue
    p=json.loads(pj); ords=json.loads(oj)
    if lab['bucket'] not in ords: continue
    pps=json.loads(pps or '{}'); split=json.loads(split or '[]'); hb=json.loads(hb or '{}'); cyc=json.loads(cyc or '{}')
    w=json.loads(w or '{}'); cal=json.loads(cal or '{}'); shas=json.loads(shas or '{}')
    d=0.0; touched=False; dk={'provisional_borrow':0.0,'certified_wrong_lead':0.0}
    for s in split:
        if s not in shas or s not in cal: continue
        own=fit(st,s,season,hb.get(s),cyc.get(s),at)
        if own is None: continue
        if s in pps:
            used=pps[s]['bias_c']; raw=cal[s]-used; kind='provisional_borrow'
        else:
            cur=fit(st,s,season,tl,cyc.get(s),at)
            if cur is None or cur[1]==own[1]: continue
            b0=_resolved_bias_c(cal[s]-cur[0].get('bias_c',0), cur[0]); raw=cal[s]-b0
            used=_resolved_bias_c(raw, cur[0]); kind='certified_wrong_lead'
        new=_resolved_bias_c(raw, own[0]); d+=w.get(s,0)*(new-used); dk[kind]+=w.get(s,0)*(new-used); touched=True; kinds[kind]+=1
    ONLY=sys.argv[2] if len(sys.argv)>2 else None
    if ONLY: d=dk[ONLY]
    if not touched or abs(d)<1e-9: continue
    shifts.append(d)
    r0=rps(p,ords,lab['bucket']); r1=rps(shift(p,ords,d/bw),ords,lab['bucket']); rc=rps(shift(p,ords,-d/bw),ords,lab['bucket'])
    per_sd[(st,ev,lead)].append((r0,r1,rc,d))
print('系统次数',dict(kinds),'受影响期数',len(shifts),'站日×lead',len(per_sd), '站日', len({(a,b) for a,b,_ in per_sd}))
sd=[(S.mean(x[0] for x in v),S.mean(x[1] for x in v),S.mean(x[2] for x in v)) for v in per_sd.values()]
base=S.mean(a for a,_,_ in sd); fix=S.mean(b for _,b,_ in sd); ctl=S.mean(c_ for _,_,c_ in sd)
diff=[b-a for a,b,_ in sd]; cdiff=[c_-a for a,_,c_ in sd]
random.seed(7); boot=[]; keys=list(range(len(diff)))
for _ in range(2000):
    smp=[diff[random.choice(keys)] for _ in keys]; boot.append(S.mean(smp))
boot.sort()
print(f'RPS 现行 {base:.4f}  改用自己档 {fix:.4f} (Δ {fix-base:+.4f}, {100*(fix-base)/base:+.2f}%, 95%CI [{boot[50]:+.4f},{boot[1949]:+.4f}])  反号对照 {ctl:.4f} (Δ {ctl-base:+.4f})')
print('变好/变坏 站日×lead', sum(1 for x in diff if x<-1e-9), sum(1 for x in diff if x>1e-9))
a=[abs(x) for x in shifts]; print('中心移动 |中位|',round(S.median(a),3),'p90',round(sorted(a)[int(.9*len(a))],3),'带符号均值',round(S.mean(shifts),3))
