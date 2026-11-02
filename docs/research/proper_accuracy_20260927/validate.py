"""Independent arithmetic, calibration uncertainty, temporal boundaries and sensitivity."""
import collections
import datetime as dt
import json
import math
import pathlib
import statistics
import numpy as np
from bayes_basket_target.settlement_time import hours_to_settlement, settlement_day_window

P=pathlib.Path(__file__).resolve().parent
rows=json.loads((P/'rows.json').read_text())
res=json.loads((P/'results.json').read_text())
recent=[r for r in rows if r['after_anchor']]
assert len({(r['station'],r['date']) for r in rows})==len(rows)
assert all(r['hours']>36 and r['effective_hours']>36 for r in rows)
assert all(len(r['p'])==11 for r in recent)
for r in rows:
    p=r['p'];assert abs(sum(p.values())-1)<1e-6
    # Expand the squared distance independently: sum(p^2)-2p_y+1.
    b=sum(v*v for v in p.values())-2*p[r['hit']]+1
    assert abs(b-r['brier'])<1e-9
    assert abs(-math.log(max(p[r['hit']],1e-12))-r['nll_capped'])<1e-10
assert abs(statistics.mean(r['brier'] for r in recent)-res['since_evidence_anchor']['brier']['mean'])<1e-12
recent=[r for r in recent if not r['activation_errors'] and not r['late_sources'] and r['activation_count']>=3 and r['source_clock_count']>=2]
assert abs(statistics.mean(r['brier'] for r in recent)-res['activation_clock_clean']['brier']['mean'])<1e-12

# Strict threshold and DST boundaries use the production clock in both C/F regions.
boundary=[]
for date,tz in [('2026-09-25','Asia/Seoul'),('2026-11-01','America/New_York'),('2026-03-08','America/New_York')]:
    w=settlement_day_window(date,tz)
    end=dt.datetime.fromisoformat(w['target_day_end_utc'].replace('Z','+00:00'))
    vals=[]
    for seconds in [-1,0,1]:
        at=end-dt.timedelta(hours=36)+dt.timedelta(seconds=seconds)
        h=hours_to_settlement(date,as_of_utc=at,settlement_timezone=tz)
        vals.append(h>36)
    assert vals==[True,False,False]
    boundary.append({'date':date,'timezone':tz,'before_at_after_36h':vals})

rng=np.random.default_rng(987)
cal=[]
for lo,hi in [(.3,.4),(.4,.5),(.5,.6),(.6,.7),(.4,1.000001)]:
    pairs=[(r['date'],p,int(k==r['hit'])) for r in recent for k,p in r['p'].items() if lo<=p<hi]
    days=sorted({r['date'] for r in recent})
    n=np.array([sum(d==day for d,_,_ in pairs) for day in days])
    delta=np.array([sum(y-p for d,p,y in pairs if d==day) for day in days])
    picks=rng.integers(0,len(days),(5000,len(days)))
    denom=n[picks].sum(axis=1); ok=denom>0
    ci=np.quantile(delta[picks].sum(axis=1)[ok]/denom[ok],[.025,.975]).tolist()
    cal.append({'lo':lo,'hi':min(1,hi),'n':len(pairs),'claim':statistics.mean(p for _,p,_ in pairs),
      'actual':statistics.mean(y for _,_,y in pairs),'gap_ci95_event_day_cluster':ci})

# No production re-flooring: epsilon sensitivity only, explicitly analytical.
eps={str(e):statistics.mean(-math.log(max(r['p_hit'],e)) for r in recent) for e in [1e-12,1e-6,1e-3]}
loo=[]
for day in sorted({r['date'] for r in recent}):
    rr=[r for r in recent if r['date']!=day]
    loo.append({'excluded_day':day,'brier':statistics.mean(r['brier'] for r in rr)})
out={'unique_station_day':True,'all_vector_scores_recomputed':len(rows),'boundary_checks':boundary,
  'calibration_with_cluster_ci':cal,'nll_epsilon_sensitivity_not_served_forecast':eps,
  'leave_one_day_out_brier':loo,'formal_promotion_claim':False,
  'raw_nll':'infinite: two realized outcomes had zero historical probability',
  'current_v19_first_decision_samples':sum(r['version'].endswith('_v19') for r in rows)}
(P/'validation.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps(out,ensure_ascii=False,indent=2))
