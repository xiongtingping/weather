"""Predeclared inner selection and frozen outer evaluation; no source writes."""
from __future__ import annotations
import os
os.environ['OPENBLAS_NUM_THREADS']='1';os.environ['OMP_NUM_THREADS']='1'
import collections
import hashlib
import json
import math
from pathlib import Path
import numpy as np
from study import Guard, OUT, digest, save, stamp
from model import ARMS, RIDGES, fit, scored_rows, summarize, geometry


def validate_evidence(samples):
    seen=set()
    for s in samples:
        key=(s['station'],s['event_date'])
        if key in seen:raise ValueError('duplicate_station_day')
        seen.add(key)
        if stamp(s['taf_known_at'])>stamp(s['decision_at']):raise ValueError('future_taf')
        if any(stamp(t)>stamp(s['p0_nominal_decision_at']) for t in s['root_receipts'].values()):raise ValueError('future_native_receipt')
        if stamp(s['p0_created_at'])>stamp(s['decision_at']):raise ValueError('root_not_yet_created')
        if stamp(s['truth_known_at'])<=stamp(s['decision_at']):raise ValueError('truth_already_known_at_forecast')
        if any(not math.isfinite(float(s[k])) for k in ['cloud','precip','wind_mps','taf_age_h','taf_day_coverage']):raise ValueError('invalid_feature')


def partition(samples):
    primary=sorted((s for s in samples if s['primary_eligible']),key=lambda s:(s['event_date'],s['station']))
    val=[s for s in primary if '2026-09-05'<=s['event_date']<='2026-09-11']
    outer=[s for s in primary if '2026-09-12'<=s['event_date']<='2026-09-19']
    if not val or not outer:raise RuntimeError('missing_temporal_partition')
    val_at=min(stamp(s['decision_at']) for s in val); outer_at=min(stamp(s['decision_at']) for s in outer)
    train=[s for s in primary if s['event_date']<='2026-09-04' and stamp(s['truth_known_at'])<val_at]
    selectable_val=[s for s in val if stamp(s['truth_known_at'])<outer_at]
    refit=[s for s in primary if s['event_date']<='2026-09-11' and stamp(s['truth_known_at'])<outer_at]
    return train,selectable_val,refit,outer,{'validation_fit_cutoff':val_at.isoformat(),'outer_fit_cutoff':outer_at.isoformat(),
        'train_n':len(train),'validation_n':len(selectable_val),'refit_n':len(refit),'outer_n':len(outer),
        'validation_unavailable_labels':len(val)-len(selectable_val),'outer_days':sorted({s['event_date'] for s in outer}),
        'outer_stations':sorted({s['station'] for s in outer})}


def paired_intervals(base,candidate):
    if [r['sample_sha'] for r in base]!=[r['sample_sha'] for r in candidate]:raise ValueError('unpaired_scores')
    days=sorted({r['event_date'] for r in base});by_day={d:[] for d in days}
    for b,c in zip(base,candidate):by_day[b['event_date']].append((b['crps_c'],c['crps_c']))
    arr=[np.array(by_day[d]) for d in days]
    rng=np.random.default_rng(20260920); intervals={}
    for block in (1,2):
        values=[]
        for _ in range(1500):
            idx=[]
            while len(idx)<len(days):
                k=int(rng.integers(len(days)))
                idx.extend((k+j)%len(days) for j in range(block))
            a=np.concatenate([arr[i] for i in idx[:len(days)]])
            values.append(100*(a[:,1].mean()/a[:,0].mean()-1))
        intervals[f'date_block_{block}_ci95_pct']=np.quantile(values,[.025,.975]).tolist()
    intervals['crps_change_pct']=100*(np.mean([r['crps_c'] for r in candidate])/np.mean([r['crps_c'] for r in base])-1)
    return intervals


def slices(sample_by_sha,rows):
    masks=collections.defaultdict(list)
    for r in rows:
        s=sample_by_sha[r['sample_sha']];y=s['truth_c'];cs=list(s['centers'].values());lo,hi=min(cs),max(cs)
        group='common_cold' if y>hi else 'common_warm' if y<lo else 'inside_center_hull'
        masks[group].append(r)
        masks['tx_present' if s['tx_c'] is not None else 'tx_absent'].append(r)
        masks['four_systems' if len(s['weights'])==4 else 'other_pool_size'].append(r)
        masks['disagreement_ge_2c' if hi-lo>=2 else 'disagreement_lt_2c'].append(r)
        masks['station:'+s['station']].append(r)
        masks['version:'+s['posterior_version']].append(r)
        if s['station']!='ZUUU':masks['without_ZUUU'].append(r)
    return {k:summarize(v) for k,v in masks.items()}


def attribution(samples):
    groups=collections.defaultdict(list); total_loss=0.0;unavoidable=0.0
    for s in samples:
        y=s['truth_c'];cs=list(s['centers'].values());lo,hi=min(cs),max(cs);f=s['native_mean_c']
        if not lo-1e-8<=f<=hi+1e-8:raise ValueError('blend_outside_own_centers')
        loss=abs(f-y);dist=max(0,lo-y,y-hi)
        if loss<dist-1e-8:raise ValueError('invalid_projection')
        total_loss+=loss;unavoidable+=dist
        key='common_cold' if y>hi else 'common_warm' if y<lo else 'inside_center_hull'
        groups[key].append(s)
    desc={}
    for key,rows in groups.items():
        desc[key]={'n':len(rows),'mean_native_error_c':float(np.mean([s['native_mean_c']-s['truth_c'] for s in rows])),
                   'cloud_mean':float(np.mean([s['cloud'] for s in rows])),
                   'precip_mean':float(np.mean([s['precip'] for s in rows])),
                   'wind_mean_mps':float(np.mean([s['wind_mps'] for s in rows]))}
    return {'groups':desc,'native_center_absolute_error_sum_c':total_loss,
            'outside_hull_unavoidable_sum_c':unavoidable,'oracle_reweight_reducible_sum_c':total_loss-unavoidable,
            'unavoidable_share':unavoidable/total_loss if total_loss else None,
            'interpretation':'Descriptive, not causal; per-model centers, not full ensemble support.'}


def main():
    guard=Guard();data_path=OUT/'samples.json';samples=json.loads(data_path.read_text())
    if len(samples)>2500:raise RuntimeError('sample_limit')
    validate_evidence(samples)
    train,val,refit,outer,split=partition(samples)
    print('SPLIT',split,flush=True)
    for rows,cut in [(train,split['validation_fit_cutoff']),(refit,split['outer_fit_cutoff'])]:
        assert all(stamp(s['truth_known_at'])<stamp(cut) for s in rows)
    validation=[];choices={};base_val=scored_rows(val,{'arm':'identity'})
    for arm in ARMS:
        if arm=='identity':
            choices[arm]={'arm':'identity','ridge':None,'validation':summarize(base_val)}
            validation.append(choices[arm]);continue
        for ridge in RIDGES:
            guard.check();artifact=fit(train,arm,ridge);scores=scored_rows(val,artifact);summary=summarize(scores)
            row={'arm':arm,'ridge':ridge,'validation':summary};validation.append(row)
            if arm not in choices or (summary['crps_c'],-ridge)<(choices[arm]['validation']['crps_c'],-choices[arm]['ridge']):choices[arm]=row
            print('INNER',arm,ridge,summary['crps_c'],summary['nll_status'],flush=True)
    selected=min(choices,key=lambda a:(choices[a]['validation']['crps_c'],ARMS.index(a)))
    frozen={};score_by_arm={}
    # Refit and persist BEFORE evaluating any outer outcomes.
    for arm in ARMS:
        artifact={'arm':'identity','stage':'P0_BASELINE'} if arm=='identity' else fit(refit,arm,choices[arm]['ridge'])
        artifact['fit_at']=split['outer_fit_cutoff'];artifact['training_sample_shas']=[s['sample_sha'] for s in refit]
        artifact['content_sha']=digest(artifact);frozen[arm]=artifact
    save('candidate_artifacts.json',{'selected_on_inner':selected,'artifacts':frozen,'split':split,'validation':validation,
                                    'protocol_sha':hashlib.sha256((OUT/'PROTOCOL.md').read_bytes()).hexdigest(),
                                    'data_sha':hashlib.sha256(data_path.read_bytes()).hexdigest()})
    for arm in ARMS:
        score_by_arm[arm]=scored_rows(outer,frozen[arm]);print('OUTER',arm,summarize(score_by_arm[arm]),flush=True)
    sample_by_sha={s['sample_sha']:s for s in samples};base=score_by_arm['identity']
    results={'schema':'physical_candidate_outer_v1','selected_on_inner':selected,'split':split,
             'primary_scope':'non-ASOS stations with as-of official TAF and native P0; not 45-station promotion evidence',
             'validation':validation,'outer':{},'attribution_outer':attribution(outer),
             'candidate_attribution_not_causal':True,'production_changed':False,'resource_peak_mib':guard.peak/1024**2}
    for arm,rows in score_by_arm.items():
        results['outer'][arm]={'metrics':summarize(rows),'paired_crps':paired_intervals(base,rows),
                               'slices':slices(sample_by_sha,rows)}
    # This comparison isolates weather information beyond the TX arm, not
    # just improvement over a P0 that has not seen any TAF.
    results['joint_vs_tx']=paired_intervals(score_by_arm['tx'],score_by_arm['joint'])
    results['joint_vs_unconditional']=paired_intervals(score_by_arm['unconditional'],score_by_arm['joint'])
    results['decision']='RESEARCH_ONLY_NO_PROMOTION'
    save('outer_scores.json',score_by_arm);save('results.json',results)
    print('SELECTED',selected,'RESULT',results['outer'][selected]['paired_crps'],flush=True)


if __name__=='__main__':main()
