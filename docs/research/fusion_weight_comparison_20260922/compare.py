"""Read-only, bounded retrospective weighting experiments; never a serving import."""
from __future__ import annotations
import os
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
os.environ.setdefault('OMP_NUM_THREADS','1')
import sys, json, hashlib, gzip, collections, math
from pathlib import Path
from datetime import timedelta
from zoneinfo import ZoneInfo
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path.insert(0,str(HERE.parent/'p0_physical_20260920'))
import study
study.OUT=HERE
import numpy as np
from scipy.optimize import minimize
from model import geometry
from bayes_basket_target.native_ensemble_oos import _capped_family_weights

ARMS=['current','equal','station3','station5','station21','fleet5','fleet21',
      'station3_shrink','station5_shrink','station_lead_eb21','center_stack21','crps_stack21','crps_hier21']

def load():
    data=json.loads((HERE.parent/'p0_physical_20260920/samples.json').read_text())
    enrich={x['sample_sha']:x for x in json.loads((HERE/'enrichment.json').read_text())}
    out=[]
    for x in data:
        if not x['primary_eligible'] or (x['station'],x['event_date']) in {('MMMX','2026-08-31'),('RKPK','2026-08-17')}:
            continue
        e=enrich[x['sample_sha']]
        assert set(x['weights'])<=study.NATIVE
        assert set(x['weights'])<=set(e['families'])
        assert abs(sum(x['weights'].values())-1)<1e-6
        x.update(e)
        x['at']=study.stamp(x['decision_at'])
        x['known']=study.stamp(x['truth_known_at'])
        x['local_decision_day']=x['at'].astimezone(ZoneInfo(x['timezone'])).date()
        out.append(x)
    return sorted(out,key=lambda x:(x['at'],x['station']))

def cap(raw,x):
    total=sum(raw.values()); w={s:float(v/total) for s,v in raw.items()}
    fam=collections.defaultdict(float)
    for s,v in w.items():fam[x['families'][s]]+=v
    fc=_capped_family_weights(dict(fam),{f:x['caps'][f] for f in fam})
    return {s:fc[x['families'][s]]*v/fam[x['families'][s]] for s,v in w.items()}

def assert_weight(w,x):
    assert set(w)==set(x['weights'])
    assert min(w.values())>=-1e-9 and abs(sum(w.values())-1)<1e-7
    fam=collections.defaultdict(float)
    for s,v in w.items():fam[x['families'][s]]+=v
    assert all(v<=x['caps'][f]+1e-7 for f,v in fam.items())

def eligible(h,x,days,scope='station',lead=False,prob=False):
    floor=(x['local_decision_day']-timedelta(days=days)).isoformat()
    end=x['local_decision_day'].isoformat()
    keys=set(x['weights'])
    return [r for r in h if floor<=r['event_date']<end and r['event_date']<x['event_date']
            and r['at']<x['at'] and r['known']<=x['at']
            and (scope=='fleet' or r['station']==x['station'])
            and keys<=set(r['weights'])
            and (not lead or all(s in x['lead'] and r['lead'].get(s)==x['lead'][s] for s in keys))
            and (not prob or keys<=set(r.get('system_probabilities',{})))]

def mse(rows,keys):
    return {s:float(np.mean([(r['centers'][s]-r['truth_c'])**2 for r in rows])) for s in keys}

def constraints(x,keys):
    out=[{'type':'eq','fun':lambda w:w.sum()-1,'jac':lambda w:np.ones(len(w))}]
    for f in set(x['families'][s] for s in keys):
        ix=np.array([x['families'][s]==f for s in keys],dtype=float)
        out.append({'type':'ineq','fun':lambda w,ix=ix,f=f:x['caps'][f]-ix@w,
                    'jac':lambda w,ix=ix:-ix})
    return out

def prob_components(x,keys):
    buckets,z,_,hit,width=geometry(x)
    p=np.array([[x['system_probabilities'][s][b] for b in buckets] for s in keys])
    a=p@np.abs(z-x['truth_c'])
    b=p@np.abs(z[:,None]-z[None,:])@p.T
    return a,b,p,hit,z,buckets

def stack(rows,x,prob=False,prior_map=None,ridge=1.):
    keys=sorted(x['weights']);prior=np.array([(prior_map or x['weights'])[s] for s in keys])
    # Fixed predeclared regularization: 1 C^2 for center MSE; 1 C for CRPS.
    if prob:
        parts=[prob_components(r,keys) for r in rows]
        a=np.mean([p[0] for p in parts],axis=0);b=np.mean([p[1] for p in parts],axis=0)
        fun=lambda w:float(a@w-.5*w@b@w+ridge*np.sum((w-prior)**2))
        jac=lambda w:a-b@w+2*ridge*(w-prior)
    else:
        e=np.array([[r['centers'][s]-r['truth_c'] for s in keys] for r in rows]);q=e.T@e/len(e)
        fun=lambda w:float(w@q@w+ridge*np.sum((w-prior)**2))
        jac=lambda w:2*q@w+2*ridge*(w-prior)
    fit=minimize(fun,prior,jac=jac,bounds=[(0,1)]*len(keys),constraints=constraints(x,keys),method='SLSQP',options={'maxiter':100,'ftol':1e-10})
    if not fit.success:raise RuntimeError('optimization_failed:'+fit.message)
    w=dict(zip(keys,map(float,fit.x)));assert_weight(w,x);return w

def candidates(h,x):
    keys=sorted(x['weights']);base=x['weights'];weights={'current':base,'equal':cap({s:1 for s in keys},x)}
    counts={'current':None,'equal':None}
    for scope,n in [('station',3),('station',5),('station',21),('fleet',5),('fleet',21)]:
        rows=eligible(h,x,n,scope);name=f'{scope}{n}';counts[name]=len(rows)
        weights[name]=cap({s:1/max(v,.25) for s,v in mse(rows,keys).items()},x) if rows else base
        if scope=='station' and n in (3,5):
            k=len(rows)/(len(rows)+10);name2=name+'_shrink';counts[name2]=len(rows)
            weights[name2]={s:(1-k)*base[s]+k*weights[name][s] for s in keys}
    rows=eligible(h,x,21,'station',True);fleet=eligible(h,x,21,'fleet',True)
    counts['station_lead_eb21']=len(rows)
    if fleet:
        a=mse(rows,keys) if rows else {};b=mse(fleet,keys);n=len(rows)
        weights['station_lead_eb21']=cap({s:1/max((n*a.get(s,0)+10*b[s])/(n+10),.25) for s in keys},x)
    else:weights['station_lead_eb21']=base
    for name,prob in [('center_stack21',False),('crps_stack21',True)]:
        rows=eligible(h,x,21,prob=prob);counts[name]=len(rows)
        weights[name]=stack(rows,x,prob) if len(rows)>=5 and (not prob or x.get('system_probabilities')) else base
    # Secondary exploratory arm, added after observing local sample scarcity.
    local=eligible(h,x,21,prob=True);fleet=eligible(h,x,21,'fleet',True,True)
    if not fleet:fleet=eligible(h,x,21,'fleet',False,True)
    counts['crps_hier21']=len(local)
    if fleet and x.get('system_probabilities'):
        prior=stack(fleet,x,True)
        weights['crps_hier21']=stack(local,x,True,prior,10/len(local)) if local else prior
    else:weights['crps_hier21']=base
    for w in weights.values():assert_weight(w,x)
    return weights,counts

def reconstruct():
    from bayes_basket_target.native_ensemble_p0 import _calibrated_system_probability,_resolved_bias_c
    guard=study.Guard();c=study.connect(guard);data=load();out=[];counts=collections.Counter();examples={};cache={}
    for x in data:
        guard.check()
        try:
            if not x['packet_exists']:raise ValueError('packet_missing')
            if x['provisional']:raise ValueError('provisional_scope_not_reconstructed')
            path=ROOT/x['packet_path']
            with gzip.open(path) as f:raw=f.read(2*1024**2+1)
            if len(raw)>2*1024**2:raise ValueError('packet_size_limit')
            p=json.loads(raw)
            assert p['station_id']==x['station'] and p['event_date']==x['event_date']
            assert p['snapshot_content_sha256'][:16] in path.name
            assert p['bucket_intervals']==x['intervals_c']
            systems={s['ensemble_system_id']:s for s in p['systems']};sp={};arts={}
            for s in x['weights']:
                sha=x['artifacts'][s]
                if sha not in cache:
                    r=c.execute('select * from calibration_native_ensemble_system_artifact where content_sha256=?',(sha,)).fetchone()
                    cache[sha]=dict(r) if r else None
                a=cache[sha]
                if a is None:raise ValueError('artifact_missing')
                if not study.stamp(a['valid_from_utc'])<=study.stamp(x['p0_nominal_decision_at'])<study.stamp(a['valid_until_utc']):raise ValueError('artifact_not_valid_then')
                assert a['ensemble_system_id']==s
                assert study.stamp(systems[s]['archive_query_at_utc'])<=study.stamp(x['p0_nominal_decision_at'])
                assert systems[s]['archive_query_at_utc']==p['archive_query_at_utc_by_system'][s]
                assert study.stamp(systems[s]['archive_query_at_utc'])==study.stamp(x['root_receipts'][s])
                vals=[float(v['raw_tmax_c']) for v in systems[s]['members']]
                bias=json.loads(a['bias_parameters_json']);spread=json.loads(a['spread_parameters_json']);rel=json.loads(a['reliability_parameters_json'])
                mean=sum(vals)/len(vals);ctr=mean+_resolved_bias_c(mean,bias)
                if abs(ctr-x['centers'][s])>1e-7:raise ValueError('historical_center_mismatch')
                sp[s]=_calibrated_system_probability(values=vals,intervals=x['intervals_c'],bias_parameters=bias,spread_parameters=spread,reliability_parameters=rel)
                arts[s]=dict(sha=sha,bias=bias,spread=spread,reliability=rel,values=vals)
                assert abs(sum(sp[s].values())-1)<1e-8
            out.append(dict(sample_sha=x['sample_sha'],system_probabilities=sp,artifacts=arts,packet_sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            counts['reconstructed']+=1
        except (ValueError,KeyError,AssertionError) as e:
            reason=str(e) or type(e).__name__;counts[reason]+=1
            examples.setdefault(reason,dict(station=x['station'],day=x['event_date']))
    study.save('reconstructed.json',out)
    study.save('reconstruction_audit.json',dict(counts=counts,examples=examples,peak_mib=guard.peak/1024**2,
        limitation='Parameters and centers match archived lineage; historical runtime and full downstream serving probability not yet replicated. These are reconstructed single-system pools, not final P0.'))
    print(dict(counts),flush=True)

def summarize(rows,arm):
    errs=np.array([r['arms'][arm]['error'] for r in rows]);out=dict(n=len(rows),rmse=float(np.sqrt(np.mean(errs**2))),mae=float(np.mean(abs(errs))),bias=float(np.mean(errs)))
    for metric in ['pool_crps','pool_nll','coverage80','coverage95','turnover']:
        vals=[r['arms'][arm].get(metric) for r in rows if r['arms'][arm].get(metric) is not None]
        out[metric]=float(np.mean(vals)) if vals else None
        out[metric+'_n']=len(vals)
    out['pool_zero_n']=sum(r['arms'][arm].get('zero',False) for r in rows)
    out['fallback_share']=float(np.mean([r['arms'][arm]['fallback'] for r in rows]))
    out['max_weight_mean']=float(np.mean([max(r['arms'][arm]['weights'].values()) for r in rows]))
    return out

def bootstrap(rows,arm,metric):
    days=sorted(set(r['day'] for r in rows)); n=len(days)
    daily=[]
    for day in days:
        sub=[r for r in rows if r['day']==day and r['arms'][arm].get(metric) is not None and r['arms']['current'].get(metric) is not None]
        daily.append([sum(r['arms'][arm][metric] for r in sub),sum(r['arms']['current'][metric] for r in sub),len(sub)])
    ar=np.array(daily);rng=np.random.default_rng(220922);deltas=[]
    for _ in range(2000):
        starts=rng.integers(0,max(1,n-2),math.ceil(n/3));ix=np.array([min(k+j,n-1) for k in starts for j in range(3)])[:n];a,b,c=ar[ix].sum(axis=0)
        if c:
            deltas.append((math.sqrt(a/c)-math.sqrt(b/c)) if metric=='se' else (a-b)/c)
    return list(map(float,np.quantile(deltas,[.025,.975]))) if deltas else None

def run():
    guard=study.Guard();data=load();sp={x['sample_sha']:x for x in json.loads((HERE/'reconstructed.json').read_text())}
    for x in data:
        if x['sample_sha'] in sp:x['system_probabilities']=sp[x['sample_sha']]['system_probabilities']
    rows=[];previous={};history=[]
    for x in data:
        guard.check()
        if x['event_date']<'2026-08-28':history.append(x);continue
        weights,counts=candidates(history,x);keys=sorted(x['weights'])
        lo=min(x['centers'][s] for s in keys);hi=max(x['centers'][s] for s in keys)
        row=dict(station=x['station'],day=x['event_date'],sample_sha=x['sample_sha'],decision_at=x['decision_at'],truth_c=x['truth_c'],system_count=len(keys),
                 slice='all_cold' if x['truth_c']>hi else 'all_warm' if x['truth_c']<lo else 'inside',disagreement_c=hi-lo,arms={})
        pc=prob_components(x,keys) if x['system_probabilities'] else None
        for name,w in weights.items():
            pred=sum(w[s]*x['centers'][s] for s in keys);err=pred-x['truth_c'];pr=previous.get((x['station'],name));n=counts[name]
            fallback=(n==0 if n is not None else False)
            if name.endswith('stack21'):fallback=n<5 or (name=='crps_stack21' and pc is None)
            if name=='station_lead_eb21':fallback=not eligible(history,x,21,'fleet',True)
            if name=='crps_hier21':fallback=pc is None or not eligible(history,x,21,'fleet',False,True)
            rr=dict(weights=w,prediction=pred,error=err,se=err**2,n_train=n,fallback=bool(fallback),
                    turnover=.5*sum(abs(w.get(s,0)-pr.get(s,0)) for s in set(w)|set(pr)) if pr else None)
            previous[(x['station'],name)]=w
            if pc:
                a,b,p,hit,z,buckets=pc;v=np.array([w[s] for s in keys]);q=v@p;pt=q[hit]
                rr.update(pool_crps=float(a@v-.5*v@b@v),pool_nll=-math.log(pt) if pt>0 else None,zero=bool(pt<=0))
                cf=np.cumsum(q)
                for level in (80,95):
                    tail=(1-level/100)/2;left=min(int(np.searchsorted(cf,tail)),len(q)-1);right=min(int(np.searchsorted(cf,1-tail)),len(q)-1)
                    rr['coverage'+str(level)]=int(left<=hit<=right)
            row['arms'][name]=rr
        rows.append(row);history.append(x)
    result=dict(n=len(rows),stations=len(set(r['station'] for r in rows)),days=len(set(r['day'] for r in rows)),
                period=[min(r['day'] for r in rows),max(r['day'] for r in rows)],arms={})
    for arm in ARMS:
        ss=summarize(rows,arm);ss['rmse_delta_ci']=bootstrap(rows,arm,'se');ss['pool_crps_delta_ci']=bootstrap(rows,arm,'pool_crps')
        stations=sorted(set(r['station'] for r in rows));per={st:summarize([r for r in rows if r['station']==st],arm) for st in stations}
        ss['station_rmse_wins']=sum(per[st]['rmse']<summarize([r for r in rows if r['station']==st],'current')['rmse']-1e-9 for st in stations)
        ss['station_metrics']=per
        ss['slices']={sl:summarize([r for r in rows if r['slice']==sl],arm) for sl in sorted(set(r['slice'] for r in rows))}
        ss['halves']={label:summarize([r for r in rows if (r['day']<'2026-09-09')==early],arm) for label,early in [('first',True),('second',False)]}
        result['arms'][arm]=ss
    result['peak_mib']=guard.peak/1024**2
    result['inputs_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [HERE/'PROTOCOL.md',Path(__file__),HERE/'enrichment.json',HERE/'reconstructed.json',HERE.parent/'p0_physical_20260920/samples.json']}
    study.save('scores.json',rows);study.save('results.json',result)
    print(json.dumps({a:{k:v for k,v in m.items() if k not in ['station_metrics','slices','halves']} for a,m in result['arms'].items()},indent=2))

if __name__=='__main__':
    {'reconstruct':reconstruct,'run':run}[sys.argv[1]]()
