"""Per station-day (last pre-midnight P0): last 5 native runs per system known at decision time (archive_query <= decision),
each run's member mean/sd -> trajectory features.  Writes trajectory_data.json."""
import json, pathlib, sqlite3, sys, collections
import numpy as np
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]
SYSTEMS = ['ECMWF_IFS_ENS_OPEN_PF50', 'ECMWF_AIFS_ENS_GLOBAL', 'DWD_ICON_GLOBAL_EPS', 'NOAA_GEFS_05', 'ECCC_GEM_GLOBAL_ENS', 'NOAA_AIGEFS_025']
c = sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=2); c.execute('pragma query_only=ON')
cap = json.load(open(HERE / 'capture.json')); out = []; cnt = collections.Counter()
for r in cap['rows']:
    e = r['last']
    if 'rejected' in e: continue
    traj = {}
    for s in SYSTEMS:
        runs = c.execute('''select native_run_init_utc, max(archive_query_at_utc) from calibration_native_ensemble_historical_member_run
            where ensemble_system_id=? and station_id=? and target_local_date=? and native_run_init_utc is not null and archive_query_at_utc<=? and qc_status='ACCEPTED'
            group by native_run_init_utc order by native_run_init_utc desc limit 5''', (s, r['station'], r['date'], e['at'])).fetchall()
        seq = []
        for run_init, aq in runs:
            v = [x[0] for x in c.execute('''select raw_tmax_c from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date=? and native_run_init_utc=? and archive_query_at_utc=? and qc_status='ACCEPTED' ''', (s, r['station'], r['date'], run_init, aq))]
            if len(v) >= 5: seq.append(dict(run=run_init, mean=float(np.mean(v)), sd=float(np.std(v)), n=len(v)))
        if seq: traj[s] = seq[::-1]  # oldest -> newest
        cnt[(s, len(seq))] += 1
    out.append(dict(station=r['station'], date=r['date'], at=e['at'], traj=traj))
json.dump(out, open(HERE / 'trajectory_data.json', 'w'))
print(len(out)); print(sorted(cnt.items()))
