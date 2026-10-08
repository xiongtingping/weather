"""Rebuild raw TAF clouds and audit exact current-chain replay readiness.

Reads frozen prefixes of the prior source audit; no network or DB writes.
An unavailable current-chain root is never substituted by an old served P1.
"""
import collections
import gzip
import hashlib
import importlib.util
import json
import resource
import shutil
import sys
import time
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from raw_cloud import decode,scenarios,interval,cloud_increment,base_increment,utc,stamp,DecodeError

HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[2]
PRIOR=HERE.parent/'taf_cloud_increment_20261007'
ORIGINAL=HERE.parent/'obs_increment_recheck_20261007'
sys.path.insert(0,str(ROOT))
from bayes_basket_target.materialization import rehydrate_lineage_documents

# Reuse audited source schema and read-only helpers; do not invoke its main.
spec=importlib.util.spec_from_file_location('previous_cloud_audit',PRIOR/'audit.py')
old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
MAX_BYTES=128*1024**2
P0='calibrated_native_cross_ensemble_p0_v27'
P0M='p0m_taf_likelihood_v30'
P1='p1_joint_likelihood_v38'
HEIGHTS=(1500,3000,5000,6500)


def guard():
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform!='darwin':rss*=1024
    if rss>1024**3 or shutil.disk_usage(HERE).free<2*1024**3:raise RuntimeError('resource_limit_or_probe_failed')


def fetch(c,sql,args=()):
    limit=time.monotonic()+45
    c.set_progress_handler(lambda:int(time.monotonic()>limit),10000)
    return c.execute(sql,args)


def sources(c):
    manifest=json.loads((PRIOR/'results.json').read_text())
    _,bindings=old.native_sources(c)
    by_key={};counts=collections.Counter();qa=collections.Counter();exceptions=[];nbytes=0;qa_cases={}
    for meta in manifest['input_fingerprints']:
        path=ROOT/meta['path'];length=meta['bytes'];used=0;h=hashlib.sha256()
        with path.open('rb') as f:
            while used<length:
                line=f.readline(min(2*1024**2,length-used));used+=len(line);nbytes+=len(line);h.update(line)
                if not line.endswith(b'\n') or nbytes>MAX_BYTES:raise RuntimeError('source_bytes_or_row_failed')
                r=json.loads(line);counts['raw_rows']+=1
                if r['schema']!='taf_raw_archive_row_v1':raise ValueError('schema_changed')
                raw=r['raw_taf'];sha=r['raw_taf_sha256'];st=r['station_id'];w=r['awc_row']
                if hashlib.sha256(raw.encode()).hexdigest()!=sha or raw!=w['rawTAF'].strip() or st!=w['icaoId'] or utc(r['issue_time_utc'])!=utc(w['issueTime']):raise ValueError('raw_identity_invalid')
                key=(st,sha)
                if key not in by_key:
                    try:d=decode(raw,station=st,issue_time=r['issue_time_utc'])
                    except DecodeError as e:
                        counts['decode_reject:'+str(e)]+=1;by_key[key]=None
                        if len(exceptions)<20:exceptions.append(dict(station=st,sha=sha,reason=str(e),raw=raw))
                    else:by_key[key]=dict(raw=raw,sha=sha,station=st,decoded=d,receipts=[],native_decode_receipt=False)
                t=by_key[key]
                if t is None:continue
                d=t['decoded']
                # Whole validity comes from raw text, not retrospectively
                # decoded fcsts. Identity agreement is required for binding.
                validity_agrees=old.utc(w['validTimeFrom'])==utc(d['valid_from']) and old.utc(w['validTimeTo'])==utc(d['valid_to'])
                if not validity_agrees:counts['decoded_whole_validity_mismatch_rows']+=1
                replay='#backfill' in r['source_url'] or 'date' in parse_qs(urlparse(r['source_url']).query)
                if not replay and r.get('response_sha256') and urlparse(r['source_url']).hostname in ('aviationweather.gov','tgftp.nws.noaa.gov') and validity_agrees:
                    seen=max(utc(r['received_at_utc']),utc(r['issue_time_utc']),utc(r['db_pop_time_utc']))
                    t['receipts'].append((seen,'official_local_raw_receipt',r['response_sha256']))
                    t['native_decode_receipt'] |= bool(w.get('fcsts'))
                # Official decoded clouds are an independent QA comparison,
                # never an input to this raw-only parser or receipt clock.
                if w.get('fcsts') and validity_agrees:
                    at=utc(d['valid_from']);end=utc(d['valid_to'])
                    while at<end:
                        awc=old.forecast_scenarios(r,at)
                        for height in HEIGHTS:
                            actual=interval(d,at,height);ref=old.scenario_interval(awc,height)
                            if actual is not None and ref is not None:
                                qa['compared_'+str(height)]+=1
                                if actual!=ref:
                                    qa['mismatch_'+str(height)]+=1
                                    q=qa_cases.setdefault(sha,dict(station=st,sha=sha,raw=raw,awc_groups=w['fcsts'],differences=[]))
                                    if len(q['differences'])<4:q['differences'].append(dict(at=stamp(at),height=height,raw_interval=actual,awc_interval=ref))
                            else:qa['uncomparable_'+str(height)]+=1
                        at+=timedelta(hours=1)
        if used!=length or h.hexdigest()!=meta['sha256']:raise ValueError('frozen_source_prefix_changed')
        guard()
    by_station=collections.defaultdict(list)
    for key,t in by_key.items():
        if t is None:continue
        d=t['decoded'];counts['decoded_unique']+=1
        for r in bindings.get(key,[]):
            if utc(r['issued_at_utc'])!=utc(d['issue']) or utc(r['valid_from_utc'])!=utc(d['valid_from']) or utc(r['valid_until_utc'])!=utc(d['valid_to']):
                counts['ssot_validity_mismatch']+=1;continue
            seen=max(utc(r['received_at_utc']),utc(r['provider_available_at_utc']),utc(r['issued_at_utc']))
            t['receipts'].append((seen,'raw_sha_bound_local_receipt',r['taf_capture_id']))
        if d['modifier']=='COR':counts['COR_excluded']+=1;continue
        if not t['receipts']:counts['no_real_local_receipt']+=1;continue
        seen,kind,proof=min(t['receipts']);t.update(known_at=seen,receipt_kind=kind,receipt_id=proof)
        by_station[t['station']].append(t);counts['receipt_bound_decoded_unique']+=1
    for ls in by_station.values():ls.sort(key=lambda t:utc(t['decoded']['issue']))
    (HERE/'decode_qa.json').write_text(json.dumps(dict(disagreements=list(qa_cases.values())),ensure_ascii=False,indent=2))
    return by_station,dict(counts),dict(qa),exceptions


def choose(tafs,valid,decision,mode):
    cutoff=min(valid,decision) if mode=='latest' else valid-timedelta(hours=24)
    candidates=[t for t in tafs if utc(t['decoded']['issue'])<=cutoff and t['known_at']<=cutoff and utc(t['decoded']['valid_from'])<=valid<utc(t['decoded']['valid_to'])]
    if not candidates:return None
    latest=max(utc(t['decoded']['issue']) for t in candidates)
    choices=[t for t in candidates if utc(t['decoded']['issue'])==latest]
    if len(choices)>1:
        choices=[t for t in choices if t['decoded']['modifier']=='AMD']
        if len(choices)!=1:return None
    return choices[0]


def pair(row,tafs,observation):
    at=utc(row['decided_at']);valid=utc(observation['valid_at'])
    if any(not observation[k] or utc(observation[k])>at for k in ('valid_at','first_seen_at','canonical_committed_at')) or observation['correction_suspected']:raise ValueError('observation_visibility_failed')
    if observation['source_lane']!='awc' or observation['station_id']!=row['station']:raise ValueError('observation_lineage_failed')
    obs=old.metar_layers(observation['raw_text']);result={k:row[k] for k in ('station','day','decided_at','observation_id','posterior_version')}
    result.update(observation_valid_at=stamp(valid),observation_first_seen=observation['first_seen_at'],observation_committed=observation['canonical_committed_at'],obs_layers=obs,truth=row.get('truth'),running_high=row.get('running_high'),evidence_role='observational_candidate_not_oos')
    for mode in ('latest','day1'):
        t=choose(tafs,valid,at,mode);result[mode]=None
        if t is None:continue
        d=t['decoded'];poss=scenarios(d,valid)
        fc={str(h):interval(d,valid,h) for h in HEIGHTS}
        oi={str(h):old.cloud_interval(obs,h) for h in HEIGHTS}
        result[mode]=dict(sha=t['sha'],issue=d['issue'],known_at=stamp(t['known_at']),valid_from=d['valid_from'],valid_to=d['valid_to'],receipt_kind=t['receipt_kind'],receipt_id=t['receipt_id'],raw=t['raw'],scenarios=poss,forecast_intervals=fc,obs_intervals=oi,
                          coverage_increment={str(h):cloud_increment(oi[str(h)],fc[str(h)]) for h in HEIGHTS},base_lowering_km=base_increment(obs,poss))
    return result


def lineage_audit(c,rows):
    cache={};profile=collections.Counter();versions=collections.Counter();artifact_cutoffs=collections.Counter();complete_days=collections.defaultdict(set)
    def env(checkpoint):
        if checkpoint not in cache:
            found=list(fetch(c,'select stage,posterior_version,decision_at_utc,lineage_json,created_at from basket_probability_envelope where checkpoint_id=? and state=?',(checkpoint,'COMMITTED')))
            if len(found)!=1:cache[checkpoint]=None
            else:
                z=found[0];L=rehydrate_lineage_documents(c,json.loads(z['lineage_json']))
                cache[checkpoint]=dict(stage=z['stage'],version=z['posterior_version'],at=z['decision_at_utc'],created=z['created_at'],p0=L.get('p0_root_checkpoint_id'),
                                       availability=json.loads(L.get('native_forward_availability_evidence_json') or '{}'),forecast_sha=L.get('forecast_source_object_sha256'),packet_sha=L.get('native_forward_snapshot_sha256'))
        return cache[checkpoint]
    for r in rows:
        found=fetch(c,'select lineage_json,created_at from basket_probability_envelope where envelope_id=?',(r['envelope_id'],)).fetchone()
        if found is None:raise ValueError('sample_envelope_missing')
        L=rehydrate_lineage_documents(c,json.loads(found['lineage_json']));receipt=json.loads(L.get('p1_observation_likelihood_json') or '{}')
        pid=L.get('p0_root_checkpoint_id');rid=L.get('prediction_root_checkpoint_id');root=env(rid) if rid else None;p0=env(pid) if pid else None
        blockers=[];at=utc(r['decided_at'])
        if r['posterior_version']!=P1:blockers.append('old_p1_method_tag')
        if not p0 or p0['version']!=P0:blockers.append('old_or_missing_p0_parent')
        if not root or root['stage'] not in ('P0','P0-M'):blockers.append('prediction_root_missing')
        elif root['stage']=='P0-M' and root['version']!=P0M:blockers.append('old_p0m_parent')
        if any(x and (utc(x['at'])>at or utc(x['created'])>at) for x in (root,p0)):blockers.append('parent_not_committed_asof')
        if not receipt:blockers.append('p1_fitted_receipt_missing')
        elif not receipt.get('trained_through') or receipt['trained_through']>=r['day']:blockers.append('p1_parameter_training_cutoff_not_earlier')
        if not L.get('hard_excluded_buckets') and r.get('hard_excluded'):blockers.append('mask_lineage_changed')
        source=(p0 or {}).get('availability',{})
        if source.get('source_class')!='OFFICIAL_NATIVE_ARCHIVE_QUERY_RECEIPT' or not source.get('by_system'):blockers.append('native_input_receipts_missing')
        elif any(not v.get('archive_query_at_utc') or utc(v['archive_query_at_utc'])>at for v in source['by_system'].values()):blockers.append('native_input_arrival_after_decision')
        versions[(p0 or {}).get('version','MISSING')+' / '+(root or {}).get('version','MISSING')]+=1
        artifact_cutoffs[(receipt.get('trained_through') or 'NONE')]+=1
        r['chain_audit']=dict(p0_version=(p0 or {}).get('version'),prediction_version=(root or {}).get('version'),p1_parameter_train_end=receipt.get('trained_through'),p1_artifact_sha=receipt.get('artifact_sha256'),p0_checkpoint=pid,prediction_checkpoint=rid,p0_packet_sha=(p0 or {}).get('packet_sha'),blockers=blockers,exact_current_parent_chain=not blockers)
        profile.update(blockers);profile['rows']+=1
        if not blockers:
            profile['exact_current_parent_chain']+=1;complete_days[r['station']].add(r['day'])
        if profile['rows']%500==0:guard()
    return dict(counts=dict(profile),parent_versions=dict(versions),p1_parameter_train_ends=dict(artifact_cutoffs),exact_current_days_by_station={st:sorted(ds) for st,ds in complete_days.items()})


def main():
    guard();c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');a=old.connect(ROOT/'data/archive.db')
    tafs,source_profile,qa,exceptions=sources(c)
    print('raw_decode',json.dumps(dict(source_profile=source_profile,qa=qa)),flush=True)
    rows=[];nbytes=0
    with gzip.open(ORIGINAL/'audited_rows.jsonl.gz','rt') as f:
        for line in f:
            nbytes+=len(line.encode())
            if nbytes>MAX_BYTES:raise RuntimeError('research_input_limit')
            rows.append(json.loads(line))
    lineage=lineage_audit(c,rows)
    obs_cache={};pairs=[];summaries={m:collections.Counter() for m in ('latest','day1')}
    fresh_by_mode={m:set() for m in summaries};bytes_out=0
    with gzip.open(HERE/'raw_pairs.jsonl.gz','wt') as out:
        for i,r in enumerate(rows):
            oid=r['observation_id']
            if oid not in obs_cache:obs_cache[oid]=dict(fetch(a,'select * from reported_trajectory_observations where observation_id=?',(oid,)).fetchone())
            p=pair(r,tafs[r['station']],obs_cache[oid]);p['chain_audit']=r['chain_audit'];pairs.append(p)
            stale=(utc(p['decided_at'])-utc(p['observation_valid_at'])).total_seconds()>7200
            for mode,s in summaries.items():
                s['rows']+=1;t=p[mode]
                if t is None:s['no_taf']+=1;continue
                s['taf_paired']+=1
                if stale:s['stale_observation']+=1;continue
                for h in HEIGHTS:
                    inc=t['coverage_increment'][str(h)]
                    if inc is None:s[str(h)+'_unknown']+=1
                    else:s[str(h)+'_'+('more' if inc>0 else 'less' if inc<0 else 'overlap')]+=1
                if t['base_lowering_km'] is not None:s['unique_base_comparison']+=1
                if not p['chain_audit']['blockers'] and t['coverage_increment']['6500'] is not None:
                    s['current_chain_cloud_rows']+=1;fresh_by_mode[mode].add(p['day'])
            line=json.dumps(p,ensure_ascii=False)+'\n';bytes_out+=len(line.encode())
            if bytes_out>MAX_BYTES:raise RuntimeError('output_limit')
            out.write(line)
            if i%500==0:guard()
    old_case=json.loads((ORIGINAL/'audit.json').read_text())['case'];case=[]
    for x in old_case:
        if not x['observation'] or not 12<=x['hour']<=16:continue
        o=x['observation'];ob=dict(station_id='ZUUU',source_lane=o['lane'],valid_at=o['valid'],first_seen_at=o['seen'],canonical_committed_at=o['committed'],correction_suspected=o['correction'],raw_text=o['raw'])
        r=dict(station='ZUUU',day='2026-10-07',decided_at=x['at'],observation_id=o['id'],posterior_version=x['version'],running_high=x['running_high'])
        p=pair(r,tafs['ZUUU'],ob);p['local_hour']=x['hour'];case.append(p)
    # Existing parents are the exact historical roots. Until the upstream
    # P0/P0-M methods are themselves rebuilt, old parents cannot become v27/
    # v30 by merely applying today's P1 arithmetic or renaming a version.
    artifact=json.loads((ROOT/'data/runtime/bayes_basket_target_nwp/p1_observation_likelihood_artifact.json').read_text())
    needed_days=20+8
    days={m:sorted(ds) for m,ds in fresh_by_mode.items()}
    gate=dict(state='UPSTREAM_REBUILD_REQUIRED',required_train_days=20,required_holdout_days=8,eligible_days_by_mode=days,
              scores_computed=False,production_modified=False,current_artifact_train_end=artifact['trained_through'],
              current_artifact_fitted_at=artifact['fitted_at_utc'],fitting_today_artifact_on_prior_test_days_forbidden=True,
              reason='cloud evidence now raw-reconstructible; full historical current P0/P0-M parents are still absent from this sample set; refitting only P1 on old parents is not current-chain OOS')
    # Report readiness, rather than silently replacing the hard gate by an
    # exploratory mixed-root score. No probability artifact is produced.
    result=dict(schema='raw_cloud_current_chain_replay_readiness_v1',captured_at=stamp(datetime.now(timezone.utc)),source_profile=source_profile,raw_awc_crosscheck=qa,exceptions=exceptions,
                lineage=lineage,summaries={m:dict(s) for m,s in summaries.items()},case=case,gate=gate,evidence_role='observational_candidate_not_oos',
                fingerprints=dict(raw_pairs=old.sha_file(HERE/'raw_pairs.jsonl.gz'),previous_audit=old.sha_file(PRIOR/'results.json'),input_rows=old.sha_file(ORIGINAL/'audited_rows.jsonl.gz'),raw_decoder=old.sha_file(HERE/'raw_cloud.py')),
                limits=dict(rss_bytes=1024**3,input_bytes=MAX_BYTES,output_bytes=MAX_BYTES,min_disk_bytes=2*1024**3,sql_seconds=45))
    (HERE/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(dict(lineage=lineage['counts'],summaries=result['summaries'],gate=gate),ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
