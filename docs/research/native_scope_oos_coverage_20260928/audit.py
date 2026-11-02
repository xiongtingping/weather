"""Bounded read-only coverage inventory; never a promotion or skill certificate."""
import collections, concurrent.futures, datetime as dt, hashlib, json, os
import pathlib, resource, shutil, signal, sqlite3, sys, time
from zoneinfo import ZoneInfo

ROOT=pathlib.Path('/Users/xiong/weather')
OUT=pathlib.Path(__file__).parent
SYSTEMS=['ECMWF_IFS_ENS_OPEN_PF50','ECMWF_AIFS_ENS_GLOBAL','DWD_ICON_GLOBAL_EPS','NOAA_GEFS_05','ECCC_GEM_GLOBAL_ENS','NOAA_AIGEFS_025']
CODE=['native_ensemble_oos.py','native_archive_lead.py','follow_run.py','truth_exclusions.py','canonical_contract.py']
def fingerprints():
    return {p:hashlib.sha256((ROOT/'bayes_basket_target'/p).read_bytes()).hexdigest() for p in CODE}
def stamp(s): return dt.datetime.fromisoformat(s.replace('Z','+00:00'))
def connect():
    c=sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=3)
    c.execute('pragma query_only=ON'); c.execute('pragma cache_size=-8192')
    c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,8*1024**2)
    return c
def worker(args):
    station,asof,cutoff=args
    started=time.monotonic()
    def guard(*unused):
        if time.monotonic()-started>180: raise RuntimeError('wall cap')
        if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss>600*1024**2: raise RuntimeError('rss cap')
        if shutil.disk_usage(ROOT).free<4*1024**3: raise RuntimeError('disk floor')
    signal.signal(signal.SIGALRM,lambda *a: (_ for _ in ()).throw(RuntimeError('wall alarm')))
    signal.alarm(185)
    c=connect()
    def progress():
        try: guard(); return 0
        except RuntimeError: return 1
    c.set_progress_handler(progress,20000); c.execute('begin')
    from bayes_basket_target.native_ensemble_oos import _load_cases, _load_systems
    from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
    before=fingerprints()
    registry=_load_systems(c)
    cases=_load_cases(c,station_id=station,as_of_utc=asof)
    selected={k:[v for v in vv if v['event_date']<=cutoff and v['transport_provider_id']!='OPEN_METEO'] for k,vv in cases.items() if k[0] in SYSTEMS}
    needed={s for vv in selected.values() for v in vv for s in v['member_run_content_shas']}
    raw={}; raw_count=0
    for r in c.execute('select content_sha256,archive_query_at_utc,native_run_init_utc,raw_object_ref,member_id,target_local_date,ensemble_system_id from calibration_native_ensemble_historical_member_run where station_id=?',(station,)):
        raw_count+=1
        if raw_count>700000: raise RuntimeError('row cap')
        if r[0] in needed: raw[r[0]]=r[1:]
        if raw_count%10000==0: guard()
    assert len(raw)==len(needed),(station,'missing member hashes')
    truths=collections.defaultdict(list)
    for r in c.execute('select local_date,official_tmax_c,canonical_committed_at_utc,revision_no,settlement_definition from calibration_station_daily_high_truth where station_id=? and source_lane IN '+FORMAL_TRUTH_LANES_SQL+" and qc_status='ACCEPTED' and authority_scope in ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') and julianday(canonical_committed_at_utc)<=julianday(?)",(station,asof)):
        truths[r[0]].append(r[1:])
    scopes=[]; case_rows=[]
    for key,vv in sorted(selected.items()):
        if not vv: continue
        system,season,lead,cycle=key
        compact=[]
        for v in vv:
            rr=[raw[s] for s in v['member_run_content_shas']]
            n=registry[system]['expected_member_count']
            assert len(rr)==n==len({r[3] for r in rr})
            assert all(r[4]==v['event_date'] and r[5]==system for r in rr)
            end=(dt.datetime.fromisoformat(v['event_date']).replace(tzinfo=ZoneInfo(v['timezone_name']))+dt.timedelta(days=1)).astimezone(dt.timezone.utc)
            latest=max(stamp(r[0]) for r in rr)
            x=dict(station=station,system=system,season=season,lead=lead,cycle=cycle,date=v['event_date'],members=n,member_set_sha256=v['member_set_sha256'],loader_eligible=v['holdout_eligible'],timing_class=v['evidence_timing_class'],run_init=v['aggregator_snapshot_at_utc'],last_archive_query=latest.isoformat(),all_queries_before_close=latest<end,all_queries_before_asof=latest<=stamp(asof),receipt_refs=sorted({r[2] for r in rr}),actual=v['actual_tmax_c'],settlement_definition=v['settlement_definition'])
            compact.append(x)
        eligible=[v for v in compact if v['loader_eligible']]
        hold=eligible[-8:]
        train=[]; known=[]; revised=[]; unknown=[]
        if len(hold)==8:
            boundary=min(stamp(v['run_init']) for v in hold)
            start=(dt.date.fromisoformat(hold[0]['date'])-dt.timedelta(days=60)).isoformat()
            train=[v for v in compact if start<=v['date']<hold[0]['date'] and stamp(v['run_init'])<boundary]
            for v in train:
                tt=[r for r in truths[v['date']] if stamp(r[1])<=boundary and r[3]==v['settlement_definition']]
                if not tt: unknown.append(v['date']); continue
                rev=max(r[2] for r in tt); values={r[0] for r in tt if r[2]==rev}
                if values=={v['actual']}: known.append(v['date'])
                else: revised.append(v['date'])
        scopes.append(dict(station=station,system=system,family=registry[system]['independence_family_id'],season=season,lead=lead,cycle=cycle,days=len(vv),first=vv[0]['event_date'],last=vv[-1]['event_date'],loader_eligible_days=len(eligible),train_days=len(train),holdout_days=len(hold),count_20_8=len(train)>=20 and len(hold)==8,train_truth_known_same=len(known),train_truth_unavailable=len(unknown),train_truth_different=len(revised),all_query_before_close_days=sum(v['all_queries_before_close'] for v in compact),queries_after_audit_asof=sum(not v['all_queries_before_asof'] for v in compact),holdout_dates=[v['date'] for v in hold],train_dates=[v['date'] for v in train],unknown_train_truth_dates=unknown,different_train_truth_dates=revised))
        case_rows.extend(compact)
    assert before==fingerprints(),'code changed'
    c.rollback();c.close();guard();signal.alarm(0)
    out=dict(station=station,asof=asof,cutoff=cutoff,code=before,scopes=scopes,cases=case_rows,raw_rows_scanned=raw_count,seconds=time.monotonic()-started,rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    encoded=json.dumps(out,ensure_ascii=False,sort_keys=True)
    assert len(encoded)<12*1024**2
    (OUT/(station+'.json')).write_text(encoded)
    return station,len(scopes),len(case_rows)
if __name__=='__main__':
    c=connect();stations=[r[0] for r in c.execute('select distinct station_id from basket_probability_bridge_latest')];c.close()
    assert len(stations)==45 and all(len(s)==4 for s in stations)
    manifest=OUT/'manifest.json'
    if manifest.exists(): m=json.loads(manifest.read_text())
    else:
        m=dict(asof=dt.datetime.now(dt.timezone.utc).isoformat(),cutoff='2026-09-26',stations=stations,code=fingerprints())
        manifest.write_text(json.dumps(m,indent=2))
    with concurrent.futures.ProcessPoolExecutor(max_workers=2,max_tasks_per_child=1) as pool:
        futures={pool.submit(worker,(s,m['asof'],m['cutoff'])):s for s in stations if not (OUT/(s+'.json')).exists()}
        for f in concurrent.futures.as_completed(futures): print(f.result(),flush=True)
