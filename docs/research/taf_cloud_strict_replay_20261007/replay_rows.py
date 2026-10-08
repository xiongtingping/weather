"""Build full-prefix offline fit rows from refitted P0 and raw TAF receipts.

The fixed historical hourly anchors are replayed when their output actually
became visible. Formal temperature prefixes obey the user-supplied :00
METAR/SPECI contract; old provider-minute behavior is not relabelled as
current authority. US ASOS authority is not guessed from archived carriers.
"""
import argparse
import collections
import gzip
import hashlib
import json
import re
import sys
from datetime import timedelta
from zoneinfo import ZoneInfo
from rebuild import HERE,ROOT,ORIGINAL,old,fetch,guard,utc,stamp,MAX_BYTES,sources,choose
from raw_cloud import day_time
from upstream import day_clock,truth_at
from bayes_basket_target.contracts import ProbabilityEnvelope,content_sha256,ContractError
from bayes_basket_target.p1_compiler import _bucket_geometry_c
from bayes_basket_target.metar_path_state import compile_conditional_metar_path_state
from bayes_basket_target.metar_four_layer import curve_points,curve_shape_realized,recent_curve_bias
from bayes_basket_target.p1_observation_likelihood import feature_vector
from bayes_basket_target.observed_floor_censoring import p1_root_probabilities
from bayes_basket_target.regional_features import load_runs_for_station_day,regional_features_at
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from historical_sources import HistoricalAsos,regional_rows,regional_at

US_ASOS={'KATL','KAUS','KBKF','KDAL','KHOU','KLAX','KLGA','KMIA','KORD','KSEA','KSFO'}


def tx_for_day(raw,issue,day,tz,lo,hi):
    found=[]
    for token in raw.split(' RMK ')[0].replace('=','').split():
        if not token.startswith('TX'):continue
        m=re.fullmatch(r'TX(M?\d{2})/(\d{2})(\d{2})Z',token)
        if not m:raise ValueError('raw_tx_grammar_unsupported')
        temp,dd,hh=m.groups();at=day_time(dd,hh,0,utc(issue))
        if not utc(lo)<=at<utc(hi):raise ValueError('raw_tx_time_outside_whole_validity')
        if at.astimezone(ZoneInfo(tz)).date().isoformat()==day:found.append(-int(temp[1:]) if temp.startswith('M') else int(temp))
    if len(set(found))>1:raise ValueError('ambiguous_raw_tx_same_event_day')
    return found[0] if found else None


def prefix_rows(a,station,day,tz):
    start=day_clock(day,tz);end=day_clock(day,tz,True);out=[];used=0
    sql='select o.*,r.origin_source_id,r.origin_transport,r.origin_authority,r.origin_url,r.committed_at as origin_committed_at from reported_trajectory_observations o left join reported_trajectory_observation_origins r on r.observation_id=o.observation_id and r.source_lane=o.source_lane and r.station_id=o.station_id where o.station_id=? and o.source_lane=? and o.valid_at>=? and o.valid_at<?'
    for rr in fetch(a,sql,(station,'awc',start.date().isoformat(),(end.date()+timedelta(days=1)).isoformat())):
        r=dict(rr);used+=len(json.dumps(r).encode())
        if used>MAX_BYTES:raise RuntimeError('prefix_input_limit')
        if not start<=utc(r['valid_at'])<end:continue
        r['_valid']=utc(r['valid_at']);out.append(r)
    return out,used


def visible_prefix(rows,at):
    slot={};rejected=collections.Counter()
    for r in rows:
        if not r['canonical_committed_at'] or not r['first_seen_at'] or r['_valid']>at or utc(r['canonical_committed_at'])>at or utc(r['first_seen_at'])>at:continue
        if r['correction_suspected']:raise ValueError('correction_visible_requires_manual_review')
        if r['report_type']=='METAR' and (r['_valid'].minute!=0 or r['_valid'].second!=0):rejected['not_official_00_metar']+=1;continue
        if r['report_type'] not in ('METAR','SPECI'):continue
        if r['reported_temperature_c'] is None:continue
        if not r['origin_committed_at'] or utc(r['origin_committed_at'])>at:raise ValueError('prefix_origin_not_visible')
        if r['origin_source_id'] not in ('global_noaa_awc_relay','us_noaa_awc','global_noaa_tgftp_relay') or not str(r['origin_transport']).startswith(('official_awc_','official_noaa_tgftp_')):raise ValueError('prefix_not_official_origin')
        if hashlib.sha256(r['raw_text'].encode()).hexdigest()!=r['raw_sha256']:raise ValueError('prefix_raw_sha_failed')
        key=(r['_valid'],r['report_type']);oldrow=slot.get(key)
        if oldrow is None or (utc(r['canonical_committed_at']),r['observation_id'])>(utc(oldrow['canonical_committed_at']),oldrow['observation_id']):slot[key]=r
    return sorted(slot.values(),key=lambda r:(r['_valid'],r['observation_id'])),dict(rejected)


def sample_for(p,root,rows,at,tz,regional=()):
    prefix,rejected=visible_prefix(rows,at)
    if not prefix:raise ValueError('current_formal_prefix_missing')
    high=max(r['reported_temperature_c'] for r in prefix)
    centers,uppers=_bucket_geometry_c(root,unit='C',upper_by_bucket={})
    probs=p1_root_probabilities(root)
    state_rows=[dict(observation_id=r['observation_id'],observed_at_utc=r['valid_at'],temperature_c=float(r['reported_temperature_c']),raw_text=r['raw_text'],source_lane='awc') for r in prefix]
    try:state=compile_conditional_metar_path_state(state_rows,as_of_utc=stamp(at),settlement_timezone=tz,root_probabilities=probs,bucket_centers_c=centers)
    except ContractError:state=None
    if state is not None and not state.complete:state=None
    def v(k):return None if state is None else getattr(state,k)
    points=curve_points(root.lineage.get('conditional_future_temperature_curve_json'));bias,_=recent_curve_bias(points,[(r['_valid'],float(r['reported_temperature_c'])) for r in prefix],as_of=at)
    rf,rreceipt=regional_at(regional,p['station'],at,high,prefix[-1]['reported_temperature_c'])
    features=feature_vector(remaining_hours=max(0,(day_clock(p['day'],tz,True)-at).total_seconds()/3600),drawdown_from_high_c=v('drawdown_from_high_c'),momentum_c_per_hour=v('momentum_c_per_hour'),cloud_cover_fraction=v('cloud_cover_fraction'),precipitation_present=v('precipitation_present'),dewpoint_depression_c=v('dewpoint_depression_c'),wind_speed_mps=v('wind_speed_mps'),warming_so_far_c=float(high-prefix[0]['reported_temperature_c']),curve_shape_realized=curve_shape_realized(points,at),root_bias_recent_c=bias,regional_headroom_c=rf['regional_headroom_c'],regional_live_gap_c=rf['regional_live_gap_c'])
    intervals=json.loads(root.lineage['bucket_intervals_c_json'])
    running=[b for b,(lo,hi) in intervals.items() if (lo is None or high>=lo) and (hi is None or high<hi)]
    if len(running)!=1:raise ValueError('running_high_not_uniquely_mapped')
    excluded=[b for b,upper in uppers.items() if upper is not None and upper<=high]
    return dict(features=features,high=float(high),running=running[0],excluded=excluded,view=dict(probabilities=probs,ordinals=root.bucket_ordinals,centers=centers,uppers=uppers,intervals=intervals),prefix_ids=[r['observation_id'] for r in prefix],prefix_manifest=[dict(id=r['observation_id'],raw_sha=r['raw_sha256'],valid_at=r['valid_at'],first_seen=r['first_seen_at'],committed=r['canonical_committed_at'],origin_committed=r['origin_committed_at']) for r in prefix],physical_state_complete=state is not None,rejected=rejected,regional_features=rf,regional_receipt=rreceipt)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--stations',nargs='*');ap.add_argument('--baseline',action='store_true');args=ap.parse_args();guard()
    c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');a=old.connect(ROOT/'data/archive.db');tafs,sp,qa,ex=sources(c)
    _,bindings=old.native_sources(c);by=collections.defaultdict(list);available={};used=0
    with gzip.open(ORIGINAL/'audited_rows.jsonl.gz','rt') as f:
        for ln in f:
            used+=len(ln.encode())
            if used>MAX_BYTES:raise RuntimeError('input_rows_limit')
            r=json.loads(ln);e=fetch(c,'select created_at from basket_probability_envelope where envelope_id=?',(r['envelope_id'],)).fetchone()
            if e is None:raise ValueError('p1_actual_commit_receipt_missing')
            available[(r['station'],r['day'],r['decided_at'],r['observation_id'])]=max(utc(r['decided_at']),utc(e[0]))
    used=0
    with gzip.open(HERE/'raw_pairs.jsonl.gz','rt') as f:
        for ln in f:
            used+=len(ln.encode())
            if used>MAX_BYTES:raise RuntimeError('pairs_input_limit')
            p=json.loads(ln)
            if not args.stations or p['station'] in args.stations:by[p['station']].append(p)
    inputdir=HERE/('p0_roots' if args.baseline else 'p0_refitted_roots');outdir=HERE/('prefix_baseline_rows' if args.baseline else 'prefix_refitted_rows');outdir.mkdir(exist_ok=True);profile=collections.Counter();stationstats={}
    for st,ps in sorted(by.items()):
        path=inputdir/(st+'.jsonl.gz')
        if not path.exists():raise ValueError('unfinished_p0_station:'+st)
        roots={};used=0
        with gzip.open(path,'rt') as f:
            for ln in f:
                used+=len(ln.encode())
                if used>MAX_BYTES:raise RuntimeError('station_roots_input_limit')
                r=json.loads(ln);roots[r['original_checkpoint']]=r
        tz=fetch(c,'select settlement_timezone from calibration_station_market_timezone_binding where station_id=?',(st,)).fetchone()[0];truths=collections.defaultdict(list)
        for rr in fetch(c,'select * from calibration_station_daily_high_truth where station_id=? and source_lane IN '+FORMAL_TRUTH_LANES_SQL,(st,)):truths[rr['local_date']].append(dict(rr))
        ocache={};rcache={};regionalbytes=0;regionalprofile=collections.Counter();sourceprofile=collections.Counter();asos=HistoricalAsos(a,st,tz) if st in US_ASOS else None
        sc=collections.Counter();written=0;inputbytes=0;dst=outdir/(st+'.jsonl.gz');tmp=dst.with_suffix('.tmp.gz')
        with gzip.open(tmp,'wt') as out:
            for p in ps:
                at=available[(st,p['day'],p['decided_at'],p['observation_id'])];q=roots.get(p['chain_audit']['p0_checkpoint']);r=dict(station=st,day=p['day'],anchor_decided_at=p['decided_at'],as_of=stamp(at),observation_id=p['observation_id'],cloud_observation_valid_at=p['observation_valid_at'])
                authority=asos.at(p['day'],at)[0] if asos else dict(selected_lane='AWC',priority=100,source_policy='non_ASOS_official_AWC')
                r['observation_authority']=authority;sourceprofile['selected:'+authority['selected_lane']]+=1
                for lane,cv in authority.get('candidates',{}).items():
                    sourceprofile[lane+':coverage_'+str(cv['complete'])]+=1
                    sourceprofile[lane+':qualification_'+str(cv['qualification_complete'])]+=1
                try:
                    if q is None or 'output' not in q:raise ValueError('p0_rebuild_unavailable')
                    if utc(q['parent_committed_at'])>at:raise ValueError('p0_parent_not_visible')
                    if authority['selected_lane']!='AWC':raise ValueError('qualified_asos_probability_sample_adapter_pending')
                    if at>=day_clock(p['day'],tz,True):raise ValueError('anchor_committed_after_local_day')
                    root=ProbabilityEnvelope.from_mapping(q['output']['packet']['native_probability_envelope'])
                    if p['day'] not in ocache:
                        ocache[p['day']],n=prefix_rows(a,st,p['day'],tz);inputbytes+=n
                        if inputbytes>MAX_BYTES:raise RuntimeError('station_prefix_input_limit')
                    if p['day'] not in rcache:
                        rcache[p['day']],rp,n=regional_rows(c,st,p['day'],tz);regionalprofile.update(rp);regionalbytes+=n
                        if regionalbytes>MAX_BYTES:raise RuntimeError('station_regional_input_limit')
                    s=sample_for(p,root,ocache[p['day']],at,tz,rcache[p['day']]);t=choose(tafs[st],at,at,'latest');forecast=None
                    if t:
                        try:
                            d=t['decoded'];tx=tx_for_day(t['raw'],d['issue'],p['day'],tz,d['valid_from'],d['valid_to'])
                            caps=[x for x in bindings.get((st,t['sha']),[]) if x['event_date']==p['day'] and utc(x['received_at_utc'])<=at and utc(x['provider_available_at_utc'])<=at and x['source_class']=='official_forward_native_receipt']
                            caps=[x for x in caps if (tx is None and not x['tx_available']) or (tx is not None and x['tx_available'] and float(x['tx_c'])==tx)]
                            if tx is not None and not caps:raise ValueError('raw_tx_day_capture_disagrees')
                            if caps:
                                cap=max(caps,key=lambda x:utc(x['received_at_utc']));forecast=dict(raw_sha=t['sha'],raw=t['raw'],received_at=stamp(t['known_at']),revision=cap['revision_id'],tx_c=tx,cloud_cover=cap['cloud_cover_fraction'],precip_prob=cap['precipitation_probability'],capture_id=cap['taf_capture_id'])
                        except ValueError as exc:r['p0m_evidence_refusal']=str(exc)
                    end=day_clock(p['day'],tz,True);labels=[]
                    for tr in truths[p['day']]:
                        if tr['qc_status']=='ACCEPTED' and tr['timestamps_complete']==1 and tr['authority_scope'] in ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE'):
                            labels.append(dict(known_at=stamp(max(end,utc(tr['canonical_committed_at_utc']))),truth_c=tr['official_tmax_c'],id=tr['truth_id'],revision=tr['revision_no'],lane=tr['source_lane']))
                    if not labels:raise ValueError('formal_truth_revision_missing')
                    r.update(state='OFFLINE_PREFIX_REBUILT_NOT_PROMOTED',tz=tz,sample=s,p0=root.as_dict() if hasattr(root,'as_dict') else q['output']['packet']['native_probability_envelope'],forecast=forecast,truth_revisions=labels,cloud_pairs={m:p[m] for m in ('latest','day1')},p0_refit_receipts=q.get('current_parameter_refits'),p0_parameter_audit=q['parameter_audit'],parent_packet_sha=q['packet_sha'])
                except (ValueError,ContractError) as exc:r.update(state='REFUSED',reason=str(exc))
                b=json.dumps(r,ensure_ascii=False)+'\n';written+=len(b.encode())
                if len(b.encode())>2*1024**2 or written>MAX_BYTES:raise RuntimeError('station_replay_output_limit')
                out.write(b);sc[r['state']]+=1
                if r.get('reason'):sc[r['reason']]+=1
                guard()
        tmp.replace(dst);stationstats[st]=dict(counts=dict(sc),source_arbitration=dict(sourceprofile),regional_profile=dict(regionalprofile),regional_input_bytes=regionalbytes,output_bytes=written,prefix_input_bytes=inputbytes,sha256=old.sha_file(dst));profile.update(sc);print(st,json.dumps(stationstats[st]),flush=True)
    report=dict(schema='strict_prefix_current_parameter_research_rows_v1',counts=dict(profile),stations=stationstats,production_modified=False,whole_chain_current_authority_certified=False,
                exclusions=['Existing ASOS artifacts lack exact-source current P1 binding; actual historical coverage evaluated, AWC fallback evaluated','Unresolved corrections and unsupported raw TX grammar fail closed'],grain='fixed_hourly_old_service_anchors_at_actual_output_availability',source_profile=sp)
    (HERE/('prefix_baseline_rows.json' if args.baseline else 'prefix_refitted_rows.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
