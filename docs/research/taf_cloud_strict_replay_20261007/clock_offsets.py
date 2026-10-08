"""Current peak-clock formula on closed, already-visible formal prefixes.

The eligible population is explicitly recorded. Missing US authority is
never silently filled with AWC's old provider-minute observations.
"""
import collections
import statistics
from datetime import timedelta
from rebuild import HERE,ROOT,old,fetch,utc,stamp,guard
from replay_rows import prefix_rows,visible_prefix,US_ASOS
from upstream import day_clock
from bayes_basket_target.p1_observation_likelihood import MAX_STATION_CLOCK_OFFSET_H
from scripts.fit_p1_observation_likelihood import STATION_PEAK_MIN_REPORTS,STATION_PEAK_SHRINK_K,STATION_PEAK_WINDOW_DAYS


class ClosedClocks:
    def __init__(self):
        self.a=old.connect(ROOT/'data/archive.db');self.memo={}

    def fit(self,rows,labels,day,cut):
        selected={}
        for r,lab in zip(rows,labels):selected.setdefault((r['station'],r['day']),(r,lab))
        peaks=collections.defaultdict(list);proof=[];rejected=collections.Counter();phasebytes=0
        floor=(__import__('datetime').date.fromisoformat(day)-timedelta(days=STATION_PEAK_WINDOW_DAYS)).isoformat()
        for (st,d),(r,lab) in sorted(selected.items()):
            if not floor<=d<day or day_clock(d,r['tz'],True)>cut:continue
            if r.get('observation_authority',{}).get('selected_lane')!='AWC':raise ValueError('clock_source_arbitration_required')
            if (st,d) not in self.memo:
                values,used=prefix_rows(self.a,st,d,r['tz']);phasebytes+=used
                if phasebytes>128*1024**2:raise RuntimeError('clock_source_phase_limit')
                self.memo[(st,d)]=values
            try:prefix,_=visible_prefix(self.memo[(st,d)],cut)
            except ValueError as e:rejected[str(e)]+=1;continue
            if len(prefix)<STATION_PEAK_MIN_REPORTS:rejected['too_few_reports']+=1;continue
            high=max(x['reported_temperature_c'] for x in prefix)
            if abs(high-lab['truth_c'])>0.05:rejected['formal_prefix_peak_not_equal_truth']+=1;continue
            first=min(x['_valid'] for x in prefix if x['reported_temperature_c']==high).astimezone(__import__('zoneinfo').ZoneInfo(r['tz']));hour=first.hour+first.minute/60
            peaks[st].append(hour);proof.append(dict(station=st,day=d,truth_id=lab['id'],truth_revision=lab['revision'],peak_hour=hour,report_ids=[x['observation_id'] for x in prefix]))
        hours=[h for hs in peaks.values() for h in hs];offsets={};fleet=statistics.median(hours) if hours else None
        if fleet is not None:
            for st in sorted({r['station'] for r in rows}):
                hs=peaks.get(st,[]);median=statistics.median(hs) if hs else fleet
                shrunk=(len(hs)*median+STATION_PEAK_SHRINK_K*fleet)/(len(hs)+STATION_PEAK_SHRINK_K)
                offsets[st]=round(max(-MAX_STATION_CLOCK_OFFSET_H,min(MAX_STATION_CLOCK_OFFSET_H,shrunk-fleet)),4)
        guard();return offsets,dict(schema='closed_formal_peak_clock_research_v1',cutoff=stamp(cut),before_day=day,fleet_peak_hour=fleet,eligible_station_days=len(proof),rejected=dict(rejected),train_manifest=proof,population='authorized_formal_prefixes_only_not_full_fleet')
