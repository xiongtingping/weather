"""Bounded read-only source audit and official TAF/P0 conditional distribution study."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')
ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import psutil

FIRST, LAST = '2026-08-14', '2026-09-19'
NATIVE = {'DWD_ICON_GLOBAL_EPS', 'ECCC_GEM_GLOBAL_ENS', 'ECMWF_IFS_ENS_OPEN_PF50',
          'ECMWF_AIFS_ENS_GLOBAL', 'NOAA_GEFS_05'}
ASOS = {'KATL','KAUS','KBKF','KDAL','KHOU','KLAX','KLGA','KMIA','KORD','KSEA','KSFO'}


def stamp(s):
    t = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    if t.tzinfo is None:
        raise ValueError('timestamp_without_timezone')
    return t.astimezone(timezone.utc)


def iso(t):
    return t.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def digest(x):
    return hashlib.sha256(json.dumps(x, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def decoded(x):
    return json.loads(x) if isinstance(x, str) else x


class Guard:
    def __init__(self):
        self.start = time.monotonic()
        self.process = psutil.Process()
        self.peak = 0
        self.error = ''
        self.check()
        threading.Thread(target=self.watch, daemon=True).start()

    def check(self):
        rss = self.process.memory_info().rss
        self.peak = max(self.peak, rss)
        if rss > 384 * 1024**2: raise RuntimeError('rss_limit')
        if time.monotonic() - self.start > 300: raise RuntimeError('time_limit')
        if shutil.disk_usage(OUT).free < 2 * 1024**3: raise RuntimeError('free_disk_limit')
        if sum(p.stat().st_size for p in OUT.iterdir() if p.is_file()) > 32 * 1024**2:
            raise RuntimeError('output_limit')

    def watch(self):
        while True:
            time.sleep(0.25)
            try: self.check()
            except Exception as e:
                print('RESOURCE_FAIL_CLOSED', str(e), file=sys.stderr, flush=True)
                os._exit(75)

    def progress(self):
        try:
            self.check()
            return 0
        except Exception as e:
            self.error = str(e)
            return 1


def connect(guard):
    c = sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=2)
    c.row_factory = sqlite3.Row
    c.execute('pragma query_only=on')
    c.execute('pragma cache_size=-4096')
    c.execute('pragma temp_store=memory')
    c.set_progress_handler(guard.progress, 10000)
    return c


def save(name, obj):
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    if len(data.encode()) > 16 * 1024**2: raise RuntimeError('file_size_limit')
    path = OUT / name
    temporary = path.with_suffix(path.suffix+'.part')
    temporary.write_text(data+'\n')
    temporary.replace(path)


def extract():
    from bayes_basket_target.materialization import rehydrate_lineage_documents
    guard = Guard(); c = connect(guard)
    cutoff = datetime.now(timezone.utc)
    domains = {}
    for table, cols in [
        ('calibration_station_market_timezone_binding','station_id,settlement_timezone'),
        ('calibration_station_forecast_physics','source_id,capture_mode,reference_only'),
        ('calibration_forward_taf_capture','source_class,authority_scope'),
        ('calibration_station_daily_high_truth','source_lane,qc_status,authority_scope,settlement_definition'),
        ('basket_probability_envelope','stage,state'),
    ]:
        domains[table] = [list(r) for r in c.execute(f'select distinct {cols} from {table}')]
    systems = {r['ensemble_system_id']:dict(r) for r in c.execute('select * from calibration_native_ensemble_system')}
    stations = dict(domains['calibration_station_market_timezone_binding'])
    excludes=collections.Counter(); coverage=[]; samples=[]; issues=[]
    physics_totals=collections.Counter()
    for st,tz in sorted(stations.items()):
        guard.check(); local=ZoneInfo(tz)
        pstats=collections.Counter()
        for r in c.execute('select * from calibration_station_forecast_physics where station_id=? and local_date between ? and ?', (st,FIRST,LAST)):
            midnight=datetime.fromisoformat(r['local_date']).replace(tzinfo=local)
            pstats['rows']+=1; pstats[r['capture_mode']]+=1
            if stamp(r['captured_at_utc']) < midnight: pstats['captured_before_day']+=1
            else: pstats['captured_at_or_after_day']+=1
        physics_totals.update(pstats)
        truths=collections.defaultdict(list)
        for r in c.execute('select * from calibration_station_daily_high_truth where station_id=? and local_date between ? and ? order by revision_no', (st,FIRST,LAST)):
            truths[r['local_date']].append(dict(r))
        tafs=collections.defaultdict(list)
        for r in c.execute('select * from calibration_forward_taf_capture where station_id=? and event_date between ? and ?', (st,FIRST,LAST)):
            tafs[r['event_date']].append(dict(r))
        stat=collections.Counter(station=0)
        for d in (datetime.fromisoformat(FIRST)+timedelta(days=i) for i in range((datetime.fromisoformat(LAST)-datetime.fromisoformat(FIRST)).days+1)):
            day=d.date().isoformat(); midnight=d.replace(tzinfo=local); end=midnight+timedelta(days=1)
            stat['planned_days']+=1
            candidates_root=c.execute('select * from basket_probability_envelope indexed by basket_probability_envelope_asof_idx where station_id=? and event_date=? and state=? and decision_at_utc>=? and decision_at_utc<? and stage=? order by decision_at_utc desc limit 300', (st,day,'COMMITTED',iso(midnight-timedelta(days=1)),iso(midnight),'P0')).fetchall()
            if len(candidates_root)==300:raise RuntimeError('root_row_limit')
            available_root=[r for r in candidates_root if max(stamp(r['decision_at_utc']),stamp(r['created_at']))<midnight]
            row=max(available_root,key=lambda r:(max(stamp(r['decision_at_utc']),stamp(r['created_at'])),r['decision_at_utc'])) if available_root else None
            if row is None: excludes['no_day_ahead_p0']+=1;continue
            stat['p0_present']+=1
            try:
                r=dict(row); nominal_at=stamp(r['decision_at_utc']);at=max(nominal_at,stamp(r['created_at']))
                if r['settlement_timezone']!=tz: raise ValueError('p0_timezone_mismatch')
                l=rehydrate_lineage_documents(c,decoded(r['lineage_json']))
                weights=decoded(l.get('system_weights_json') or '{}')
                if len(weights)<2 or not set(weights)<=NATIVE or any(float(v)<=0 for v in weights.values()):raise ValueError('non_native_or_thin_pool')
                families={systems[s]['independence_family_id'] for s in weights}
                if len(families)<2:raise ValueError('one_family')
                arrivals=decoded(l.get('archive_query_at_utc_by_system_json') or '{}')
                if set(weights)-set(arrivals) or any(stamp(arrivals[s])>nominal_at for s in weights):raise ValueError('member_receipt_not_asof')
                probs=decoded(r['probabilities_json']); intervals=decoded(l['bucket_intervals_c_json'])
                centers=decoded(l.get('native_calibrated_system_centers_c_json') or '{}')
                if set(weights)-set(centers):raise ValueError('system_centers_missing')
                if set(probs)!=set(intervals):raise ValueError('bucket_geometry_mismatch')
                if abs(sum(probs.values())-1)>1e-5 or min(probs.values())<0:raise ValueError('invalid_probabilities')
                ts=[x for x in truths[day] if x['qc_status']=='ACCEPTED' and x['source_lane']=='AWC' and x['authority_scope']=='FORMAL_NATIVE_ELIGIBLE' and x['timestamps_complete']==1 and x['settlement_timezone']==tz and x['canonical_committed_at_utc'] and stamp(x['canonical_committed_at_utc'])<=cutoff]
                if not ts or end>cutoff:raise ValueError('no_closed_formal_truth')
                truth=max(ts,key=lambda x:x['revision_no'])
                stat['root_truth_ready']+=1
                candidates=[]
                for a in tafs[day]:
                    if a['settlement_timezone']!=tz:continue
                    known=max(stamp(a[k]) for k in ['issued_at_utc','received_at_utc','provider_available_at_utc'])
                    if known>at:continue
                    lo,hi=stamp(a['valid_from_utc']),stamp(a['valid_until_utc'])
                    overlap=max(0,(min(hi,end)-max(lo,midnight)).total_seconds())
                    if overlap<=0:continue
                    if a['source_class']!='official_forward_native_receipt':continue
                    candidates.append((stamp(a['issued_at_utc']),known,a,overlap))
                if not candidates:raise ValueError('no_asof_taf_overlap')
                _,known,taf,overlap=max(candidates,key=lambda a:(a[0],a[1],a[2]['content_sha256']))
                stat['paired_taf_ready']+=1
                native_mean=sum(float(weights[s])*float(centers[s]) for s in weights)/sum(weights.values())
                truth_known=max(end,*(stamp(truth[k]) for k in ['canonical_committed_at_utc','received_at_utc','published_at_utc']))
                entry={'station':st,'event_date':day,'decision_at':iso(at),'p0_nominal_decision_at':r['decision_at_utc'],'p0_created_at':r['created_at'],'timezone':tz,
                       'checkpoint':r['checkpoint_id'],'p0_sha':r['content_sha256'],'posterior_version':r['posterior_version'],
                       'probabilities':probs,'intervals_c':intervals,'centers':centers,'weights':weights,'native_mean_c':native_mean,
                       'root_receipts':arrivals,'truth_c':truth['official_tmax_c'],'truth_id':truth['truth_id'],
                       'truth_sha':truth['content_sha256'],'truth_known_at':iso(truth_known),
                       'truth_revision':truth['revision_no'],'taf_id':taf['taf_capture_id'],'taf_sha':taf['content_sha256'],
                       'taf_known_at':iso(known),'taf_issued_at':taf['issued_at_utc'],'taf_valid_until':taf['valid_until_utc'],
                       'tx_c':taf['tx_c'] if taf['tx_available'] else None,
                       'cloud':taf['cloud_cover_fraction'],'precip':taf['precipitation_probability'],
                       'wind_mps':taf['wind_speed_mps'],'taf_age_h':(at-stamp(taf['issued_at_utc'])).total_seconds()/3600,
                       'taf_day_coverage':overlap/(end-midnight).total_seconds(),
                       'primary_eligible':st not in ASOS,'exclude_primary_reason':'canonical_asos_winner_not_bound' if st in ASOS else None}
                entry['sample_sha']=digest(entry);samples.append(entry)
            except (ValueError,KeyError,TypeError) as e:
                reason=str(e);excludes[reason]+=1
                if len(issues)<30:issues.append({'station':st,'day':day,'reason':reason})
        coverage.append({'station':st,'timezone':tz,'counts':dict(stat),'reference_physics':dict(pstats),'asos':st in ASOS})
        print(st,dict(stat),flush=True)
    audit={'schema':'physical_candidate_source_audit_v1','cutoff':iso(cutoff),'domains':domains,'coverage':coverage,
           'exclusions':dict(excludes),'examples':issues,'reference_physics':dict(physics_totals),
           'sample_n':len(samples),'primary_n':sum(x['primary_eligible'] for x in samples),
           'source_schema_sha':digest([list(x) for x in c.execute("select name,sql from sqlite_master where type='table' order by name")]),
           'code_sha':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'resource_peak_mib':guard.peak/1024**2}
    save('samples.json',samples);save('source_audit.json',audit)
    print('AUDIT',len(samples),audit['primary_n'],dict(excludes),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['extract'])
    args=parser.parse_args()
    if args.command=='extract':extract()
