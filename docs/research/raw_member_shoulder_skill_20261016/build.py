"""Raw ensemble members per (system, station, event date, archive lead bucket, run) with truth, 2026-06-28..2026-10-14.
Read-only; one short SSOT connection per (system, station)."""
import gzip, json, sqlite3, time
from pathlib import Path
HERE = Path(__file__).resolve().parent; SSOT = HERE.parents[2] / 'data/bayes_basket_target_ssot.db'
SYSTEMS = ['DWD_ICON_GLOBAL_EPS', 'ECCC_GEM_GLOBAL_ENS', 'ECMWF_AIFS_ENS_GLOBAL', 'ECMWF_IFS_ENS_OPEN_PF50', 'NOAA_AIGEFS_025', 'NOAA_GEFS_05']
c = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=5)
truth = {}
for st, d, t, rev in c.execute("select station_id, local_date, official_tmax_c, revision_no from calibration_station_daily_high_truth"
                               " where source_lane in ('AWC','SETTLEMENT_SOURCE') and qc_status='ACCEPTED' order by revision_no"):
    truth[(st, d)] = float(t)
unit = {}
for st, rj in c.execute("select station_id, registry_json from basket_target_market_registry where station_id!='' group by station_id"):
    unit[st] = 'F' if '°F' in rj else 'C'
stations = sorted({s for s, _ in truth})
c.close()
out = gzip.open(HERE / 'members.jsonl.gz', 'wt'); n = 0; t0 = time.time()
for sysid in SYSTEMS:
    for st in stations:
        con = sqlite3.connect(f'file:{SSOT}?mode=ro', uri=True, timeout=5)
        q = con.execute(
            "select target_local_date, archive_lead_bucket, native_run_init_utc, run_cycle, extraction_version, group_concat(v) from ("
            " select target_local_date, archive_lead_bucket, native_run_init_utc, run_cycle, extraction_version, member_id, min(raw_tmax_c) v"
            " from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date between '2026-06-28' and '2026-10-14'"
            " group by target_local_date, archive_lead_bucket, native_run_init_utc, member_id)"
            " group by target_local_date, archive_lead_bucket, native_run_init_utc", (sysid, st))
        for d, lb, ri, rc, ev, vs in q:
            if (st, d) not in truth or vs is None: continue
            vals = [round(float(x), 2) for x in vs.split(',')]
            if len(vals) < 5: continue
            out.write(json.dumps({'sys': sysid, 'st': st, 'day': d, 'lead': lb, 'run': ri, 'cycle': rc, 'ext': ev, 'unit': unit.get(st, 'C'),
                                  'truth': truth[(st, d)], 'vals': vals}) + '\n'); n += 1
        con.close()
    print(sysid, 'rows so far', n, round(time.time() - t0), 's', flush=True)
out.close(); print('done', n)
