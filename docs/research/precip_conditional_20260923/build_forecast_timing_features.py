"""Forecast-side rain timing per row, off the hourly precipitation versions.

For every row of the conditional-belief dataset: the hourly Open-Meteo
precipitation for that station-day resolved (a) as-of the decision from
``forward`` versions -- honest, empty until the forward lane has run -- and
(b) from the ``historical`` short-lead archive (ceiling on the forecast
side, captured after the day).  Index-aligned ``forecast_timing_features.json``.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.forecast_precip_asof import resolve_precip_hourly_as_of  # noqa: E402

BASE = HERE.parent / "conditional_belief_20260923"
SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"


def main():
    started = time.time()
    data = json.loads((BASE / "dataset.json").read_text())
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    out = []
    cache = {}
    for r in data:
        key = (r["station"], r["date"])
        if key not in cache:
            hist, receipt = resolve_precip_hourly_as_of(
                conn, station_id=r["station"], local_date=r["date"],
                cutoff_utc="2099-01-01T00:00:00Z", modes=("historical",))
            cache[key] = hist
        hist = cache[key]
        asof, receipt = resolve_precip_hourly_as_of(
            conn, station_id=r["station"], local_date=r["date"], cutoff_utc=r["at"])
        row = {}
        for prefix, agg in (("hist", hist), ("asof", asof)):
            for name in ("precip_sum_mm", "precip_before_peak_mm", "precip_after_peak_mm", "first_wet_hour", "wet_hours"):
                row[f"{prefix}_{name}"] = None if agg is None else agg[name]
        row["asof_age_h"] = receipt.get("version_age_hours") if asof is not None else None
        out.append(row)
    conn.close()
    (HERE / "forecast_timing_features.json").write_text(json.dumps(out))
    cov = {k: sum(1 for o in out if o.get(k) is not None) for k in out[0]}
    print("rows", len(out), "coverage", cov, "seconds", round(time.time() - started))


if __name__ == "__main__":
    main()
