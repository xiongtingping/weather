"""Read-only current implementation audit and bounded counterexample checks."""
import collections
import copy
import hashlib
import json
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]
sys.path.insert(0,str(ROOT/'docs/research/p0_physical_20260920'))
import study
study.OUT=OUT
from study import Guard, connect, save, stamp, iso
from bayes_basket_target import p1_direct_update as DU
from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target import native_ensemble_oos as OOS
from bayes_basket_target import native_ensemble_p0 as P0
from bayes_basket_target import cross_consensus as CC
from bayes_basket_target import taf_likelihood as TL
from bayes_basket_target.materialization import rehydrate_lineage_documents, _bucket_centers_c, floor_probabilities

SOURCE_FILES=[
 'bayes_basket_target/native_ensemble_oos.py','bayes_basket_target/native_ensemble_p0.py',
 'bayes_basket_target/cross_consensus.py','bayes_basket_target/served_shape.py',
 'bayes_basket_target/center_width_calibration.py','bayes_basket_target/center_width_store.py',
 'bayes_basket_target/taf_likelihood.py','bayes_basket_target/p1_direct_update.py',
 'bayes_basket_target/p1_observation_likelihood.py','bayes_basket_target/materialization.py',
 'scripts/fit_p1_observation_likelihood.py','scripts/fit_taf_likelihood.py',
]


def source_hashes():
    return {f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in SOURCE_FILES}


def unpack(conn,row):
    r=dict(row)
    l=rehydrate_lineage_documents(conn,json.loads(r['lineage_json']))
    fields=['center_width_json','band_reliability_json','synoptic_cooling_json',
            'p0m_taf_likelihood_json','p1_observation_likelihood_json','bucket_intervals_c_json',
            'prediction_root_checkpoint_id','prediction_root_content_sha256','prediction_root_stage',
            'p0_root_checkpoint_id','p0_root_content_sha256','hard_excluded_buckets',
            'native_cross_ensemble_artifact_sha256','model_center_c','system_weights_json']
    return {k:r[k] for k in ['station_id','event_date','decision_at_utc','created_at','checkpoint_id',
                             'stage','posterior_version','content_sha256','settlement_timezone']} | {
        'probabilities':json.loads(r['probabilities_json']),
        'ordinals':json.loads(r['bucket_ordinals_json']),
        'lineage':{k:l[k] for k in fields if k in l}}


def capture(guard):
    hashes=source_hashes();conn=connect(guard)
    domains=[list(r) for r in conn.execute('select distinct station_id,settlement_timezone from calibration_station_market_timezone_binding')]
    rows=[];roots={};counts=collections.Counter()
    for station,_ in domains:
        if len(station)!=4:continue
        for stage in ['P0','P0-M','P1']:
            r=conn.execute('select * from basket_probability_envelope indexed by basket_probability_envelope_station_stage_recency_idx where station_id=? and stage=? and state=? order by decision_at_utc desc limit 1',
                           (station,stage,'COMMITTED')).fetchone()
            if r is None:continue
            row=unpack(conn,r);rows.append(row);counts[stage+':'+row['posterior_version']]+=1
            if stage=='P1':
                lineage=row['lineage'];key=lineage.get('prediction_root_checkpoint_id')
                if key and key not in roots:
                    rs=conn.execute('select * from basket_probability_envelope where checkpoint_id=?',(key,)).fetchall()
                    exact=[v for v in rs if v['content_sha256']==lineage.get('prediction_root_content_sha256')]
                    if len(exact)!=1: raise RuntimeError('prediction_root_ambiguous')
                    roots[key]=unpack(conn,exact[0])
            guard.check()
    for name in ['p1_observation_likelihood_artifact','taf_likelihood_artifact']:
        p=ROOT/'data/runtime/bayes_basket_target_nwp'/f'{name}.json'
        if p.stat().st_size>1024**2:raise RuntimeError('artifact_size_limit')
        save(name+'.json',json.loads(p.read_text()))
    if source_hashes()!=hashes: raise RuntimeError('source_changed_during_capture')
    result={'captured_at':iso(datetime.now(timezone.utc)),'source_sha256':hashes,'domains':domains,
            'rows':rows,'roots':roots,'counts':dict(counts),'production_writes':0,
            'scope':'Latest committed envelope per station and stage; not a complete day/history or process-memory audit.'}
    save('current_snapshot.json',result);print('SNAPSHOT',dict(counts),flush=True)


def decoded(row,key):
    return json.loads(row['lineage'].get(key) or '{}')


def norm_product(prior,likelihood,excluded=()):
    x={k:(0.0 if k in excluded else p*likelihood[k]) for k,p in prior.items()};t=sum(x.values())
    assert t>0
    return floor_probabilities({k:v/t for k,v in x.items()},excluded)[0]


def main():
    guard=Guard()
    if '--capture' in sys.argv:capture(guard)
    snapshot=json.loads((OUT/'current_snapshot.json').read_text())
    if snapshot['source_sha256']!=source_hashes():raise RuntimeError('review_source_drift')
    artifact=json.loads((OUT/'p1_observation_likelihood_artifact.json').read_text())
    taf=json.loads((OUT/'taf_likelihood_artifact.json').read_text())
    cases=[];checked=0;stable=0;layer_states=collections.Counter()
    for r in snapshot['rows']:
        for key in ['center_width_json','band_reliability_json','p0m_taf_likelihood_json','p1_observation_likelihood_json']:
            receipt=decoded(r,key)
            if receipt:layer_states[r['stage']+':'+key+':'+str(receipt.get('state'))]+=1
    targets=list(snapshot['rows'])
    if (OUT/'near_boundary_rows.json').exists():
        for item in json.loads((OUT/'near_boundary_rows.json').read_text()):
            if item['row']['checkpoint_id'] not in {r['checkpoint_id'] for r in targets}:
                targets.append(item['row'])
                snapshot['roots'][item['root']['checkpoint_id']]=item['root']
    for r in targets:
        if r['stage']!='P1':continue
        receipt=decoded(r,'p1_observation_likelihood_json')
        if receipt.get('update')!=DU.UPDATE or receipt.get('artifact_sha256')!=artifact['content_sha256']:continue
        root=snapshot['roots'][r['lineage']['prediction_root_checkpoint_id']]
        axis=_bucket_centers_c(root['lineage'],bucket_ordinals=root['ordinals'])
        if axis is None:continue
        intervals=decoded(root,'bucket_intervals_c_json');features=receipt['features']
        excluded=r['lineage'].get('hard_excluded_buckets','NONE')
        excluded=[] if excluded=='NONE' else excluded.split(',')
        kwargs={'root_probabilities':root['probabilities'],'bucket_ordinals':root['ordinals'],
                'bucket_centers_c':dict(zip(*axis)),'bucket_upper_bounds_c':{k:v[1] for k,v in intervals.items()},
                'running_high_c':receipt['running_high_c'],'running_high_bucket':receipt['running_high_bucket'],
                'hard_excluded_buckets':excluded,'station_id':r['station_id']}
        for edge in [6.0,10.0]:
            limits=[]
            for epsilon in [1e-3,1e-6,1e-9]:
                output=[]
                for sign in [-1,1]:
                    f=dict(features,remaining_hours=edge+sign*epsilon-OL.station_clock_offset(artifact,r['station_id']))
                    l,_=DU.compile_direct_update(f,artifact,**kwargs)
                    output.append(norm_product(root['probabilities'],l,excluded))
                limits.append(max(abs(output[0][k]-output[1][k]) for k in output[0]))
            delta={k:output[1][k]-output[0][k] for k in output[0]}
            maxbucket=max(delta,key=lambda k:abs(delta[k]));checked+=1
            assert all(p[k]==0.0 for p in output for k in excluded)
            assert all(abs(sum(p.values())-1)<1e-12 and min(p.values())>=0 for p in output)
            cases.append({'station':r['station_id'],'source_checkpoint':r['checkpoint_id'],'edge_clock_h':edge,
                          'source_clock_h':receipt['clock_hours'],'distance_from_source_h':abs(receipt['clock_hours']-edge),
                          'max_bucket':maxbucket,'max_probability_jump':abs(delta[maxbucket]),
                          'below':output[0][maxbucket],'above':output[1][maxbucket],
                          'jumps_at_epsilon_1e_3_1e_6_1e_9_hours':limits,
                          'total_variation':sum(abs(v) for v in delta.values())/2,
                          'interpretation':'Frozen root/state, current matched artifact; counterfactual boundary probe, not observed historical jump.'})
        # Off the edges, an equally small clock change should stay small.
        qs=[]
        for eps in [-1e-6,1e-6]:
            f=dict(features,remaining_hours=8+eps-OL.station_clock_offset(artifact,r['station_id']))
            l,_=DU.compile_direct_update(f,artifact,**kwargs);qs.append(norm_product(root['probabilities'],l,excluded))
        assert max(abs(qs[0][k]-qs[1][k]) for k in qs[0])<1e-5;stable+=1
    # Radiation gate and serving use different scales for the same feature.
    c=sqlite3.connect(':memory:')
    c.execute('create table calibration_station_forecast_physics (station_id text,local_date text,reference_only integer,shortwave_radiation_sum real,captured_at_utc text)')
    event='2026-09-19';day=date.fromisoformat(event);history=[]
    for i in range(1,41):
        d=(day-timedelta(days=i)).isoformat();v=12+(i%7)*0.5;history.append((d,v))
        c.execute('insert into calibration_station_forecast_physics values (?,?,?,?,?)',('ZUUU',d,1,v,d+'T00:00:00Z'))
    c.execute('insert into calibration_station_forecast_physics values (?,?,?,?,?)',('ZUUU',event,1,19.0,'2099-01-01T00:00:00Z'))
    pred={s:{event:{'calibrated_center_c':25.0}} for s in ['NOAA_GEFS_05','ECMWF_IFS_ENS_OPEN_PF50']}
    signals=OOS._holdout_conditioning_signals(c,station_id='ZUUU',holdout_days=[event],predictions_by_system=pred,radiation_shift_sigma=0.15)
    lat=float(OOS.station_coordinates('ZUUU')['latitude'])
    unit_shift,_=CC.forecast_clearness_shift_sigma(radiation_sum=19,event_date=event,latitude_deg=lat,station_history=history,shift_sigma_per_unit=0.15)
    probability={'24':.25,'25':.5,'26':.25};sigma=P0._distribution_sigma_c(probability,[24.,25.,26.])
    served_shift=unit_shift*sigma;gate_shift=signals[event]['synoptic_cooling_shift_c']
    assert gate_shift is None and abs(served_shift)>0.01
    pred_spread={s:{event:{'calibrated_center_c':value}} for s,value in [('NOAA_GEFS_05',24.),('ECMWF_IFS_ENS_OPEN_PF50',26.)]}
    before=OOS._holdout_conditioning_signals(c,station_id='ZUUU',holdout_days=[event],predictions_by_system=pred_spread,radiation_shift_sigma=0.15)[event]
    c.execute('update calibration_station_forecast_physics set captured_at_utc=?',('1900-01-01T00:00:00Z',))
    after=OOS._holdout_conditioning_signals(c,station_id='ZUUU',holdout_days=[event],predictions_by_system=pred_spread,radiation_shift_sigma=0.15)[event]
    assert before==after
    assert abs(before['synoptic_cooling_shift_c'])>0.01
    # Same data values, arbitrary receipt dates: current reader ignores arrival.
    assert P0._forecast_radiation_sum(c,station_id='ZUUU',event_date=event)==19
    radiation={'equal_system_centers_c':25,'unit_shift_sigma':unit_shift,'gate_shift_c':gate_shift,
               'served_distribution_sigma_c':sigma,'served_shift_c':served_shift,
               'future_vs_old_capture_same_signals':before==after,'nonzero_gate_shift_with_future_capture_c':before['synoptic_cooling_shift_c']}
    # Market ladder coarsening must not be mistaken for weather evidence.
    def taf_posterior(intervals):
        prior=P0._interval_probability([25.,27.,29.,31.],intervals)
        keys=list(intervals);centers={k:((a+b)/2 if a is not None and b is not None else (b-.5 if a is None else a+.5)) for k,(a,b) in intervals.items()}
        a={'schema':TL.SCHEMA,'gate':{'state':'SERVE'},'pool':{'a':0.,'b':1.,'tau':1.},'stations':{}}
        l,_=TL.compile_taf_likelihood(a,station_id='TEST',tx_c=31.,root_center_c=28.,bucket_centers_c=centers,buckets=prior)
        raw={k:prior[k]*l[k] for k in prior};total=sum(raw.values());raw={k:v/total for k,v in raw.items()}
        return raw,norm_product(prior,l)
    coarse={'low':[None,26.5],'mid':[26.5,27.5],'top':[27.5,None]}
    fine={'low':[None,26.5],'mid':[26.5,27.5],'28':[27.5,28.5],'29':[28.5,29.5],'30':[29.5,30.5],'top':[30.5,None]}
    (rawc,pc),(rawf,pf)=taf_posterior(coarse),taf_posterior(fine)
    topfine=sum(v for k,v in pf.items() if k not in ['low','mid'])
    assert abs(pc['top']-topfine)>1e-4
    table_factors=(((-.5,.5),1.0),((.5,1.5),1.1),((1.5,100.),1.0))
    bands=[]
    for eps in [-1e-9,1e-9]:
        p={'a':.45+eps,'b':.45-eps,'c':.1}
        q,receipt=CC.apply_band_reliability(p,factors=table_factors,ordinals={'a':0,'b':1,'c':2})
        bands.append({'probabilities':q,'anchor':receipt['anchor_bucket']})
    bandjump=max(abs(bands[0]['probabilities'][k]-bands[1]['probabilities'][k]) for k in ['a','b','c'])
    assert bandjump>.01
    result={'status':'READ_ONLY_IMPLEMENTATION_REVIEW','source_sha256':source_hashes(),'layer_states':dict(layer_states),
            'p1_boundary_probes':sorted(cases,key=lambda r:-r['max_probability_jump']),
            'p1_probe_count':checked,'p1_off_boundary_checks':stable,
            'radiation_scale_and_availability':radiation,
            'taf_open_tail_geometry_counterexample':{'coarse_p_y_ge_27_5':pc['top'],'fine_same_event_probability':topfine,
                'before_floor_coarse':rawc['top'],'before_floor_fine':sum(v for k,v in rawf.items() if k not in ['low','mid']),
                'parameters':'synthetic a=0,b=1,tau=1; unchanged continuous prior/TX; not measured production loss'},
            'band_argmax_counterexample':{'jump':bandjump,'factor_table':'synthetic 1.0/1.1/1.0; inspect snapshot for actual nonidentity activation'},
            'taf_measured_gate':taf['gate'],'taf_loaded_gate':TL.apply_gate_ruling(taf)['gate'],
            'production_writes':0,'no_new_effectiveness_claims':True}
    save('review_results.json',result)
    assert checked>=90 and stable>=45
    print('BOUNDARY_PROBES',checked,'TOP',result['p1_boundary_probes'][:3])
    print('RADIATION',radiation);print('LAYER_STATES',dict(layer_states));print('OTHER',result['taf_open_tail_geometry_counterexample'],bandjump)


if __name__=='__main__':main()
