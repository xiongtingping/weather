"""Read-only, resource-bounded audit of Proper's first P0 forecast, >36h.

Historical observed outcomes are not automatically a strict OOS activation artifact.
Run from repository root with PYTHONPATH=. and the project Python containing numpy.
"""
import collections
import datetime as dt
import hashlib
import json
import math
import pathlib
import resource
import shutil
import sqlite3
import time

import numpy as np

from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from bayes_basket_target.probability_bridge import history_envelope_payload
from bayes_basket_target.settlement_time import hours_to_settlement

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = pathlib.Path(__file__).resolve().parent
START = time.monotonic()
NOW = dt.datetime.now(dt.timezone.utc)
ANCHOR = '2026-09-14T05:52:13Z'
resource.setrlimit(resource.RLIMIT_FSIZE, (20*1024**2, 20*1024**2))
resource.setrlimit(resource.RLIMIT_CPU, (150, 150))
def rss_bytes():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if rss <= 0:
        raise RuntimeError('RSS probe unavailable')
    return rss  # macOS reports bytes.

def instant(s):
    v = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    assert v.tzinfo is not None
    return v

def guard():
    if time.monotonic()-START > 240 or rss_bytes() > 768*1024**2:
        raise RuntimeError('resource limit')
    if shutil.disk_usage(OUT).free < 4*1024**3:
        raise RuntimeError('disk reserve limit')

guard()
c = sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro', uri=True)
c.execute('PRAGMA query_only=ON')
c.execute('PRAGMA cache_size=-8192')
c.execute('PRAGMA temp_store=MEMORY')
c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4*1024**2)
c.set_progress_handler(lambda: (guard() or 0), 10000)
bindings = dict(c.execute('select station_id,settlement_timezone from calibration_station_market_timezone_binding'))
truth = {}
query = f'''SELECT station_id,local_date,official_tmax_c,revision_no,truth_id,
 source_lane,qc_status,settlement_timezone,authority_scope,received_at_utc
 FROM calibration_station_daily_high_truth
 WHERE local_date >= '2026-08-15' AND source_lane IN {FORMAL_TRUTH_LANES_SQL}
 ORDER BY revision_no'''
for r in c.execute(query):
    truth[r[:2]] = r
counts = collections.Counter()
rows = []
for (st, day), tr in sorted(truth.items()):
    guard()
    counts['truth_station_days'] += 1
    if tr[6] != 'ACCEPTED' or st not in bindings or tr[7] != bindings[st]:
        counts['truth_or_timezone_rejected'] += 1
        continue
    if hours_to_settlement(day, as_of_utc=NOW, settlement_timezone=bindings[st]) >= 0:
        counts['day_not_closed'] += 1
        continue
    f = c.execute('''SELECT fact_id,decision_at_utc,created_at_utc,probability_envelope_id,
      settlement_timezone,posterior_version,authority_scope FROM basket_policy_training_fact
      WHERE station_id=? AND event_date=? AND stage='P0'
      ORDER BY decision_at_utc,created_at_utc,fact_id LIMIT 1''',(st,day)).fetchone()
    if not f:
        counts['no_first_p0_fact'] += 1
        continue
    counts['first_p0_facts'] += 1
    hours = hours_to_settlement(day,as_of_utc=f[1],settlement_timezone=f[4])
    if hours <= 36:
        counts['first_p0_at_or_below_36h'] += 1
        continue
    e = c.execute('''SELECT checkpoint_id,created_at,decision_at_utc,settlement_timezone,
      stage,state,station_id,event_date FROM basket_probability_envelope WHERE envelope_id=?''',(f[3],)).fetchone()
    if not e or e[3] != bindings[st] or f[4] != bindings[st] or e[4:] != ('P0','COMMITTED',st,day):
        counts['envelope_identity_rejected'] += 1
        continue
    if instant(e[2]) > instant(f[1]) or instant(e[1]) > instant(f[2]):
        counts['envelope_not_available_at_fact'] += 1
        continue
    available = max(instant(f[1]),instant(f[2]),instant(e[1]))
    effective_hours = hours_to_settlement(day,as_of_utc=available,settlement_timezone=f[4])
    if effective_hours <= 36:
        counts['commit_crossed_36h'] += 1
        continue
    hs = c.execute('''SELECT envelope_content_sha256,envelope_json,created_at_utc
      FROM basket_probability_bridge_history WHERE station_id=? AND event_date=?
      AND state='COMMITTED' AND decision_at_utc=? AND stage='P0' AND checkpoint_id=?''',
      (st,day,e[2],e[0])).fetchall()
    valid_h = [h for h in hs if instant(h[2]) <= available]
    if len({h[0] for h in valid_h}) != 1:
        counts['history_unavailable_or_ambiguous'] += 1
        continue
    h = min(valid_h,key=lambda r:instant(r[2]))
    try:
        env = history_envelope_payload(c,checkpoint_id=e[0],envelope_content_sha256=h[0],stored=h[1])
    except Exception as ex:
        counts['history_hash_rejected:'+type(ex).__name__] += 1
        continue
    lin = env['lineage']; p = env['probabilities']
    intervals = json.loads(lin['bucket_intervals_c_json'])
    if set(p) != set(intervals) or any(not math.isfinite(v) or v<0 or v>1 for v in p.values()) or abs(sum(p.values())-1)>1e-6:
        counts['vector_invalid'] += 1
        continue
    hits=[k for k,(lo,hi) in intervals.items() if (lo is None or tr[2]>=lo) and (hi is None or tr[2]<hi)]
    if len(hits)!=1:
        counts['truth_bucket_ambiguous'] += 1
        continue
    hit=hits[0]
    acts=json.loads(lin.get('system_activation_shas_json','{}'))
    acts['cross']=lin.get('native_cross_activation_sha256')
    authorities=[]; act_errors=[]
    for name,sha in acts.items():
        a=c.execute('''SELECT activated_at_utc,valid_from_utc,valid_until_utc,authority_scope,
          station_id FROM calibration_native_ensemble_activation WHERE content_sha256=?''',(sha,)).fetchone()
        if not a or a[4]!=st or instant(a[0])>instant(e[2]) or not instant(a[1])<=instant(e[2])<instant(a[2]):
            act_errors.append(name)
        if a: authorities.append(a[3])
    known_times=json.loads(lin.get('archive_query_at_utc_by_system_json','{}'))
    late_sources=[k for k,v in known_times.items() if instant(v)>instant(e[2])]
    first=max(p,key=p.get); py=p[hit]
    rows.append(dict(station=st,date=day,decision=f[1],available=available.isoformat(),hours=hours,
      effective_hours=effective_hours,fact_id=f[0],envelope_id=f[3],envelope_sha=h[0],version=env['posterior_version'],
      truth_id=tr[4],truth_revision=tr[3],truth_lane=tr[5],truth_authority=tr[8],truth_c=tr[2],
      p=p,hit=hit,p_hit=py,brier=sum((v-(k==hit))**2 for k,v in p.items()),
      nll_capped=-math.log(max(py,1e-12)),zero_hit=py==0,flat_brier=1-1/len(p),flat_nll=math.log(len(p)),
      top1_hit=first==hit,top1_probability=p[first],activation_errors=act_errors,late_sources=late_sources,
      activation_authorities=sorted(set(authorities)),activation_count=len(acts),source_clock_count=len(known_times),
      formal_eligible=lin.get('native_formal_p0_eligible'),fact_authority=f[6],
      after_anchor=instant(f[1])>=instant(ANCHOR)))
c.close()
guard()
(OUT/'rows.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))

def summarize(rr):
    if not rr:return {'n':0}
    days=sorted({r['date'] for r in rr}); rng=np.random.default_rng(20260927)
    out={'n':len(rr),'days':len(days),'stations':len({r['station'] for r in rr}),
      'date_range':[min(days),max(days)],'versions':dict(collections.Counter(r['version'] for r in rr)),
      'zero_hits':sum(r['zero_hit'] for r in rr),'raw_nll_state':'infinite' if any(r['zero_hit'] for r in rr) else 'finite',
      'p_hit_le_1pct':sum(r['p_hit']<=.01 for r in rr),
      'activation_error_rows':sum(bool(r['activation_errors']) for r in rr),
      'late_source_rows':sum(bool(r['late_sources']) for r in rr),
      'formal_eligible':dict(collections.Counter(str(r['formal_eligible']) for r in rr))}
    for field in ['brier','nll_capped','top1_hit','flat_brier','flat_nll']:
        ns=np.array([sum(r['date']==d for r in rr) for d in days])
        sums=np.array([sum(r[field] for r in rr if r['date']==d) for d in days])
        draw=rng.integers(0,len(days),(2000,len(days)))
        boot=sums[draw].sum(axis=1)/ns[draw].sum(axis=1)
        out[field]={'mean':float(sums.sum()/ns.sum()),'ci95':np.quantile(boot,[.025,.975]).tolist()}
    bins=[]
    for lo,hi in [(0,.01),(.01,.05),(.05,.1),(.1,.2),(.2,.3),(.3,.4),(.4,.5),(.5,.6),(.6,.7),(.7,1.000001)]:
        pairs=[(p,k==r['hit'],r['date']) for r in rr for k,p in r['p'].items() if lo<=p<hi]
        if not pairs:continue
        n=len(pairs); claim=sum(x[0] for x in pairs)/n; actual=sum(x[1] for x in pairs)/n
        bins.append({'lo':lo,'hi':min(1,hi),'bucket_cases':n,'event_days':len({x[2] for x in pairs}),
          'claim':claim,'actual':actual,'gap':actual-claim})
    out['reliability_bins']=bins
    out['ece']=sum(b['bucket_cases']*abs(b['gap']) for b in bins)/sum(b['bucket_cases'] for b in bins)
    out['worst_cases']=[{k:r[k] for k in ['station','date','version','hours','hit','p_hit','nll_capped']} for r in sorted(rr,key=lambda r:r['p_hit'])[:8]]
    return out

recent=[r for r in rows if r['after_anchor']]
results={'captured_at_utc':NOW.isoformat(),'counts':dict(counts),
 'classification':'closed historical served-forecast audit; not a strict-as-of OOS promotion artifact',
 'full':summarize(rows),'since_evidence_anchor':summarize(recent),
 'by_hours':{name:summarize([r for r in recent if lo<r['hours']<=hi]) for name,lo,hi in [('36-48',36,48),('48-60',48,60),('60+',60,1000)]},
 'by_version':{v:summarize([r for r in recent if r['version']==v]) for v in sorted({r['version'] for r in recent})},
 'activation_clock_clean':summarize([r for r in recent if not r['activation_errors'] and not r['late_sources'] and r['activation_count']>=3 and r['source_clock_count']>=2]),
 'rows_sha256':hashlib.sha256((OUT/'rows.json').read_bytes()).hexdigest(),
 'elapsed_sec':time.monotonic()-START,'rss_bytes':rss_bytes()}
(OUT/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in results.items() if k not in ['full','by_version','by_hours']},ensure_ascii=False,indent=2))
