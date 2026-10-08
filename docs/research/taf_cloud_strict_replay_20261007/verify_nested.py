"""Full object/fit/truth/ladder audit of fleet nested prototypes."""
import collections
import gzip
import json
import math
from rebuild import HERE,ROOT,old,fetch,guard,utc,MAX_BYTES
from upstream import day_clock,truth_at
from recompile_nested import read_report
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL


def main():
    guard();c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');stations=sorted(json.loads((HERE/'upstream.json').read_text())['stations']);counts=collections.Counter();records={}
    for station in stations:
        path=HERE/'nested_oos'/station/'2026-10-05'/'summary.json';s=json.loads(path.read_text());sc=collections.Counter()
        if s.get('method_version')!='actual_arrival_inner_truth_ladder_v3' or 'report_header' not in s:
            records[station]=dict(state='PENDING_REPORT');counts['PENDING_REPORT']+=1;continue
        summary,report=read_report(station);tz=fetch(c,'select settlement_timezone from calibration_station_market_timezone_binding where station_id=?',(station,)).fetchone()[0];outer=utc(s['outer_cutoff']);truths=collections.defaultdict(list);used=0;param_shas=set();system_objects=[];ladders={}
        for r in fetch(c,'select * from calibration_station_daily_high_truth where station_id=? and source_lane IN '+FORMAL_TRUTH_LANES_SQL,(station,)):truths[r['local_date']].append(dict(r))
        for ref in s['objects']:
            with gzip.open(HERE/ref['path'],'rb') as f:body=f.read(2*1024**2+1)
            used+=len(body)
            if len(body)>2*1024**2 or used>MAX_BYTES:raise RuntimeError('nested_verify_station_input_limit')
            obj=json.loads(body)
            if content_sha256(obj)!=ref['sha256']:raise ValueError('nested_ref_identity_failed')
            sc['objects']+=1;sc[ref['kind']+'_objects']+=1
            if ref['kind']=='fit':
                at=utc(obj['cutoff']);rows=obj['train_manifest']
                if at>outer or len(rows)<20 or content_sha256(rows)!=obj['training_sha']:raise ValueError('nested_inner_fit_identity_failed')
                for p in rows:
                    if p['day']>=s['event'] or max(utc(p['actual_member_arrival']),utc(p['reconstructed_decision_at']),utc(p['truth_committed']),day_clock(p['day'],tz,True))>at:raise ValueError('nested_training_time_travel')
                    truth=truth_at(truths,p['day'],at,tz)
                    if truth is None or (truth['truth_id'],truth['revision_no'],truth['official_tmax_c'],truth['canonical_committed_at_utc'])!=(p['truth_id'],p['truth_revision'],p['truth_c'],p['truth_committed']):raise ValueError('nested_not_actual_inner_truth_revision')
                    sc['fit_rows']+=1
                params=obj['parameters']
                if params['fit_through_date']!=max(p['day'] for p in rows) or params['fit_case_count']!=len(rows) or params['kernel_rule']!='dress_v1':raise ValueError('nested_current_parameter_binding_failed')
                if not all(math.isfinite(v) for v in params['climate_samples_c']):raise ValueError('nested_fit_nonfinite')
                param_shas.add(content_sha256(params))
            elif ref['kind']=='system':system_objects.append(obj)
            elif ref['kind']=='ladder':
                d=obj['day'];ladder=obj['ladder'];at=utc(obj['as_of']);h=fetch(c,"select station_id,event_date,created_at_utc,decision_at_utc from basket_probability_bridge_history where history_id=? and state='COMMITTED'",(ladder['history_id'],)).fetchone()
                if h is None or (h[0],h[1])!=(station,d) or max(utc(h[2]),utc(h[3]))>at or at>outer:raise ValueError('nested_geometry_not_actually_visible')
                keys=ladder['ordered'];intervals=ladder['intervals'];widths={round(float(hi)-float(lo),9) for lo,hi in intervals.values() if lo is not None and hi is not None}
                if not widths or min(widths)<=0:raise ValueError('nested_geometry_width_failed')
                ladders[d]=set(keys)
        for obj in system_objects:
            e=obj['evaluation']
            for k in ('parameters','serving_parameters','cross_parameters'):
                if e.get(k) is not None and content_sha256(e[k]) not in param_shas:raise ValueError('nested_system_parameter_without_manifest')
            for p in e['predictions']:
                if p['event_date']>=s['event'] or utc(p['decision_at_utc'])>=day_clock(p['event_date'],tz,True) or p['holdout_grid']!='market_ladder' or set(p['probability'])!=ladders.get(p['event_date']):raise ValueError('nested_system_holdout_clock_or_ladder_failed')
                if content_sha256(p['member_run_content_shas'])!=p['member_set_sha256']:raise ValueError('nested_holdout_member_identity_failed')
                sc['system_holdout_rows']+=1
        for cross in report['cross_candidates']:
            if cross['future_event_count'] or cross['station_id']!=station:raise ValueError('nested_cross_source_scope_failed')
            sc['cross_state:'+cross['state']]+=1
            for p in cross['predictions']:
                if p['trained_through_event_date']>=p['event_date'] or p['event_date']>=s['event'] or utc(p['decision_at_utc'])>=day_clock(p['event_date'],tz,True) or set(p['probability'])!=ladders.get(p['event_date']):raise ValueError('nested_cross_clock_or_geometry_failed')
                sc['cross_holdout_rows']+=1
        records[station]=dict(state='COMPONENT_AUDIT_PASS',counts=dict(sc),summary_sha=old.sha_file(path),input_uncompressed_bytes=used);counts['COMPONENT_AUDIT_PASS']+=1;counts.update(sc);print(station,json.dumps(sc),flush=True);guard()
    out=dict(schema='fleet_nested_objects_and_inner_truth_audit_v1',passed=counts['COMPONENT_AUDIT_PASS']==len(stations),counts=dict(counts),stations=records,production_modified=False,whole_current_chain_certified=False,scope='all referenced hashes, inner fit truth revisions/clocks, current parameter bindings, real geometry availability and holdout dates')
    (HERE/'nested_verification.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
