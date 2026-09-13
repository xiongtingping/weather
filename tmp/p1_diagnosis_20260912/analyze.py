from common import *
import sys, math, statistics
from collections import Counter,defaultdict
from zoneinfo import ZoneInfo
sys.path.insert(0,str(ROOT))
from bayes_basket_target.metar_path_state import compile_conditional_metar_path_state
from bayes_basket_target.metar_four_layer import phase_rise_probability,apply_phase_split
from bayes_basket_target.materialization import _bucket_centers_c

def dt(s):return datetime.datetime.fromisoformat(s.replace('Z','+00:00'))
def stats(xs):
    if not xs:return {'n':0}
    import numpy as np
    return {'n':len(xs),'mean':float(np.mean(xs)),'p50':float(np.quantile(xs,.5)),'p90':float(np.quantile(xs,.9)),'p95':float(np.quantile(xs,.95)),'max':max(xs)}
def obj(s):return json.loads(s) if isinstance(s,str) and s.startswith('{') else {}
envelopes=json.loads((OUT/'envelopes.json').read_text()); observations=json.loads((OUT/'observations.json').read_text());truths=json.loads((OUT/'truths.json').read_text())
obs_by=defaultdict(list);obs_id={}
for r in observations:
    obs_by[(r['station_id'],r['event_date'])].append(r);obs_id[r['observation_id']]=r
truth_by=defaultdict(list)
for r in truths:truth_by[(r['station_id'],r['local_date'])].append(r)
artifact=json.loads((OUT/'artifacts_snapshot.json').read_text())['metar_phase_hazard_artifact.json']
rows=[];experiments=[];failures=[]
for r in envelopes:
    key=(r['station_id'],r['event_date']);l=r['lineage_json'];f=obj(l.get('p1_shadow_four_layer_json','')).get('features',{});served=obj(l.get('p1_four_layer_serving_json',''))
    sample=dt(r['sample_at_utc']);decision=dt(r['decision_at_utc']);end=datetime.datetime.fromisoformat(r['event_date']).replace(tzinfo=ZoneInfo(r['settlement_timezone']))+datetime.timedelta(days=1)
    terminal=obs_id.get(l.get('canonical_observation_event_id'))
    eligible=[x for x in obs_by[key] if x['source_lane']=='awc' and not x['correction_suspected'] and x['reported_temperature_c'] is not None and dt(x['canonical_committed_at'])<=sample and dt(x['valid_at'])<=sample]
    collapsed={}
    for x in sorted(eligible,key=lambda x:(dt(x['valid_at']),dt(x['canonical_committed_at']))):collapsed[x['valid_at']]=x
    eligible=sorted(collapsed.values(),key=lambda x:dt(x['valid_at']))
    latest=eligible[-1] if eligible else None
    current_high=max((x['reported_temperature_c'] for x in eligible),default=None)
    current_gap=current_high-latest['reported_temperature_c'] if latest else None
    feature_clock=end-datetime.timedelta(hours=float(f['remaining_hours'])) if f else None
    row={'station':key[0],'date':key[1],'hour':r['sample_local_hour'],'envelope_id':r['envelope_id'],'checkpoint':r['checkpoint_id'],'sample_at':r['sample_at_utc'],'decision_at':r['decision_at_utc'],'created_at':r['created_at'],'physically_available':dt(r['created_at'])<=sample,'fresh_at_sample':dt(r['fresh_until_utc'])>=sample,'version':r['posterior_version'],'served_applied':served.get('applied'),'reaffirm':bool(l.get('reaffirmation_of_checkpoint_id')),'regional':served.get('regional_system_id'),'shape_missing':f.get('curve_shape_realized') is None,'features':f,'high':float(l['reported_high_c']),'closing_valid':terminal['valid_at'] if terminal else None,'closing_age_minutes':(sample-dt(terminal['valid_at'])).total_seconds()/60 if terminal else None,'clock_vs_closing_minutes':(feature_clock-dt(terminal['valid_at'])).total_seconds()/60 if feature_clock and terminal else None,'latest_available_valid':latest['valid_at'] if latest else None,'latest_temperature':latest['reported_temperature_c'] if latest else None,'actual_high':current_high,'actual_gap':current_gap,'latest_observation_age_minutes':(sample-dt(latest['valid_at'])).total_seconds()/60 if latest else None,'gap_mismatch':current_gap-f.get('drawdown_from_high_c',0) if latest and f else None}
    rows.append(row)
    try:
        if not f or served.get('applied')!='true' or served.get('phase',{}).get('state')!='APPLIED':continue
        if not row['physically_available']:continue
        # Isolate refresh without a new hard high, and exclude unresolved correction days.
        if any(x['correction_suspected'] for x in obs_by[key]):continue
        if latest is None or abs(current_high-row['high'])>0.051:continue
        candidates=truth_by[key]
        if not candidates:continue
        truth=max(candidates,key=lambda x:x['revision_no'])
        if truth['qc_status']!='ACCEPTED':continue
        truth_c=float(truth['official_tmax_c'])
        geometry=json.loads(l['bucket_intervals_c_json'])
        def bucket(v):
            result=[b for b,(lo,hi) in geometry.items() if (lo is None or v>=lo) and (hi is None or v<hi)]
            if len(result)!=1:raise ValueError('ambiguous geometry')
            return result[0]
        actual=bucket(truth_c);high_bucket=bucket(current_high)
        if actual not in r['probabilities_json']:continue
        ordered,centers=_bucket_centers_c(l,bucket_ordinals=r['bucket_ordinals_json']);center_map=dict(zip(ordered,centers))
        state=compile_conditional_metar_path_state([{'observation_id':x['observation_id'],'observed_at_utc':x['valid_at'],'temperature_c':x['reported_temperature_c'],'raw_text':x['raw_text']} for x in eligible],as_of_utc=r['sample_at_utc'],settlement_timezone=r['settlement_timezone'],root_probabilities=r['probabilities_json'],bucket_centers_c=center_map)
        if not state.complete:continue
        new_features={**f,'remaining_hours':(end-sample).total_seconds()/3600,'drawdown_from_high_c':state.drawdown_from_high_c,'momentum_c_per_hour':state.momentum_c_per_hour,'cloud_cover_fraction':state.cloud_cover_fraction,'precipitation_present':float(state.precipitation_present)}
        # Keep regional covariates fixed: isolate the phase's observation/clock update.
        p_new=phase_rise_probability(new_features,artifact)
        updated,reason=apply_phase_split(r['probabilities_json'],bucket_ordinals=r['bucket_ordinals_json'],running_high_bucket=high_bucket,rise_probability=p_new)
        if reason!='APPLIED':continue
        floor_note=obj(l.get('probability_floor_json',''));epsilon=float(floor_note.get('epsilon',.001))
        excluded=set(str(l.get('hard_excluded_buckets','')).split(','))
        def finish(p):
            result={b:v if b in excluded else max(v,epsilon) for b,v in p.items()}
            total=sum(result.values());return {b:v/total for b,v in result.items()}
        updated=finish(updated)
        old=r['probabilities_json']
        def score(p):return {'nll':-math.log(max(p[actual],1e-12)),'hit':int(max(p,key=p.get)==actual),'mae':abs(sum(center_map[b]*v for b,v in p.items())-truth_c),'brier':sum((v-int(b==actual))**2 for b,v in p.items()),'truth_p':p[actual]}
        other_arms={}
        for arm,features in [('clock_only',{**f,'remaining_hours':new_features['remaining_hours']}),('state_only',{**new_features,'remaining_hours':f['remaining_hours']}),('identity',f)]:
            arm_p,_=apply_phase_split(old,bucket_ordinals=r['bucket_ordinals_json'],running_high_bucket=high_bucket,rise_probability=phase_rise_probability(features,artifact))
            arm_p=finish(arm_p)
            other_arms[arm]=score(arm_p)
            if arm=='identity':identity_l1=sum(abs(arm_p[b]-old[b]) for b in old)
        exp={'station':key[0],'date':key[1],'hour':row['hour'],'old':score(old),'updated':score(updated),'other_arms':other_arms,'identity_l1':identity_l1,'actual':actual,'truth_c':truth_c,'high_bucket':high_bucket,'old_phase':served.get('phase',{}).get('rise_probability'),'new_phase':p_new,'old_features':f,'new_features':new_features,'fresh_at_sample':row['fresh_at_sample'],'forecast_available':row['physically_available']}
        experiments.append(exp)
    except Exception as e:failures.append({'key':key,'hour':r['sample_local_hour'],'error':str(e)})

def aggregate(exps):
    if not exps:return {'n':0}
    return {'n':len(exps),'station_days':len({(r['station'],r['date']) for r in exps}),'stations':len({r['station'] for r in exps}),'old':{k:statistics.mean(r['old'][k] for r in exps) for k in ['nll','hit','mae','brier']},'updated':{k:statistics.mean(r['updated'][k] for r in exps) for k in ['nll','hit','mae','brier']}}
quality={'n':len(rows),'unique_envelopes':len({r['envelope_id'] for r in rows}),'not_available_asof':sum(not r['physically_available'] for r in rows),'fresh_at_sample':sum(r['fresh_at_sample'] for r in rows),'reaffirm':sum(r['reaffirm'] for r in rows),'versions':dict(Counter(r['version'] for r in rows)),'four_layer':dict(Counter(str(r['served_applied']) for r in rows)),'region':dict(Counter(str(r['regional']) for r in rows)),'shape_missing':sum(r['shape_missing'] for r in rows),'zero_drawdown':sum(r['features'].get('drawdown_from_high_c')==0 for r in rows),'gap_mismatch_over_half_c':sum(r['gap_mismatch'] is not None and abs(r['gap_mismatch'])>.5 for r in rows),'closing_age_minutes':stats([r['closing_age_minutes'] for r in rows if r['closing_age_minutes'] is not None]),'clock_vs_closing_minutes':stats([r['clock_vs_closing_minutes'] for r in rows if r['clock_vs_closing_minutes'] is not None]),'latest_observation_age_minutes':stats([r['latest_observation_age_minutes'] for r in rows if r['latest_observation_age_minutes'] is not None])}
awc=[r for r in observations if r['source_lane']=='awc' and not r['correction_suspected']]
latency={name:stats([(dt(r[right])-dt(r[left])).total_seconds() for r in awc if r[left] and r[right]]) for name,left,right in [('valid_to_seen_seconds','valid_at','first_seen_at'),('request_seconds','fetch_started_at','fetch_completed_at'),('seen_to_commit_seconds','first_seen_at','canonical_committed_at')]}
latency['by_station_valid_to_seen']={s:stats([(dt(r['first_seen_at'])-dt(r['valid_at'])).total_seconds() for r in awc if r['station_id']==s]) for s in sorted({r['station_id'] for r in awc})}
summary={'quality':quality,'latency':latency,'experiment':{'all':aggregate(experiments),'fresh_only':aggregate([r for r in experiments if r['fresh_at_sample']]),'by_hour':{str(h):aggregate([r for r in experiments if r['hour']==h]) for h in [8,12,16,20,23]},'by_day':{d:aggregate([r for r in experiments if r['date']==d]) for d in ['2026-09-09','2026-09-10','2026-09-11']},'failures':failures,'caveat':'DIAGNOSTIC_PHASE_ONLY_COUNTERFACTUAL_FIXED_REGIONAL_INPUTS_NOT_STRICT_OOS_NO_HIGH_ADVANCE_NO_CORRECTIONS'},'examples':sorted(rows,key=lambda r:abs(r.get('gap_mismatch') or 0),reverse=True)[:8]}
save('features_audit.json',rows);save('phase_refresh_experiment.json',experiments);save('analysis.json',summary)
print(json.dumps({'quality':quality,'latency':{k:v for k,v in latency.items() if k!='by_station_valid_to_seen'},'experiment':summary['experiment']},ensure_ascii=False,indent=2))
