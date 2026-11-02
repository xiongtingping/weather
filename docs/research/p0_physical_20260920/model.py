"""Offline conditional probability family. No database access or production imports."""
from __future__ import annotations
import math
import numpy as np
from scipy.optimize import minimize
from scipy.special import logsumexp

ARMS = ('identity', 'unconditional', 'tx', 'weather', 'joint')
RIDGES = (0.01, 0.1, 1.0)


def geometry(sample):
    iv=sample['intervals_c']; p=sample['probabilities']
    keys=sorted(iv,key=lambda k: -math.inf if iv[k][0] is None else float(iv[k][0]))
    if len(keys)<3 or set(keys)!=set(p):raise ValueError('invalid_geometry')
    finite=[float(hi)-float(lo) for lo,hi in iv.values() if lo is not None and hi is not None]
    if not finite or min(finite)<=0:raise ValueError('invalid_width')
    for left,right in zip(keys,keys[1:]):
        if iv[left][1] is None or iv[right][0] is None or abs(iv[left][1]-iv[right][0])>1e-8:
            raise ValueError('noncontiguous_geometry')
    if iv[keys[0]][0] is not None or iv[keys[-1]][1] is not None:raise ValueError('open_tails_required')
    width=float(np.median(finite)); centers=[]
    for k in keys:
        lo,hi=iv[k]
        centers.append(float(hi)-width/2 if lo is None else float(lo)+width/2 if hi is None else (float(lo)+float(hi))/2)
    prob=np.array([p[k] for k in keys],dtype=float)
    if not np.isfinite(prob).all() or prob.min()<0 or abs(prob.sum()-1)>1e-5:raise ValueError('invalid_probability')
    prob=prob/prob.sum()
    y=sample['truth_c']
    # The market intervals are lower-inclusive / upper-exclusive, like the
    # source bucket-probability truth scoring contract. Boundary tests pin it.
    hits=[i for i,k in enumerate(keys) if (iv[k][0] is None or y>=iv[k][0]) and (iv[k][1] is None or y<iv[k][1])]
    if len(hits)!=1:raise ValueError('truth_not_uniquely_mapped')
    return keys,np.array(centers),prob,hits[0],width


def feature_names(arm):
    if arm in ('identity','unconditional'):return []
    names=['taf_age_h','taf_day_coverage']
    if arm in ('tx','joint'):names+=['tx_innovation_c','tx_missing']
    if arm in ('weather','joint'):names+=['cloud','precip','log_wind']
    return names


def features(samples,arm):
    names=feature_names(arm);rows=[]
    for s in samples:
        # Missing TX is an explicit feature state, not an invented TX value.
        d={'taf_age_h':s['taf_age_h'],'taf_day_coverage':s['taf_day_coverage'],
           'tx_innovation_c':0.0 if s['tx_c'] is None else s['tx_c']-s['native_mean_c'],
           'tx_missing':float(s['tx_c'] is None),'cloud':s['cloud'],'precip':s['precip'],
           'log_wind':math.log1p(s['wind_mps'])}
        rows.append([d[k] for k in names])
    x=np.array(rows,dtype=float).reshape(len(samples),len(names))
    if not np.isfinite(x).all():raise ValueError('nonfinite_feature')
    return x


class Batch:
    def __init__(self,samples):
        self.samples=samples;self.geometries=[geometry(s) for s in samples]
        n=len(samples);m=max(len(g[0]) for g in self.geometries)
        self.p=np.zeros((n,m));self.c=np.zeros((n,m));self.hit=np.zeros(n,dtype=int)
        self.z=np.zeros((n,m));self.variance_z=np.zeros((n,m));self.y=np.array([s['truth_c'] for s in samples])
        for i,(keys,centers,p,hit,width) in enumerate(self.geometries):
            l=len(keys);mu=float(p@centers);sigma=max(float(np.sqrt(p@((centers-mu)**2))),width/2)
            z=np.clip((centers-mu)/sigma,-4,4)
            self.p[i,:l]=p;self.c[i,:l]=centers;self.hit[i]=hit;self.z[i,:l]=z;self.variance_z[i,:l]=z*z-1
        self.dist=np.abs(self.c[:,:,None]-self.c[:,None,:]);self.abs_y=np.abs(self.c-self.y[:,None])
        self.logp=np.full_like(self.p,-np.inf);np.log(self.p,out=self.logp,where=self.p>0)

    def predict(self,theta,x):
        a=x@theta[:,0];b=x@theta[:,1]
        logits=self.logp+a[:,None]*self.z+b[:,None]*self.variance_z
        return np.exp(logits-logsumexp(logits,axis=1)[:,None])

    def loss(self,theta_flat,x,ridge):
        theta=theta_flat.reshape(x.shape[1],2);q=self.predict(theta,x)
        dq=np.einsum('nij,nj->ni',self.dist,q)
        score=np.sum(q*self.abs_y,axis=1)-0.5*np.sum(q*dq,axis=1)
        dp=self.abs_y-dq
        centered_dp=dp-np.sum(dp*q,axis=1)[:,None]
        da=np.sum(q*centered_dp*self.z,axis=1)
        db=np.sum(q*centered_dp*self.variance_z,axis=1)
        grad=np.stack((x.T@da,x.T@db),axis=1)/len(q)+ridge*theta
        return float(score.mean()+ridge/2*np.sum(theta*theta)),grad.ravel()


def fit(samples,arm,ridge):
    if len(samples)<100 or len({s['event_date'] for s in samples})<10:raise ValueError('insufficient_fit_days_or_rows')
    raw=features(samples,arm);mean=raw.mean(axis=0);scale=raw.std(axis=0);scale=np.where(scale>1e-8,scale,1.0)
    x=np.column_stack((np.ones(len(samples)),np.clip((raw-mean)/scale,-5,5)))
    batch=Batch(samples)
    result=minimize(batch.loss,np.zeros(x.shape[1]*2),args=(x,ridge),jac=True,method='L-BFGS-B',bounds=[(-2,2)]*(x.shape[1]*2),options={'maxiter':180,'ftol':1e-11,'gtol':1e-7})
    if not result.success:raise RuntimeError('optimizer_failed:'+result.message)
    return {'schema':'offline_conditional_distribution_v1','stage':'P0-M_RESEARCH_ONLY','arm':arm,'ridge':ridge,
            'names':feature_names(arm),'mean':mean.tolist(),'scale':scale.tolist(),'theta':result.x.reshape(x.shape[1],2).tolist(),
            'train_n':len(samples),'train_days':sorted({s['event_date'] for s in samples}),'iterations':int(result.nit),
            'objective':float(result.fun),'no_live_authority':True}


def predict(samples,artifact):
    batch=Batch(samples)
    if artifact['arm']=='identity':return batch,batch.p.copy()
    raw=features(samples,artifact['arm'])
    x=np.column_stack((np.ones(len(samples)),np.clip((raw-np.array(artifact['mean']))/np.array(artifact['scale']),-5,5)))
    return batch,batch.predict(np.array(artifact['theta']),x)


def scored_rows(samples,artifact):
    batch,q=predict(samples,artifact);dq=np.einsum('nij,nj->ni',batch.dist,q)
    crps=np.sum(q*batch.abs_y,axis=1)-0.5*np.sum(q*dq,axis=1)
    out=[]
    for i,s in enumerate(samples):
        keys,centers,_,hit,width=batch.geometries[i];p=q[i,:len(keys)];prob=float(p[hit]);cdf=np.cumsum(p)
        center=float(p@centers)
        rec={'sample_sha':s['sample_sha'],'station':s['station'],'event_date':s['event_date'],
             'decision_at':s['decision_at'],'crps_c':float(crps[i]),'nll':-math.log(prob) if prob>0 else None,
             'zero_hit':prob==0,'error_c':center-s['truth_c'],'center_c':center,'tx_available':s['tx_c'] is not None}
        for mass in (.8,.9):
            lo=int(np.searchsorted(cdf,(1-mass)/2));hi=min(len(keys)-1,int(np.searchsorted(cdf,1-(1-mass)/2)))
            rec[f'coverage{int(mass*100)}']=lo<=hit<=hi
            bounds=s['intervals_c'];a=bounds[keys[lo]][0];b=bounds[keys[hi]][1]
            rec[f'width{int(mass*100)}_c']=None if a is None or b is None else b-a
            rec[f'open{int(mass*100)}']=a is None or b is None
        out.append(rec)
    return out


def summarize(rows):
    if not rows:return {'n':0}
    out={'n':len(rows),'days':len({r['event_date'] for r in rows}), 'stations':len({r['station'] for r in rows}),
         'crps_c':float(np.mean([r['crps_c'] for r in rows])),
         'zero_hits':sum(r['zero_hit'] for r in rows),
         'rmse_c':float(np.sqrt(np.mean([r['error_c']**2 for r in rows]))),
         'mae_c':float(np.mean([abs(r['error_c']) for r in rows])),
         'bias_c':float(np.mean([r['error_c'] for r in rows]))}
    out['nll']=None if out['zero_hits'] else float(np.mean([r['nll'] for r in rows]))
    out['nll_status']='POSITIVE_INFINITY' if out['zero_hits'] else 'FINITE'
    for level in (80,90):
        out[f'coverage{level}']=float(np.mean([r[f'coverage{level}'] for r in rows]))
        widths=[r[f'width{level}_c'] for r in rows if r[f'width{level}_c'] is not None]
        out[f'finite_width{level}_c']=float(np.mean(widths)) if widths else None
        out[f'open_interval{level}_share']=float(np.mean([r[f'open{level}'] for r in rows]))
    return out
