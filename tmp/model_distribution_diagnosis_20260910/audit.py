"""Bounded, read-only diagnosis. No artifacts or activations are published."""
from __future__ import annotations
import collections, datetime as dt, gc, hashlib, json, math, os, pathlib, resource
import shutil, sqlite3, sys, threading, time
import psutil

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from bayes_basket_target import native_ensemble_oos as oos

ASOF = '2026-09-09T16:36:32Z'
SYSTEMS = {'ECMWF_IFS_ENS_OPEN_PF50', 'ECMWF_AIFS_ENS_GLOBAL',
           'DWD_ICON_GLOBAL_EPS', 'NOAA_GEFS_05', 'ECCC_GEM_GLOBAL_ENS'}
START = time.monotonic()
process = psutil.Process()
def guard():
    while True:
        if (process.memory_info().rss > 850*1024**2 or
            shutil.disk_usage(ROOT).free < 5*1024**3 or
            time.monotonic()-START > 900):
            os.write(2, b'FAIL_CLOSED_RESOURCE_LIMIT\n'); os._exit(73)
        time.sleep(.25)
threading.Thread(target=guard, daemon=True).start()
resource.setrlimit(resource.RLIMIT_FSIZE, (128*1024**2,128*1024**2))
c = sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=2)
c.execute('pragma query_only=on'); c.execute('pragma cache_size=-8192')
c.execute('pragma temp_store=memory')
deadline = time.monotonic()+25
c.set_progress_handler(lambda: int(time.monotonic()>deadline), 10000)
stations = sorted(r[0] for r in c.execute('select distinct station_id from calibration_station_market_timezone_binding')
                  if r[0] not in {'LTFM','UUWW','LLBG'})
assert len(stations) == 45, stations
manifest = {'asof':ASOF, 'stations':stations, 'systems':sorted(SYSTEMS),
            'authority':'DIAGNOSTIC_ONLY_NOT_PROMOTION_OOS', 'read_only':True,
            'resource_limits':{'rss_mb':850,'output_bytes':128*1024**2,'min_free_gb':5,'wall_seconds':900},
            'case_source':'production native_ensemble_oos._load_cases; AWC truth selected by current loader',
            'failures':[], 'station_counts':{}}
retry = '--retry-missing' in sys.argv
if retry:
    manifest = json.loads((OUT/'manifest.json').read_text())
    work_stations = sorted({r['station'] for r in manifest['failures']})
else:
    work_stations = stations
f = (OUT/'cases.jsonl').open('a' if retry else 'w')
for station in work_stations:
    deadline = time.monotonic()+(60 if retry else 25)
    t = time.monotonic()
    try:
        scopes = oos._load_cases(c, station_id=station, as_of_utc=ASOF)
        count = 0
        for (system,season,lead,cycle),cases in sorted(scopes.items()):
            if system not in SYSTEMS: continue
            for case in cases:
                if case['event_date'] < '2026-06-01': continue
                values=case['values']; mean=sum(values)/len(values)
                row={k:case[k] for k in ['event_date','values','actual_tmax_c','holdout_eligible',
                     'evidence_timing_class','transport_provider_id','historical_closed_day',
                     'future_event_count','member_set_sha256','timezone_name','aggregator_snapshot_at_utc']}
                row.update(station=station,system=system,season=season,lead=lead,cycle=cycle,
                           mean=mean,spread=math.sqrt(sum((v-mean)**2 for v in values)/len(values)))
                f.write(json.dumps(row,separators=(',',':'))+'\n');count+=1
        manifest['station_counts'][station]=count
        manifest['failures']=[r for r in manifest['failures'] if r['station']!=station]
        print(station,count,round(time.monotonic()-t,2),flush=True)
        del scopes;gc.collect()
    except Exception as e:
        manifest['failures'].append({'station':station,'error':str(e)})
        print(station,'FAILED',str(e),flush=True)
    f.flush()
f.close()
manifest['elapsed_seconds']=time.monotonic()-START
manifest['cases_sha256']=hashlib.sha256((OUT/'cases.jsonl').read_bytes()).hexdigest()
(OUT/'manifest.json').write_text(json.dumps(manifest,indent=2))
c.close()
print('DONE',sum(manifest['station_counts'].values()),manifest['failures'],flush=True)
