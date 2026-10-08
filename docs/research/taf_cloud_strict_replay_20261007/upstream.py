"""Bounded exact raw native inputs and as-of calibration capacity audit.

Capacity is an optimistic upper bound, not an OOS activation. Archives and
current registry identities are read only; no parameters or DB writes occur.
"""
import collections
import gzip
import hashlib
import json
import math
import sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from rebuild import ROOT,HERE,fetch,guard,old,utc,stamp,MAX_BYTES
sys.path.insert(0,str(ROOT))
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from bayes_basket_target.native_archive_lead import lead_hour_bucket_for_run
from bayes_basket_target.native_ensemble_oos import _historical_transport_scope_allowed

SYSTEMS=('ECMWF_IFS_ENS_OPEN_PF50','ECMWF_AIFS_ENS_GLOBAL','DWD_ICON_GLOBAL_EPS','NOAA_GEFS_05','ECCC_GEM_GLOBAL_ENS','NOAA_AIGEFS_025')
ARCHIVE=ROOT/'data/runtime/bayes_basket_target_nwp/forward_packet_archive'


def day_clock(day,tz,end=False):
    return (datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz))+(timedelta(days=1) if end else timedelta())).astimezone(timezone.utc)


def truth_at(truths,day,at,tz):
    if day_clock(day,tz,True)>at:return None
    known=[r for r in truths.get(day,[]) if r['qc_status']=='ACCEPTED' and r['timestamps_complete']==1 and r['authority_scope'] in ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE') and utc(r['canonical_committed_at_utc'])<=at]
    if not known:return None
    rev=max(r['revision_no'] for r in known);win=[r for r in known if r['revision_no']==rev]
    if len(win)!=1:raise ValueError('ambiguous_truth_revision')
    return win[0]


def load_members(c,station,tz,registry):
    keys=('ensemble_system_id','transport_provider_id','archive_lead_bucket','run_cycle','authority_scope','qc_status')
    values={k:set() for k in keys}
    for r in fetch(c,'select distinct '+','.join(keys)+' from calibration_native_ensemble_historical_member_run INDEXED BY calibration_native_ensemble_historical_member_run_scope_idx where station_id=? and target_local_date BETWEEN ? AND ?',(station,'2026-07-03','2026-10-06')):
        for k in keys:values[k].add(r[k])
    domains={k:sorted(v) for k,v in values.items()}
    groups=collections.defaultdict(list);nbytes=0;fingerprint=hashlib.sha256();count=0
    sql='select ensemble_system_id,target_local_date,native_run_init_utc,archive_lead_bucket,run_cycle,extraction_version,transport_provider_id,member_id,raw_tmax_c,archive_query_at_utc,source_object_sha256,content_sha256,qc_status,authority_scope from calibration_native_ensemble_historical_member_run INDEXED BY calibration_native_ensemble_historical_member_run_scope_idx where station_id=? and target_local_date>=? and target_local_date<=? and ensemble_system_id IN ('+','.join('?' for _ in SYSTEMS)+') and transport_provider_id<>?'
    for rr in fetch(c,sql,(station,'2026-07-03','2026-10-06',*SYSTEMS,'OPEN_METEO')):
        r=dict(rr);b=json.dumps(r,sort_keys=True).encode();nbytes+=len(b);fingerprint.update(b);count+=1
        if nbytes>MAX_BYTES:raise RuntimeError('station_member_phase_byte_limit')
        sid=r['ensemble_system_id']
        if sid not in SYSTEMS or r['transport_provider_id']=='OPEN_METEO' or r['qc_status']!='ACCEPTED' or r['authority_scope']!='CALIBRATION_TRAIN_ONLY' or not r['native_run_init_utc']:continue
        groups[(sid,r['target_local_date'],r['native_run_init_utc'],r['archive_lead_bucket'],r['run_cycle'],r['extraction_version'],r['transport_provider_id'])].append(r)
        if count%1000==0:guard()
    cases=[];bad=collections.Counter()
    for (sid,day,run,lead,cycle,extract,transport),rs in groups.items():
        if not _historical_transport_scope_allowed(system_id=sid,transport_provider_id=transport,lead_bucket=lead,run_cycle=cycle,extraction_version=extract):bad['superseded_or_unregistered_extraction_scope']+=1;continue
        if len(rs)!=registry[sid]['expected_member_count'] or len({r['member_id'] for r in rs})!=len(rs):bad['incomplete_member_group']+=1;continue
        if any(not r['source_object_sha256'] or not math.isfinite(r['raw_tmax_c']) for r in rs):bad['invalid_member_fact']+=1;continue
        arrival=max(utc(r['archive_query_at_utc']) for r in rs)
        if arrival<utc(run):bad['arrival_before_run']+=1;continue
        traininglead=lead_hour_bucket_for_run(day_start_utc=day_clock(day,tz),run_init=utc(run))
        cases.append(dict(system=sid,day=day,run=run,lead=traininglead,archive_lead=lead,cycle=cycle,extraction=extract,transport=transport,arrival=arrival,
                          members={r['member_id']:r['raw_tmax_c'] for r in rs},shas=sorted(r['content_sha256'] for r in rs),source_shas=sorted(set(r['source_object_sha256'] for r in rs))))
    return cases,dict(domains=domains,rows=count,bytes=nbytes,sha256=fingerprint.hexdigest(),groups=len(cases),invalid=dict(bad),query=sql)


def capacity(cases,truths,at,event,tz):
    ds=collections.defaultdict(set);predictable=collections.defaultdict(set)
    floor=(datetime.fromisoformat(event)-timedelta(days=60)).date().isoformat()
    for x in cases:
        if not floor<=x['day']<event or x['arrival']>at or truth_at(truths,x['day'],at,tz) is None:continue
        scope=(x['system'],x['lead'],x['cycle']);ds[scope].add(x['day'])
        if x['arrival']<day_clock(x['day'],tz,True):predictable[scope].add(x['day'])
    return {scope:dict(available_closed_train_days=len(days),preclose_input_days=len(predictable[scope])) for scope,days in ds.items()}


def write_upstream_report(result):
    result=dict(result);packets=result.pop('packet_fingerprints');by=collections.defaultdict(dict);refs={};folder=HERE/'upstream_packet_fingerprints';folder.mkdir(exist_ok=True)
    for path,meta in packets.items():by[Path(path).name.split('-')[0]][path]=meta
    for station,part in sorted(by.items()):
        body=json.dumps(part).encode()
        if len(body)>2*1024**2:raise RuntimeError('upstream_packet_manifest_object_limit')
        output=folder/(station+'.json.gz')
        with gzip.open(output,'wb') as f:f.write(body)
        refs[station]=dict(path=str(output.relative_to(HERE)),sha=content_sha256(part),packet_count=len(part))
    result['packet_fingerprint_manifest']=refs;body=json.dumps(result,ensure_ascii=False,indent=2)
    if len(body.encode())>2*1024**2:raise RuntimeError('upstream_summary_object_limit')
    (HERE/'upstream.json').write_text(body)


def main():
    guard();c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');pairs=collections.defaultdict(list);nbytes=0
    with gzip.open(HERE/'raw_pairs.jsonl.gz','rt') as f:
        for line in f:
            nbytes+=len(line.encode())
            if nbytes>MAX_BYTES:raise RuntimeError('pairs_input_limit')
            r=json.loads(line);pairs[r['station']].append(r)
    registry={r['ensemble_system_id']:dict(r) for r in fetch(c,'select * from calibration_native_ensemble_system') if r['ensemble_system_id'] in SYSTEMS}
    if set(registry)!=set(SYSTEMS):raise ValueError('native_registry_missing')
    counts=collections.Counter();station_result={};global_days=collections.defaultdict(set);out_bytes=0;packet_fingerprints={};examples=[]
    with gzip.open(HERE/'upstream_rows.jsonl.gz','wt') as out:
        for station,rows in sorted(pairs.items()):
            bind=fetch(c,'select * from calibration_station_market_timezone_binding where station_id=?',(station,)).fetchone()
            if not bind or bind['market_registry_timezone_count']!=1:raise ValueError('timezone_binding_missing')
            tz=bind['settlement_timezone'];truths=collections.defaultdict(list)
            for rr in fetch(c,'select * from calibration_station_daily_high_truth where station_id=? and source_lane IN '+FORMAL_TRUTH_LANES_SQL,(station,)):
                r=dict(rr)
                if r['settlement_timezone']!=tz:raise ValueError('truth_timezone_mismatch')
                truths[r['local_date']].append(r)
            cases,member_proof=load_members(c,station,tz,registry);by_day=collections.defaultdict(list)
            for x in cases:by_day[x['day']].append(x)
            packet_cache={};phasebytes=0;sc=collections.Counter();qualified_days=set()
            for p in rows:
                at=utc(p['decided_at']);day=p['day'];sha=p['chain_audit']['p0_packet_sha'];key=(day,sha);block=[]
                if key not in packet_cache:
                    paths=list((ARCHIVE/day).glob(f'{station}-{sha[:16]}.json.gz')) if sha else []
                    if len(paths)!=1:packet_cache[key]=None
                    else:
                        path=paths[0]
                        with gzip.open(path,'rb') as f:body=f.read(2*1024**2+1)
                        phasebytes+=len(body)
                        if len(body)>2*1024**2 or phasebytes>MAX_BYTES:raise RuntimeError('station_packet_phase_byte_limit')
                        q=json.loads(body)
                        if q['snapshot_content_sha256']!=sha or content_sha256({k:v for k,v in q.items() if k!='snapshot_content_sha256'})!=sha:raise ValueError('packet_identity_failed')
                        if q['station_id']!=station or q['event_date']!=day or q['settlement_timezone']!=tz:raise ValueError('packet_scope_failed')
                        packet_fingerprints[str(path.relative_to(ROOT))]=dict(bytes=len(body),decoded_sha256=hashlib.sha256(body).hexdigest(),content_sha256=sha)
                        packet_cache[key]=q
                q=packet_cache[key];cap=capacity(cases,truths,at,day,tz);eligible=[];sources=[]
                if q is None:block.append('exact_root_packet_not_retained')
                else:
                    sc['exact_packet_retained']+=1
                    if utc(q['received_at_utc'])>at:block.append('packet_after_decision')
                    for s in q['systems']:
                        sid=s['ensemble_system_id']
                        if sid not in SYSTEMS:continue
                        run=s['run_init_utc'];cycle=s['run_cycle'];lead=lead_hour_bucket_for_run(day_start_utc=day_clock(day,tz),run_init=utc(run))
                        membermap={m['member_id']:m['raw_tmax_c'] for m in s['members']}
                        matched=[x for x in by_day[day] if x['system']==sid and x['run']==run and x['cycle']==cycle and x['members']==membermap and x['arrival']<=at]
                        curve=s.get('family_temperature_curve');npts=len((curve or {}).get('points',[]))
                        curveok=bool(curve and curve.get('source_field_receipt_set_sha256') and utc(curve['available_at_utc'])<=at and content_sha256({k:v for k,v in curve.items() if k!='content_sha256'})==curve['content_sha256'])
                        info=cap.get((sid,lead,cycle),dict(available_closed_train_days=0,preclose_input_days=0))
                        # This 28-day bound is necessary, never sufficient:
                        # it does not certify residual fitting or cross-family
                        # weights. Missing it is a proved gap; meeting it
                        # requires full current-method reconstruction next.
                        enough=info['available_closed_train_days']>=28 and info['preclose_input_days']>=8
                        ok=bool(matched) and enough
                        if ok:eligible.append(registry[sid]['independence_family_id'])
                        sources.append(dict(system=sid,run=run,lead=lead,cycle=cycle,member_exact_receipt=bool(matched),curve_receipt=curveok,curve_points=npts,capacity=info,necessary_28_day_capacity=enough))
                    if len(set(eligible))<2:block.append('fewer_than_two_families_with_28_day_capacity')
                    if not any(s['curve_receipt'] for s in sources):block.append('native_curve_receipt_missing')
                summary=dict(station=station,day=day,decided_at=p['decided_at'],p0_packet_sha=sha,blockers=block,native_sources=sources,necessary_input_capacity_pass=not block,not_a_complete_current_chain_oos=True)
                b=json.dumps(summary)+'\n';out_bytes+=len(b.encode())
                if out_bytes>MAX_BYTES:raise RuntimeError('output_byte_limit')
                out.write(b);counts['rows']+=1;sc['rows']+=1;counts.update(block);sc.update(block)
                if not block:sc['necessary_input_capacity_pass']+=1;counts['necessary_input_capacity_pass']+=1;qualified_days.add(day);global_days[day].add(station)
                if len(examples)<8 and station=='ZUUU' and not any(x.get('station')==station and x.get('day')==day for x in examples):examples.append(summary)
            station_result[station]=dict(counts=dict(sc),capacity_pass_days=sorted(qualified_days),member_evidence=member_proof,packet_phase_bytes=phasebytes)
            print(station,json.dumps(dict(sc)),flush=True);guard()
    result=dict(schema='native_upstream_raw_rebuild_capacity_v1',counts=dict(counts),stations=station_result,input_capacity_stations_by_day={d:sorted(ss) for d,ss in sorted(global_days.items())},examples=examples,packet_fingerprints=packet_fingerprints,registry=registry,
                evidence_role='rebuild_capacity_upper_bound_not_oos',production_modified=False,scores_computed=False,raw_rows_sha256=old.sha_file(HERE/'raw_pairs.jsonl.gz'),upstream_rows_sha256=old.sha_file(HERE/'upstream_rows.jsonl.gz'),
                scope_validator_code_sha256=old.sha_file(ROOT/'bayes_basket_target/native_ensemble_oos.py'),
                limitations=['28 days is a necessary count bound only; crossfit, calibrated weights, P0-M fit, and P1 refit still required','Exact root packet absent does not rule out other historical native captures; alternative input search is still required'])
    write_upstream_report(result);print(json.dumps(dict(counts=counts,capacity_days=len(global_days))),flush=True)


if __name__=='__main__':main()
