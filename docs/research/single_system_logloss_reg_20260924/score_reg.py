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


def load():
    rows = []
    for path in sorted((HERE / "pass1").glob("*.jsonl.gz")):
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
    return out


def main():
    rows = load()
    july = [r for r in rows if r["event_date"] <= "2026-07-19"]
    fwd = [r for r in rows if r["event_date"] == "2026-09-23"]
    fits = [f for r in rows for f in r["fit"].values()]
    result = {
        "protocol": "PROTOCOL.md", "updates": len(rows),
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
        result["arms"][arm] = {
            "pooled": compare(rows, arm, "两段合并（主读数）"),
            "july": compare(july, arm, "07-01..07-19"),
            "forward": compare(fwd, arm, "09-23") if fwd else None,
        }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print("updates", result["updates"], "stations", result["stations"], "days", len(result["event_days"]))
    print(json.dumps(result["fits"], indent=1))
    for arm, slices in result["arms"].items():
        for c in slices.values():
            if not c:
                continue
            s, l, ci = c["seat"], c["blend"], c["panel_ci95"]
            print(f"\n== {arm} · {c['label']}: {c['station_days']} 站日 / {c['event_days']} 天 / {c['updates']} 次更新"
                  f"  四项点估计全好={c['four_point_estimates_improve']}")
            for m, name, scale, fmt in (("brier", "桶 Brier", 1, ".5f"), ("nll", "LogScore", 1, ".5f"),
                                        ("rel", "REL×1e-3", 1e3, ".4f"), ("res", "RES×1e-3", 1e3, ".3f"),
                                        ("rmse", "RMSE°C 诊断", 1, ".4f"), ("crps", "CRPS 诊断", 1, ".5f")):
                band = ci.get(m) or c["ci95"].get(m)
                interval = f" [{band[0] * scale:+{fmt}}, {band[1] * scale:+{fmt}}]" if band else ""
                print(f"  {name:12s} 生产 {s[m] * scale:{fmt}}  臂 {l[m] * scale:{fmt}}  差 {(l[m] - s[m]) * scale:+{fmt}}{interval}")


if __name__ == "__main__":
    main()
