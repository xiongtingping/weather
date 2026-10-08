"""Bind alternate packet members to exact accepted historical source groups."""
import collections
import gzip
import json
import math
from rebuild import HERE,ROOT,old,fetch,guard,utc,MAX_BYTES
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.native_ensemble_oos import _historical_transport_scope_allowed


def main():
    guard();source=json.loads((HERE/'alternative_packets.json').read_text());c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');rows=[];profile=collections.Counter();used=collections.Counter()
    for r in source['rows']:
        if 'candidate' not in r:continue
        path=ROOT/r['candidate']['path']
        with gzip.open(path,'rb') as f:body=f.read(2*1024**2+1)
        used[r['station']]+=len(body)
        if len(body)>2*1024**2 or used[r['station']]>MAX_BYTES:raise RuntimeError('alternate_member_input_limit')
        q=json.loads(body)
        if content_sha256({k:v for k,v in q.items() if k!='snapshot_content_sha256'})!=r['candidate']['sha']:raise ValueError('alternate_packet_frozen_identity_changed')
        sources={x['ensemble_system_id']:x for x in q['source_archives']};proofs={};families=set()
        for system in q['systems']:
            sid=system['ensemble_system_id'];groups=collections.defaultdict(list)
            sql='select extraction_version,transport_provider_id,archive_lead_bucket,run_cycle,member_id,raw_tmax_c,archive_query_at_utc,source_object_sha256,content_sha256,qc_status,authority_scope from calibration_native_ensemble_historical_member_run INDEXED BY calibration_native_ensemble_historical_member_run_scope_idx where station_id=? and target_local_date=? and ensemble_system_id=? and native_run_init_utc=?'
            consumed=0
            for x in fetch(c,sql,(r['station'],r['day'],sid,system['run_init_utc'])):
                b=json.dumps(dict(x)).encode();consumed+=len(b)
                if consumed>2*1024**2:raise RuntimeError('alternate_member_scope_input_limit')
                groups[(x['extraction_version'],x['transport_provider_id'],x['archive_lead_bucket'],x['run_cycle'])].append(dict(x))
            matches=[];expected={m['member_id']:m['raw_tmax_c'] for m in system['members']}
            for (extract,transport,lead,cycle),members in groups.items():
                if not _historical_transport_scope_allowed(system_id=sid,transport_provider_id=transport,lead_bucket=lead,run_cycle=cycle,extraction_version=extract):continue
                if cycle!=system['run_cycle'] or transport!=sources[sid]['transport_provider_id'] or len(members)!=len(expected) or len({x['member_id'] for x in members})!=len(members):continue
                if any(x['qc_status']!='ACCEPTED' or x['authority_scope']!='CALIBRATION_TRAIN_ONLY' or not x['source_object_sha256'] or not math.isfinite(x['raw_tmax_c']) for x in members):continue
                if {x['member_id']:x['raw_tmax_c'] for x in members}!=expected:continue
                arrival=max(utc(x['archive_query_at_utc']) for x in members)
                if arrival>utc(q['received_at_utc']) or arrival>utc(r['original_parent_available_at']):continue
                matches.append(dict(extraction=extract,transport=transport,archive_lead=lead,cycle=cycle,actual_arrival=arrival.isoformat(),member_content_shas=sorted(x['content_sha256'] for x in members),source_object_shas=sorted({x['source_object_sha256'] for x in members})))
            proofs[sid]=dict(exact_match_count=len(matches),matches=matches)
            if len(matches)==1:families.add(system['independence_family_id'])
        state='TWO_FAMILY_EXACT_SOURCE_CANDIDATE' if len(families)>=2 else 'REFUSED_EXACT_MEMBER_BINDING';profile[state]+=1
        rows.append(dict(station=r['station'],day=r['day'],original_checkpoint=r['original_checkpoint'],candidate_sha=r['candidate']['sha'],state=state,exact_independent_families=sorted(families),system_matches=proofs,qualified_replacement=False));guard()
    # Per-row proofs can be large: one bounded object per station.
    dst=HERE/'alternative_member_proofs';dst.mkdir(exist_ok=True);by=collections.defaultdict(list);refs={}
    for r in rows:by[r['station']].append(r)
    for station,rs in by.items():
        body=json.dumps(rs).encode()
        if len(body)>2*1024**2:raise RuntimeError('alternate_member_proof_output_limit')
        p=dst/(station+'.json.gz')
        with gzip.open(p,'wb') as f:f.write(body)
        refs[station]=dict(path=str(p.relative_to(HERE)),sha=content_sha256(rs),rows=len(rs))
    result=dict(schema='alternate_native_packet_exact_member_bindings_v1',counts=dict(profile),station_proofs=refs,source_index_sha=old.sha_file(HERE/'alternative_packets.json'),production_modified=False,replacement_inputs_activated=0,remaining_checks=['nested exact-scope OOS','compiler geometry and parent role','full downstream causal replay'])
    (HERE/'alternative_member_verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(profile),flush=True)


if __name__=='__main__':main()
