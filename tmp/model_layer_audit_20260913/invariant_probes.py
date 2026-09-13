"""Bounded, isolated counterexamples. Assertions confirm defects, not expected correctness."""
import ast
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bayes_basket_target.center_width_calibration import apply_center_width_calibration
from bayes_basket_target.cross_consensus import apply_band_reliability, _centre_distances
from bayes_basket_target.band_reliability_store import persist_and_activate_band_table, load_band_table, retire_band_table
from bayes_basket_target.native_ensemble_oos import _select_minimum_crps_subset, _probabilities, _apply_parameters
from bayes_basket_target.native_ensemble_p0 import _calibrated_system_probability, _interval_probability
from bayes_basket_target import band_reliability_store as band_store

HERE=Path(__file__).resolve().parent
saved=json.loads((HERE/'database_evidence.json').read_text())
band_row=next(r for r in saved['band_tables'] if r['stage']=='P0')
factors=[(tuple(a),b) for a,b in json.loads(band_row['factors_json'])]
keys=[str(i) for i in range(7)];ordinals={k:i for i,k in enumerate(keys)}
p=dict(zip(keys,[.03,.09,.18,.28,.22,.13,.07]))
def cw(v):
    return apply_center_width_calibration(v,delta_c=0.,width_lambda=.75,ordered_keys=keys,key_centers_c=list(range(7)))[0]
def band(v):return apply_band_reliability(v,factors=factors,ordinals=ordinals)[0]
gated=cw(band(p));served=band(cw(p))
gap=max(abs(gated[k]-served[k]) for k in keys)
assert gap>1e-3
out={'order_counterexample':{'input':p,'gated_band_then_width':gated,'served_width_then_band':served,'max_probability_difference':gap,'band_sha':band_row['band_table_sha256']}}

# Actual active factors admit a neighbour to overtake the peak.
p2=dict(zip(keys,[.02,.04,.48,.46,0.,0.,0.]))
b2=band(p2)
before=max(p2,key=p2.get);after=max(b2,key=b2.get)
assert before!=after
out['peak_invariance_counterexample']={'input':p2,'output':b2,'peak_before':before,'peak_after':after,'distances_before':_centre_distances(p2,ordinals),'distances_after':_centre_distances(b2,ordinals),'band_sha':band_row['band_table_sha256']}

# A held-out outcome alone changes selected membership: selection learns from that label.
probabilities={'A':_probabilities([19.8,20.,20.2]),'B':_probabilities([21.8,22.,22.2]),'C':_probabilities([23.8,24.,24.2])}
def pick(truth):
    prediction={s:{'2026-08-01':{'probability':probabilities[s],'calibrated_center_c':20.+2*i,'actual_tmax_c':truth}} for i,s in enumerate(probabilities)}
    weights,receipt=_select_minimum_crps_subset(ready_system_ids=list(probabilities),predictions_by_system=prediction,common_holdout=['2026-08-01'],family_by_system={s:s for s in probabilities},configured_caps={s:.75 for s in probabilities},train_error_scale={s:1. for s in probabilities},train_n_by_system={s:28 for s in probabilities},band_factors=())
    return {'weights':weights,'receipt':receipt}
lo=pick(20.);hi=pick(24.)
assert set(lo['weights'])!=set(hi['weights'])
out['holdout_selection_counterexample']={'only_truth_changed':[20.,24.],'low':lo,'high':hi}

# Pinned versions accept a cross-stage table and replace retired history regardless of decision time.
c=sqlite3.connect(':memory:')
a=persist_and_activate_band_table(c,stage='P1',factors=factors,evidence={},serving_day_count=20,axis='centre_distance')
sha=a['band_table_sha256'];wrong_stage=load_band_table(c,stage='P0',table_sha256=sha)
assert wrong_stage and wrong_stage[1]==sha
retire_band_table(c,stage='P1')
retired=load_band_table(c,stage='P1',table_sha256=sha)
assert retired[0]==()
out['pinned_table_contract_counterexamples']={'p1_sha_loadable_as_p0':sha,'before_retirement_factor_count':len(wrong_stage[0]),'after_retirement_factors':retired[0],'loader_has_no_as_of_parameter':True}

# Width scaling on a finite bucket grid destroys strictly positive Gaussian support.
long_keys=[str(i) for i in range(15)]
long_axis={k:[None if int(k)==0 else int(k)-.5,None if int(k)==14 else int(k)+.5] for k in long_keys}
base_params={'bias_c':0.,'spread_factor':1.,'reliability_weight':1.,'climate_samples_c':[4.,7.,10.]}
members=[-1.,3.,7.,11.,15.]
smooth=_calibrated_system_probability(values=members,intervals=long_axis,bias_parameters=base_params,spread_parameters=base_params,reliability_parameters=base_params)
shrunk,_=apply_center_width_calibration(smooth,delta_c=0.,width_lambda=.75,ordered_keys=long_keys,key_centers_c=[-.5]+list(range(1,14))+[14.5])
removed={k:v for k,v in smooth.items() if v>0. and shrunk[k]==0.}
assert removed
continuous=_interval_probability([7.+.75*(v-7.) for v in members],long_axis,bandwidth_c=.375)
out['support_truncation_counterexample']={'members':members,'all_input_buckets_positive':all(v>0 for v in smooth.values()),'no_hard_exclusions_supplied':True,'artificial_zero_buckets_original_mass':removed,'input':smooth,'output':shrunk,'continuous_affine_then_bucket_control':continuous}

# Observe the real fitter passing its validation days into factor estimation.
days={f'2026-08-{i:02d}': [[(.3,0),(.5,1),(.2,0)] for _ in range(40)] for i in range(1,21)}
station_days={d:[f'S{i:02d}' for i in range(40)] for d in days}
fit_days=[];scored_days=[]
real_report=band_store.band_lane_report;real_forward=band_store._forward_nll_delta
def report_spy(data,**kw):
    fit_days.extend(sorted(data));return real_report(data,**kw)
def forward_spy(data,**kw):
    ordered=sorted(data);cut=max(1,int(len(ordered)*(1-band_store.FORWARD_WINDOW_FRACTION)))
    scored_days.extend(ordered[cut:] or ordered[-1:]);return real_forward(data,**kw)
with patch.object(band_store,'_truth',return_value={}),patch.object(band_store,'serving_lane_by_day',return_value=(days,station_days)),patch.object(band_store,'load_band_table_with_axis',return_value=None),patch.object(band_store,'_first_decision_by_station_day',return_value={}),patch.object(band_store,'_active_band_activated_at',return_value=None),patch.object(band_store,'band_lane_report',side_effect=report_spy),patch.object(band_store,'_forward_nll_delta',side_effect=forward_spy):
    fitted=band_store.fit_band_reliability_table(sqlite3.connect(':memory:'),stage='P1',bootstrap_samples=30,axis='centre_distance',min_decision_at_utc='')
overlap=sorted(set(fit_days)&set(scored_days))
assert len(overlap)==8
out['band_forward_training_overlap']={'factor_fit_days':sorted(set(fit_days)),'claimed_forward_days':sorted(set(scored_days)),'overlap_count':len(overlap),'factors':fitted['factors'],'basis':fitted['evidence']['basis']}

# Control: single-system calibration is identical on the same exhaustive C grid,
# across constant/tercile bias and spread extrema. Broad tails remove gate truncation.
axis={str(i):[None if i==-20 else i-.5,None if i==60 else i+.5] for i in range(-20,61)}
controls=[]
for spread in [.5,1.,3.,5.]:
 for bias in [-2.,0.,2.]:
    params={'bias_c':bias,'correction_form':'constant','spread_factor':spread,'reliability_weight':1.,'climate_samples_c':[18.,20.,22.], 'train_residual_sigma_c':1.}
    values=[18.,19.5,20.,20.5,22.]
    gp=_apply_parameters(values,params)
    sp=_calibrated_system_probability(values=values,intervals=axis,bias_parameters=params,spread_parameters=params,reliability_parameters=params)
    err=max(abs(gp.get(k,0)-sp.get(k,0)) for k in set(gp)|set(sp))
    assert err<1e-4
    controls.append({'bias':bias,'spread':spread,'max_difference':err})
out['same_grid_single_system_controls']=controls
tercile_controls=[]
for center in [18.,20.,22.]:
 for spread in [.5,1.,5.]:
    params={'bias_c':0.,'correction_form':'level_terciles_v1','bias_bin_cuts_c':[19.,21.],'bias_bin_shifts_c':[-2.,.5,2.],'spread_factor':spread,'reliability_weight':1.,'climate_samples_c':[18.,20.,22.]}
    values=[center+x for x in [-1.,-.5,0.,.5,1.]]
    gp=_apply_parameters(values,params);sp=_calibrated_system_probability(values=values,intervals=axis,bias_parameters=params,spread_parameters=params,reliability_parameters=params)
    err=max(abs(gp.get(k,0)-sp.get(k,0)) for k in set(gp)|set(sp));assert err<1e-4
    tercile_controls.append({'center':center,'spread':spread,'max_difference':err})
out['tercile_single_system_controls']=tercile_controls

# The OOS shape function can be inspected independently of imports and live data.
source=(HERE.parents[1]/'bayes_basket_target/native_ensemble_oos.py').read_text()
tree=ast.parse(source)
shape=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_shaped')
calls=[(n.lineno,n.func.id) for n in ast.walk(shape) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in ('apply_band_reliability','apply_center_width_calibration')]
out['oos_shape_source_calls']=sorted(calls)
(HERE/'invariant_evidence.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps(out,ensure_ascii=False,indent=2))
