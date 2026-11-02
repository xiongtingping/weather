"""Frozen Pareto candidates: multiclass scores and explicitly binned Murphy decomposition."""
import sys,json,math,hashlib
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'joint_objective'))
import joint as J
import numpy as np
C=J.C;C.study.OUT=HERE
ARMS=['current','blend25','blend50','blend75']
EDGES=np.array([0,.01,.025,.05,.1,.2,.3,.4,.5,.6,.7,.8,.9,1.])

def load():
    data,_=J.inputs();idx={x['sample_sha']:x for x in data}
    scores=json.loads((HERE.parent/'joint_objective/scores.json').read_text())
    assert len(scores)==515
    return [(idx[r['sample_sha']],r) for r in scores]

def bin_stats(p,y,edges):
    # Each station-day contributes mass one, divided equally over its K buckets.
    k=len(p);b=np.clip(np.searchsorted(edges,p,side='right')-1,0,len(edges)-2);w=np.full(k,1/k)
    return np.array([np.bincount(b,weights=z,minlength=len(edges)-1) for z in [w,w*p,w*y,w*p*p,w*p*y]]).T

def decompose(stats):
    W,P,Y,P2,PY=stats.T;n=W.sum();mask=W>0;W=W[mask];P=P[mask];Y=Y[mask]
    avg=Y.sum()/n;rel=np.sum((P-Y)**2/W)/n;res=np.sum(W*(Y/W-avg)**2)/n;unc=avg*(1-avg)
    raw=(P2.sum()-2*PY.sum()+Y.sum())/n;correction=raw-(rel-res+unc)
    return {'mean_bucket_brier':float(raw),'reliability':float(rel),'resolution':float(res),'uncertainty':float(unc),
            'binning_correction':float(correction),'ece':float(np.sum(abs(P-Y))/n),'base_rate':float(avg)}

def calculate():
    guard=C.study.Guard();pairs=load();days=sorted({x['event_date'] for x,_ in pairs});rows=[];daily={a:[] for a in ARMS}
    bins={name:edges for name,edges in [('primary',EDGES),('deciles',np.linspace(0,1,11)),('twentieths',np.linspace(0,1,21))]}
    vectors={a:[] for a in ARMS};counts={d:sum(x['event_date']==d for x,_ in pairs) for d in days}
    for x,r in pairs:
        keys=sorted(x['weights']);_,_,p,hit,_,buckets=C.prob_components(x,keys);y=np.zeros(len(buckets));y[hit]=1
        rr={'station':x['station'],'day':x['event_date'],'sample_sha':x['sample_sha'],'arms':{}}
        for a in ARMS:
            w=r['arms'][a]['weights'];C.assert_weight(w,x);q=np.array([w[s] for s in keys])@p
            assert min(q)>=0 and abs(q.sum()-1)<1e-8
            ls=math.log(q[hit]) if q[hit]>0 else None
            rr['arms'][a]={'brier':float(np.sum((q-y)**2)),'mean_bucket_brier':float(np.mean((q-y)**2)),'logscore':ls,'nll':-ls if ls is not None else None,
                           'zero':ls is None,'truth_bucket_probability':float(q[hit])}
            vectors[a].append((x['event_date'],q,y))
        rows.append(rr)
    freeze={'schema':'frozen_pareto_candidates_v1','cohort_status':'retrospective_asof_not_independent_oos','sample_n':len(rows),
            'sample_sha256':C.study.digest([r['sample_sha'] for r in rows]),'source_weights_sha256':hashlib.sha256((HERE.parent/'joint_objective/scores.json').read_bytes()).hexdigest(),
            'candidates':{a:{'center_fraction':f,'weights_sha256':C.study.digest([{r['sample_sha']:r['arms'][a]['weights']} for _,r in pairs])} for a,f in [('blend25',.25),('blend50',.5),('blend75',.75)]}}
    C.study.save('candidates.json',freeze);C.study.save('scores.json',rows)
    results={};stats_by={}
    for a in ARMS:
        metrics={k:float(np.mean([r['arms'][a][k] for r in rows])) for k in ['brier','mean_bucket_brier','logscore','nll']}
        metrics['zero_n']=sum(r['arms'][a]['zero'] for r in rows)
        metrics['decomposition']={};stats_by[a]={}
        for name,edges in bins.items():
            ds=np.array([sum((bin_stats(p,y,edges) for d,p,y in vectors[a] if d==day),start=np.zeros((len(edges)-1,5))) for day in days])
            stats_by[a][name]=ds;metrics['decomposition'][name]=decompose(ds.sum(axis=0))
        total=stats_by[a]['primary'].sum(axis=0);binrows=[]
        for i,s in enumerate(total):
            W,P,Y,P2,PY=s
            binrows.append({'lo':float(EDGES[i]),'hi':float(EDGES[i+1]),'event_weight':float(W),'mean_probability':float(P/W) if W else None,'observed_frequency':float(Y/W) if W else None})
        metrics['reliability_bins']=binrows
        metrics['halves']={h:{k:float(np.mean([r['arms'][a][k] for r in rows if (r['day']<'2026-09-09')==early])) for k in ['brier','logscore','nll']} for h,early in [('first',True),('second',False)]}
        daily[a]=np.array([[sum(r['arms'][a]['brier'] for r in rows if r['day']==d),sum(r['arms'][a]['nll'] for r in rows if r['day']==d),counts[d]] for d in days])
        results[a]=metrics
    rng=np.random.default_rng(240922);draws=[];n=len(days)
    for _ in range(4000):
        starts=rng.integers(0,n-2,math.ceil(n/3));draws.append([k+j for k in starts for j in range(3)][:n])
    for a in ARMS:
        delta=[]
        for ix in draws:
            aa=daily[a][ix].sum(axis=0);bb=daily['current'][ix].sum(axis=0)
            ad=decompose(stats_by[a]['primary'][ix].sum(axis=0));bd=decompose(stats_by['current']['primary'][ix].sum(axis=0))
            delta.append([aa[0]/aa[2]-bb[0]/bb[2],aa[1]/aa[2]-bb[1]/bb[2],ad['reliability']-bd['reliability'],ad['resolution']-bd['resolution']])
        results[a]['delta_ci95']=dict(zip(['brier','nll','reliability','resolution'],np.quantile(delta,[.025,.975],axis=0).T.tolist()))
        results[a]['change_pct']={k:100*(results[a][k]/results['current'][k]-1) for k in ['brier','nll']}
    result={'sample_n':len(rows),'station_n':len({r['station'] for r in rows}),'date_n':len(days),'days':days,'arms':results,'cohort_status':freeze['cohort_status'],
            'peak_mib':guard.peak/1024**2,'input_sha256':{str(p.relative_to(C.ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [HERE/'PROTOCOL.md',Path(__file__),HERE.parent/'joint_objective/scores.json',HERE.parent/'reconstructed.json',HERE.parent/'compare.py']}}
    C.study.save('results.json',result)
    for a,v in results.items():print(a,{k:v[k] for k in ['brier','logscore','change_pct','delta_ci95']},v['decomposition']['primary'],flush=True)

if __name__=='__main__':calculate()
