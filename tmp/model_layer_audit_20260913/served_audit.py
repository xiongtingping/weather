import collections
from datetime import datetime,timezone
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
saved=json.loads((HERE/'database_evidence.json').read_text())
stations=saved['station_ids']
c=sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=2)
c.row_factory=sqlite3.Row
c.execute('PRAGMA query_only=ON');c.execute('PRAGMA cache_size=-8192')
deadline=time.monotonic()+70
c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)

def parsed(d,k,default=None):
    v=d.get(k)
    return json.loads(v) if isinstance(v,str) else (v if v is not None else default)

cutoff=sys.argv[1] if len(sys.argv)>1 else datetime.now(timezone.utc).isoformat()
out={'at_utc':datetime.now(timezone.utc).isoformat(),'selector_cutoff_utc':cutoff,'station_count':len(stations),'rows':[]}
out['active_band_tables']=[dict(r) for r in c.execute('select * from calibration_band_reliability_table where activated=1')]
out['active_center_width_tables']=[dict(r) for r in c.execute('select * from calibration_center_width_table where activated=1')]
for station in stations:
    # Deliberately report latest committed P0, not a claim that every row is fresh/tradable.
    r=c.execute("select * from basket_probability_envelope where station_id=? and state='COMMITTED' and stage='P0' and decision_at_utc<=? and julianday(created_at)<=julianday(?) order by decision_at_utc desc,rowid desc limit 1",(station,cutoff,cutoff)).fetchone()
    if r is None: continue
    r=dict(r);ln=json.loads(r.pop('lineage_json'))
    keep=['envelope_id','station_id','event_date','decision_at_utc','fresh_until_utc','posterior_version','probabilities_json','bucket_ordinals_json']
    v={k:r[k] for k in keep}
    v['lineage']={k:ln[k] for k in ln if k in ('bucket_intervals_c_json','center_width_json','band_reliability_json','system_weights_json','ensemble_system_ids_json','system_artifact_shas_json','native_cross_ensemble_artifact_sha256','native_selection_evidence_sha256','provisional_uncalibrated_system_ids_json','provisional_parameter_sources_json','lead_bucket','training_lead_bucket','run_cycle','season_bin')}
    cross=c.execute('select * from calibration_cross_native_ensemble_artifact where content_sha256=?',(ln.get('native_cross_ensemble_artifact_sha256'),)).fetchone()
    if cross is not None:
        v['cross_artifact']=dict(cross)
        ev=c.execute('select * from calibration_cross_native_ensemble_oos_evaluation where station_id=? and season_bin=? and lead_bucket=? and run_cycle=? and selection_evidence_sha256=? order by evaluated_at_utc desc limit 1',tuple(cross[k] for k in ('station_id','season_bin','lead_bucket','run_cycle','selection_evidence_sha256'))).fetchone()
        if ev is not None: v['cross_evaluation']=dict(ev)
    v['systems']={}
    for system,sha in parsed(ln,'system_artifact_shas_json',{}).items():
        art=c.execute('select * from calibration_native_ensemble_system_artifact where content_sha256=?',(sha,)).fetchone()
        if art:
            art=dict(art);p=parsed(art,'reliability_parameters_json',{});p.pop('climate_samples_c',None);art['reliability_parameters_json']=json.dumps(p)
            v['systems'][system]=art
    out['rows'].append(v)
out['summary']={
 'latest_p0_station_count':len(out['rows']),
 'dates':dict(collections.Counter(r['event_date'] for r in out['rows'])),
 'band_axes':dict(collections.Counter(parsed(r['lineage'],'band_reliability_json',{}).get('axis','ABSENT') for r in out['rows'])),
 'center_width_receipt_count':sum('center_width_json' in r['lineage'] for r in out['rows']),
 'provisional_station_count':sum(bool(parsed(r['lineage'],'provisional_uncalibrated_system_ids_json',[])) for r in out['rows']),
 'cross_gate_profiles':dict(collections.Counter(r.get('cross_evaluation',{}).get('threshold_profile','MISSING') for r in out['rows'])),
 'systems':dict(collections.Counter(s for r in out['rows'] for s in r['systems'])),
 'cw_and_band_nonidentity':sum(bool(parsed(r['lineage'],'center_width_json',{})) and parsed(r['lineage'],'band_reliability_json',{}).get('state')=='APPLIED' for r in out['rows']),
}
(HERE/'served_evidence.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps(out['summary'],ensure_ascii=False,indent=2))
print('examples')
for r in out['rows']:
    ln=r['lineage'];cw=parsed(ln,'center_width_json',{});band=parsed(ln,'band_reliability_json',{})
    print(r['station_id'],r['event_date'],r['decision_at_utc'], 'cw',cw.get('width_lambda'), 'band',band.get('axis'), 'provisional',parsed(ln,'provisional_uncalibrated_system_ids_json',[]))
