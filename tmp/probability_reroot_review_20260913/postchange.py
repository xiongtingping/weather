from common import *
from collections import Counter
c=connect(); manifest=json.loads((OUT/'manifest.json').read_text());stations=manifest['stations']
start='2026-09-13T07:01:21.391628Z';cutoff=now().replace('+00:00','Z')
fields=['p0_root_checkpoint_id','prediction_root_checkpoint_id','forecast_revision_id','reaffirmation_of_checkpoint_id','p1_state_refresh_of_checkpoint_id','system_weights_json','run_init_utc_by_system_json','archive_query_at_utc_by_system_json']
cols='checkpoint_id,station_id,event_date,stage,decision_at_utc,fresh_until_utc,posterior_version,'+','.join("json_extract(lineage_json,'$."+k+"') as "+k for k in fields)
sql='select '+cols+" from basket_probability_envelope where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc>=? and decision_at_utc<=? order by decision_at_utc"
rows=[];heads=[];errors=[]
for s in stations:
 for day in ['2026-09-13','2026-09-14']:
  try:
   recent=query(c,sql,(s['station_id'],day,start,cutoff),limit=1200)
   rows+=recent
   for stage in ['P0','P0-M','P1']:
    heads+=query(c,'select '+cols+" from basket_probability_envelope where station_id=? and event_date=? and state='COMMITTED' and stage=? and decision_at_utc<=? order by decision_at_utc desc limit 1",(s['station_id'],day,stage,cutoff),limit=1)
  except Exception as e:errors.append({'scope':[s['station_id'],day],'error':str(e)})
parents={};tafs={}
for r in rows+heads:
 rid=r['p0_root_checkpoint_id']
 if rid and rid not in parents:
  got=query(c,'select '+cols+' from basket_probability_envelope where checkpoint_id=?',(rid,),limit=1)
  parents[rid]=got[0] if got else None
 tid=r['forecast_revision_id']
 if tid and tid not in tafs:
  got=query(c,'select revision_id,issued_at_utc,received_at_utc,valid_until_utc from calibration_forward_taf_capture where revision_id=?',(tid,),limit=2)
  tafs[tid]=got[0] if got else None
def t(s):return datetime.datetime.fromisoformat(s.replace('Z','+00:00'))
pm=[r for r in rows if r['stage']=='P0-M'];p0=[r for r in rows if r['stage']=='P0'];p1=[r for r in rows if r['stage']=='P1']
renew_bad=[]
for r in p0:
 weights=json.loads(r['system_weights_json'] or '{}');receipts=json.loads(r['archive_query_at_utc_by_system_json'] or '{}')
 selected=[t(receipts[k]) for k,v in weights.items() if v>1e-12 and k in receipts]
 if selected and t(r['fresh_until_utc'])>max(selected)+datetime.timedelta(hours=6,microseconds=1):renew_bad.append(r)
result={'start':start,'cutoff':cutoff,'counts':dict(Counter(r['stage'] for r in rows)),'stations_with_new_p0':len({r['station_id'] for r in p0}),'stations_with_new_p0m':len({r['station_id'] for r in pm}),'stations_with_new_p1':len({r['station_id'] for r in p1}),'p0_later_than_seated_clock':len(renew_bad),'p0m_missing_parent':sum(parents.get(r['p0_root_checkpoint_id']) is None for r in pm),'p0m_extends_parent':sum(t(r['fresh_until_utc'])>t(parents[r['p0_root_checkpoint_id']]['fresh_until_utc']) for r in pm if parents.get(r['p0_root_checkpoint_id'])),'p0m_extends_taf':sum(t(r['fresh_until_utc'])>t(tafs[r['forecast_revision_id']]['valid_until_utc']) for r in pm if tafs.get(r['forecast_revision_id'])),'p1_reaffirmations':sum(bool(r['reaffirmation_of_checkpoint_id']) for r in p1),'p1_refreshes':sum(bool(r['p1_state_refresh_of_checkpoint_id']) for r in p1),'errors':errors}
save('postchange_rows.json',rows);save('postchange_heads.json',heads);save('postchange_parents.json',parents);save('postchange_tafs.json',tafs);save('postchange_summary.json',result)
save('postchange_queries.json',{'recent':sql,'range':[start,cutoff],'scope_stations':stations,'dates':['2026-09-13','2026-09-14']})
print(json.dumps(result,ensure_ascii=False,indent=2))
