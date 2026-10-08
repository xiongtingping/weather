"""Actual-receipt nested calibration prototype; never an activation.

Use the production builder and fitting formulas in a dedicated offline
process. Replace its retrospective run-time arrival approximation with
audited local arrival facts, and select truth revisions at each inner fit.
The remaining table/fusion provenance is explicitly uncertified.
"""
import argparse
import collections
import gzip
import json
import signal
from datetime import timedelta
from rebuild import HERE,ROOT,MAX_BYTES,old,fetch,guard,utc,stamp
from upstream import SYSTEMS,load_members,truth_at,day_clock
from bayes_basket_target import native_ensemble_oos as OOS
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from bayes_basket_target.station_season import station_season_bin
from bayes_basket_target.follow_run import live_never_seats


class ActualCases:
    def __init__(self,c,station,at,event):
        self.station=station;self.at=utc(at);self.event=event;self.proofs=[]
        self.tz=fetch(c,'select settlement_timezone from calibration_station_market_timezone_binding where station_id=?',(station,)).fetchone()[0]
        self.settlement=fetch(c,'select settlement_definition from calibration_station_registry where station_id=?',(station,)).fetchone()[0]
        self.registry={r['ensemble_system_id']:dict(r) for r in fetch(c,'select * from calibration_native_ensemble_system') if r['ensemble_system_id'] in SYSTEMS}
        raw,self.receipt=load_members(c,station,self.tz,self.registry);self.truths=collections.defaultdict(list)
        for r in fetch(c,'select * from calibration_station_daily_high_truth where station_id=? and source_lane IN '+FORMAL_TRUTH_LANES_SQL,(station,)):self.truths[r['local_date']].append(dict(r))
        by={};self.rejections=collections.Counter();self.index={}
        for x in raw:
            if x['day']>=event or x['arrival']>self.at:continue
            if live_never_seats(x['system'],event_date=x['day'],settlement_timezone=self.tz,run_init=utc(x['run'])):self.rejections['never_seated_run']+=1;continue
            lab=truth_at(self.truths,x['day'],self.at,self.tz)
            if lab is None:continue
            key=(x['system'],x['lead'],x['cycle'],x['day']);prev=by.get(key)
            if prev is None or (utc(x['run']),x['arrival'])>(utc(prev[0]['run']),prev[0]['arrival']):by[key]=(x,lab)
        self.scopes=collections.defaultdict(list);season=station_season_bin(station,self.at.astimezone(__import__('zoneinfo').ZoneInfo(self.tz)).date().isoformat())
        for key,(x,lab) in sorted(by.items()):
            preclose=x['arrival']<day_clock(x['day'],self.tz,True)
            if not preclose:self.rejections['retrospective_train_only']+=1
            case=dict(event_date=x['day'],archive_lead_bucket=x['archive_lead'],aggregator_snapshot_at_utc=stamp(x['arrival']),values=[x['members'][m] for m in sorted(x['members'])],member_run_content_shas=x['shas'],member_set_sha256=content_sha256(x['shas']),member_native_run_init_utcs=[x['run']]*len(x['members']),holdout_eligible=preclose,evidence_timing_class='OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE' if preclose else 'RETROSPECTIVE_ARCHIVE_TRAIN_ONLY',transport_provider_id=x['transport'],historical_closed_day=True,oos_event_temporal_scope=OOS.HISTORICAL_OOS_TEMPORAL_SCOPE,future_event_count=0,historical_oos_contract_ids=[],settlement_definition=self.settlement,timezone_name=self.tz,
                actual_tmax_c=lab['official_tmax_c'],truth_id=lab['truth_id'],truth_revision_no=lab['revision_no'],truth_canonical_committed_at_utc=lab['canonical_committed_at_utc'])
            scope=(x['system'],season,x['lead'],x['cycle']);self.scopes[scope].append(case);self.index[case['member_set_sha256']]=scope

    def fit(self,original,cases):
        if not cases:raise ValueError('nested_fit_empty')
        # The next withheld case sets an upper bound on the training clock.
        # Serving fits, containing each scope's final case, use the outer
        # cutoff. This also handles the builder's wider cross exposure.
        scopes={self.index[x['member_set_sha256']] for x in cases};last=max(x['event_date'] for x in cases);cut=self.at
        for scope in scopes:
            later=[utc(x['aggregator_snapshot_at_utc']) for x in self.scopes[scope] if x['event_date']>last and x['holdout_eligible']]
            if later:cut=min(cut,min(later))
        train=[];manifest=[];missing=0
        for case in cases:
            lab=truth_at(self.truths,case['event_date'],cut,self.tz)
            if lab is None or utc(case['aggregator_snapshot_at_utc'])>cut:missing+=1;continue
            r={**case,'actual_tmax_c':lab['official_tmax_c'],'truth_id':lab['truth_id'],'truth_revision_no':lab['revision_no'],'truth_canonical_committed_at_utc':lab['canonical_committed_at_utc']};train.append(r)
            manifest.append(dict(day=r['event_date'],member_set_sha=r['member_set_sha256'],actual_member_arrival=r.get('actual_member_arrival_utc',r['aggregator_snapshot_at_utc']),reconstructed_decision_at=r['aggregator_snapshot_at_utc'],truth_id=r['truth_id'],truth_revision=r['truth_revision_no'],truth_c=r['actual_tmax_c'],truth_committed=r['truth_canonical_committed_at_utc']))
        if len(train)<20:raise ValueError('nested_actual_truth_train_below_20')
        guard();params=original(train)
        self.proofs.append(dict(cutoff=stamp(cut),scopes=[list(s) for s in sorted(scopes)],missing_truth_at_inner_clock=missing,train_manifest=manifest,parameters=params,training_sha=content_sha256(manifest)))
        return params


class ReadScope:
    def __init__(self,c,station,tz):self.c=c;self.station=station;self.tz=tz
    def __getattr__(self,k):return getattr(self.c,k)
    def execute(self,sql,args=()):
        s=' '.join(sql.split())
        if s=="SELECT DISTINCT settlement_timezone FROM basket_target_market_registry WHERE station_id=? AND settlement_timezone<>''":
            if tuple(args)!=(self.station,):raise ValueError('nested_timezone_scope_mismatch')
            return fetch(self.c,'select ? as settlement_timezone',(self.tz,))
        if s.startswith('SELECT history_id, decision_at_utc FROM basket_probability_bridge_history') and 'AND decision_at_utc<=?' in s:
            # The canonical helper filters declared time. This study also
            # requires the geometry's actual commit to have occurred.
            return fetch(self.c,"SELECT history_id,decision_at_utc FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND state='COMMITTED' AND julianday(decision_at_utc)<=julianday(?) AND julianday(created_at_utc)<=julianday(?) ORDER BY decision_at_utc DESC LIMIT 1",(*args,args[2]))
        return fetch(self.c,sql,args)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--station',default='ZUUU');ap.add_argument('--day',default='2026-10-05');args=ap.parse_args();guard()
    roots=[];used=0
    with gzip.open(HERE/'p0_roots'/(args.station+'.jsonl.gz'),'rt') as inp:
        for ln in inp:
            used+=len(ln.encode())
            if used>MAX_BYTES:raise RuntimeError('nested_anchor_input_limit')
            r=json.loads(ln)
            if r['day']==args.day and 'parent_committed_at' in r:roots.append(r)
    if not roots:raise ValueError('nested_outer_anchor_missing')
    anchor=min(roots,key=lambda r:utc(r['parent_committed_at']));at=anchor['parent_committed_at'];c=old.connect(ROOT/'data/bayes_basket_target_ssot.db');cases=ActualCases(c,args.station,at,args.day);scope=ReadScope(c,args.station,cases.tz)
    original_load,original_fit,original_evaluate,original_ladder=OOS._load_cases,OOS._fit_parameters,OOS._evaluate_system,OOS._station_day_ladder
    arrivals={};first_geometry={}
    for values in cases.scopes.values():
        for r in values:
            d=r['event_date']
            if d not in first_geometry:
                first=fetch(c,"select created_at_utc,decision_at_utc from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' order by julianday(created_at_utc) limit 1",(args.station,d)).fetchone()
                first_geometry[d]=max(utc(first[0]),utc(first[1])) if first else None
            geometry_time=first_geometry[d]
            if geometry_time is not None:
                # A forecast can be evaluated when both its raw inputs and
                # the actual target ladder have arrived. This is explicitly
                # a reconstructed availability clock, not a native receipt.
                effective=max(utc(r['aggregator_snapshot_at_utc']),geometry_time)
                r['actual_member_arrival_utc']=r['aggregator_snapshot_at_utc'];r['aggregator_snapshot_at_utc']=stamp(effective)
                if effective>=day_clock(d,cases.tz,True) or effective>cases.at:r['holdout_eligible']=False
                arrivals[d]=min(arrivals.get(d,cases.at),effective)
    ladders={d:original_ladder(scope,station_id=args.station,event_date=d,as_of_utc=stamp(t)) for d,t in arrivals.items() if t<=cases.at}
    for key,values in list(cases.scopes.items()):
        for r in values:
            if ladders.get(r['event_date']) is None:
                r['holdout_eligible']=False;cases.rejections['missing_asof_market_ladder_train_only']+=1
        # Train-only raw temperature facts do not need an invented ladder.
        # Holdout scoring, including F markets, always requires real edges.
    def evaluate(**kw):
        try:return original_evaluate(**kw)
        except ValueError as e:
            if str(e)!='nested_actual_truth_train_below_20':raise
            return dict(state='ACCUMULATING_STRICT_OOS',system_id=kw['system_id'],transport_provider_id=kw['cases'][0]['transport_provider_id'],case_count=len(kw['cases']),strict_holdout_case_count=min(8,len(kw['cases'])),required_train_case_count=20,required_strict_holdout_case_count=8,reason=str(e))
    try:
        OOS._load_cases=lambda *a,**k:dict(cases.scopes)
        OOS._fit_parameters=lambda rows:cases.fit(original_fit,rows)
        OOS._evaluate_system=evaluate
        OOS._station_day_ladder=lambda *a,**k:ladders.get(k['event_date'])
        def timeout(*a):raise TimeoutError('nested_build_wall_limit')
        signal.signal(signal.SIGALRM,timeout);signal.alarm(180)
        result=OOS.build_native_ensemble_oos(scope,station_id=args.station,as_of_utc=at,tables_as_of_utc=at,max_event_date=(__import__('datetime').date.fromisoformat(args.day)-timedelta(days=1)).isoformat(),system_mask=SYSTEMS)
    finally:
        signal.alarm(0);OOS._load_cases=original_load;OOS._fit_parameters=original_fit;OOS._evaluate_system=original_evaluate;OOS._station_day_ladder=original_ladder
    out=HERE/'nested_oos'/args.station/args.day;out.mkdir(parents=True,exist_ok=True);refs=[];nbytes=0
    objects=[('fit',p) for p in cases.proofs]+[('system',p) for p in result['ready_system_evaluations']]+[('cross',p) for p in result['cross_candidates']]+[('ladder',dict(day=d,as_of=stamp(arrivals[d]),ladder=v)) for d,v in ladders.items() if v is not None]
    for kind,obj in objects:
        sha=content_sha256(obj);body=json.dumps(obj,ensure_ascii=False).encode();nbytes+=len(body)
        if len(body)>2*1024**2 or nbytes>MAX_BYTES:raise RuntimeError('nested_output_limit')
        path=out/(sha+'.json.gz')
        if not path.exists():
            with gzip.open(path,'wb') as f:f.write(body)
        refs.append(dict(kind=kind,path=str(path.relative_to(HERE)),sha256=sha))
    summary=dict(schema='actual_arrival_nested_oos_probe_v1',method_version='actual_arrival_inner_truth_ladder_v3',decision_clock='maximum of actual member arrival and actual first target ladder availability',station=args.station,outer_cutoff=at,event=args.day,anchor_checkpoint=anchor['original_checkpoint'],member_source=cases.receipt,input_rejections=dict(cases.rejections),readiness=result['readiness'],report_header={k:v for k,v in result.items() if k not in ('readiness','ready_system_evaluations','cross_candidates')},cross_states=dict(collections.Counter(x['state'] for x in result['cross_candidates'])),objects=refs,production_modified=False,current_chain_certified=False,limitations=['historical center/width table fits still require current-method reconstruction','fusion weights still require current-method historical refit','candidate reports have not been activated or used as replacement parents'])
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(dict(station=args.station,readiness_states=dict(collections.Counter(r['state'] for r in result['readiness'])),cross_states=summary['cross_states'],fits=len(cases.proofs),objects=len(refs))),flush=True)


if __name__=='__main__':main()
