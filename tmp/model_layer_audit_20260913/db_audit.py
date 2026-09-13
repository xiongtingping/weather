import collections
from datetime import datetime, timezone
import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
c = sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=2)
c.row_factory = sqlite3.Row
c.execute('PRAGMA query_only=ON')
c.execute('PRAGMA cache_size=-8192')
c.execute('PRAGMA temp_store=MEMORY')
deadline = time.monotonic() + 70
c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
out = {'at_utc': datetime.now(timezone.utc).isoformat()}

def rows(sql, params=()):
    return [dict(r) for r in c.execute(sql, params)]

tables = ('calibration_native_ensemble_activation', 'calibration_native_ensemble_system_artifact',
          'calibration_cross_native_ensemble_artifact', 'calibration_band_reliability_table',
          'calibration_center_width_table', 'calibration_native_ensemble_system',
          'calibration_cross_native_ensemble_oos_evaluation', 'basket_probability_envelope')
for t in tables:
    cols = [r['name'] for r in rows(f'pragma table_info({t})')]
    out[t+'_columns'] = cols
    for col in ('station_id', 'stage', 'lead_bucket', 'run_cycle', 'season_bin', 'artifact_kind', 'authority_scope', 'activated'):
        if col in cols:
            out[t+'_'+col+'_domain'] = [r[col] for r in rows(f'select distinct {col} from {t}')]
out['band_tables'] = rows('select * from calibration_band_reliability_table where activated=1')
out['center_width_tables'] = rows('select * from calibration_center_width_table where activated=1')
now = out['at_utc'].replace('+00:00', 'Z')
out['current_cross'] = rows('''select a.station_id,a.season_bin,a.lead_bucket,a.run_cycle,
 a.authority_scope,a.activated_at_utc,r.*
 from calibration_native_ensemble_activation a
 join calibration_cross_native_ensemble_artifact r on r.cross_ensemble_artifact_id=a.artifact_id
 where a.artifact_kind='CROSS_NATIVE_ENSEMBLE' and a.valid_from_utc<=? and a.valid_until_utc>?
 order by a.activated_at_utc desc''', (now,now))
out['current_systems'] = rows('''select a.authority_scope,a.activated_at_utc,r.*
 from calibration_native_ensemble_activation a
 join calibration_native_ensemble_system_artifact r on r.system_artifact_id=a.artifact_id
 where a.artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' and a.valid_from_utc<=? and a.valid_until_utc>?
 order by a.activated_at_utc desc''', (now,now))
for kind in ('current_cross', 'current_systems'):
    raw = out[kind]
    out[kind+'_valid_interval_row_count'] = len(raw)
    unique = {}
    for r in raw:
        key = tuple(r.get(k) for k in ('station_id','season_bin','lead_bucket','run_cycle','ensemble_system_id','ensemble_system_ids_json','run_cycle_by_system_json'))
        if key not in unique:
            unique[key] = r
    out[kind] = list(unique.values())
    if kind == 'current_systems':
        for r in out[kind]:
            p=json.loads(r['reliability_parameters_json'])
            p.pop('climate_samples_c', None)
            r['reliability_parameters_json']=json.dumps(p)
out['recent_envelopes'] = []
for r in c.execute('select * from basket_probability_envelope order by rowid desc limit 180'):
    d=dict(r)
    lineage=json.loads(d.pop('lineage_json'))
    d['lineage_keys']=sorted(lineage)
    d['lineage']={k:v for k,v in lineage.items() if any(x in k for x in ('system','center_width','band_reliability','provisional','combination','cross_ensemble','lead_bucket')) and len(str(v))<8000}
    out['recent_envelopes'].append(d)
out['systems'] = rows('select * from calibration_native_ensemble_system')
compact={k:v for k,v in out.items() if k=='at_utc' or k.endswith('_domain') or k.endswith('_columns') or k in ('band_tables','center_width_tables')}
compact['station_ids']=sorted({r['station_id'] for r in out['current_systems']})
compact['valid_interval_deduplicated_counts']={k:len(out[k]) for k in ('current_cross','current_systems')}
(Path(__file__).parent/'database_evidence.json').write_text(json.dumps(compact,ensure_ascii=False,indent=2))
for kind in ('current_cross','current_systems'):
    v=out[kind]
    print(kind, 'rows',len(v),'stations',len({r['station_id'] for r in v}))
    print('authority',dict(collections.Counter(r['authority_scope'] for r in v)))
    if kind=='current_cross':
        print('methods', dict(collections.Counter(r['combination_method'].split('::')[0] for r in v)))
        print('nonempty_pins',dict(collections.Counter(k for r in v for k in ('center_width_sha256=', 'station_width_sha256=', 'band_table_sha256=') if k in r['combination_method'])))
for k in ('band_tables','center_width_tables'):
    for r in out[k]:
        print(k, {a:b for a,b in r.items() if a!='evidence_json'})
        e=json.loads(r['evidence_json']); print('evidence', e)
print('evidence_path',str(Path(__file__).parent/'database_evidence.json'))
