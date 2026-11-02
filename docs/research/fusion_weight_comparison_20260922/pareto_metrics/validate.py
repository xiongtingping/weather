import json,hashlib
import numpy as np
import evaluate as E
C=E.C

def main():
    g=C.study.Guard();pairs=E.load();r=json.loads((E.HERE/'results.json').read_text());rows=json.loads((E.HERE/'scores.json').read_text());f=json.loads((E.HERE/'candidates.json').read_text());checks={}
    assert C.study.digest([z['sample_sha'] for z in rows])==f['sample_sha256']
    assert len({(z['station'],z['day']) for z in rows})==len(rows)==515
    n=0;diff=[];Ks=[]
    for (x,old),z in zip(pairs,rows):
        assert x['sample_sha']==z['sample_sha'];keys=sorted(x['weights']);_,_,p,hit,_,buckets=C.prob_components(x,keys);Ks.append(len(buckets))
        y=np.zeros(len(buckets));y[hit]=1
        for a in E.ARMS:
            q=np.array([old['arms'][a]['weights'][s] for s in keys])@p
            independent=1-2*q[hit]+q@q;assert abs(independent-z['arms'][a]['brier'])<1e-12
            assert abs(np.log(q[hit])-z['arms'][a]['logscore'])<1e-12
            n+=1
    for a,v in r['arms'].items():
        for name,d in v['decomposition'].items():
            rebuilt=d['reliability']-d['resolution']+d['uncertainty']+d['binning_correction'];diff.append(abs(rebuilt-v['mean_bucket_brier']));assert diff[-1]<1e-12
            assert d['reliability']>=0 and d['resolution']>=0 and d['uncertainty']>=0
        assert abs(v['nll']+v['logscore'])<1e-12
    # Boundary cases: probabilities 0/1 assigned to valid bins; perfect forecast
    # has zero Brier, and uniform 11-class forecast has Brier 10/11.
    p=np.array([0,0,1.]);y=p.copy();d=E.decompose(E.bin_stats(p,y,E.EDGES));assert abs(d['mean_bucket_brier'])<1e-12
    p=np.ones(11)/11;y=np.zeros(11);y[2]=1;assert abs(np.sum((p-y)**2)-10/11)<1e-12
    checks.update(sample_n=515,frozen_weights=True,independent_brier_and_logscore_n=n,max_decomposition_error=max(diff),bucket_counts=sorted(set(Ks)),boundary_cases_passed=True)
    for a,fraction in [('blend25',.25),('blend50',.5),('blend75',.75)]:
        assert f['candidates'][a]['center_fraction']==fraction
        assert C.study.digest([{z['sample_sha']:z['arms'][a]['weights']} for _,z in pairs])==f['candidates'][a]['weights_sha256']
    # Future market evidence must not be slipped into the exact-time subset.
    qa=json.loads((E.HERE/'economic_quote_audit.json').read_text());assert sum(qa['counts'].values())==515
    qidx={q['sample_sha']:q for q in json.loads((E.HERE/'asof_quotes.json').read_text())}
    for z in qa['rows']:
        if z['status']=='basic_quote_eligible_execution_unverified':
            q=qidx[z['sample_sha']];at=C.study.stamp(z['decision_at']);assert C.study.stamp(q['metadata']['quote_expires_at'])>at
            assert all(C.study.stamp(b['book_observed_at'])<=at for b in q['buckets'])
    checks['exact_quote_audit_total']=515
    ex=json.loads((E.HERE/'execution_audit.json').read_text());eq={q['sample_sha']:q for q in json.loads((E.HERE/'execution_window_quotes.json').read_text())}
    assert sum(ex['counts'].values())==515
    for row in ex['rows']:
        if row['status']!='first_quote_found':continue
        q=eq[row['sample_sha']];at=C.study.stamp(row['execution_at']);m=q['snapshot_metadata']
        assert 0<=row['delay_s']<=300
        assert at<C.study.stamp(row['deadline']) and at<C.study.stamp(m['quote_expires_at'])
        assert all(C.study.stamp(b['book_observed_at'])<=at for b in q['buckets'])
        assert max(C.study.stamp(m[k]) for k in ['observed_at','quote_observed_at','created_at'])<=at
    checks['fixed_execution_window_quotes_checked']=len(eq)
    checks['economic_result_not_imputed']=ex['economic_status']=='NOT_IDENTIFIED'
    C.study.save('validation.json',checks);print(json.dumps(checks,indent=2))

if __name__=='__main__':main()
