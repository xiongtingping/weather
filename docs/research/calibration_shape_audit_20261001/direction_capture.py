"""Task 1/2 dataset: one row per station-day (last pre-midnight P0), e = truth - served centre, plus
as-of structural features of the six systems (raw member means, previous-run trend) and the market
mean at decision time (reference arm only).  Reads capture.json + SSOT (read-only)."""
import json, math, pathlib, sqlite3, datetime as dt, collections
import numpy as np, sys
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE))
from analyze import axis, cont
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1
from bayes_basket_target.center_width_calibration import served_bucket_axis
SYSTEMS = ['ECMWF_IFS_ENS_OPEN_PF50', 'ECMWF_AIFS_ENS_GLOBAL', 'DWD_ICON_GLOBAL_EPS', 'NOAA_GEFS_05', 'ECCC_GEM_GLOBAL_ENS', 'NOAA_AIGEFS_025']
def inst(s): return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
def main():
    c = sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=2); c.execute('pragma query_only=ON')
    cap = json.load(open(HERE / 'capture.json')); out = []; miss = collections.Counter()
    for r in cap['rows']:
        e = r['last']
        if 'rejected' in e: continue
        x = e['inputs']; keys, lo, hi, rung = axis(x['bucket_intervals_c_json'])
        mu, sd, sk, u, pt, idx, pv = cont(e['probability'], keys, lo, hi, rung, r['truth_c'])
        row = dict(station=r['station'], date=r['date'], unit='F' if rung > 1.05 else 'C', truth=r['truth_c'], mu=mu, sd=sd, e=r['truth_c'] - mu,
                   hours=e['hours_to_midnight'] + 24.0, version=e['posterior_version'], weights=x.get('system_weights_json') or {},
                   centers=x.get('native_calibrated_system_centers_c_json') or {}, gap=(x.get('consensus_bridge_json') or {}).get('center_gap_c'))
        sc = x.get('synoptic_cooling_json') or {}
        row['clearness'] = sc.get('clearness_index'); row['clearness_z'] = ((sc.get('clearness_index') - sc['history_clearness_mean']) / sc['history_clearness_sd']) if sc.get('history_clearness_sd') else None
        row['radiation_shift_c'] = sc.get('shift_c')
        # as-of raw members for all six systems: latest native run with archive_query <= decision, and the run before it
        sysf = {}
        for s in SYSTEMS:
            runs = c.execute('''select native_run_init_utc, max(archive_query_at_utc) from calibration_native_ensemble_historical_member_run
                where ensemble_system_id=? and station_id=? and target_local_date=? and native_run_init_utc is not null and archive_query_at_utc<=? and qc_status='ACCEPTED'
                group by native_run_init_utc order by native_run_init_utc desc limit 2''', (s, r['station'], r['date'], e['at'])).fetchall()
            if not runs: miss[s] += 1; continue
            means = []
            for run_init, aq in runs:
                vals = [v[0] for v in c.execute('''select raw_tmax_c from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date=?
                    and native_run_init_utc=? and archive_query_at_utc=? and qc_status='ACCEPTED' ''', (s, r['station'], r['date'], run_init, aq))]
                if len(vals) >= 5: means.append((run_init, float(np.mean(vals)), float(np.std(vals)), len(vals)))
            if not means: miss[s] += 1; continue
            sysf[s] = dict(run=means[0][0], mean=means[0][1], sd=means[0][2], n=means[0][3], prev_mean=means[1][1] if len(means) > 1 else None,
                           age_h=(inst(e['at']) - inst(means[0][0])).total_seconds() / 3600)
        row['systems'] = sysf
        # market mean at decision time (reference arm only)
        book = c.execute('''select observed_at,buckets_json from basket_official_ws_snapshot indexed by basket_official_ws_snapshot_asof_idx
            where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? order by observed_at desc limit 1''', (r['station'], r['date'], e['at'])).fetchone()
        row['market_mean'] = None
        if book and inst(e['at']) - inst(book[0]) <= dt.timedelta(minutes=30):
            m = market_reference_v1(json.loads(book[1]))
            if m['state'] == 'complete' and set(m['vector']) == set(keys):
                mm, *_ = cont(m['vector'], keys, lo, hi, rung, r['truth_c']); row['market_mean'] = mm
        out.append(row)
    json.dump(out, open(HERE / 'direction_data.json', 'w'), default=float)
    print(len(out), 'rows; systems missing:', dict(miss)); print('with market', sum(1 for r in out if r['market_mean'] is not None))
    print('systems per row', collections.Counter(len(r['systems']) for r in out))
if __name__ == '__main__': main()
