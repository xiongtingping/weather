"""Current-fit distributions on actual ladders for causal fusion refitting.

These facts retain the old parent's seating qualification as a limitation;
they cannot certify the replacement full chain on their own.
"""
import collections
import gzip
import json
import math
from rebuild import HERE,ROOT,old,fetch,guard,utc,stamp,MAX_BYTES
from upstream import SYSTEMS,day_clock
from bayes_basket_target import fusion_blend as FB
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL


def horizon_bin(at,day,tz):
    hours=(day_clock(day,tz,True)-utc(at)).total_seconds()/3600
    if hours<=0:raise ValueError('fusion_fact_outside_preclose_horizon')
    hits=[int(lo) for lo,hi in FB.HORIZON_BINS_H if lo<=hours<hi]
    if len(hits)!=1:raise ValueError('fusion_fact_outside_preclose_horizon')
    return hits[0]


def project(r):
    root=r['output']['packet']['native_probability_envelope'];L=root['lineage'];at=r['parent_committed_at'];day=r['day'];tz=r['output']['packet']['settlement_timezone']
    systems=json.loads(L['native_system_probabilities_json']);seat=json.loads(L['fusion_blend_json'])['seat_weights'];intervals=json.loads(L['bucket_intervals_c_json'])
    if not set(systems)<=set(SYSTEMS) or set(systems)!=set(seat):raise ValueError('fusion_exact_seated_system_scope_failed')
    for sid,p in systems.items():
        if set(p)!=set(intervals) or any(not math.isfinite(v) or v<0 for v in p.values()) or abs(sum(p.values())-1)>1e-6:raise ValueError('fusion_system_probability_invalid')
    if any(not math.isfinite(v) or v<0 for v in seat.values()) or abs(sum(seat.values())-1)>1e-6:raise ValueError('fusion_seat_probability_invalid')
    return dict(station_id=r['station'],event_date=day,horizon_bin=horizon_bin(at,day,tz),decision_at_utc=at,settlement_timezone=tz,checkpoint=r['original_checkpoint'],parent_packet_sha=r['packet_sha'],root_probability_sha=content_sha256(root),system_probabilities=systems,seat_weights=seat,intervals=intervals,refit_manifest_refs={sid:{k:ref[k] for k in ('path','content_sha256','as_of')} for sid,ref in r['current_parameter_refits'].items()},source_role='current_single_system_override_old_seating_not_certified_parent')


def main():
    guard();c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');profile=collections.Counter();stationstats={};written=0
    dst=HERE/'fusion_facts.jsonl.gz';tmp=dst.with_suffix('.tmp.gz')
    with gzip.open(tmp,'wt') as out:
        for path in sorted((HERE/'p0_refitted_roots').glob('*.jsonl.gz')):
            station=path.name.split('.')[0];best={};used=0;sc=collections.Counter();truths=collections.defaultdict(list)
            for tr in fetch(c,'select * from calibration_station_daily_high_truth where station_id=? and source_lane IN '+FORMAL_TRUTH_LANES_SQL,(station,)):truths[tr['local_date']].append(dict(tr))
            for line in gzip.open(path,'rt'):
                used+=len(line.encode());sc['roots']+=1
                if used>MAX_BYTES or len(line.encode())>2*1024**2:raise RuntimeError('fusion_root_input_limit')
                r=json.loads(line)
                if r['state']=='REFUSED':continue
                try:p=project(r)
                except ValueError as e:sc['refused:'+str(e)]+=1;continue
                key=(p['event_date'],p['horizon_bin']);prior=best.get(key)
                if prior is None or utc(p['decision_at_utc'])>utc(prior['decision_at_utc']):best[key]=p
            for key,p in sorted(best.items()):
                day=p['event_date'];tz=p['settlement_timezone'];revisions=[]
                for tr in truths[day]:
                    if tr['qc_status']!='ACCEPTED' or tr['timestamps_complete']!=1 or tr['authority_scope'] not in ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE'):continue
                    if tr['settlement_timezone']!=tz:raise ValueError('fusion_truth_timezone_mismatch')
                    keys,points=FB.representative_points(p['intervals']);index=FB.truth_bucket_index(p['intervals'],keys,float(tr['official_tmax_c']));bucket=keys[index]
                    revisions.append(dict(truth_revision=tr['revision_no'],truth_id=tr['truth_id'],truth_lane=tr['source_lane'],truth_c=tr['official_tmax_c'],truth_bucket=bucket,truth_probability={sid:probs[bucket] for sid,probs in p['system_probabilities'].items()},truth_known_at_utc=stamp(max(day_clock(day,tz,True),utc(tr['canonical_committed_at_utc'])))))
                if not revisions:sc['refused:truth_missing']+=1;continue
                revisions.sort(key=lambda x:x['truth_revision'])
                if len({x['truth_revision'] for x in revisions})!=len(revisions):raise ValueError('fusion_ambiguous_truth_revision')
                p['truth_revisions']=revisions;p['content_sha256']=content_sha256(p);body=json.dumps(p,ensure_ascii=False)+'\n';written+=len(body.encode())
                if len(body.encode())>2*1024**2 or written>MAX_BYTES:raise RuntimeError('fusion_fact_output_limit')
                out.write(body);sc['representative_facts']+=1;sc['truth_revisions']+=len(revisions)
            stationstats[station]=dict(counts=dict(sc),input_sha=old.sha_file(path));profile.update(sc);guard();print(station,json.dumps(sc),flush=True)
    tmp.replace(dst);result=dict(schema='current_fit_actual_ladder_fusion_facts_v1',counts=dict(profile),stations=stationstats,output_bytes=written,output_sha=old.sha_file(dst),production_modified=False,full_parent_chain_certified=False,limitations=['seating still uses original qualifications','facts available only on fixed old hourly-anchor roots','fusion must be refitted using newest truth revision visible at each fit cutoff'])
    (HERE/'fusion_facts.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
