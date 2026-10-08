"""Current single-system parameter fits on exact, already-known native inputs.

An offline override is a counterfactual fit, never an activation. Each fit
exports its full raw-member/truth lineage so nested OOS can bind it later.
"""
import collections
import json
from datetime import timedelta,datetime
from upstream import load_members,truth_at,SYSTEMS
from rebuild import fetch,utc,guard
from bayes_basket_target import native_ensemble_oos as OOS
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from bayes_basket_target.contracts import content_sha256


class CurrentFits:
    def __init__(self,c,station,tz):
        registry={r['ensemble_system_id']:dict(r) for r in fetch(c,'select * from calibration_native_ensemble_system') if r['ensemble_system_id'] in SYSTEMS}
        self.cases,self.source_receipt=load_members(c,station,tz,registry);self.truths=collections.defaultdict(list);self.tz=tz;self.station=station;self.memo={}
        for rr in fetch(c,'select * from calibration_station_daily_high_truth where station_id=? and source_lane IN '+FORMAL_TRUTH_LANES_SQL,(station,)):
            r=dict(rr)
            if r['settlement_timezone']!=tz:raise ValueError('truth_timezone_mismatch')
            self.truths[r['local_date']].append(r)

    def at(self,q,baseline,at):
        l=baseline['packet']['native_probability_envelope']['lineage'];leads=json.loads(l['system_calibration_lead_by_system_json']);overrides={};proof={}
        day=q['event_date'];floor=(datetime.fromisoformat(day)-timedelta(days=OOS.ROLLING_TRAIN_WINDOW_DAYS)).date().isoformat();clock=utc(at)
        cycles={s['ensemble_system_id']:s['run_cycle'] for s in q['systems']};provisional=set(json.loads(l.get('provisional_uncalibrated_system_ids_json','[]')))
        for sid,lead in leads.items():
            # A provisional rider has no own fitted artifact. Production
            # borrows its qualified peers' (now refitted) parameters; an
            # override for the rider would be ignored by the compiler.
            if sid in provisional:continue
            if sid not in cycles:raise ValueError('fit_source_system_missing')
            perday={}
            for x in self.cases:
                if (x['system'],x['lead'],x['cycle'])!=(sid,lead,cycles[sid]) or not floor<=x['day']<day or x['arrival']>clock:continue
                t=truth_at(self.truths,x['day'],clock,self.tz)
                if t is None:continue
                old=perday.get(x['day'])
                if old is None or (utc(x['run']),x['arrival'])>(utc(old[0]['run']),old[0]['arrival']):perday[x['day']]=(x,t)
            pairs=[perday[d] for d in sorted(perday)]
            if len(pairs)<20:raise ValueError('current_parameter_train_days_below_20:'+sid)
            train=[dict(event_date=x['day'],values=[x['members'][k] for k in sorted(x['members'])],actual_tmax_c=t['official_tmax_c'],transport_provider_id=x['transport']) for x,t in pairs]
            receipt=[dict(day=x['day'],run=x['run'],lead=x['lead'],cycle=x['cycle'],arrival=x['arrival'].isoformat(),member_content_shas=x['shas'],source_object_shas=x['source_shas'],truth_id=t['truth_id'],truth_revision=t['revision_no'],truth_value_c=t['official_tmax_c'],truth_committed_at=t['canonical_committed_at_utc']) for x,t in pairs]
            key=content_sha256(receipt)
            if key not in self.memo:self.memo[key]=OOS._fit_parameters(train)
            params=self.memo[key]
            # All flat fitted keys are carried in all three maps; the
            # production functions read their own required subset only.
            overrides[sid]={k:params for k in ('bias_parameters','spread_parameters','reliability_parameters')}
            proof[sid]=dict(parameters=params,train_manifest=receipt,train_manifest_sha256=key,as_of=at,scope=dict(system=sid,lead=lead,cycle=cycles[sid]),activation_role='offline_parameter_override_not_activation')
        guard();return overrides,proof
