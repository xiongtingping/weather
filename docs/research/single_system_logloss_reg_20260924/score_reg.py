#!/usr/bin/env python3
"""Score the unseen days: regularised (primary) and unregularised arms vs production.

Same panel as the previous rounds (``fusion_logloss_20260924/run.py``'s
``panel``).  See PROTOCOL.md; writes results.json.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location(
    "fusion_ll_run", ROOT / "docs" / "research" / "fusion_logloss_20260924" / "run.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def load(folder="pass1"):
    rows = []
    for path in sorted((HERE / folder).glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def compare(rows, arm, label):
    for r in rows:
        r["seat"] = r["prod"]
        r["blend"] = r[arm]
    out = R.panel(rows, label)
    d = out["delta"]
    out["four_point_estimates_improve"] = bool(d["brier"] < 0 and d["nll"] < 0 and d["rel"] < 0 and d["res"] > 0)
    # Charter 211 as amended 2026-09-24: closer to P(Y=k|X) = both proper
    # scores lower; Reliability / Resolution are Brier's decomposition.
    out["proper_scores_improve"] = bool(d["brier"] < 0 and d["nll"] < 0)
    return out


def main():
    import sys
    period = sys.argv[1] if len(sys.argv) > 1 else "unseen"
    rows = load("pass1" if period == "unseen" else f"pass1_{period}")
    if period == "unseen":
        segments = {"july": [r for r in rows if r["event_date"] <= "2026-07-19"],
                    "forward": [r for r in rows if r["event_date"] == "2026-09-23"]}
    else:
        segments = {"before_134": [r for r in rows if r["event_date"] < "2026-09-14"],
                    "after_134": [r for r in rows if r["event_date"] >= "2026-09-14"]}
    fits = [f for r in rows for f in r["fit"].values()]
    result = {
        "protocol": "PROTOCOL.md", "period": period, "updates": len(rows),
        "stations": len({r["station"] for r in rows}),
        "event_days": sorted({r["event_date"] for r in rows}),
        "fits": {
            "count": len(fits),
            **{f"{k}_p10_p50_p90": [float(np.percentile([f[k] for f in fits], q)) for q in (10, 50, 90)]
               for k in ("s_prod", "s_ll", "s_reg", "delta_ll", "delta_reg")},
        },
        "arms": {},
    }
    for arm in ("ll_both_reg", "ll_both"):
        result["arms"][arm] = {"pooled": compare(rows, arm, "合并（主读数）")}
        for name, seg in segments.items():
            result["arms"][arm][name] = compare(seg, arm, name) if seg else None
    out = HERE / ("results.json" if period == "unseen" else f"results_{period}.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print("period", period, "updates", result["updates"], "stations", result["stations"], "days", len(result["event_days"]))
    print(json.dumps(result["fits"], indent=1))
    for arm, slices in result["arms"].items():
        for c in slices.values():
            if not c:
                continue
            s_, l, ci = c["seat"], c["blend"], c["panel_ci95"]
            print(f"\n== {arm} · {c['label']}: {c['station_days']} 站日 / {c['event_days']} 天 / {c['updates']} 次更新"
                  f"  两个proper score都好={c['proper_scores_improve']}")
            for m, name, scale, fmt in (("brier", "桶 Brier", 1, ".5f"), ("nll", "LogScore", 1, ".5f"),
                                        ("rel", "REL×1e-3", 1e3, ".4f"), ("res", "RES×1e-3", 1e3, ".3f"),
                                        ("rmse", "RMSE°C 诊断", 1, ".4f"), ("crps", "CRPS 诊断", 1, ".5f")):
                band = ci.get(m) or c["ci95"].get(m)
                interval = f" [{band[0] * scale:+{fmt}}, {band[1] * scale:+{fmt}}]" if band else ""
                print(f"  {name:12s} 生产 {s_[m] * scale:{fmt}}  臂 {l[m] * scale:{fmt}}  差 {(l[m] - s_[m]) * scale:+{fmt}}{interval}")


if __name__ == "__main__":
    main()
