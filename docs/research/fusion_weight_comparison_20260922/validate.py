"""Analysis checks: timestamp boundaries, scoring math, support constraints and slices."""
import copy, json, hashlib
from datetime import timedelta
import numpy as np
import compare as C

def main():
    guard=C.study.Guard();data=C.load();checks={}
    sp={r['sample_sha']:r for r in json.loads((C.HERE/'reconstructed.json').read_text())}
    for r in data:
        if r['sample_sha'] in sp:r['system_probabilities']=sp[r['sample_sha']]['system_probabilities']
    scores=json.loads((C.HERE/'scores.json').read_text());results=json.loads((C.HERE/'results.json').read_text())
    bysha={r['sample_sha']:r for r in data}
    assert len({(r['station'],r['day']) for r in scores})==len(scores)
    checks['unique_station_day']=len(scores)
    checks['future_training_rows']=0
    for x in data:
        for r in C.eligible(data,x,21,'fleet'):
            assert r['at']<x['at'] and r['known']<=x['at'] and r['event_date']<x['local_decision_day'].isoformat()
    checks['all_asof_training_eligible_checked']=True
    x=copy.deepcopy(data[-1]);h=copy.deepcopy(x)
    h['event_date']=(x['local_decision_day']-timedelta(days=2)).isoformat();h['at']=x['at']-timedelta(days=3);h['known']=x['at']+timedelta(microseconds=1)
    assert not C.eligible([h],x,5)
    h['known']=x['at'];assert C.eligible([h],x,5)
    h['event_date']=(x['local_decision_day']-timedelta(days=6)).isoformat();assert not C.eligible([h],x,5)
    checks['late_truth_boundary_and_calendar_window']=True
    # Changing an inaccessible label must not change fitted weights.
    x=bysha[scores[-1]['sample_sha']];past=[r for r in data if r['at']<x['at']]
    w,_=C.candidates(past,x);fake=copy.deepcopy(x);fake['truth_c']=999.;fake['known']=x['at']+timedelta(days=10)
    w2,_=C.candidates(past+[fake],x);assert w==w2
    checks['inaccessible_label_mutation_invariance']=True
    # Independent CRPS integral for the discrete representative-point distribution.
    errors=[]
    for r in scores:
        x=bysha[r['sample_sha']]
        for name,a in r['arms'].items():
            C.assert_weight(a['weights'],x)
            assert abs(sum(a['weights'][s]*x['centers'][s] for s in a['weights'])-a['prediction'])<1e-9
            if 'pool_crps' not in a:continue
            keys=sorted(x['weights']);_,_,p,hit,z,_=C.prob_components(x,keys)
            q=np.array([a['weights'][s] for s in keys])@p;grid=np.sort(np.unique(np.r_[z,x['truth_c']]))
            mids=(grid[:-1]+grid[1:])/2;cdf=np.array([q[z<=m].sum() for m in mids]);val=float(np.sum((cdf-(mids>=x['truth_c']))**2*np.diff(grid)))
            errors.append(abs(val-a['pool_crps']))
            assert errors[-1]<1e-9
            if q[hit]>0:assert abs(-np.log(q[hit])-a['pool_nll'])<1e-9
    checks['independent_crps_max_error']=max(errors);checks['independent_crps_cases']=len(errors)
    for arm in C.ARMS:
        s=C.summarize(scores,arm)
        assert s['rmse']==results['arms'][arm]['rmse']
        assert s['pool_crps']==results['arms'][arm]['pool_crps']
    checks['summary_recomputed']=True
    # Weight jitter: distinguish unchanged model sets from forced seat changes.
    prev={};jitter={a:[] for a in C.ARMS};changes=0
    for r in sorted(scores,key=lambda r:r['decision_at']):
        st=r['station'];old=prev.get(st)
        if old:
            same=set(r['arms']['current']['weights'])==set(old['arms']['current']['weights'])
            changes+=not same
            if same:
                for a in C.ARMS:
                    w=r['arms'][a]['weights'];v=old['arms'][a]['weights'];jitter[a].append(.5*sum(abs(w[k]-v[k]) for k in w))
        prev[st]=r
    checks['model_set_changes']=changes;checks['same_set_turnover']={a:dict(n=len(v),mean=float(np.mean(v))) for a,v in jitter.items()}
    influence={}
    for a in C.ARMS:
        station_deltas={st:sum(r['arms'][a].get('pool_crps',0)-r['arms']['current'].get('pool_crps',0) for r in scores if r['station']==st) for st in sorted(set(r['station'] for r in scores))}
        n=sum('pool_crps' in r['arms'][a] for r in scores)
        loo=[]
        for st,d in station_deltas.items():
            removed=sum(r['station']==st and 'pool_crps' in r['arms'][a] for r in scores)
            loo.append((sum(station_deltas.values())-d)/(n-removed))
        influence[a]=dict(station_contributions=station_deltas,leave_one_station_out_delta_range=[min(loo),max(loo)])
    checks['influence']=influence
    checks['resource_peak_mib']=guard.peak/1024**2
    C.study.save('validation.json',checks)
    print(json.dumps({k:v for k,v in checks.items() if k not in ('influence','same_set_turnover')},indent=2))
    print('same_set_turnover',checks['same_set_turnover'])

if __name__=='__main__':main()
