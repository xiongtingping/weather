from guard import *
import guard as limits
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from collections import Counter
import gzip
from bayes_basket_target.materialization import rehydrate_lineage_documents
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from bayes_basket_target.station_registry import _COORDINATES

def dec(x):return json.loads(x) if isinstance(x,str) else x
asof=datetime.now(timezone.utc).isoformat().replace('+00:00','Z');c=connect();c.row_factory=sqlite3.Row
meta={'asof_utc':asof,'source':'production-stored basket_probability_envelope','no_refitting':True,'stations':sorted(_COORDINATES),'selected_checkpoints':['earliest_P0','last_before_36h_to_settlement','last_before_local_day_start'],'rows':{},'skips':{},'code_sha':{},'domains':{}}
for name in ['native_ensemble_p0','cross_consensus','center_width_calibration','served_shape','materialization']:
 p=ROOT/'bayes_basket_target'/(name+'.py');meta['code_sha'][str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
for table,cols in [('basket_probability_envelope',['stage','state','station_id']),('calibration_station_daily_high_truth',['source_lane','authority_scope','qc_status'])]:
 meta['domains'][table]={k:[r[0] for r in c.execute('select distinct '+k+' from '+table+' where station_id=?',('CYYZ',))] for k in cols}
meta['bindings']={r['station_id']:r['settlement_timezone'] for r in c.execute('select distinct station_id,settlement_timezone from calibration_station_market_timezone_binding')}
c.close()
if os.environ.get('CAPTURE_STATION'):
 meta=json.loads((OUT/'manifest.json').read_text());asof=meta['asof_utc']
for station in meta['stations']:
 if os.environ.get('CAPTURE_STATION') and station!=os.environ['CAPTURE_STATION']:continue
 limits.DEADLINE=time.monotonic()+100;c=connect();c.row_factory=sqlite3.Row;skips=Counter();out=[];seen=set();tz=ZoneInfo(meta['bindings'][station])
 truths={}
 for rr in c.execute('SELECT truth_id,station_id,local_date,official_tmax_c,revision_no,source_lane,canonical_committed_at_utc,settlement_timezone,authority_scope FROM calibration_station_daily_high_truth WHERE station_id=? AND local_date>=? AND local_date<=? AND qc_status=\'ACCEPTED\' AND source_lane IN '+FORMAL_TRUTH_LANES_SQL+' AND authority_scope IN (\'FORMAL_NATIVE_ELIGIBLE\',\'NO_LIVE_REPLAY_CANDIDATE\') ORDER BY local_date,revision_no', (station,'2026-09-15','2026-09-30')):
  t=dict(rr);end=datetime.fromisoformat(t['local_date']).replace(tzinfo=tz)+timedelta(days=1)
  if t['settlement_timezone']!=str(tz) or not t['canonical_committed_at_utc'] or datetime.fromisoformat(t['canonical_committed_at_utc'].replace('Z','+00:00'))<end or end>datetime.fromisoformat(asof.replace('Z','+00:00')):continue
  if t['canonical_committed_at_utc']>asof:continue
  truths[t['local_date']]=t
 for day,t in truths.items():
  local_start=datetime.fromisoformat(day).replace(tzinfo=tz);start=local_start.astimezone(timezone.utc);end=(local_start+timedelta(days=1)).astimezone(timezone.utc)
  for label,upper,order in [('earliest_P0',start,'ASC'),('last_before_36h_to_settlement',end-timedelta(hours=36),'DESC'),('last_before_local_day_start',start,'DESC')]:
   stamp=upper.isoformat().replace('+00:00','Z')
   sql="SELECT * FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P0' AND decision_at_utc<? AND created_at<? ORDER BY decision_at_utc "+order+",created_at "+order+" LIMIT 1"
   r=c.execute(sql,(station,day,stamp,stamp)).fetchone()
   if not r:skips['no_checkpoint']+=1;continue
   if r['envelope_id'] in seen:continue
   seen.add(r['envelope_id']);line=rehydrate_lineage_documents(c,dec(r['lineage_json']))
   required=['native_system_probabilities_json','system_weights_json','native_calibrated_system_centers_c_json','bucket_intervals_c_json','center_width_json']
   if any(not line.get(k) for k in required):skips['missing_production_components_or_width_receipt']+=1;continue
   cw=dec(line['center_width_json'])
   if not cw.get('input_probability'):skips['missing_recorded_width_input']+=1;continue
   fields=['native_system_probabilities_json','system_weights_json','native_calibrated_system_centers_c_json','bucket_intervals_c_json','center_width_json','consensus_bridge_json','synoptic_cooling_json','probability_floor_json','band_reliability_json','neighbour_direction_json','fusion_blend_json','system_artifact_shas_json','system_activation_shas_json','run_init_utc_by_system_json','archive_query_at_utc_by_system_json','hour_lead_bucket_by_system_json','system_family_ids_json']
   record={k:r[k] for k in ['envelope_id','checkpoint_id','station_id','event_date','decision_at_utc','created_at','posterior_version','content_sha256','settlement_timezone']}
   record.update(selection=label,truth=t,probabilities=dec(r['probabilities_json']),ordinals=dec(r['bucket_ordinals_json']),lineage={k:dec(line[k]) for k in fields if k in line},hours_to_settlement=(end-datetime.fromisoformat(r['decision_at_utc'].replace('Z','+00:00'))).total_seconds()/3600)
   out.append(record)
 with gzip.open(OUT/(station+'.jsonl.gz'),'wt') as f:
  for r in out:f.write(json.dumps(r,separators=(',',':'))+'\n')
 c.close();meta['rows'][station]=len(out);meta['skips'][station]=dict(skips);save('manifest.json',meta);print(station,len(out),dict(skips),flush=True)
print('DONE',sum(meta['rows'].values()))
