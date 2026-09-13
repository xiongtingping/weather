import collections
from datetime import date
import json
import sqlite3
import statistics
import time
from pathlib import Path

HERE=Path(__file__).resolve().parent
p=json.loads((HERE/'served_evidence.json').read_text())
arts={a['content_sha256']:a for r in p['rows'] for a in r['systems'].values()}
parsed=[(a,json.loads(a['bias_parameters_json']),json.loads(a['spread_parameters_json']),json.loads(a['reliability_parameters_json'])) for a in arts.values()]
def dist(v):return {'min':min(v),'median':statistics.median(v),'max':max(v)} if v else {}
out={'at_utc':p['at_utc'],'selector_cutoff_utc':p['selector_cutoff_utc'],'latest_45_p0':p['summary'], 'unique_single_system_artifact_count':len(arts),
 'spread_factor':dist([s['spread_factor'] for a,b,s,r in parsed]),
 'spread_at_upper_cap_count':sum(s['spread_factor']>=5 for a,b,s,r in parsed),
 'bias_c':dist([b['bias_c'] for a,b,s,r in parsed]),
 'effective_n':dist([r.get('train_n_effective',0) for a,b,s,r in parsed]),
 'fit_through_dates':dict(collections.Counter(r.get('fit_through_date') for a,b,s,r in parsed)),
 'reliability_weight_values':sorted({r.get('reliability_weight') for a,b,s,r in parsed}),
 'bias_forms':dict(collections.Counter(b.get('correction_form','constant') for a,b,s,r in parsed)),
 'provisional_sources':dict(collections.Counter(v['source'] for row in p['rows'] for v in json.loads(row['lineage'].get('provisional_parameter_sources_json','{}')).values())),
 'observed_p0_peak_moves':[]}
for row in p['rows']:
    ln=row['lineage']; receipt=json.loads(ln.get('band_reliability_json','{}'))
    if receipt.get('axis')!='centre_distance':continue
    prob=json.loads(row['probabilities_json']);ords=json.loads(row['bucket_ordinals_json'])
    raw={k:v*receipt['pre_normalisation_mass']/receipt['applied_factors'].get(k,1.) for k,v in prob.items()}
    peak=lambda v:max(v,key=lambda k:(v[k],-ords[k],k))
    if peak(prob)!=peak(raw):out['observed_p0_peak_moves'].append({'station_id':row['station_id'],'event_date':row['event_date'],'before':peak(raw),'after':peak(prob),'before_probabilities':raw,'after_probabilities':prob})
c=sqlite3.connect(f'file:{HERE.parents[1]}/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=2)
c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('PRAGMA cache_size=-8192')
end=time.monotonic()+35;c.set_progress_handler(lambda:int(time.monotonic()>end),10000)
for col in ('source_lane','settlement_definition','qc_status','source_role','authority_scope','timestamps_complete'):
    out['truth_'+col+'_domain']=[r[0] for r in c.execute('select distinct '+col+' from calibration_station_daily_high_truth')]
out['truth_latest_source_collisions']=[dict(r) for r in c.execute('''select t.station_id,t.local_date,count(*) as n,min(t.official_tmax_c) as lo,max(t.official_tmax_c) as hi,group_concat(t.source_lane) as lanes from calibration_station_daily_high_truth t where t.revision_no=(select max(x.revision_no) from calibration_station_daily_high_truth x where x.station_id=t.station_id and x.local_date=t.local_date and x.source_lane=t.source_lane and x.settlement_definition=t.settlement_definition) group by t.station_id,t.local_date having count(*)>1 limit 30''')]
(HERE/'quality_summary.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
