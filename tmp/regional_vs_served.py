"""Compare the regional models against what production actually serves.

The first pass compared against raw member runs at one lead bucket and got
nothing back: that vocabulary is thin, and requiring twenty paired days per
system left every bar empty.  The right bar was never that anyway.  What a
new source has to beat is the distribution production serves after every
correction it already has -- bias, spread, blend, the lot -- because that is
what it would be joining.

Dispersion about the mean, not raw error: a constant offset is what
``bias_c`` removes for free on any source that gets seated, so a model that
only wins on offset has brought nothing.  A model that is tighter about its
own mean is bringing information.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.center_width_store import (  # noqa: E402
    _truth, serving_cells_by_day,
)
from bayes_basket_target.fleet_regime_shift import (  # noqa: E402
    cell_residual_c,
)

probe = json.loads(
    (ROOT / "tmp" / "regional_model_probe.json").read_text(encoding="utf-8")
)
conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=120.0,
)
truth = _truth(conn)
by_day = serving_cells_by_day(
    conn, truth, stage="P0",
    min_decision_at_utc="2026-08-01T00:00:00Z", pre_band=False,
)
served: dict[str, list[float]] = defaultdict(list)
for _day, cells in by_day.items():
    per: dict[str, list[float]] = defaultdict(list)
    for cell in cells:
        actual = truth.get((cell["station_id"], cell["event_date"]))
        if actual is None:
            continue
        value = cell_residual_c(cell, actual)
        if value is not None:
            per[str(cell["station_id"])].append(value)
    for station, values in per.items():
        served[station].append(sum(values) / len(values))

print("对照：区域模型 vs 生产实际服务出去的分布（都按去均值后的散度 σ）\n")
print("站     旗标 格点高程  最好区域模型              σ_新   σ_服务  比值  判定")
print("-" * 88)
KILL = 0.9
passed, failed, thin = [], [], []
for entry in sorted(
    probe["results"], key=lambda e: -e["flag_count"]
):
    station = entry["station_id"]
    models = entry.get("models") or []
    base = served.get(station) or []
    if not models:
        continue
    if len(base) < 8:
        thin.append(station)
        continue
    sigma_served = pstdev(base)
    best = min(models, key=lambda m: m["dispersion_c"])
    ratio = best["dispersion_c"] / sigma_served if sigma_served else None
    mark = "✅过线" if ratio is not None and ratio < KILL else (
        "" if ratio is None else "✗"
    )
    (passed if mark == "✅过线" else failed).append(
        (station, best["label"], ratio)
    )
    print(
        f"{station:6s} {entry['flag_count']}/5"
        f" {str(entry['grid_elevation_m']):>7s}m"
        f"  {best['label']:24s}"
        f" {best['dispersion_c']:>5.2f}"
        f" {sigma_served:>7.2f}"
        f" {(f'{ratio:.2f}×' if ratio else '—'):>6s}  {mark}"
    )

print(f"\n过线（σ 比现役服务低 10% 以上）: {len(passed)} 站")
for station, label, ratio in sorted(passed, key=lambda t: t[2]):
    print(f"  {station:6s} {label:24s} {ratio:.2f}×")
print(f"未过线: {len(failed)} 站；服务样本太薄未判: {len(thin)} 站")

if passed:
    print("\n按模型汇总（过线站）:")
    grouped: dict[str, list[str]] = defaultdict(list)
    for station, label, _r in passed:
        grouped[label].append(station)
    for label, stations in sorted(
        grouped.items(), key=lambda kv: -len(kv[1])
    ):
        print(f"  {label:26s} {len(stations)} 站: {', '.join(stations)}")
