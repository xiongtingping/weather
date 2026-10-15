"""Join each hourly served-P1 row (obs_minus_forecast_station_direction_20261015/rows.jsonl.gz)
to its P0-M / P0 roots: vectors, decision instants, and the P0's native run inits.  Read-only."""
import gzip, json, sqlite3
from pathlib import Path
HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[2]
aud = {}
for l in gzip.open(ROOT/'docs/research/obs_increment_recheck_20261007/audited_rows.jsonl.gz','rt'):
    r = json.loads(l); aud[(r['station'], r['decided_at'])] = r['envelope_id']
c = sqlite3.connect(f"file:{ROOT/'data/bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=5)
cache = {}
def env_by_ck(ck):
    if ck in cache: return cache[ck]
    r = c.execute("select stage, decision_at_utc, probabilities_json, lineage_json, posterior_version from basket_probability_envelope where checkpoint_id=? and state='COMMITTED' limit 1",(ck,)).fetchone()
    if r is None: cache[ck] = None; return None
    lin = json.loads(r[3])
    out = {'stage': r[0], 'decided_at': r[1], 'p': json.loads(r[2]), 'version': r[4],
           'p0_ck': lin.get('p0_root_checkpoint_id') or lin.get('predecessor_checkpoint_id') if r[0]=='P0-M' else None,
           'run_init_by_system': lin.get('run_init_utc_by_system_json'), 'run_cycle': lin.get('run_cycle'),
           'lead_bucket': lin.get('lead_bucket'), 'taf': bool(lin.get('taf_nwp_live_compatibility_json')),
           'censor': lin.get('observed_floor_censoring_json')}
    cache[ck] = out; return out
out = gzip.open(HERE/'joined.jsonl.gz','wt'); n=miss=0
for l in gzip.open(ROOT/'docs/research/obs_minus_forecast_station_direction_20261015/rows.jsonl.gz','rt'):
    r = json.loads(l); eid = aud.get((r['station'], r['decided_at']))
    if not eid: miss+=1; continue
    row = c.execute("select lineage_json from basket_probability_envelope where envelope_id=?",(eid,)).fetchone()
    lin = json.loads(row[0])
    root = env_by_ck(lin.get('prediction_root_checkpoint_id'))
    p0 = env_by_ck(lin.get('p0_root_checkpoint_id')) if lin.get('p0_root_checkpoint_id') else (root if root and root['stage']=='P0' else None)
    if root and root['stage']=='P0-M' and p0 is None and root.get('p0_ck'): p0 = env_by_ck(root['p0_ck'])
    r['root'] = root; r['p0'] = p0
    out.write(json.dumps(r)+'\n'); n+=1
out.close(); print('rows', n, 'unmatched', miss, 'roots cached', len(cache))
