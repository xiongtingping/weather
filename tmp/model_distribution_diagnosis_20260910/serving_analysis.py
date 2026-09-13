import pathlib,sys,json,collections,math,sqlite3
import numpy as np
ROOT=pathlib.Path(__file__).resolve().parents[2];OUT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from bayes_basket_target.native_ensemble_p0 import _bucket_centers
from bayes_basket_target.center_width_calibration import apply_center_width_calibration,bucket_centers_from_intervals
from bayes_basket_target.center_width_store import _pre_band_claim,_cell_nll
from bayes_basket_target.cross_consensus import apply_band_reliability
x=json.loads((OUT/'serving_snapshot.json').read_text());rows=x['rows'];truth={}
for line in (OUT/'cases.jsonl').open():
    r=json.loads(line);key=(r['station'],r['event_date'])
    if key in truth:assert truth[key]==r['actual_tmax_c']
    truth[key]=r['actual_tmax_c']
for r in rows:
    intervals=r['intervals'];keys=sorted(intervals,key=lambda k:float('-inf') if intervals[k][0] is None else intervals[k][0])
    cs=_bucket_centers(intervals,keys);p=r['probabilities'];mu=sum(p[k]*v for k,v in zip(keys,cs))
    r['serving_axis_mean_c']=mu;r['serving_axis_sigma_c']=math.sqrt(sum(p[k]*(v-mu)**2 for k,v in zip(keys,cs)))
    r['truth_c']=truth.get((r['station'],r['day']))
by={(r['station'],r['day'],r['lead']):r for r in rows};pairs=[]
for (st,day,lead),r in by.items():
    s=by.get((st,day,'h00_12'))
    if lead!='h24_48' or not s:continue
    pairs.append({'station':st,'day':day,'long_h':r['entropy_nat'],'short_h':s['entropy_nat'],
      'long_sd':r['serving_axis_sigma_c'],'short_sd':s['serving_axis_sigma_c'],
      'long_center_error':None if r['truth_c'] is None else abs(r['model_center_c']-r['truth_c']),
      'short_center_error':None if s['truth_c'] is None else abs(s['model_center_c']-s['truth_c']),
      'same_systems':set(r['system_weights_json'])==set(s['system_weights_json']),
      'same_cw_version':(r['center_width_json']or{}).get('center_width_sha256')==(s['center_width_json']or{}).get('center_width_sha256'),
      'both_edge_mass_below_01':max(r['edge_mass'],s['edge_mass'])<.1})
pair_summaries=[]
for label,pp in [('all',pairs)]+[(day,[p for p in pairs if p['day']==day]) for day in x['dates']]+[
 ('same_systems_same_cw',[p for p in pairs if p['same_systems'] and p['same_cw_version']]),
 ('edge_mass_below_01',[p for p in pairs if p['both_edge_mass_below_01']])]:
    if not pp:continue
    lh=np.mean([p['long_h'] for p in pp]);sh=np.mean([p['short_h'] for p in pp]);ls=np.mean([p['long_sd'] for p in pp]);ss=np.mean([p['short_sd'] for p in pp])
    et=[p for p in pp if p['long_center_error'] is not None and p['short_center_error'] is not None]
    pair_summaries.append({'cohort':label,'pairs':len(pp),'stations':len({p['station'] for p in pp}),
      'long_mean_entropy_nat':lh,'short_mean_entropy_nat':sh,'entropy_relative_change':sh/lh-1,'exp_delta_entropy_change':math.exp(sh-lh)-1,
      'long_mean_bucket_sigma_c':ls,'short_mean_bucket_sigma_c':ss,'bucket_sigma_relative_change':ss/ls-1,
      'truth_pairs':len(et),'median_center_error_long_c':float(np.median([p['long_center_error'] for p in et])) if et else None,
      'median_center_error_short_c':float(np.median([p['short_center_error'] for p in et])) if et else None})
current=[r for r in rows if r['day']=='2026-09-08'];shas=set(s for r in current for s in (r['system_artifact_shas_json']or{}).values());art=[x['artifacts'][s] for s in shas if s in x['artifacts']]
artifact_summary={}
for system in sorted({r['system'] for r in art}):
    a=[r for r in art if r['system']==system]
    artifact_summary[system]={'n':len(a),'neff_min_med_max':list(np.quantile([r['reliability']['train_n_effective'] for r in a],[0,.5,1])),
       'spread_factor_min_med_max':list(np.quantile([r['spread']['spread_factor'] for r in a],[0,.5,1])),
       'fit_cases_min_med_max':list(np.quantile([r['reliability']['fit_case_count'] for r in a],[0,.5,1]))}
c=sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro',uri=True);c.execute('pragma query_only=on')
br=c.execute("select factors_json,axis,band_table_sha256 from calibration_band_reliability_table where stage='P0' and activated=1 limit 1").fetchone()
factors=json.loads(br[0]);checks=[];double=[]
for r in current:
    p=r['probabilities'];keys=sorted(r['intervals'],key=lambda k:float('-inf') if r['intervals'][k][0] is None else r['intervals'][k][0]);cs=_bucket_centers(r['intervals'],keys);fitcs=bucket_centers_from_intervals(r['intervals'],ordered_keys=keys)
    cw=r['center_width_json'] or {};lam=cw.get('width_lambda',1);ordinals={k:i for i,k in enumerate(keys)}
    a=apply_band_reliability(p,factors=factors,ordinals=ordinals)[0]
    a=apply_center_width_calibration(a,ordered_keys=keys,key_centers_c=cs,delta_c=0,width_lambda=.75)[0]
    b=apply_center_width_calibration(p,ordered_keys=keys,key_centers_c=cs,delta_c=0,width_lambda=.75)[0]
    b=apply_band_reliability(b,factors=factors,ordinals=ordinals)[0]
    # A controlled same-input order comparison; p is a real served seed,
    # not an assertion that p equals its own historical pre-shape input.
    checks.append({'station':r['station'],'lead':r['lead'],'total_variation_order_change':.5*sum(abs(a[k]-b[k]) for k in keys),
      'max_bucket_probability_change':max(abs(a[k]-b[k]) for k in keys),
      'edge_axis_mean_difference_c':r['bucket_mean_c']-r['serving_axis_mean_c']})
    actual=r['truth_c']
    if not cw or actual is None:continue
    preband=_pre_band_claim(p,{'band_reliability_json':json.dumps(r['band_reliability_json'])})
    win=next(k for k in keys if (r['intervals'][k][0] is None or actual>=r['intervals'][k][0]) and (r['intervals'][k][1] is None or actual<r['intervals'][k][1]))
    cell={'probabilities':preband,'ordered_keys':keys,'key_centers_c':fitcs,'excluded_buckets':[],'settled_bucket':win}
    identity=_cell_nll(cell,delta_c=0,width_lambda=1)
    twice=_cell_nll(cell,delta_c=cw.get('fitted_delta_c',0),width_lambda=cw.get('pooled_width_lambda',1))
    double.append({'station':r['station'],'lead':r['lead'],'identity_nll':identity,'incumbent_reapplied_nll':twice})
result={'matched_serving_pairs':pair_summaries,'current_sample_rows':len(current),'current_artifact_count':len(art),'current_artifacts':artifact_summary,
 'current_cw_composed_lambda_quantiles':list(np.quantile([(r['center_width_json']or{}).get('width_lambda',1) for r in current],[0,.1,.5,.9,1])),
 'current_station_scale_floor_count':sum((r['center_width_json']or{}).get('station_width_scale',1)==.5 for r in current),
 'same_input_order_checks':{'n':len(checks),'band_sha':br[2], 'tv_p50_p90_max':list(np.quantile([r['total_variation_order_change'] for r in checks],[.5,.9,1])),
  'max_bucket_prob_difference':max(r['max_bucket_probability_change'] for r in checks)},
 'incumbent_double_application':{'n':len(double),'identity_mean_nll':float(np.mean([r['identity_nll'] for r in double])),
  'reapplied_incumbent_mean_nll':float(np.mean([r['incumbent_reapplied_nll'] for r in double]))},
 'notes':['Four preselected dates, latest P0 per station/event_date/lead; not original attachment sample','Entropy exact on bucket distribution; sigma is finite-center proxy, open tails not identifiable','Order checks reuse served seeds and prove noncommutativity, not production improvement','Double-application check reproduces scorer input semantics, not a proposed strategy backtest']}
(OUT/'serving_analysis.json').write_text(json.dumps(result,indent=2));(OUT/'serving_pairs.json').write_text(json.dumps(pairs,indent=2))
print(json.dumps(result,indent=2))
