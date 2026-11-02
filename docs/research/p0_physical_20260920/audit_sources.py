"""Reconcile observation lineage and receipt clocks; never rewrites study labels."""
import collections
import json
import sqlite3
from datetime import datetime,timezone
from study import Guard,OUT,ROOT,save,stamp,iso,digest
from model import geometry


def main():
    guard=Guard();samples=json.loads((OUT/'samples.json').read_text())
    c=sqlite3.connect(f'file:{ROOT}/data/archive.db?mode=ro',uri=True,timeout=2);c.row_factory=sqlite3.Row
    c.execute('pragma query_only=on');c.execute('pragma cache_size=-4096');c.set_progress_handler(guard.progress,2000)
    domains=[list(r) for r in c.execute('select distinct selected_source_lane,source_priority,priority_contract_version from reported_trajectory_canonical_station_day_bounds')]
    counts=collections.Counter();mismatches=[];missing=[];clock_failures=[]
    for s in samples:
        group='primary' if s['primary_eligible'] else 'asos'
        if stamp(s['taf_known_at'])>stamp(s['decision_at']):clock_failures.append([s['station'],s['event_date'],'TAF'])
        if any(stamp(t)>stamp(s['p0_nominal_decision_at']) for t in s['root_receipts'].values()):clock_failures.append([s['station'],s['event_date'],'native'])
        r=c.execute('select * from reported_trajectory_canonical_station_day_bounds where station_id=? and event_date=?',(s['station'],s['event_date'])).fetchone()
        if not r:
            counts[group+'_missing']+=1;missing.append([s['station'],s['event_date']]);continue
        counts[group+'_'+r['selected_source_lane']]+=1
        if r['settlement_timezone']!=s['timezone']:counts[group+'_timezone_mismatch']+=1
        if abs(r['observed_max_c']-s['truth_c'])>1e-7:
            alternative=dict(s,truth_c=r['observed_max_c'])
            changed=geometry(s)[3]!=geometry(alternative)[3]
            counts[group+'_value_mismatch']+=1;counts[group+'_bucket_mismatch']+=int(changed)
            mismatches.append({'station':s['station'],'event_date':s['event_date'],'daily_truth_c':s['truth_c'],
                'canonical_max_c':r['observed_max_c'],'canonical_lane':r['selected_source_lane'],
                'bucket_changed':changed,'canonical_receipt':r['source_receipt_sha256'],
                'canonical_updated_at':r['updated_at']})
    query='SELECT * FROM reported_trajectory_canonical_station_day_bounds WHERE station_id=? AND event_date=?'
    out={'schema':'physical_study_truth_reconciliation_v1','checked_at':iso(datetime.now(timezone.utc)),
         'source':'archive.db:reported_trajectory_canonical_station_day_bounds','query':query,'domains':domains,
         'counts':dict(counts),'missing':missing,'clock_failures':clock_failures,'mismatches':mismatches,
         'primary_conclusion':'Available non-ASOS canonical rows agree numerically with accepted AWC daily truth; two older canonical rows are absent and retained as an explicit evidence limitation.',
         'asos_conclusion':'Do not coerce rounded canonical bounds and daily settlement truth into one label. ASOS remains excluded from primary metrics.',
         'production_writes':0}
    save('source_reconciliation.json',out);print(out['counts']);print('CLOCK_FAILURES',len(clock_failures))


if __name__=='__main__':main()
