"""Offline joint-objective follow-up; frozen inputs, no SQL or production changes."""
import sys,json,math,hashlib,collections
from pathlib import Path
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent))
import compare as C
import numpy as np
from scipy.optimize import minimize
C.study.OUT=HERE
ARMS=['current','center_stack21','crps_hier21','blend25','blend50','blend75','joint','joint_guarded']

def inputs():
    data=C.load();sp={r['sample_sha']:r for r in json.loads((HERE.parent/'reconstructed.json').read_text())}
    for x in data:
        if x['sample_sha'] in sp:x['system_probabilities']=sp[x['sample_sha']]['system_probabilities']
    old=json.loads((HERE.parent/'scores.json').read_text())
    return data,{r['sample_sha']:r for r in old}

class Objective:
    def __init__(self,rows,x,prior,ridge):
        self.keys=sorted(x['weights']);self.n=len(rows)
        self.e=np.array([[r['centers'][s]-r['truth_c'] for s in self.keys] for r in rows]);self.Q=self.e.T@self.e/self.n
        parts=[C.prob_components(r,self.keys) for r in rows]
        self.a=np.mean([p[0] for p in parts],axis=0);self.B=np.mean([p[1] for p in parts],axis=0)
        self.T=np.array([p[2][:,p[3]] for p in parts])
        self.base=np.array([x['weights'][s] for s in self.keys]);self.prior=np.array([prior[s] for s in self.keys]);self.ridge=ridge
        self.reference=self.losses(self.base)
        if not np.isfinite(self.reference).all():raise ValueError('training_reference_zero_probability')
        self.scales=np.maximum(self.reference,[.25,.1,.1])

    def losses(self,w):
        q=self.T@w
        return np.array([w@self.Q@w,self.a@w-.5*w@self.B@w,-np.log(q).mean() if (q>0).all() else math.inf])
    def gradients(self,w):
        q=self.T@w
        if (q<=0).any():raise ValueError('optimization_zero_probability')
        return np.array([2*self.Q@w,self.a-self.B@w,-(self.T/q[:,None]).mean(axis=0)])
    def fun(self,w):return float(np.mean(self.losses(w)/self.scales)+self.ridge*np.sum((w-self.prior)**2))
    def jac(self,w):return np.mean(self.gradients(w)/self.scales[:,None],axis=0)+2*self.ridge*(w-self.prior)

def fit(rows,x,prior,ridge,guarded):
    obj=Objective(rows,x,prior,ridge);keys=obj.keys;cons=C.constraints(x,keys)
    if guarded:
        cons.append({'type':'ineq','fun':lambda w:(obj.reference-obj.losses(w))/obj.scales,
                     'jac':lambda w:-obj.gradients(w)/obj.scales[:,None]})
    z=minimize(obj.fun,obj.base,jac=obj.jac,bounds=[(0,1)]*len(keys),constraints=cons,method='SLSQP',options={'maxiter':150,'ftol':1e-10})
    if not z.success:return dict(x['weights']),{'status':'optimizer_fallback','message':z.message,'n':len(rows)}
    w=dict(zip(keys,map(float,z.x)));C.assert_weight(w,x)
    diff=(obj.losses(z.x)-obj.reference)/obj.scales
    if guarded and max(diff)>1e-7:raise RuntimeError('training_guard_violation')
    return w,{'status':'fit','n':len(rows),'training_relative_loss_delta':list(map(float,diff))}

def predict(data,x,guarded):
    local=C.eligible(data,x,21,prob=True);fleet=C.eligible(data,x,21,'fleet',True,True)
    if not fleet:fleet=C.eligible(data,x,21,'fleet',False,True)
    if not fleet:return dict(x['weights']),{'status':'no_training_fallback','fleet_n':0,'station_n':0}
    try:
        prior,a=fit(fleet,x,x['weights'],1.,guarded)
        w,b=fit(local,x,prior,10/len(local),guarded) if local else (prior,{'status':'no_local_use_fleet','n':0})
        return w,{'status':'predicted','fleet':a,'local':b,'fleet_n':len(fleet),'station_n':len(local)}
    except ValueError as e:
        return dict(x['weights']),{'status':str(e),'fleet_n':len(fleet),'station_n':len(local)}

def score(x,w):
    keys=sorted(x['weights']);a,b,p,hit,z,buckets=C.prob_components(x,keys);v=np.array([w[s] for s in keys]);q=v@p
    err=sum(w[s]*x['centers'][s] for s in keys)-x['truth_c'];pt=float(q[hit]);cf=np.cumsum(q)
    out={'weights':w,'error':err,'se':err**2,'crps':float(a@v-.5*v@b@v),'nll':-math.log(pt) if pt>0 else None,'zero':pt<=0}
    for level in [80,95]:
        tail=(1-level/100)/2;lo=min(int(np.searchsorted(cf,tail)),len(q)-1);hi=min(int(np.searchsorted(cf,1-tail)),len(q)-1)
        out['coverage'+str(level)]=lo<=hit<=hi
    return out

def metrics(rows,arm):
    r=[x['arms'][arm] for x in rows];zero=sum(x['zero'] for x in r)
    return {'n':len(r),'rmse':float(np.sqrt(np.mean([x['se'] for x in r]))),'crps':float(np.mean([x['crps'] for x in r])),
            'nll':None if zero else float(np.mean([x['nll'] for x in r])),'nll_status':'infinite' if zero else 'finite','zero_n':zero,
            'coverage80':float(np.mean([x['coverage80'] for x in r])),'coverage95':float(np.mean([x['coverage95'] for x in r]))}

def summarize(rows):
    base=metrics(rows,'current');out={};days=sorted({r['day'] for r in rows});n=len(days);rng=np.random.default_rng(230922)
    draws=[]
    for _ in range(4000):
        starts=rng.integers(0,n-2,math.ceil(n/3));draws.append([k+j for k in starts for j in range(3)][:n])
    for arm in ARMS:
        m=metrics(rows,arm);m['change_pct']={k:100*(m[k]/base[k]-1) if m[k] is not None else None for k in ['rmse','crps','nll']}
        sums=np.array([[sum(r['arms'][a][k] for r in rows if r['day']==d) for a in [arm,'current'] for k in ['se','crps','nll']]+[sum(r['day']==d for r in rows)] for d in days])
        dif=[]
        for ix in draws:
            a,b,c,d,e,f,nn=sums[ix].sum(axis=0);dif.append([math.sqrt(a/nn)-math.sqrt(d/nn),(b-e)/nn,(c-f)/nn])
        dif=np.array(dif);m['delta_ci95']=dict(zip(['rmse','crps','nll'],np.quantile(dif,[.025,.975],axis=0).T.tolist()))
        m['bootstrap_all_three_lower_share']=float(np.mean(np.all(dif<0,axis=1)))
        m['halves']={label:metrics([r for r in rows if (r['day']<'2026-09-09')==early],arm) for label,early in [('first',True),('second',False)]}
        st=sorted({r['station'] for r in rows});loo=[]
        m['station_all_three_lower_n']=0
        for station in st:
            local=[r for r in rows if r['station']==station];b0=metrics(local,'current');a0=metrics(local,arm)
            m['station_all_three_lower_n']+=all(a0[k]<b0[k] for k in ['rmse','crps','nll'])
            without=[r for r in rows if r['station']!=station];b0=metrics(without,'current');a0=metrics(without,arm)
            loo.append([a0[k]-b0[k] for k in ['rmse','crps','nll']])
        m['leave_one_station_out_delta_range']=dict(zip(['rmse','crps','nll'],np.array([np.min(loo,axis=0),np.max(loo,axis=0)]).T.tolist()))
        out[arm]=m
    return out

def run():
    guard=C.study.Guard();data,old=inputs();rows=[];diag=[]
    for x in data:
        guard.check()
        if x['sample_sha'] not in old or not x['system_probabilities']:continue
        prev=old[x['sample_sha']];ws={a:prev['arms'][a]['weights'] for a in ARMS[:3]}
        for name,alpha in [('blend25',.25),('blend50',.5),('blend75',.75)]:
            ws[name]={s:alpha*ws['center_stack21'][s]+(1-alpha)*ws['crps_hier21'][s] for s in ws['current']}
        dd={'sample_sha':x['sample_sha']}
        for name,guarded in [('joint',False),('joint_guarded',True)]:ws[name],dd[name]=predict(data,x,guarded)
        for w in ws.values():C.assert_weight(w,x)
        rows.append({'station':x['station'],'day':x['event_date'],'decision_at':x['decision_at'],'sample_sha':x['sample_sha'],'arms':{a:score(x,w) for a,w in ws.items()}})
        diag.append(dd)
    C.study.save('scores.json',rows);C.study.save('fit_diagnostics.json',diag)
    result={'n':len(rows),'days':len({r['day'] for r in rows}),'stations':len({r['station'] for r in rows}),'arms':summarize(rows),'peak_mib':guard.peak/1024**2,
            'inputs_sha256':{str(p.relative_to(C.ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),HERE/'PROTOCOL.md',HERE.parent/'compare.py',HERE.parent/'scores.json',HERE.parent/'reconstructed.json',HERE.parent/'enrichment.json',HERE.parent.parent/'p0_physical_20260920/samples.json']}}
    C.study.save('results.json',result)
    for a,r in result['arms'].items():print(a,r['change_pct'],r['delta_ci95'],'joint_bootstrap',r['bootstrap_all_three_lower_share'],flush=True)

if __name__=='__main__':run()
