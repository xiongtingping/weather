#!/usr/bin/env python3
"""Score the three single-system arms on the charter 211 panel.

Reuses the fusion log-loss study's panel (``fusion_logloss_20260924/run.py``):
bucket Brier and log score with 3-day block intervals, Reliability and
Resolution on the board's bins with intervals, RMSE / CRPS as diagnostics.
Production is the baseline arm; each log-loss arm is compared against it on
the same updates.  See PROTOCOL.md; writes results.json.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location(
    "fusion_ll_run", ROOT / "docs" / "research" / "fusion_logloss_20260924" / "run.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

STABLE_POOL_FROM = "2026-09-14"


def _stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def load():
    rows = []
    for path in sorted((HERE / "pass1").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def roots_of(rows):
    by_day = defaultdict(list)
    for r in rows:
        by_day[(r["station"], r["event_date"])].append(r)
    out = []
    for (_station, day), recs in by_day.items():
        midnight = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(recs[0]["tz"]))
        before = [r for r in recs if midnight - timedelta(days=1) <= _stamp(r["at"]) < midnight]
        if before:
            out.append(max(before, key=lambda r: r["at"]))
    return out


def compare(rows, arm, label):
    for r in rows:
        r["seat"] = r["prod"]
        r["blend"] = r[arm]
    return R.panel(rows, label)


def main():
    rows = load()
    stations = len({r["station"] for r in rows})
    print("updates", len(rows), "stations", stations)
    fits = [f for r in rows for f in r["fit"].values()]
    s_prod = np.array([f["s_prod"] for f in fits])
    result = {
        "protocol": "PROTOCOL.md", "updates": len(rows), "stations": stations,
        "fits": {
            "count": len(fits),
            "spread_prod_median": float(np.median(s_prod)),
            "spread_ll_spread_median": float(np.median([f["s_ll_spread"] for f in fits])),
            "spread_ratio_ll_over_prod_p10_p50_p90": [float(np.percentile(
                [f["s_ll_spread"] / f["s_prod"] for f in fits], q)) for q in (10, 50, 90)],
            "delta_ll_both_p10_p50_p90": [float(np.percentile(
                [f["delta_ll_both"] for f in fits], q)) for q in (10, 50, 90)],
            "in_sample_nll": {k: float(np.mean([f[f"nll_{k}"] for f in fits]))
                              for k in ("prod", "ll_spread", "ll_both")},
        },
        "arms": {},
    }
    roots = roots_of(rows)
    for arm in ("ll_spread", "ll_both"):
        result["arms"][arm] = {
            "all_updates": compare(rows, arm, "全部更新（主读数）"),
            "day_ahead_roots": compare(roots, arm, "日前根"),
            "stable_pool_era": compare([r for r in rows if r["event_date"] >= STABLE_POOL_FROM], arm,
                                       "宪章 134 之后"),
        }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result["fits"], indent=1))
    for arm, slices in result["arms"].items():
        for c in slices.values():
            s, l, ci = c["seat"], c["blend"], c["panel_ci95"]
            print(f"\n== {arm} · {c['label']}: {c['station_days']} 站日 / {c['event_days']} 天 / {c['updates']} 次更新"
                  f"  panel_passes={c['panel_passes']}")
            for m, name, scale, fmt in (("brier", "桶 Brier", 1, ".5f"), ("nll", "LogScore", 1, ".5f"),
                                        ("rel", "REL×1e-3", 1e3, ".4f"), ("res", "RES×1e-3", 1e3, ".3f"),
                                        ("rmse", "RMSE°C 诊断", 1, ".4f"), ("crps", "CRPS 诊断", 1, ".5f")):
                band = ci.get(m) or c["ci95"].get(m)
                interval = f" [{band[0] * scale:+{fmt}}, {band[1] * scale:+{fmt}}]" if band else ""
                print(f"  {name:12s} 生产 {s[m] * scale:{fmt}}  LogLoss {l[m] * scale:{fmt}}  "
                      f"差 {(l[m] - s[m]) * scale:+{fmt}}{interval}")


if __name__ == "__main__":
    main()
