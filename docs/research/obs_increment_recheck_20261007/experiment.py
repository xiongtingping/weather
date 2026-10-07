"""Conditional, direction-constrained incremental update; research only.

No inferred sunshine, no added temperature likelihood. Fits an increment ON
the existing P1, so any strength must pay after the existing weather features.
Satellite historical availability is hypothetical (lag stress tests), not a
receipt. Cross-version walk-forward is exploratory and reported by version.
"""
from __future__ import annotations
import collections
import gzip
import json
import math
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import numpy as np
from scipy.optimize import minimize, minimize_scalar
from audit import HERE, guard, utc

def satellite_maps():
    manifests=json.load(open(HERE/'station_sources.json'))['stations'];out={}
    for m in manifests:
        if m['status']!='HISTORICAL_CANDIDATE_ONLY':continue
        p=HERE/'source_cache'/f"{m['station']}_{m['model']}_2026-09-01_2026-10-07.json.gz"
        j=json.load(gzip.open(p,'rt'))['payload']
        if j['hourly_units']['shortwave_radiation']!='W/m²':raise ValueError('unit_mismatch')
        out[m['station']]=dict(zip(j['hourly']['time'],j['hourly']['shortwave_radiation']))
    return out

def innovation(r,satellite,lag):
    at=utc(r['decided_at']).astimezone(ZoneInfo(r['tz']))
    end=(at-timedelta(hours=lag)).replace(minute=0,second=0,microsecond=0)
    samples=[]
    for j in (1,0):
        when=end-timedelta(hours=j)
        if when.date().isoformat()!=r['day']:return None
        observed=satellite.get(when.strftime('%Y-%m-%dT%H:00'))
        forecast=r['fc'][when.hour]['sw']
        if observed is None or forecast is None or not all(math.isfinite(float(v)) and v>=0 for v in (observed,forecast)):return None
        samples.append((float(observed),float(forecast)))
    a=sum(v[0] for v in samples);f=sum(v[1] for v in samples)
    if f<100:return None # two hours mean <50 W/m2: no shortwave evidence
    rad=max(-1.,min(1.,(a-f)/f))
    # A report of rain when the forecast hour said none is categorical evidence.
    # A dry/NSW report supplies no counter-evidence, and no rainfall mm is invented.
    h=utc(r['observation_valid_at']).astimezone(ZoneInfo(r['tz'])).hour
    predicted=r['fc'][h]['pr']
    rain=1. if r['precip_now'] and predicted is not None and float(predicted)==0. else 0.
    return rad,rain,dict(ends_at=end.isoformat(),actual_mean_wm2=a/2,forecast_mean_wm2=f/2)

def prepare(rows,maps,lag):
    out=[];counts=collections.Counter()
    for r in rows:
        guard()
        if r['station'] not in maps:counts['no_verified_source']+=1;continue
        if 'observation_source_lane' in r and r['observation_source_lane']!='awc':raise ValueError('unexpected_observation_lineage')
        iv=r['intervals']
        if not iv:counts['missing_intervals']+=1;continue
        keys=sorted(iv,key=lambda k: -1e6 if iv[k][0] is None else iv[k][0])
        lo=[iv[k][0] for k in keys];hi=[iv[k][1] for k in keys]
        if any(hi[j]!=lo[j+1] for j in range(len(keys)-1)):raise ValueError('ladder_gap')
        k=next((i for i,key in enumerate(keys) if (iv[key][0] is None or r['truth']>=iv[key][0]) and (iv[key][1] is None or r['truth']<iv[key][1])),None)
        p=np.array([r['p1'][key] for key in keys]);support=np.array([key not in r['hard_excluded'] for key in keys])
        if k is None or not support[k] or p[k]<=0:counts['truth_outside_support']+=1;continue
        inc=innovation(r,maps[r['station']],lag)
        if inc is None:counts['missing_or_dark_shortwave']+=1;continue
        centers=np.array([(l+h)/2 if l is not None and h is not None else h-(hi[1]-lo[1])/2 if l is None else l+(hi[-2]-lo[-2])/2 for l,h in zip(lo,hi)])
        u=np.clip(centers-r['running_high'],0,3)
        out.append(dict(r=r,keys=keys,k=k,p=p,support=support,centers=centers,u=u,rad=inc[0],rain=inc[1],rad_evidence=inc[2]))
    return out,dict(counts)

def pack(data):
    n=len(data);K=max(len(x['p']) for x in data)
    p=np.zeros((n,K));support=np.zeros((n,K),bool);u=np.zeros((n,K));width=np.zeros((n,K-1));k=np.empty(n,int)
    for i,x in enumerate(data):
        kk=len(x['p']);p[i,:kk]=x['p'];support[i,:kk]=x['support'];u[i,:kk]=x['u'];width[i,:kk-1]=np.diff(x['centers']);k[i]=x['k']
    z=np.array([[x['rad'],-x['rain']] for x in data]);return p,support,u,width,k,z

def update(p,support,u,z,beta):
    if not np.any(beta):return p.copy()
    f=(z@np.array(beta))[:,None]*u
    f=f-f.max(axis=1,keepdims=True);q=p*np.exp(f);q=q/q.sum(axis=1,keepdims=True)
    low=(q<.001)&support;lifted=np.where(support,np.maximum(q,.001),0.)
    # Mirror production's identity when no bucket needs lifting.
    result=np.where(low.any(axis=1)[:,None],lifted/lifted.sum(axis=1,keepdims=True),q)
    silent=(z@np.array(beta))==0.
    result[silent]=p[silent]
    return result

def scores(q,k,width):
    y=np.eye(q.shape[1])[k];log=-np.log(q[np.arange(len(k)),k]);brier=((q-y)**2).sum(1)
    crps=((np.cumsum(q,1)[:,:-1]-np.cumsum(y,1)[:,:-1])**2*width).sum(1)
    return np.column_stack([log,crps,brier])

def weights(data):
    sizes=collections.Counter((x['r']['station'],x['r']['day']) for x in data)
    w=np.array([1/sizes[(x['r']['station'],x['r']['day'])] for x in data]);return w/w.sum()

def fit(data,objective,kind):
    p,s,u,w,k,z=pack(data);wt=weights(data)
    bounds=[(0.,8.) if kind in ('radiation','joint') else (0.,0.),(0.,8.) if kind in ('rain','joint') else (0.,0.)]
    def loss(b):
        q=update(p,s,u,z,b);return float(wt@scores(q,k,w)[:,0 if objective=='nll' else 2])+.002*float(np.dot(b,b))
    active=[i for i,(_,hi) in enumerate(bounds) if hi>0]
    def expand(v):
        result=np.zeros(2);result[active]=v;return result
    starts=[np.full(len(active),v) for v in (0.,.25,1.)]
    start=min(starts,key=lambda v:loss(expand(v)))
    # Production floor is piecewise and exact identity is a distinct endpoint.
    # Use a bounded derivative-free fit; no fragile numerical floor derivative.
    opt=minimize(lambda v:loss(expand(v)),start,method='Powell',bounds=[bounds[i] for i in active],options={'maxiter':100,'ftol':1e-9,'xtol':1e-5})
    if not opt.success:raise RuntimeError('optimizer_failed:'+opt.message)
    beta=expand(opt.x)
    beta[np.abs(beta)<1e-6]=0. # below numerical fit precision: exact identity
    return beta.tolist() if loss(beta)<loss([0.,0.]) else [0.,0.]

def physical_means(data,gamma):
    result=[];neutral=[]
    for x in data:
        r=x['r'];h=r['local_hour'];i=min(23,int(h));f=r['fc'];now=f[i]['t']+(f[min(23,i+1)]['t']-f[i]['t'])*(h-i)
        rise=max(0.,max([now]+[f[j]['t'] for j in range(i+1,24)])-now)
        ratio=np.clip(1.+x['rad'],.25,2.)
        neutral.append(max(r['running_high'],r['temp_now']+rise))
        result.append(max(r['running_high'],r['temp_now']+rise*ratio**gamma))
    return np.array(result),np.array(neutral)

def fit_physical(train):
    wt=weights(train)
    truth=np.array([x['fit_truth'] for x in train])
    H=np.array([x['r']['running_high'] for x in train]);T=np.array([x['r']['temp_now'] for x in train]);R=[]
    for x in train:
        r=x['r'];h=r['local_hour'];i=min(23,int(h));f=r['fc'];now=f[i]['t']+(f[min(23,i+1)]['t']-f[i]['t'])*(h-i)
        R.append(max(0.,max([now]+[f[j]['t'] for j in range(i+1,24)])-now))
    R=np.array(R);ratio=np.clip(1.+np.array([x['rad'] for x in train]),.25,2.)
    def mse(g):return float(wt@((np.maximum(H,T+R*ratio**g)-truth)**2))
    opt=minimize_scalar(mse,bounds=(0.,3.),method='bounded',options={'xatol':1e-5})
    choices=[0.,float(opt.x),3.];gamma=min(choices,key=mse)
    return dict(gamma=gamma,sigma_c=max(.5,math.sqrt(mse(gamma))),train_mse=mse(gamma),neutral_train_mse=mse(0.))

def physical_update(data,params):
    p,s,u,width,k,z=pack(data);new,neutral=physical_means(data,params['gamma'])
    # Gaussian mean-shift log likelihood RATIO; no duplicate temperature term.
    dz=(new-neutral)/params['sigma_c']**2
    q=update(p,s,u,np.column_stack([dz,np.zeros(len(dz))]),[1.,0.])
    return q,new,neutral

def label_at(x,cutoff):
    for truth,rev,lane,committed,complete in x['r']['truth_history']:
        if complete and committed and utc(committed)<=cutoff:
            iv=x['r']['intervals']
            return next((i for i,key in enumerate(x['keys']) if (iv[key][0] is None or truth>=iv[key][0]) and (iv[key][1] is None or truth<iv[key][1]) and x['support'][i]),None)
    return None

def truth_at(x,cutoff):
    return next((float(t[0]) for t in x['r']['truth_history'] if t[4] and t[3] and utc(t[3])<=cutoff),None)

def aggregate(rows):
    if not rows:return dict(rows=0)
    sd=collections.defaultdict(list)
    for r in rows:sd[(r['day'],r['station'])].append(r['delta'])
    byday=collections.defaultdict(list)
    for (d,st),vs in sd.items():byday[d].append(np.mean(vs,axis=0))
    days=sorted(byday);vectors=np.array([v for d in days for v in byday[d]])
    rng=np.random.default_rng(71);boot=[]
    for _ in range(2000):
        sampled=rng.choice(days,len(days));boot.append(np.mean([v for d in sampled for v in byday[d]],axis=0))
    ci=np.percentile(boot,[2.5,97.5],axis=0)
    return dict(rows=len(rows),station_days=len(sd),days=len(days),delta=dict(zip(['logscore','crps_c','brier'],vectors.mean(0).tolist())),ci95={name:ci[:,j].tolist() for j,name in enumerate(['logscore','crps_c','brier'])},statistical_gate=bool(ci[1,0]<0 and ci[0,1]<=0 and ci[0,2]<=0))

def main(only=None):
    rows=[json.loads(l) for l in gzip.open(HERE/'audited_rows.jsonl.gz','rt')];maps=satellite_maps();all_runs=json.load(open(HERE/'experiment.json'))['runs'] if only else {}
    for lag in (0,1,2):
        data,excluded=prepare(rows,maps,lag);days=sorted({x['r']['day'] for x in data});output=[];fitted=json.load(open(HERE/f'fits_lag{lag}.json'))['fitted'] if only else {}
        for objective,kind in [('nll','radiation'),('brier','radiation'),('nll','rain'),('nll','joint')]:
            if only and objective!=only:continue
            run=[];last_beta=[0.,0.]
            for i,d in enumerate(days):
                test=[x for x in data if x['r']['day']==d]
                if i<12:continue
                cutoff=min(utc(x['r']['decided_at']) for x in test)
                train=[]
                for x in data:
                    if x['r']['day']>=d:continue
                    label=label_at(x,cutoff)
                    if label is not None:train.append({**x,'k':label})
                if len({x['r']['day'] for x in train})<12 or len(train)<500:continue
                beta=fit(train,objective,kind);last_beta=beta;fitted[f'{objective}_{kind}_{d}']=dict(beta=beta,train_rows=len(train),train_days=len({x['r']['day'] for x in train}),training_cutoff_utc=cutoff.isoformat())
                p,s,u,w,k,z=pack(test);q=update(p,s,u,z,beta);diff=scores(q,k,w)-scores(p,k,w)
                for x,v,pnew in zip(test,diff,q):
                    r=x['r'];run.append(dict(station=r['station'],day=d,hour=r['local_hour'],posterior_version=r['posterior_version'],rad=x['rad'],rain=x['rain'],delta=v.tolist(),baseline_truth_p=float(x['p'][x['k']]),candidate_truth_p=float(pnew[x['k']])))
            summary=dict(all_descriptive_only=aggregate(run),current_v38=aggregate([r for r in run if r['posterior_version']=='p1_joint_likelihood_v38']),dark=aggregate([r for r in run if r['rad']<-.2]),bright=aggregate([r for r in run if r['rad']>.2]),afternoon=aggregate([r for r in run if 12<=r['hour']<18]),by_version={v:aggregate([r for r in run if r['posterior_version']==v]) for v in sorted({r['posterior_version'] for r in run})},final_beta=last_beta,activation_eligible=False)
            all_runs[f'lag{lag}_{objective}_{kind}']=summary
            print('lag',lag,objective,kind,json.dumps({k:summary[k] for k in ('all_descriptive_only','current_v38','final_beta')},ensure_ascii=False),flush=True)
        (HERE/f'fits_lag{lag}.json').write_text(json.dumps(dict(fitted=fitted,excluded=excluded,usable_rows=len(data)),ensure_ascii=False,indent=2))
        if only:continue
        physical_rows=[];physical_fits={}
        for i,d in enumerate(days):
            if i<12:continue
            test=[x for x in data if x['r']['day']==d];cutoff=min(utc(x['r']['decided_at']) for x in test)
            train=[]
            for x in data:
                if x['r']['day']>=d:continue
                label=label_at(x,cutoff);truth=truth_at(x,cutoff)
                if label is not None and truth is not None:train.append({**x,'k':label,'fit_truth':truth})
            if len({x['r']['day'] for x in train})<12 or len(train)<500:continue
            params=fit_physical(train);physical_fits[d]=dict(params=params,training_cutoff_utc=cutoff.isoformat())
            p,s,u,w,k,z=pack(test);q,new,neutral=physical_update(test,params);diff=scores(q,k,w)-scores(p,k,w)
            for x,v,m0,m1 in zip(test,diff,neutral,new):
                r=x['r'];physical_rows.append(dict(station=r['station'],day=d,hour=r['local_hour'],posterior_version=r['posterior_version'],rad=x['rad'],rain=x['rain'],delta=v.tolist(),squared_error_delta=float((m1-r['truth'])**2-(m0-r['truth'])**2)))
        all_runs[f'lag{lag}_physical_mse_ratio']=dict(all_descriptive_only=aggregate(physical_rows),current_v38=aggregate([r for r in physical_rows if r['posterior_version']=='p1_joint_likelihood_v38']),dark=aggregate([r for r in physical_rows if r['rad']<-.2]),bright=aggregate([r for r in physical_rows if r['rad']>.2]),by_version={v:aggregate([r for r in physical_rows if r['posterior_version']==v]) for v in sorted({r['posterior_version'] for r in physical_rows})},squared_error_delta_descriptive=float(np.mean([r['squared_error_delta'] for r in physical_rows])),activation_eligible=False)
        (HERE/f'physical_fits_lag{lag}.json').write_text(json.dumps(physical_fits,ensure_ascii=False,indent=2))
        print('physical mse ratio',lag,json.dumps(all_runs[f'lag{lag}_physical_mse_ratio'],ensure_ascii=False)[:1700],flush=True)
    (HERE/'experiment.json').write_text(json.dumps(dict(runs=all_runs,verdict='RESEARCH_ONLY_NO_PRODUCTION',blockers=['current P1 v38 has only three closed event days','archived satellite has no historical arrival receipt','previous_day1 has no exact per-value issuance/arrival receipt','American stations have no verified satellite source in this provider']),ensure_ascii=False,indent=2))

if __name__=='__main__':
    import sys
    main('brier' if '--only-brier' in sys.argv else None)
