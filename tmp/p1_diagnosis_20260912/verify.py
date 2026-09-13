from common import *
import numpy as np,statistics
from collections import Counter,defaultdict
e=json.loads((OUT/'phase_refresh_experiment.json').read_text());env=json.loads((OUT/'envelopes.json').read_text());f=json.loads((OUT/'features_audit.json').read_text())
checks={}
checks['station_count']=len({r['station_id'] for r in env})
checks['station_day_count']=len({(r['station_id'],r['event_date']) for r in env})
checks['samples']=len(env)
checks['all_baseline_probabilities_normalized']=all(abs(sum(r['probabilities_json'].values())-1)<1e-7 for r in env)
checks['all_samples_physically_available']=all(datetime.datetime.fromisoformat(r['created_at'].replace('Z','+00:00'))<=datetime.datetime.fromisoformat(r['sample_at_utc'].replace('Z','+00:00')) for r in env)
checks['identity_l1']=dict(max=max(x['identity_l1'] for x in e),median=statistics.median(x['identity_l1'] for x in e))
checks['identity_l1_over_0_02']=sum(x['identity_l1']>.02 for x in e)
checks['phase_states']=dict(Counter(json.loads(r['lineage_json'].get('p1_four_layer_serving_json','{}')).get('phase',{}).get('state','NO_LAYER') for r in env))
for k in ['all_baseline_probabilities_normalized','all_samples_physically_available']:assert checks[k]
assert checks['station_count']==45 and checks['station_day_count']==135 and checks['samples']==675
rng=np.random.default_rng(20260912)
def report(rows):
    if not rows:return {'n':0}
    nll=np.array([r['updated']['nll']-r['old']['nll'] for r in rows]);hit=np.array([r['updated']['hit']-r['old']['hit'] for r in rows]);mae=np.array([r['updated']['mae']-r['old']['mae'] for r in rows])
    # One fixed-hour observation per station-day. Bootstrap is exploratory: only three calendar days.
    boot=rng.integers(0,len(rows),size=(3000,len(rows)))
    return {'n':len(rows),'nll_delta':float(nll.mean()),'nll_bootstrap95':np.quantile(nll[boot].mean(axis=1),[.025,.975]).tolist(),'hit_delta':float(hit.mean()),'mae_delta':float(mae.mean()),'arms':{arm:{k:statistics.mean((r['other_arms'][arm] if arm in r['other_arms'] else r[arm])[k] for r in rows) for k in ['nll','hit','mae','brier']} for arm in ['old','identity','clock_only','state_only','updated']}}
checks['hour16']=report([r for r in e if r['hour']==16])
checks['hour16_fresh']=report([r for r in e if r['hour']==16 and r['fresh_at_sample']])
checks['hour16_f']=report([r for r in e if r['hour']==16 and '°F' in r['actual']])
checks['hour16_c']=report([r for r in e if r['hour']==16 and '°C' in r['actual']])
checks['hour16_by_day']={d:report([r for r in e if r['hour']==16 and r['date']==d]) for d in ['2026-09-09','2026-09-10','2026-09-11']}
checks['four_layer_shape_missing']=sum(r['served_applied']=='true' and r['shape_missing'] for r in f)
checks['four_layer_clock_older_than_report_2h']=sum(r['served_applied']=='true' and r['clock_vs_closing_minutes'] is not None and r['clock_vs_closing_minutes']>120 for r in f)
save('verification.json',checks)
print(json.dumps(checks,ensure_ascii=False,indent=2))
