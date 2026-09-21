"""Fleet radiation coefficient on as-of radiation, the clearness form, the served spread.

Read-only.  For each station-day since 2026-08-16, the first P0 envelope of the
day's 00Z and 12Z hours gives the radiation it was served (receipt), the served
distribution spread (receipt shift_c / shift_sigma) and the pre-shift centre.
z is recomputed in the clearness form (charter 160) with the production
function over the station's own table history before the day, exactly as
serving computes it.  residual = settled truth - centre, demeaned per station.
Fit: residual ~ k * (z * spread), day-block bootstrap.
"""
import collections, json, math, random, sqlite3, statistics as st, sys
from datetime import date, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import rehydrate_lineage_documents
from bayes_basket_target.cross_consensus import forecast_clearness_shift_sigma
from bayes_basket_target.station_registry import station_coordinates

c = sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro', uri=True, timeout=5)
c.row_factory = sqlite3.Row
c.execute('pragma query_only=on')
stations = [r[0] for r in c.execute('select distinct station_id from calibration_station_market_timezone_binding') if len(r[0]) == 4]
history = collections.defaultdict(list)
for s, d, v in c.execute("select station_id,local_date,shortwave_radiation_sum from calibration_station_forecast_physics where shortwave_radiation_sum is not null"):
    history[s].append((d, float(v)))
truth = {}
for s, d, t, rev in c.execute("select station_id,local_date,official_tmax_c,revision_no from calibration_station_daily_high_truth where local_date>='2026-08-10' and official_tmax_c is not null"):
    if (s, d) not in truth or (rev or 0) > truth[(s, d)][1]:
        truth[(s, d)] = (t, rev or 0)
rows = []
for s in stations:
    try:
        lat = float(station_coordinates(s)['latitude'])
    except Exception:
        continue
    seen = set()
    for i in range(37):
        d = (date(2026, 8, 16) + timedelta(days=i)).isoformat()
        for hh in ('T00:00', 'T12:00'):
            r = c.execute("select event_date,lineage_json from basket_probability_envelope indexed by basket_probability_envelope_station_stage_recency_idx where station_id=? and stage='P0' and state='COMMITTED' and decision_at_utc>=? order by decision_at_utc asc limit 1", (s, d + hh)).fetchone()
            if not r or r['event_date'] in seen:
                continue
            l = rehydrate_lineage_documents(c, json.loads(r['lineage_json']))
            sc = l.get('synoptic_cooling_json'); sc = (json.loads(sc) if isinstance(sc, str) else sc) or {}
            if sc.get('state') != 'RESOLVED' or not sc.get('shift_sigma') or sc.get('radiation_sum') is None:
                continue
            tr = truth.get((s, r['event_date']))
            if tr is None or l.get('model_center_c') is None:
                continue
            z, receipt = forecast_clearness_shift_sigma(
                radiation_sum=float(sc['radiation_sum']), event_date=r['event_date'], latitude_deg=lat,
                station_history=[(dd, v) for dd, v in history[s] if dd < r['event_date']][-180:],
                shift_sigma_per_unit=1.0)
            if z is None:
                continue
            seen.add(r['event_date'])
            rows.append(dict(s=s, ev=r['event_date'], z=z, sig=sc['shift_c'] / sc['shift_sigma'],
                             res=tr[0] - float(l['model_center_c'])))
by = collections.defaultdict(list)
for r in rows:
    by[r['s']].append(r)
X, Y, D = [], [], []
for s, v in by.items():
    if len(v) < 8:
        continue
    m = st.mean(r['res'] for r in v)
    for r in v:
        X.append(r['z'] * r['sig']); Y.append(r['res'] - m); D.append(r['ev'])
fit = lambda ix: sum(X[i] * Y[i] for i in ix) / sum(X[i] * X[i] for i in ix)
k = fit(range(len(X)))
byday = collections.defaultdict(list)
for i, d in enumerate(D):
    byday[d].append(i)
days = sorted(byday); random.seed(1)
boots = sorted(fit([i for d in random.choices(days, k=len(days)) for i in byday[d]]) for _ in range(1000))
# Log-score proxy: mean squared residual after shifting by each coefficient.
def mse(coef):
    return st.mean((Y[i] - coef * X[i]) ** 2 for i in range(len(X)))
out = {'station_days': len(X), 'days': len(days), 'stations': sum(1 for v in by.values() if len(v) >= 8),
       'coefficient': k, 'ci95_day_block': [boots[25], boots[974]],
       'mse_by_coefficient': {f'{v:g}': mse(v) for v in (0.0, 0.03, 0.06, 0.09, 0.12, 0.15)}}
print(json.dumps(out, indent=1))
(Path(__file__).with_suffix('.json')).write_text(json.dumps(out, indent=1) + '\n')
