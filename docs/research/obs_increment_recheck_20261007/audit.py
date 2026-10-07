"""Read-only, resource-bounded audit of the former experiment and today's case."""
from __future__ import annotations
import collections
import gzip
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from bayes_basket_target.metar_path_state import parse_metar_physical_state
from bayes_basket_target.materialization import floor_probabilities, rehydrate_lineage_documents
HERE = Path(__file__).resolve().parent
OLD = HERE.parent / 'obs_increment_p1_20261007'
MAX_BYTES = 128 * 1024**2

def guard():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != 'darwin': rss *= 1024
    if rss > 1024**3 or shutil.disk_usage(HERE).free < 2 * 1024**3:
        raise RuntimeError('research_resource_limit')

def utc(s):
    return datetime.fromisoformat(str(s).replace('Z', '+00:00')).replace(tzinfo=timezone.utc) if '+' not in str(s) and not str(s).endswith('Z') else datetime.fromisoformat(str(s).replace('Z', '+00:00'))

def connection(path):
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
    c.execute('PRAGMA query_only=ON'); c.execute('PRAGMA cache_size=-8192')
    return c

def query(c, sql, args=()):
    deadline = time.monotonic()+60
    c.set_progress_handler(lambda: int(time.monotonic()>deadline), 10000)
    return c.execute(sql,args)

def doc(c, raw):
    return rehydrate_lineage_documents(c,json.loads(raw))

def read_mets(c, station, day, tz):
    start=datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz))
    from datetime import timedelta
    lo=start.astimezone(timezone.utc).isoformat()[:19]; hi=(start+timedelta(days=1)).astimezone(timezone.utc).isoformat()[:19]
    result=[]
    for row in query(c,"SELECT observation_id,source_lane,valid_at,reported_temperature_c,raw_text,first_seen_at,canonical_committed_at,correction_suspected FROM reported_trajectory_observations WHERE station_id=? AND valid_at>=? AND valid_at<? AND raw_text<>'' ORDER BY valid_at,observation_id",(station,lo,hi)):
        oid,lane,valid,temp,raw,seen,committed,correction=row
        if temp is None:continue
        try: state=parse_metar_physical_state(raw,expected_temperature_c=float(temp))
        except Exception:continue
        result.append(dict(id=oid,lane=lane,valid=valid,temp=float(temp),cloud=state.cloud_cover_fraction,rain=state.precipitation_present,raw=raw,seen=seen,committed=committed,correction=correction))
        if len(result)>10000:raise RuntimeError('metar_row_limit')
    return result

def visible(m, at):
    return all(m.get(k) and utc(m[k])<=at for k in ('valid','seen','committed')) and not m['correction']

def main():
    guard()
    con=connection(ROOT/'data/bayes_basket_target_ssot.db'); arc=connection(ROOT/'data/archive.db')
    counts=collections.Counter(); versions=collections.defaultdict(lambda:dict(rows=0,days=set(),station_days=set()))
    rows_by_sd=collections.defaultdict(list); total=0
    with gzip.open(OLD/'rows.jsonl.gz','rt') as f:
        for line in f:
            total+=len(line.encode());
            if total>MAX_BYTES:raise RuntimeError('input_cap')
            r=json.loads(line);rows_by_sd[(r['station'],r['day'])].append(r)
    counts['old_rows']=sum(map(len,rows_by_sd.values()))
    score_changes=[]; out_bytes=0
    import math
    path=HERE/'audited_rows.jsonl.gz'
    with gzip.open(path,'wt') as output:
        for (st,day),rs in sorted(rows_by_sd.items()):
            guard()
            wanted={r['decided_at'] for r in rs}; envelopes={}
            for at,eid,version,pj,lj,created in query(con,"SELECT decision_at_utc,envelope_id,posterior_version,probabilities_json,lineage_json,created_at FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND state='COMMITTED' AND +stage='P1' ORDER BY decision_at_utc",(st,day)):
                if at in wanted:
                    if at in envelopes:counts['duplicate_decision_at']+=1
                    envelopes[at]=(eid,version,pj,lj,created)
            mets=read_mets(arc,st,day,rs[0]['tz'])
            truths=list(query(con,f"SELECT official_tmax_c,revision_no,source_lane,canonical_committed_at_utc,timestamps_complete FROM calibration_station_daily_high_truth WHERE station_id=? AND local_date=? AND source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED' ORDER BY revision_no DESC",(st,day)))
            for r in rs:
                e=envelopes.get(r['decided_at'])
                if not e:counts['missing_envelope']+=1;continue
                eid,version,pj,lj,created=e
                p=json.loads(pj);L=doc(con,lj);at=utc(r['decided_at'])
                if p!=r['p1']:counts['probability_mismatch']+=1;continue
                v=versions[version];v['rows']+=1;v['days'].add(day);v['station_days'].add((st,day))
                past=[m for m in mets if utc(m['valid'])<=at]
                known=[m for m in past if visible(m,at)]
                if not known:counts['no_received_prefix']+=1;continue
                if past and not visible(past[-1],at):counts['latest_valid_report_unavailable']+=1
                if any(not visible(m,at) for m in past):counts['any_prefix_report_unavailable']+=1
                high=max(m['temp'] for m in known);cur=known[-1]
                if high!=r['running_high']:counts['high_diff_received_prefix']+=1
                if cur['temp']!=r['temp_now'] or cur['cloud']!=r['cloud_now'] or cur['rain']!=r['precip_now']:counts['feature_diff_received_prefix']+=1
                if truths and truths[0][0]!=r['truth']:counts['latest_truth_changed']+=1
                excluded=set(filter(None,L.get('hard_excluded_buckets','').split(',')))
                if any(p.get(b,0)!=0 for b in excluded):counts['excluded_nonzero_in_stored_p']+=1
                # The stored vector already passed the production floor ONCE.
                # Lifting again would move the baseline (renormalised floors can
                # be slightly below .001); retain the exact served probabilities.
                floored=p
                naive={k:max(float(v),.001) for k,v in p.items()};z=sum(naive.values());naive={k:v/z for k,v in naive.items()}
                if excluded:counts['old_floor_resurrects_excluded']+=1
                b=next((b for b,(lo,hi) in r['intervals'].items() if (lo is None or r['truth']>=lo) and (hi is None or r['truth']<hi)),None) if r['intervals'] else None
                if b and floored.get(b,0)>0:score_changes.append(-math.log(floored[b])+math.log(naive[b]))
                receipt=json.loads(L.get('p1_observation_likelihood_json') or '{}')
                r.update(envelope_id=eid,posterior_version=version,envelope_created_at=created,p1=floored,hard_excluded=sorted(excluded),running_high=high,temp_now=cur['temp'],cloud_now=cur['cloud'],precip_now=cur['rain'],observation_id=cur['id'],observation_valid_at=cur['valid'],observation_first_seen_at=cur['seen'],observation_committed_at=cur['committed'],truth_history=truths,p1_artifact_sha=receipt.get('artifact_sha256'),source_class='aggregator_republisher',strict_asof=False,evidence_role='observational_candidate_not_oos')
                s=json.dumps(r,ensure_ascii=False)+'\n';out_bytes+=len(s.encode())
                if out_bytes>MAX_BYTES:raise RuntimeError('output_cap')
                output.write(s);counts['audited_rows']+=1
    case=[]
    mets=read_mets(arc,'ZUUU','2026-10-07','Asia/Shanghai')
    last={}
    for at,eid,ver,pj,lj in query(con,"SELECT decision_at_utc,envelope_id,posterior_version,probabilities_json,lineage_json FROM basket_probability_envelope WHERE station_id='ZUUU' AND event_date='2026-10-07' AND stage='P1' AND state='COMMITTED' ORDER BY decision_at_utc"):
        h=utc(at).astimezone(ZoneInfo('Asia/Shanghai')).hour
        if 7<=h<=19:last[h]=(at,eid,ver,pj,lj)
    for h,(at,eid,ver,pj,lj) in sorted(last.items()):
        L=doc(con,lj);receipt=json.loads(L.get('p1_observation_likelihood_json') or '{}');known=[m for m in mets if visible(m,utc(at))]
        case.append(dict(hour=h,at=at,envelope_id=eid,version=ver,p=json.loads(pj),running_high=L.get('metar_trajectory_running_high_c'),receipt=receipt,observation=known[-1] if known else None,intervals=json.loads(L['bucket_intervals_c_json']),hard_excluded=L.get('hard_excluded_buckets',''),evidence_role='observational_candidate_not_oos'))
    artifact=json.load(open(ROOT/'data/runtime/bayes_basket_target_nwp/p1_observation_likelihood_artifact.json'))
    result=dict(counts=dict(counts),versions={k:dict(rows=v['rows'],days=len(v['days']),first=min(v['days']),last=max(v['days']),station_days=len(v['station_days'])) for k,v in sorted(versions.items())},floor_delta_log_mean=sum(score_changes)/len(score_changes),artifact_sha=artifact['content_sha256'],artifact_fitted_at=artifact.get('fitted_at_utc'),case=case,metars_today=mets,input_sha=hashlib.sha256((OLD/'rows.jsonl.gz').read_bytes()).hexdigest(),audited_rows_sha=hashlib.sha256(path.read_bytes()).hexdigest())
    (HERE/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ('case','metars_today')},ensure_ascii=False,indent=2))
    print('case hours',len(case),flush=True)

if __name__=='__main__':main()
