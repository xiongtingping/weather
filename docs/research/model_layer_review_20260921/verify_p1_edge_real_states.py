import sqlite3,json,statistics as st,sys,collections
sys.path.insert(0,'/Users/xiong/weather')
from bayes_basket_target import p1_direct_update as DU
from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target.materialization import rehydrate_lineage_documents,_bucket_centers_c,floor_probabilities
art=json.load(open('/Users/xiong/weather/data/runtime/bayes_basket_target_nwp/p1_observation_likelihood_artifact.json'))
c=sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=2);c.row_factory=sqlite3.Row
c.execute('pragma query_only=on')
stations=[r[0] for r in c.execute('select distinct station_id from calibration_station_market_timezone_binding') if len(r[0])==4]
q="""select checkpoint_id,event_date,lineage_json,posterior_version,json_extract(lineage_json,'$.p1_observation_likelihood_json') rc
from basket_probability_envelope indexed by basket_probability_envelope_station_stage_recency_idx
where station_id=? and stage='P1' and state='COMMITTED' order by decision_at_utc desc limit 600"""
def post(prior,l,exc):
    x={k:(0.0 if k in exc else p*l[k]) for k,p in prior.items()};t=sum(x.values())
    return floor_probabilities({k:v/t for k,v in x.items()},exc)[0]
rootcache={}
out=collections.defaultdict(list)
for s in stations:
    for r in c.execute(q,(s,)).fetchall():
        if r['posterior_version']!='p1_joint_likelihood_v17' or not r['rc']: continue
        rc=json.loads(r['rc'])
        if rc.get('update')!=DU.UPDATE or rc.get('artifact_sha256')!=art['content_sha256']: continue
        h=rc['clock_hours']
        edge=10.0 if abs(h-10)<=0.5 else (6.0 if abs(h-6)<=0.5 else None)
        if edge is None: continue
        l=rehydrate_lineage_documents(c,json.loads(r['lineage_json']))
        rid=l.get('prediction_root_checkpoint_id'); rsha=l.get('prediction_root_content_sha256')
        if (rid,rsha) not in rootcache:
            rs=[x for x in c.execute('select * from basket_probability_envelope where checkpoint_id=?',(rid,)).fetchall() if x['content_sha256']==rsha]
            if len(rs)!=1: continue
            rl=rehydrate_lineage_documents(c,json.loads(rs[0]['lineage_json']))
            rootcache[(rid,rsha)]=(json.loads(rs[0]['probabilities_json']),json.loads(rs[0]['bucket_ordinals_json']),rl)
        rp,ro,rl=rootcache[(rid,rsha)]
        axis=_bucket_centers_c(rl,bucket_ordinals=ro)
        if axis is None: continue
        iv=json.loads(rl.get('bucket_intervals_c_json') or '{}')
        exc=l.get('hard_excluded_buckets','NONE'); exc=[] if exc in ('NONE',None,'') else exc.split(',')
        kw=dict(root_probabilities=rp,bucket_ordinals=ro,bucket_centers_c=dict(zip(*axis)),bucket_upper_bounds_c={k:v[1] for k,v in iv.items()},
                running_high_c=rc['running_high_c'],running_high_bucket=rc['running_high_bucket'],hard_excluded_buckets=exc,station_id=s)
        res=[]
        for sign in (-1,1):
            f=dict(rc['features'],remaining_hours=edge+sign*1e-6-OL.station_clock_offset(art,s))
            L,rec=DU.compile_direct_update(f,art,**kw)
            p=post(rp,L,exc); res.append((p,rec['rise_probability']))
        (pb,rb),(pa,ra)=res
        tv=sum(abs(pb[k]-pa[k]) for k in pb)/2
        out[edge].append((tv,ra-rb,s,r['event_date'],h))
for e,v in out.items():
    tv=[x[0] for x in v]; dr=[x[1] for x in v]
    print(f'edge {e}h: real envelopes within ±0.5h n={len(v)} stationdays={len(set((x[2],x[3]) for x in v))}')
    print(f'   TV jump median={st.median(tv):.3f} p90={sorted(tv)[int(.9*len(tv))]:.3f} max={max(tv):.3f}')
    print(f'   rise(above-side minus below-side) median={st.median(dr):+.3f} mean={st.mean(dr):+.3f} share>0={sum(1 for x in dr if x>0)/len(dr):.2f}')
