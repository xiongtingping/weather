"""Independent edge, inverse-direction, source and chronology checks."""
import gzip,json,math
from pathlib import Path
import numpy as np
from audit import HERE,guard,utc
from experiment import update,innovation,prepare,satellite_maps,pack,scores,label_at
from bayes_basket_target.materialization import floor_probabilities

def main():
    guard();checks=[]
    p=np.array([[0.,.7,.2,.1]]);support=p>0;u=np.array([[0.,0.,1.,2.]])
    for b in (0.,.01,.25,1.,4.,8.):
        for d in (-1.,-.4,0.,.4,1.):
            q=update(p,support,u,np.array([[d,0.]]),[b,0.])
            raw={str(i):p[0,i]*math.exp(b*d*u[0,i]) for i in range(4)};z=sum(raw.values());raw={k:v/z for k,v in raw.items()}
            expected=p[0] if b*d==0 else np.array(list(floor_probabilities(raw,{'0'})[0].values()))
            assert np.allclose(q[0],expected,atol=1e-12)
            assert q[0,0]==0 and abs(q.sum()-1)<1e-12
            if d<0:assert q[0,1]>=p[0,1]-1e-12
            if d>0:assert q[0,1]<=p[0,1]+1e-12
    checks.append('30 scalar-vs-vector, dark-bright reversal and support boundary cases')
    assert np.array_equal(update(p,support,u,np.array([[.4,1.]]),[0.,0.]),p)
    assert np.array_equal(update(p,support,u,np.array([[0.,0.]]),[1.,1.]),p)
    checks.append('zero strength and zero evidence are exact identities')
    # Same C-valued temperature axis after converting a 2F settlement rung.
    x=2*5/9
    q=np.array([[.5,.5]]);k=np.array([0]);width=np.array([[x]])
    assert abs(scores(q,k,width)[0,1]-.25*x)<1e-12
    checks.append('2F=10/9C CRPS distance, without hardcoded real market bucket widths')
    rows=[json.loads(l) for l in gzip.open(HERE/'audited_rows.jsonl.gz','rt')]
    maps=satellite_maps();xrows,excluded=prepare(rows,maps,2)
    for x in xrows:
        r=x['r'];assert utc(r['observation_first_seen_at'])<=utc(r['decided_at'])
        assert r['evidence_role']=='observational_candidate_not_oos'
        assert r['observation_source_lane']=='awc'
        if r['posterior_version']=='p1_joint_likelihood_v38':
            assert r['root_versions']['P0']=='calibrated_native_cross_ensemble_p0_v27'
            if 'P0-M' in r['root_versions']:assert r['root_versions']['P0-M']=='p0m_taf_likelihood_v30'
        assert utc(r['observation_committed_at'])<=utc(r['decided_at'])
        assert utc(x['rad_evidence']['ends_at'])<=utc(r['decided_at'])
        assert abs(x['p'].sum()-1)<1e-6
        assert all(x['p'][i]==0 for i,key in enumerate(x['keys']) if key in r['hard_excluded'])
    checks.append(f"full {len(xrows)} rows: observation receipt cutoff, shortwave cutoff and zero mask")
    r=next(x['r'] for x in xrows if x['r']['station']=='ZUUU')
    assert innovation(r,{},2) is None
    assert innovation(r,{f"{r['day']}T{h:02}:00":None for h in range(24)},2) is None
    night={**r,'fc':[{**f,'sw':0.} for f in r['fc']]}
    assert innovation(night,maps['ZUUU'],2) is None
    checks.append('missing observations, explicit nulls and dark hours fail closed')
    earliest=min(utc(t[3]) for t in xrows[0]['r']['truth_history'] if t[3])
    from datetime import timedelta
    assert label_at(xrows[0],earliest-timedelta(microseconds=1)) is None
    checks.append('training refuses a truth revision before its actual commit')
    source=json.load(open(HERE/'sources.json'));probes=source['probes']
    z0=next(x for x in probes if x['station']=='ZUUU' and x['model']=='default')['day7']
    z1=next(x for x in probes if x['station']=='ZUUU' and x['model']=='jma_jaxa_himawari')['day7']
    assert z0['2026-10-07T13:00']!=z1['2026-10-07T13:00']
    assert z1['2026-10-07T23:00'] is None
    assert next(x for x in probes if x['station']=='KLGA' and x['model']=='satellite_radiation_seamless')['error']
    checks.append('default is not accepted as satellite; future satellite null; KLGA unavailable')
    out=dict(passed=True,checks=checks,resource_guard='RSS 1GiB, free disk 2GiB',production_modified=False,strict_asof_activation_eligible=False)
    (HERE/'verification.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
