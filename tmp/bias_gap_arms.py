"""H5/H15 × training gap (0 / 3 / 6 days): does the served regime (train ends ~6 days before the event) change the half-life verdict, and what does the gap itself cost?"""
from __future__ import annotations
import sys, sqlite3
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from statistics import mean
ROOT = Path("/Users/xiong/weather"); sys.path.insert(0, str(ROOT))
from bayes_basket_target import native_ensemble_oos as OOS
HL = (5.0, 15.0); GAPS = (0, 3, 6); MIN_TRAIN = 10; WINDOW = OOS.ROLLING_TRAIN_WINDOW_DAYS; POINT = set(OOS.POINT_FORECAST_SYSTEMS)
conn = sqlite3.connect(f"file:{ROOT/'data'/'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=120.0)
stations = [r[0] for r in conn.execute("SELECT DISTINCT station_id FROM calibration_station_registry ORDER BY station_id")]
truth = {}
for s, d, t in conn.execute("SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth ORDER BY revision_no"): truth[(s, d)] = float(t)
as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
records = []
for station in stations:
    try: by_scope = OOS._load_cases(conn, station_id=station, as_of_utc=as_of)
    except Exception: continue
    for scope, cases in by_scope.items():
        system_id = str(scope[0])
        if system_id in POINT: continue
        dated = []
        for case in cases:
            day = str(case.get("event_date") or "")[:10]; actual = truth.get((station, day))
            if actual is None or not case.get("values"): continue
            values = [float(v) for v in case["values"]]; dated.append((day, case, actual - sum(values)/len(values)))
        if len(dated) < MIN_TRAIN + 1: continue
        dated.sort(key=lambda x: x[0])
        for index, (day, case, error) in enumerate(dated):
            if not (day.startswith("2026-08") or day.startswith("2026-09")): continue
            floor = (date.fromisoformat(day) - timedelta(days=WINDOW)).isoformat()
            row = {"station": station, "system": system_id, "day": day}
            ok = True
            for gap in GAPS:
                cutoff = (date.fromisoformat(day) - timedelta(days=gap)).isoformat()
                history = [it[1] for it in dated[:index] if floor <= it[0] < cutoff] if gap else [it[1] for it in dated[:index] if it[0] >= floor]
                if len(history) < MIN_TRAIN: ok = False; break
                for hl in HL:
                    orig = OOS.BIAS_HALF_LIFE_DAYS; OOS.BIAS_HALF_LIFE_DAYS = hl
                    try: fitted = OOS._fit_parameters(history)
                    finally: OOS.BIAS_HALF_LIFE_DAYS = orig
                    row[f"h{int(hl)}g{gap}"] = error - float(fitted["bias_c"])
            if ok: records.append(row)
print(f"scored scope-days: {len(records)} (2026-08/09, all six arms present)")
arms = [f"h{int(h)}g{g}" for g in GAPS for h in HL]
def table(rows, label):
    print(f"\n== {label} n={len(rows)} ==  (MAE °C | RMSE | signed bias)")
    for a in arms:
        v = [r[a] for r in rows]; print(f"  {a:8s} MAE {mean(abs(x) for x in v):.4f}  RMSE {(mean(x*x for x in v))**.5:.4f}  bias {mean(v):+.3f}")
    base = mean(abs(r["h5g6"]) for r in rows)
    print(f"  vs served regime h5g6: h5g0 {100*(mean(abs(r['h5g0']) for r in rows)-base)/base:+.1f}%  h15g6 {100*(mean(abs(r['h15g6']) for r in rows)-base)/base:+.1f}%  h15g0 {100*(mean(abs(r['h15g0']) for r in rows)-base)/base:+.1f}%  h5g3 {100*(mean(abs(r['h5g3']) for r in rows)-base)/base:+.1f}%")
table(records, "全部")
table([r for r in records if r["day"] >= "2026-08-14"], "留出后半段 >= 08-14")
table([r for r in records if r["day"] >= "2026-08-29"], "账本四天 08-29..09-01")
sysn = {}
for r in records: sysn.setdefault(r["system"], []).append(r)
print("\n== by system: h5g0 vs h5g6 (MAE change from removing the gap) | h15g6 vs h5g6 ==")
for s_, rows in sorted(sysn.items()):
    b = mean(abs(r["h5g6"]) for r in rows); print(f"  {s_:28s} n={len(rows):5d}  gap0 {100*(mean(abs(r['h5g0']) for r in rows)-b)/b:+.1f}%  h15g6 {100*(mean(abs(r['h15g6']) for r in rows)-b)/b:+.1f}%")
st_ = {}
for r in records: st_.setdefault(r["station"], []).append(r)
w = sum(1 for rows in st_.values() if mean(abs(r["h5g0"]) for r in rows) < mean(abs(r["h5g6"]) for r in rows)); print(f"\nstations where gap0 beats gap6 (H5): {w}/{len(st_)}")
w2 = sum(1 for rows in st_.values() if mean(abs(r["h15g6"]) for r in rows) < mean(abs(r["h5g6"]) for r in rows)); print(f"stations where H15 beats H5 under the served gap 6: {w2}/{len(st_)}")
