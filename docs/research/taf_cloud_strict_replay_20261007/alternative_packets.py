"""Search alternate native inputs without pretending they were original parents.

Only packet identity, receipt, declared native membership/curve and actual
target geometry are checked here. Historical raw-member/exact OOS binding
must still be done before any alternate packet could enter a replay.
"""
import collections
import gzip
import json
import math
from rebuild import HERE,ROOT,old,fetch,guard,utc,MAX_BYTES
from upstream import SYSTEMS
from bayes_basket_target.contracts import content_sha256


def main():
    guard();index=json.loads((HERE/'missing_packets.json').read_text());c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');registry={r['ensemble_system_id']:dict(r) for r in fetch(c,'select * from calibration_native_ensemble_system') if r['ensemble_system_id'] in SYSTEMS};by=collections.defaultdict(list);counts=collections.Counter();results=[];exceptions=[]
    for r in index['rows']:by[r['station']].append(r)
    for station,parents in sorted(by.items()):
        used=0;cache={}
        for r in parents:
            cutoff=utc(r['parent_available_at']);day=r['day']
            if day not in cache:
                cache[day]=[]
                for path in sorted((ROOT/'data/runtime/bayes_basket_target_nwp/forward_packet_archive'/day).glob(station+'-*.json.gz')):
                    with gzip.open(path,'rb') as f:body=f.read(2*1024**2+1)
                    used+=len(body);counts['packet_objects_scanned']+=1
                    if len(body)>2*1024**2 or used>MAX_BYTES:raise RuntimeError('alternate_packet_station_input_limit')
                    q=json.loads(body);sha=content_sha256({k:v for k,v in q.items() if k!='snapshot_content_sha256'})
                    if sha!=q['snapshot_content_sha256'] or q['station_id']!=station or q['event_date']!=day:
                        counts['candidate_identity_or_scope_rejected']+=1
                        if len(exceptions)<20:exceptions.append(dict(path=str(path.relative_to(ROOT)),reason='packet_SHA_or_station_event_mismatch'))
                        continue
                    families=[];valid=True
                    for s in q['systems']:
                        sid=s['ensemble_system_id'];curve=s.get('family_temperature_curve')
                        if sid not in registry or s['independence_family_id']!=registry[sid]['independence_family_id']:valid=False;break
                        if len(s['members'])!=registry[sid]['expected_member_count'] or len({m['member_id'] for m in s['members']})!=len(s['members']) or any(not math.isfinite(m['raw_tmax_c']) for m in s['members']):valid=False;break
                        if max(utc(s['archive_query_at_utc']),utc(s['run_init_utc']))>utc(q['received_at_utc']):valid=False;break
                        families.append(s['independence_family_id'])
                        if curve and (not curve.get('source_field_receipt_set_sha256') or content_sha256({k:v for k,v in curve.items() if k!='content_sha256'})!=curve['content_sha256'] or utc(curve['available_at_utc'])>utc(q['received_at_utc'])):valid=False;break
                    if not valid or len(set(families))<2 or not any(s.get('family_temperature_curve') for s in q['systems']):continue
                    cache[day].append(dict(path=str(path.relative_to(ROOT)),sha=sha,received_at=q['received_at_utc'],intervals=q['bucket_intervals'],timezone=q['settlement_timezone'],systems=[s['ensemble_system_id'] for s in q['systems']]))
            proofs=[dict(x) for x in fetch(c,"select bucket_intervals_json,settlement_timezone,created_at_utc,decision_at_utc from basket_probability_bridge_history where checkpoint_id=? and state='COMMITTED'",(r['checkpoint'],))]
            proofs=[x for x in proofs if max(utc(x['created_at_utc']),utc(x['decision_at_utc']))<=cutoff]
            candidates=[q for q in cache[day] if utc(q['received_at'])<=cutoff and any(json.loads(p['bucket_intervals_json'])==q['intervals'] and p['settlement_timezone']==q['timezone'] for p in proofs)]
            latest=max(candidates,key=lambda q:(utc(q['received_at']),q['sha'])) if candidates else None
            state='ALTERNATE_PACKET_CLOCK_AND_GEOMETRY_CANDIDATE' if latest else 'NO_CLOCK_AND_GEOMETRY_CANDIDATE';counts[state]+=1
            result=dict(station=station,day=day,original_checkpoint=r['checkpoint'],original_packet_sha=r['packet_sha'],original_parent_available_at=r['parent_available_at'],state=state,candidate_count=len(candidates),role='alternate_counterfactual_input_not_original_parent',remaining_checks=['historical_raw_member_exact_binding','current_exact_scope_nested_OOS','compiler_and_full_downstream_replay'])
            if latest:result['candidate']={k:v for k,v in latest.items() if k not in ('intervals','timezone')}
            results.append(result);guard()
    report=dict(schema='alternate_missing_parent_native_packet_search_v1',counts=dict(counts),rows=results,exceptions=exceptions,production_modified=False,qualified_replacement_inputs=0)
    body=json.dumps(report,ensure_ascii=False,indent=2)
    if len(body.encode())>2*1024**2:raise RuntimeError('alternate_packet_output_limit')
    (HERE/'alternative_packets.json').write_text(body);print(json.dumps(counts),flush=True)


if __name__=='__main__':main()
