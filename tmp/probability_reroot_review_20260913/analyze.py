from common import *
from collections import Counter,defaultdict
from zoneinfo import ZoneInfo
import numpy as np
manifest=json.loads((OUT/'manifest.json').read_text());assert not manifest['failures']
rows=[json.loads(x) for x in (OUT/'envelopes.jsonl').open()]
byid={r['checkpoint_id']:r for r in rows};scopes=defaultdict(list)
obs=json.loads((OUT/'observations.json').read_text());obsid={r['observation_id']:r for r in obs}
tafs=json.loads((OUT/'taf.json').read_text());tafbyid={r['revision_id']:r for r in tafs}
def t(v):return datetime.datetime.fromisoformat(v.replace('Z','+00:00'))
def j(v):return json.loads(v) if v else {}
def stats(v):return {} if not v else dict(n=len(v),min=float(min(v)),median=float(np.median(v)),p90=float(np.quantile(v,.9)),max=float(max(v)))
def hours(a,b):return (t(a)-t(b)).total_seconds()/3600
def ident(r):return {k:r[k] for k in ['station_id','event_date','stage','checkpoint_id','decision_at_utc','fresh_until_utc']}
for r in rows:scopes[r['station_id'],r['event_date']].append(r)
counts=Counter((r['event_date'],r['stage']) for r in rows)
p0trans=[];p0m=[];p1=[];samples=[];lags=[]
for scope,items in scopes.items():
    items.sort(key=lambda r:(t(r['decision_at_utc']),r['checkpoint_id']))
    roots=[r for r in items if r['stage']=='P0']
    for left,right in zip(roots,roots[1:]):
        a,b=left['lineage'],right['lineage']
        same_run=j(a.get('run_init_utc_by_system_json'))==j(b.get('run_init_utc_by_system_json'))
        rec=dict(**ident(right),same_run=same_run,same_prob=left['probabilities_json']==right['probabilities_json'],expiry_extension_hours=hours(right['fresh_until_utc'],left['fresh_until_utc']),curve_changed=a.get('conditional_future_temperature_curve_sha256')!=b.get('conditional_future_temperature_curve_sha256'),weights_changed=a.get('system_weights_json')!=b.get('system_weights_json'),artifact_changed=a.get('system_artifact_shas_json')!=b.get('system_artifact_shas_json'),previous_checkpoint=left['checkpoint_id'])
        p0trans.append(rec)
    for r in items:
        l=r['lineage'];root=byid.get(l.get('p0_root_checkpoint_id'))
        if r['stage']=='P0-M':
            taf=tafbyid.get(l.get('forecast_revision_id'))
            p0m.append(dict(**ident(r),parent_found=bool(root),parent=root['checkpoint_id'] if root else None,parent_expiry=root['fresh_until_utc'] if root else None,extends_parent_hours=hours(r['fresh_until_utc'],root['fresh_until_utc']) if root else None,decision_after_parent_expiry_hours=hours(r['decision_at_utc'],root['fresh_until_utc']) if root else None,taf_found=bool(taf),taf_expiry=taf['valid_until_utc'] if taf else None,decision_after_taf_expiry_hours=hours(r['decision_at_utc'],taf['valid_until_utc']) if taf else None,expiry_after_taf_hours=hours(r['fresh_until_utc'],taf['valid_until_utc']) if taf else None,mode=l.get('taf_revision_state')))
        if r['stage']=='P1':
            o=obsid.get(l.get('canonical_observation_event_id'));previous=byid.get(l.get('reaffirmation_of_checkpoint_id'))
            kind='reaffirmation' if l.get('reaffirmation_of_checkpoint_id') else 'state_refresh' if l.get('p1_state_refresh_of_checkpoint_id') else 'walk'
            p1.append(dict(**ident(r),kind=kind,parent_expired_hours=hours(r['decision_at_utc'],root['fresh_until_utc']) if root else None,root=root['checkpoint_id'] if root else None,closing_obs_age_hours=hours(r['decision_at_utc'],o['valid_at']) if o else None,closing_obs_id=l.get('canonical_observation_event_id'),through_id=l.get('reaffirmed_through_observation_id'),same_prob_as_previous=(previous['probabilities_json']==r['probabilities_json']) if previous else None,created_minus_decision_hours=hours(r['created_at'],r['decision_at_utc']),post_refresh_commit=t(r['decision_at_utc'])>=t('2026-09-12T16:58:45Z')))
    for root in roots:
        at=t(root['decision_at_utc']);nextroot=next((x for x in roots if t(x['decision_at_utc'])>at),None)
        priorp1=any(x['stage']=='P1' and t(x['decision_at_utc'])<at for x in items)
        child=next((x for x in items if x['stage']=='P1' and x['lineage'].get('p0_root_checkpoint_id')==root['checkpoint_id'] and t(x['decision_at_utc'])>=at),None)
        if priorp1:lags.append(dict(**ident(root),lag_minutes=(t(child['decision_at_utc'])-at).total_seconds()/60 if child else None,superseded_before_p1=bool(nextroot and (not child or t(child['decision_at_utc'])>t(nextroot['decision_at_utc'])))))
    start=datetime.datetime.fromisoformat(scope[1]).replace(tzinfo=ZoneInfo(items[0]['settlement_timezone']))
    for hour in [-12,-6,0,6,12,16,20,23]:
        at=(start+datetime.timedelta(hours=hour)).astimezone(datetime.timezone.utc)
        if at>t(manifest['cutoff']):continue
        visible=[x for x in items if t(x['decision_at_utc'])<=at]
        if not visible:continue
        head=max(visible,key=lambda x:({'P0':0,'P0-M':1,'P1':2}[x['stage']],t(x['decision_at_utc'])))
        currentp0=next((x for x in reversed(visible) if x['stage']=='P0'),None)
        old=byid.get(head['lineage'].get('p0_root_checkpoint_id')) if head['stage']!='P0' else head
        latestpm=next((x for x in reversed(visible) if x['stage']=='P0-M' and currentp0 and x['lineage'].get('p0_root_checkpoint_id')==currentp0['checkpoint_id']),None)
        expiry=t(head['fresh_until_utc'])+datetime.timedelta(hours=3 if head['stage']=='P1' else 0)
        samples.append(dict(station_id=scope[0],event_date=scope[1],local_hour=hour,sample_at_utc=at.isoformat(),head=head['checkpoint_id'],stage=head['stage'],servable=at<=expiry,root_exact=bool(currentp0 and old and currentp0['checkpoint_id']==old['checkpoint_id']),root_same_probs=bool(currentp0 and old and currentp0['probabilities_json']==old['probabilities_json']),p1_misses_latest_compatible_p0m=bool(head['stage']=='P1' and latestpm and head['lineage'].get('prediction_root_checkpoint_id')!=latestpm['checkpoint_id']),latestp0=currentp0['checkpoint_id'] if currentp0 else None,latestp0m=latestpm['checkpoint_id'] if latestpm else None))
post=[x for x in p1 if x['post_refresh_commit']]
out={'counts':{str(k):v for k,v in counts.items()},'scope_count':len(scopes),'closed_scopes':sum(x['closed_at_snapshot'] for x in manifest['completion']),
 'p0':{'transitions':len(p0trans),'same_run':sum(x['same_run'] for x in p0trans),'same_run_extends_expiry':sum(x['same_run'] and x['expiry_extension_hours']>1/3600 for x in p0trans),'same_prob':sum(x['same_prob'] for x in p0trans),'same_prob_different_curve':sum(x['same_prob'] and x['curve_changed'] for x in p0trans),'same_run_expiry_extensions_hours':stats([x['expiry_extension_hours'] for x in p0trans if x['same_run'] and x['expiry_extension_hours']>1/3600])},
 'p0m':{'rows':len(p0m),'missing_parent':sum(not x['parent_found'] for x in p0m),'extends_parent':sum(x['extends_parent_hours'] is not None and x['extends_parent_hours']>1/3600 for x in p0m),'extensions_hours':stats([x['extends_parent_hours'] for x in p0m if x['extends_parent_hours'] is not None and x['extends_parent_hours']>1/3600]),'decision_after_parent_expiry':sum(x['decision_after_parent_expiry_hours'] is not None and x['decision_after_parent_expiry_hours']>=0 for x in p0m),'decision_after_taf_expiry':sum(x['decision_after_taf_expiry_hours'] is not None and x['decision_after_taf_expiry_hours']>=0 for x in p0m),'expiry_after_taf':sum(x['expiry_after_taf_hours'] is not None and x['expiry_after_taf_hours']>0 for x in p0m),'missing_taf':sum(not x['taf_found'] for x in p0m)},
 'p1':{'kinds':dict(Counter(x['kind'] for x in p1)),'post_refresh_commit_kinds':dict(Counter(x['kind'] for x in post)),'post_refresh_stations':len(set(x['station_id'] for x in post)),'post_refresh_reaffirm_close_age_hours':stats([x['closing_obs_age_hours'] for x in post if x['kind']=='reaffirmation' and x['closing_obs_age_hours'] is not None]),'reaffirmation_probs_equal':sum(x['kind']=='reaffirmation' and x['same_prob_as_previous'] is True for x in p1),'parent_expired_at_p1_decision':sum(x['parent_expired_hours'] is not None and x['parent_expired_hours']>=0 for x in p1),'creation_delays_hours':stats([x['created_minus_decision_hours'] for x in p1])},
 'samples':{'rows':len(samples),'higher_stage':sum(x['stage']!='P0' for x in samples),'mismatched_p0_exact':sum(x['stage']!='P0' and not x['root_exact'] for x in samples),'mismatched_p0_probabilities':sum(x['stage']!='P0' and not x['root_same_probs'] for x in samples),'p1_misses_latest_p0m':sum(x['p1_misses_latest_compatible_p0m'] for x in samples),'unservable':sum(not x['servable'] for x in samples)},
 'rebase_lag':{'roots_after_p1':len(lags),'with_p1':sum(x['lag_minutes'] is not None for x in lags),'superseded_before_p1':sum(x['superseded_before_p1'] for x in lags),'minutes':stats([x['lag_minutes'] for x in lags if x['lag_minutes'] is not None])}}
for name,obj in [('analysis.json',out),('p0_transitions.json',p0trans),('p0m_details.json',p0m),('p1_details.json',p1),('samples.json',samples),('rebase_lags.json',lags)]:save(name,obj)
print(json.dumps(out,ensure_ascii=False,indent=2))
