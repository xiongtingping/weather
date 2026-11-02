"""Independent checks for joint losses and convex mixtures."""
import copy,json
import numpy as np
import joint as J
C=J.C

def main():
    guard=C.study.Guard();data,old=J.inputs();bysha={x['sample_sha']:x for x in data};rows=json.loads((J.HERE/'scores.json').read_text());checks={}
    jensen=[];cdf_errors=[]
    for r in rows:
        x=bysha[r['sample_sha']];keys=sorted(x['weights']);_,_,p,hit,z,_=C.prob_components(x,keys)
        for name,alpha in [('blend25',.25),('blend50',.5),('blend75',.75)]:
            for metric in ['se','crps','nll']:
                lhs=r['arms'][name][metric];rhs=alpha*r['arms']['center_stack21'][metric]+(1-alpha)*r['arms']['crps_hier21'][metric]
                assert lhs<=rhs+1e-9;jensen.append(lhs-rhs)
        for a,row in r['arms'].items():
            C.assert_weight(row['weights'],x)
            q=np.array([row['weights'][s] for s in keys])@p;grid=np.sort(np.unique(np.r_[z,x['truth_c']]));mid=(grid[:-1]+grid[1:])/2
            f=np.array([q[z<=m].sum() for m in mid]);score=float(np.sum((f-(mid>=x['truth_c']))**2*np.diff(grid)))
            cdf_errors.append(abs(score-row['crps']));assert cdf_errors[-1]<1e-9
            assert not row['zero'];assert abs(-np.log(q[hit])-row['nll'])<1e-9
    checks['jensen_checks']=len(jensen);checks['jensen_max_excess']=max(jensen);checks['cdf_check_n']=len(cdf_errors);checks['cdf_max_error']=max(cdf_errors)
    x=bysha[rows[-1]['sample_sha']];fleet=C.eligible(data,x,21,'fleet',prob=True);obj=J.Objective(fleet,x,x['weights'],1.)
    w=np.array([x['weights'][s] for s in obj.keys]);eps=1e-6;numeric=[]
    for k in range(len(w)):
        d=np.zeros(len(w));d[k]=eps;numeric.append((obj.fun(w+d)-obj.fun(w-d))/(2*eps))
    error=float(np.max(abs(np.array(numeric)-obj.jac(w))));assert error<1e-6;checks['joint_gradient_max_error']=error
    for guarded in [False,True]:
        a,_=J.predict(data,x,guarded);mut=copy.deepcopy(x);mut['truth_c']=9999.;b,_=J.predict(data,mut,guarded)
        assert a==b
    checks['target_label_mutation_invariant']=True
    for r in rows:
        x=bysha[r['sample_sha']]
        assert all(y['known']<=x['at'] and y['at']<x['at'] for y in C.eligible(data,x,21,'fleet',prob=True))
    checks['training_asof_checked']=True
    fit=json.loads((J.HERE/'fit_diagnostics.json').read_text());deltas=[]
    for r in fit:
        for scope in ['fleet','local']:
            d=r['joint_guarded'].get(scope,{})
            if d.get('status')=='fit':deltas+=d['training_relative_loss_delta']
    assert max(deltas)<=1e-7;checks['guarded_fit_loss_checks']=len(deltas);checks['guard_max_relative_violation']=max(deltas)
    result=json.loads((J.HERE/'results.json').read_text())
    for arm in J.ARMS:
        m=J.metrics(rows,arm)
        for k in ['rmse','crps','nll']:assert abs(m[k]-result['arms'][arm][k])<1e-12
    checks['common_denominator']=len(rows);checks['summary_recomputed']=True
    # Attribution in the same population: covariance of the two endpoint errors.
    ec=np.array([r['arms']['center_stack21']['error'] for r in rows]);ep=np.array([r['arms']['crps_hier21']['error'] for r in rows]);eb=np.array([r['arms']['blend50']['error'] for r in rows])
    gain=.5*np.mean(ec**2)+.5*np.mean(ep**2)-np.mean(eb**2)
    assert abs(gain-.25*np.mean((ec-ep)**2))<1e-12
    checks['mse_blend_diversity_gain_c2']=float(gain)
    checks['zero_hit_n']=sum(r['arms'][a]['zero'] for r in rows for a in J.ARMS)
    C.study.save('validation.json',checks);print(json.dumps(checks,indent=2))

if __name__=='__main__':main()
