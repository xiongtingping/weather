"""Independent arithmetic, boundary, reversal, and sensitivity checks."""
import collections, copy, hashlib, json, math, pathlib, sqlite3, datetime as dt
import numpy as np
import analyze as A
from bayes_basket_target.cross_consensus import bridged_cross_probability
from bayes_basket_target.center_width_calibration import apply_center_width_calibration, served_bucket_axis

OUT=pathlib.Path(__file__).parent;ROOT=OUT.parents[2]
def main():
    A.guard();capture=json.loads((OUT/'capture.json').read_text());rows=json.loads((OUT/'scored_rows.json').read_text());results=json.loads((OUT/'results.json').read_text())
    by_key={(r['station'],r['date']):r for r in capture['rows']}
    assert len({(r['station'],r['date']) for r in rows})==len(rows)
    assert len({r['checkpoint'] for r in rows})==len(rows)
    total=roundtrip=zero_bridge=seat_identity=probability_vectors=0;max_error=0.;proper={};sens={};label_invariance=0
    for r in rows:
        A.guard();original=by_key[r['station'],r['date']];x=original['inputs'];keys=list(r['arms'][A.BASE]);hit=keys.index(r['hit'])
        assert len(keys)==11
        fusion=x['fusion_blend_json']
        if fusion:
            assert fusion['served_weights']==x['system_weights_json']
            assert fusion['seat_weights']==original['selection_weights']
        for arm,p in r['arms'].items():
            assert set(p)==set(keys);A.vector_ok(p)
            vector=np.array([p[k] for k in keys]);target=np.eye(len(keys))[hit]
            bs=float(vector@vector-2*vector[hit]+1);bs2=float(np.linalg.norm(vector-target)**2)
            ll=float(np.log(1/vector[hit]));s=r['scores'][arm]
            max_error=max(max_error,abs(bs-s['brier']),abs(bs2-s['brier']),abs(ll-s['logloss']))
            assert abs(bs-s['brier'])<1e-12 and abs(bs2-s['brier'])<1e-12 and abs(ll-s['logloss'])<1e-12
            total+=1
        baseline,_=A.pipeline(original)
        for flag in [('equal',False,False),('seat',True,False),('served',False,True)]:A.pipeline(original,weight=flag[0],bridge=flag[1],width=flag[2])
        restored,_=A.pipeline(original);assert baseline==restored==r['arms'][A.BASE];roundtrip+=1
        poisoned=copy.deepcopy(original);poisoned['truth_c']=original['truth_c']+1000;poisoned['truth_revision']=999999
        without_labels,_=A.pipeline(poisoned);assert without_labels==baseline;label_invariance+=1
        if not x['consensus_bridge_json'] or x['consensus_bridge_json']['lambda']==0:
            for w in ['served','seat','equal']:
                assert A.delta(r['arms'][f'{w}_b0_w1'],r['arms'][f'{w}_b1_w1'])<1e-12
            zero_bridge+=1
        if x['system_weights_json']==original['selection_weights']:
            for b in [0,1]:
                for w in [0,1]:assert r['arms'][f'served_b{b}_w{w}']==r['arms'][f'seat_b{b}_w{w}']
            seat_identity+=1
        # All source vectors share the same buckets and valid probability mass.
        for p in x['native_system_probabilities_json'].values():assert set(p)==set(keys);A.vector_ok(p);probability_vectors+=1
    assert max_error<1e-12
    # Unit-derived geometry, open tails and bridge-zero edge cases in both market units.
    boundary=0
    for unit in ['C','F']:
        r=next(r for r in rows if r['market_unit']==unit);original=by_key[r['station'],r['date']];x=original['inputs'];geometry=x['bucket_intervals_c_json'];keys=A._ordered_buckets(geometry);axis,edges=served_bucket_axis(geometry,keys)
        for i,key in enumerate(keys[:-1]):
            edge=geometry[key][1];assert edge is not None
            for value,expected in [(np.nextafter(edge,-math.inf),key),(edge,keys[i+1]),(np.nextafter(edge,math.inf),keys[i+1])]:
                hits=[k for k,(lo,hi) in geometry.items() if (lo is None or value>=lo) and (hi is None or value<hi)]
                assert hits==[expected];boundary+=1
        for i in range(len(keys)):
            p={k:float(j==i) for j,k in enumerate(keys)}
            identity,_=apply_center_width_calibration(p,delta_c=0,width_lambda=1,ordered_keys=keys,key_centers_c=axis,open_edge_bounds_c=edges)
            assert A.delta(p,identity)<1e-12;boundary+=1
        p1=x['native_system_probabilities_json'][next(iter(x['native_system_probabilities_json']))];p2={k:1/len(keys) for k in keys}
        out,receipt=bridged_cross_probability(system_probabilities={'a':p1,'b':p2},system_weights={'a':.3,'b':.7},system_centers_c={'a':20.,'b':20.},ordered_keys=keys,key_centers_c=axis)
        expected={k:.3*p1[k]+.7*p2[k] for k in keys};z=math.fsum(expected.values());expected={k:v/z for k,v in expected.items()}
        assert receipt['lambda']==0 and A.delta(out,expected)<1e-12;boundary+=1
    # Independent aggregation and reversal signs; disjoint C/F partitions reconcile.
    for arm in A.ARMS:
        for metric in ['brier','logloss']:
            values=[r['scores'][arm][metric] for r in rows];mean=math.fsum(values)/len(rows)
            assert abs(mean-results['groups']['all']['means'][arm][metric])<1e-12
            reverse=math.fsum(r['scores'][A.BASE][metric]-r['scores'][arm][metric] for r in rows)/len(rows)
            assert abs(reverse+results['groups']['all']['comparison'][arm][metric]['delta'])<1e-12
            pooled=sum(results['groups'][u]['n']*results['groups'][u]['means'][arm][metric] for u in ['C','F'])/len(rows)
            assert abs(mean-pooled)<1e-12
    # Boundaries of inference: removing a day or a station can change the sign.
    for arm in ['seat_b1_w1','equal_b1_w1','served_b0_w1','served_b1_w0']:
        sens[arm]={}
        for m in ['brier','logloss']:
            vals=[]
            for station in sorted({r['station'] for r in rows}):
                rr=[r for r in rows if r['station']!=station]
                vals.append(math.fsum(r['scores'][arm][m]-r['scores'][A.BASE][m] for r in rr)/len(rr))
            sens[arm][m]=[min(vals),max(vals)]
    # Actual first-P0 >36h coverage, using prior audit's identity-checked references.
    prior=ROOT/'docs/research/proper_accuracy_20260927/rows.json'
    c=sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=2);c.execute('pragma query_only=ON');c.execute('pragma cache_size=-8192');c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,8*1024**2);c.set_progress_handler(lambda:(A.guard() or 0),10000)
    facts=json.loads(prior.read_text());pc=collections.Counter();details=[]
    for f in facts:
        if not ('2026-09-22'<=f['date']<='2026-09-26'):continue
        if f['activation_errors'] or f['late_sources']:continue
        pc['valid_prior_first_p0_refs']+=1
        v=c.execute("select json_extract(lineage_json,'$.native_system_probabilities_json'),json_extract(lineage_json,'$.fusion_blend_json'),posterior_version from basket_probability_envelope where envelope_id=?",(f['envelope_id'],)).fetchone()
        if not v:raise RuntimeError('prior reference missing')
        pc['single_system_vectors_present']+=int(bool(v[0]));pc['current_v19']+=int(v[2]=='calibrated_native_cross_ensemble_p0_v19')
        fusion=json.loads(v[1]) if v[1] else None
        pc['fusion_applied']+=int(bool(fusion) and fusion.get('state')=='APPLIED')
    c.close()
    payload={'scored_pairs':total,'source_probability_vectors':probability_vectors,'independent_score_max_error':max_error,'all_row_roundtrip_checks':roundtrip,'outcome_label_invariance_checks':label_invariance,'zero_bridge_rows':zero_bridge,'seat_identity_rows':seat_identity,'C_F_open_bucket_boundary_checks':boundary,'leave_one_station_out_ranges':sens,'proper_first_p0_coverage':dict(pc),'baseline_parity':json.loads((OUT/'reconstruction.json').read_text())['max_baseline_error'],
      'sha256':{n:hashlib.sha256((OUT/n).read_bytes()).hexdigest() for n in ['PROTOCOL.md','capture.py','capture.json','analyze.py','historical_width_pre198.py','results.json','scored_rows.json']}}
    (OUT/'validation.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2));print(json.dumps(payload,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
