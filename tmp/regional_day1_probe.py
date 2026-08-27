"""Phase 0, corrected: regional models at the lead production actually runs.

The first pass read the historical-forecast archive and every candidate
looked strong -- sixteen stations past the kill line, some at less than half
the served dispersion.  It was measuring the wrong quantity.  Checked against
``temperature_2m_previous_day1``, which is the genuine day-ahead run, the
archive values differ by 0.98-1.49C of dispersion: the archive is returning a
much shorter lead than a day, close enough to the outcome that comparing it
with production's day-ahead forecast is comparing two different problems.

This is the same trap the research lane hit when the archive's default
fell back to reanalysis and twenty-one stations showed identically zero
residual.  The rule that came out of it was to name the model explicitly and
verify the forecast is not the observation; the rule this adds is to name the
*lead* explicitly too.

So: ``previous_day1`` only, which is a forecast issued the day before the
day it describes -- the same question P0 answers at h24_48.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.center_width_store import (  # noqa: E402
    _truth, serving_cells_by_day,
)
from bayes_basket_target.fleet_regime_shift import (  # noqa: E402
    cell_residual_c,
)

PREV = "https://previous-runs-api.open-meteo.com/v1/forecast"
KILL = 0.9
OUT = ROOT / "tmp" / "regional_day1_probe.json"

probe = json.loads(
    (ROOT / "tmp" / "regional_model_probe.json").read_text(encoding="utf-8")
)
conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=120.0,
)
coords = {
    str(r[0]): (float(r[1]), float(r[2]))
    for r in conn.execute(
        "SELECT DISTINCT station_id, latitude, longitude"
        " FROM calibration_station_forecast_physics"
    )
}
zones = {
    str(r[0]): str(r[1])
    for r in conn.execute(
        "SELECT station_id, timezone_name FROM calibration_station_registry"
    )
}
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


def fetch(url: str) -> dict | None:
    for attempt in range(3):
        result = subprocess.run(
            ["curl", "-s", "--max-time", "90", url],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                return json.loads(result.stdout)
            except ValueError:
                pass
        time.sleep(2.0 * (attempt + 1))
    return None


print("提前一天真预报 vs 生产服务分布（都按去均值散度 σ）\n")
print("站     旗标  模型                 n   σ_day1  σ_服务  比值  判定")
print("-" * 74)
results = []
for entry in sorted(probe["results"], key=lambda e: -e["flag_count"]):
    station = entry["station_id"]
    models = entry.get("models") or []
    base = served.get(station) or []
    if not models or len(base) < 8 or station not in coords:
        continue
    sigma_served = pstdev(base)
    lat, lon = coords[station]
    zone = zones.get(station, "UTC")
    for model in models:
        key = model["model"]
        url = (
            f"{PREV}?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m_previous_day1&models={key}"
            f"&past_days=60&forecast_days=1&timezone={quote(zone)}"
        )
        data = fetch(url)
        time.sleep(1.0)
        if not data or "hourly" not in data:
            continue
        hourly = data["hourly"]
        times = hourly.get("time") or []
        series = (
            hourly.get("temperature_2m_previous_day1")
            or hourly.get(f"temperature_2m_previous_day1_{key}")
        )
        if not series:
            continue
        daily: dict[str, float] = {}
        for stamp, value in zip(times, series):
            if value is None:
                continue
            day = str(stamp)[:10]
            daily[day] = max(daily.get(day, -99.0), float(value))
        paired = [
            truth[(station, day)] - value
            for day, value in daily.items()
            if (station, day) in truth
        ]
        if len(paired) < 20:
            continue
        sigma = pstdev(paired)
        ratio = sigma / sigma_served if sigma_served else None
        mark = "✅" if ratio is not None and ratio < KILL else "✗"
        results.append({
            "station_id": station, "model": key,
            "label": model["label"], "paired_days": len(paired),
            "bias_c": mean(paired), "dispersion_c": sigma,
            "served_dispersion_c": sigma_served, "ratio": ratio,
            "passes": mark == "✅", "flag_count": entry["flag_count"],
        })
        print(
            f"{station:6s} {entry['flag_count']}/5  {model['label']:20s}"
            f" {len(paired):>3d} {sigma:>7.2f} {sigma_served:>7.2f}"
            f" {ratio:>5.2f}×  {mark}"
        )

OUT.write_text(
    json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
    encoding="utf-8",
)
winners = [r for r in results if r["passes"]]
print(f"\n过线 {len(winners)} / 共测 {len(results)} 个（站×模型）组合")
best: dict[str, dict] = {}
for row in winners:
    current = best.get(row["station_id"])
    if current is None or row["ratio"] < current["ratio"]:
        best[row["station_id"]] = row
for station, row in sorted(best.items(), key=lambda kv: kv[1]["ratio"]):
    print(
        f"  {station:6s} [{row['flag_count']}/5] {row['label']:22s}"
        f" {row['ratio']:.2f}×  (σ {row['dispersion_c']:.2f}"
        f" vs 服务 {row['served_dispersion_c']:.2f})"
    )
print(f"\n写入 {OUT}")
