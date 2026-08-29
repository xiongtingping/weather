"""What H5 actually changes: the weight profile, and the fitted bias itself.

Three things get conflated when a half-life shortens, so they are separated
here: how often the fit runs (unchanged), how far back it reads (unchanged),
and how fast the weight inside that reach decays (this is the change).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as OOS

WINDOW = OOS.ROLLING_TRAIN_WINDOW_DAYS

print("=== 1. 权重形状：60 天窗内，某一天的样本占多少权重 ===")
print("  距今天数    H15 权重     H5 权重")
for age in (0, 3, 5, 10, 15, 20, 30, 45, 60):
    print(f"  {age:>4d} 天     {0.5**(age/15.0):8.4f}   {0.5**(age/5.0):8.4f}")

for half_life in (15.0, 5.0):
    weights = [0.5 ** (age / half_life) for age in range(WINDOW + 1)]
    total = sum(weights)
    n_eff = total ** 2 / sum(w * w for w in weights)
    # How many of the most recent days carry half of all the weight.
    running = 0.0
    half_span = 0
    for age, w in enumerate(weights):
        running += w
        if running >= total / 2:
            half_span = age
            break
    print(f"\n  H{int(half_life)}: 满窗有效样本数 (Kish) = {n_eff:.1f} 天;"
          f" 最近 {half_span} 天承担一半权重")

print("\n=== 2. 同一批 scope，两种半衰期各拟出什么 bias_c ===")
conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=120.0,
)
stations = [
    r[0] for r in conn.execute(
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

POINT = set(OOS.POINT_FORECAST_SYSTEMS)
TARGET = "2026-08-26"
rows: list[dict] = []
for station in stations:
    try:
        by_scope = OOS._load_cases(conn, station_id=station, as_of_utc=as_of)
    except Exception:
        continue
    for scope, cases in by_scope.items():
        if str(scope[0]) in POINT:
            continue
        dated = []
        for case in cases:
            day = str(case.get("event_date") or "")[:10]
            actual = truth.get((station, day))
            if actual is None or not case.get("values"):
                continue
            dated.append((day, case, actual))
        if len(dated) < 11:
            continue
        dated.sort(key=lambda item: item[0])
        floor = (
            date.fromisoformat(TARGET) - timedelta(days=WINDOW)
        ).isoformat()
        history = [
            item[1] for item in dated if floor <= item[0] < TARGET
        ]
        if len(history) < 10:
            continue
        fitted = {}
        for half_life in (15.0, 5.0):
            original = OOS.BIAS_HALF_LIFE_DAYS
            OOS.BIAS_HALF_LIFE_DAYS = half_life
            try:
                fitted[half_life] = OOS._fit_parameters(history)
            finally:
                OOS.BIAS_HALF_LIFE_DAYS = original
        rows.append({
            "station": station,
            "system": str(scope[0]),
            "h15": float(fitted[15.0]["bias_c"]),
            "h5": float(fitted[5.0]["bias_c"]),
            "n_eff_15": float(fitted[15.0]["train_n_effective"]),
            "n_eff_5": float(fitted[5.0]["train_n_effective"]),
        })

print(f"  可比 scope 数 (以 {TARGET} 为决策日): {len(rows)}")
deltas = [r["h5"] - r["h15"] for r in rows]
warmer = sum(1 for d in deltas if d > 0)
print(f"  bias_c 变化: 中位 {median(deltas):+.3f}C  平均 {mean(deltas):+.3f}C")
print(f"    变暖(更大正修正) {warmer}/{len(rows)}"
      f"   变冷 {len(rows)-warmer}/{len(rows)}")
print(f"    |变化| 中位 {median([abs(d) for d in deltas]):.3f}C"
      f"   p90 {sorted(abs(d) for d in deltas)[int(0.9*len(deltas))]:.3f}C")
print(f"  有效样本数: H15 中位 {median(r['n_eff_15'] for r in rows):.1f} 天"
      f" -> H5 中位 {median(r['n_eff_5'] for r in rows):.1f} 天")

per_station = defaultdict(list)
for r in rows:
    per_station[r["station"]].append(r["h5"] - r["h15"])
print("\n  变化最大的 8 个站 (该站各系统的平均变化):")
ranked = sorted(
    ((mean(v), s, len(v)) for s, v in per_station.items()),
    key=lambda t: -abs(t[0]),
)
for delta, station, count in ranked[:8]:
    print(f"    {station:6s} {delta:+.3f}C   ({count} 个系统)")
