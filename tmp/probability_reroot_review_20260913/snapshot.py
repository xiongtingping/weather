from common import *
from collections import Counter
from zoneinfo import ZoneInfo
c=connect(); a=connect('archive.db')
stations=json.loads((OUT/'stations.json').read_text())
scope_ids={s['station_id'] for s in json.loads((ROOT/'tmp/p1_diagnosis_20260912/stations.json').read_text())}
stations=[s for s in stations if s['station_id'] in scope_ids]
assert len(stations)==45
cutoff=now().replace('+00:00','Z'); dates=['2026-09-11','2026-09-12']
cols='checkpoint_id,station_id,event_date,settlement_timezone,stage,decision_at_utc,fresh_until_utc,created_at,posterior_version,posterior_sha256,bridge_sha256,content_sha256,probabilities_json,bucket_ordinals_json,lineage_json'
sql='select '+cols+" from basket_probability_envelope where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc<=? order by decision_at_utc,checkpoint_id"
obs_sql='select observation_id,source_lane,report_type,valid_at,canonical_committed_at,reported_temperature_c,correction_suspected from reported_trajectory_observations where station_id=? and valid_at>=? and valid_at<? and canonical_committed_at<=? order by valid_at,canonical_committed_at'
keys='p0_root_checkpoint_id p0_root_content_sha256 prediction_root_checkpoint_id prediction_root_content_sha256 prediction_root_stage predecessor_checkpoint_id forecast_revision_id forecast_source_object_sha256 taf_revision_state taf_likelihood_mode taf_envelope_witness_json run_init_utc_by_system_json archive_query_at_utc_by_system_json conditional_future_temperature_curve_sha256 native_forward_snapshot_sha256 native_forward_source_archive_set_sha256 native_cross_ensemble_artifact_sha256 system_artifact_shas_json system_weights_json canonical_observation_event_id canonical_observation_ids canonical_observation_committed_at reaffirmation_of_checkpoint_id reaffirmed_through_observation_id p1_state_refresh_of_checkpoint_id reported_high_advanced reported_high_c model_center_c metar_trajectory_latest_formal_temperature_c metar_trajectory_artifact_sha256 metar_trajectory_degradation_reasons p1_four_layer_serving_json'.split()
counts=Counter(); domains=[]; failures=[]; total_bytes=0; observations=[]; taf=[]; completion=[]
schema_taf=query(c,'pragma table_info(calibration_forward_taf_capture)',limit=100)
save('taf_schema.json',schema_taf)
taf_sql='select * from calibration_forward_taf_capture where station_id=? and event_date=?'
with (OUT/'envelopes.jsonl').open('w') as f:
  for s in stations:
    sid=s['station_id'];zone=ZoneInfo(s['settlement_timezone'])
    for day in dates:
      try:
        domains+=query(c,"select distinct stage,state,posterior_version from basket_probability_envelope where station_id=? and event_date=? and state='COMMITTED'",(sid,day),limit=30)
        rows=query(c,sql,(sid,day,cutoff),limit=3000)
        for r in rows:
            l=json.loads(r.pop('lineage_json'));r['lineage']={k:l[k] for k in keys if k in l}
            prefix=r['lineage'].pop('canonical_observation_ids','')
            r['lineage']['canonical_observation_count']=len([x for x in str(prefix).split(',') if x])
            r['lineage']['canonical_observation_ids_sha256']=hashlib.sha256(str(prefix).encode()).hexdigest()
            for k in ['probabilities_json','bucket_ordinals_json']:r[k]=json.loads(r[k])
            raw=json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n';total_bytes+=len(raw.encode())
            if total_bytes>40*1024**2:raise RuntimeError('TOTAL_OUTPUT_CAP')
            f.write(raw);counts[r['stage']]+=1
        start=datetime.datetime.fromisoformat(day).replace(tzinfo=zone);end=start+datetime.timedelta(days=1)
        def stamp(d):return d.astimezone(datetime.timezone.utc).isoformat().replace('+00:00','Z')
        observations.extend(dict(x,station_id=sid,event_date=day) for x in query(a,obs_sql,(sid,stamp(start),stamp(end),cutoff),limit=2000))
        taf+=query(c,taf_sql,(sid,day),limit=100)
        completion.append(dict(station_id=sid,event_date=day,closed_at_snapshot=stamp(end)<=cutoff,envelopes=len(rows)))
      except Exception as e:failures.append(dict(station_id=sid,event_date=day,error=str(e)))
    print(sid,dict(counts),flush=True)
save('observations.json',observations);save('taf.json',taf);save('domains.json',domains)
save('manifest.json',{'cutoff':cutoff,'dates':dates,'stations':stations,'completion':completion,'counts':counts,'observations':len(observations),'taf':len(taf),'failures':failures,'queries':{'envelopes':sql,'observations':obs_sql,'taf':taf_sql},'resource_limits':LIMITS,'total_envelope_bytes':total_bytes,'purpose':'operational_lifecycle_audit_not_accuracy_or_strict_OOS'})
print('DONE',counts,'failures',failures)
