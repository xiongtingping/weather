"""H5 against the incumbent H15, cut every way that could overturn it.

A pooled mean is where a bad parameter change hides, so the pooled figure is
reported first and then taken apart: per station, per system, per lead, in
the tail, on a held-out second half, and under the leave-the-best-N-out check
this repository applies to any ledger claim.

Production's own ``_load_cases`` and ``_fit_parameters`` do the work; the
only thing that varies between the two arms is ``BIAS_HALF_LIFE_DAYS``.
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as OOS

ARMS = (5.0, 15.0)
MIN_TRAIN = 10
WINDOW = OOS.ROLLING_TRAIN_WINDOW_DAYS
POINT = set(OOS.POINT_FORECAST_SYSTEMS)
CUT = "2026-08-14"

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=120.0,
)
stations = [
    row[0] for row in conn.execute(
        "SELECT DISTINCT station_id FROM calibration_station_registry"
        " ORDER BY station_id"
    )
]
as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
truth: dict[tuple[str, str], float] = {}
for station, day, value in conn.execute(
    "SELECT station_id, local_date, official_tmax_c"
    " FROM calibration_station_daily_high_truth t"
    " WHERE revision_no = (SELECT MAX(revision_no)"
    "   FROM calibration_station_daily_high_truth x"
    "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date)"
):
    truth[(str(station), str(day))] = float(value)

# One record per scored scope-day, carrying both arms' residuals.
records: list[dict] = []
for station in stations:
    try:
        by_scope = OOS._load_cases(conn, station_id=station, as_of_utc=as_of)
    except Exception:
        continue
    for scope, cases in by_scope.items():
        system_id = str(scope[0])
        if system_id in POINT:
            continue
        dated = []
        for case in cases:
            day = str(case.get("event_date") or "")[:10]
            actual = truth.get((station, day))
            if actual is None or not case.get("values"):
                continue
            values = [float(v) for v in case["values"]]
            dated.append((day, case, actual - sum(values) / len(values)))
        if len(dated) < MIN_TRAIN + 1:
            continue
        dated.sort(key=lambda item: item[0])
        for index, (day, case, error) in enumerate(dated):
            if not day.startswith("2026-08"):
                continue
            floor = (
                date.fromisoformat(day) - timedelta(days=WINDOW)
            ).isoformat()
            history = [item[1] for item in dated[:index] if item[0] >= floor]
            if len(history) < MIN_TRAIN:
                continue
            row = {
                "station": station, "system": system_id,
                "lead": str(scope[2]) if len(scope) > 2 else "?",
                "day": day, "n_train": len(history),
            }
            for half_life in ARMS:
                original = OOS.BIAS_HALF_LIFE_DAYS
                OOS.BIAS_HALF_LIFE_DAYS = half_life
                try:
                    fitted = OOS._fit_parameters(history)
                finally:
                    OOS.BIAS_HALF_LIFE_DAYS = original
                row[f"h{int(half_life)}"] = error - float(fitted["bias_c"])
            records.append(row)

n = len(records)
print(f"scored scope-days: {n}")


def cut(rows, label):
    if not rows:
        return
    a = [abs(r["h5"]) for r in rows]
    b = [abs(r["h15"]) for r in rows]
    delta = 100 * (mean(a) - mean(b)) / mean(b)
    print(f"  {label:30s} n={len(rows):6d}"
          f"  H15 {mean(b):.4f} -> H5 {mean(a):.4f}"
          f"  ({delta:+.1f}%)")


print("\n== 总体 ==")
cut(records, "全部")
print(f"  {'系统性偏差':30s}          "
      f"H15 {mean(r['h15'] for r in records):+.3f}"
      f" -> H5 {mean(r['h5'] for r in records):+.3f}")
rmse = lambda k: math.sqrt(mean(r[k] ** 2 for r in records))
print(f"  {'RMSE':30s}          "
      f"H15 {rmse('h15'):.4f} -> H5 {rmse('h5'):.4f}"
      f"  ({100*(rmse('h5')-rmse('h15'))/rmse('h15'):+.1f}%)")
for q in (0.90, 0.95, 0.99):
    a = sorted(abs(r["h5"]) for r in records)
    b = sorted(abs(r["h15"]) for r in records)
    i = int(q * len(records))
    print(f"  {'尾部 p'+str(int(q*100)):30s}          "
          f"H15 {b[i]:.3f} -> H5 {a[i]:.3f}"
          f"  ({100*(a[i]-b[i])/b[i]:+.1f}%)")

print("\n== 时间切分 ==")
cut([r for r in records if r["day"] < CUT], f"前半段 (< {CUT})")
cut([r for r in records if r["day"] >= CUT], f"后半段 (>= {CUT}) 留出")

print("\n== 按训练样本量 ==")
cut([r for r in records if r["n_train"] < 20], "薄 (<20 天)")
cut([r for r in records if 20 <= r["n_train"] < 35], "中 (20-34)")
cut([r for r in records if r["n_train"] >= 35], "厚 (>=35)")

print("\n== 按系统 ==")
for system in sorted({r["system"] for r in records}):
    cut([r for r in records if r["system"] == system], system)

print("\n== 按 lead ==")
for lead in sorted({r["lead"] for r in records}):
    cut([r for r in records if r["lead"] == lead], lead)

print("\n== 分站输赢 ==")
per_station = defaultdict(list)
for r in records:
    per_station[r["station"]].append(r)
gains = []
for station, rows in per_station.items():
    a = mean(abs(r["h5"]) for r in rows)
    b = mean(abs(r["h15"]) for r in rows)
    gains.append((b - a, station, b, a, len(rows)))
gains.sort(reverse=True)
win = sum(1 for g, *_ in gains if g > 0)
print(f"  H5 更好的站: {win}/{len(gains)}"
      f"   中位改善 {sorted(g for g,*_ in gains)[len(gains)//2]:+.4f}C")
print("  最大受益 5 站:")
for g, s, b, a, k in gains[:5]:
    print(f"    {s:6s} {b:.3f} -> {a:.3f}  ({100*(a-b)/b:+.1f}%)  n={k}")
print("  最大受损 5 站:")
for g, s, b, a, k in gains[-5:]:
    print(f"    {s:6s} {b:.3f} -> {a:.3f}  ({100*(a-b)/b:+.1f}%)  n={k}")

print("\n== 剔除检验：去掉获益最大的 N 个站后还赢吗 ==")
for drop in (0, 1, 2, 3, 5):
    kept = {s for _g, s, _b, _a, _k in gains[drop:]}
    rows = [r for r in records if r["station"] in kept]
    a = mean(abs(r["h5"]) for r in rows)
    b = mean(abs(r["h15"]) for r in rows)
    print(f"  去掉最好的 {drop} 站: n={len(rows):6d}"
          f"  H15 {b:.4f} -> H5 {a:.4f}  ({100*(a-b)/b:+.1f}%)")
