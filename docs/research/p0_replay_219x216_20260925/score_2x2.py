#!/usr/bin/env python3
"""Score the 2x2 replay on served P0 (PROTOCOL.md); writes results.json.

Same panel as the six-family 216 evidence (``fusion_logloss_20260924/run.py``
``panel``): station-day equal weights, three-day block bootstrap, bucket Brier
and log score deltas, Murphy REL/RES and RMSE/CRPS as diagnostics.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
_spec = importlib.util.spec_from_file_location(
    "fusion_ll_run", ROOT / "docs" / "research" / "fusion_logloss_20260924" / "run.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

COMPARISONS = {
    "A_216_under_219": ("mom_on", "ll_on"),
    "B_219_under_216": ("ll_off", "ll_on"),
    "C_219_under_moment": ("mom_off", "mom_on"),
    "D_both_vs_old_production": ("mom_off", "ll_on"),
}


def load():
    rows = []
    for path in sorted((HERE / "pass1").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def day_ahead_roots(rows):
    by_day = defaultdict(list)
    for r in rows:
        by_day[(r["station"], r["event_date"])].append(r)
    roots = []
    for (_s, day), recs in by_day.items():
        midnight = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(recs[0]["tz"]))
        before = [r for r in recs
                  if midnight - timedelta(days=1) <= datetime.fromisoformat(r["at"].replace("Z", "+00:00")) < midnight]
        if before:
            roots.append(max(before, key=lambda r: r["at"]))
    return roots


def compare(rows, base, test, label):
    for r in rows:
        r["seat"] = r[base]
        r["blend"] = r[test]
    out = R.panel(rows, label)
    if out is None:
        return None
    d, ci = out["delta"], out["panel_ci95"]
    out["proper_scores_improve"] = bool(d["brier"] < 0 and d["nll"] < 0)
    out["holds"] = bool(out["proper_scores_improve"] and ci["brier"][1] < 0 and ci["nll"][1] < 0)
    return out


def main():
    import sys, time
    only = sys.argv[1:]            # comparison names to run; all when empty
    rows = load()
    segments = {
        "pooled": rows,
        "day_ahead_roots": day_ahead_roots(rows),
        "before_134": [r for r in rows if r["event_date"] < "2026-09-14"],
        "after_134": [r for r in rows if r["event_date"] >= "2026-09-14"],
    }
    # Post-hoc diagnostics (labelled so in the report; not decision readings).
    diagnostics = {
        "pool_3plus_systems": [r for r in rows if len(r["pool"]) >= 3],
        "pool_2_systems": [r for r in rows if len(r["pool"]) == 2],
        "changed_scopes_in_pool": [r for r in rows if r["changed"]],
    }
    result = {
        "protocol": "PROTOCOL.md", "updates": len(rows),
        "stations": len({r["station"] for r in rows}),
        "event_days": len({r["event_date"] for r in rows}),
        "zero_truth_probability_updates": {
            arm: sum(1 for r in rows if r[arm]["served"][4] <= 0.0) for arm in ("mom_off", "ll_off", "mom_on", "ll_on")
        },
        "comparisons": {},
    }
    path = HERE / "results.json"
    if path.exists():
        result["comparisons"] = json.loads(path.read_text()).get("comparisons", {})
    for name, (base, test) in COMPARISONS.items():
        if only and name not in only:
            continue
        result["comparisons"][name] = {"base": base, "test": test, "segments": {}, "diagnostics": {}}
        for group, table in (("segments", segments), ("diagnostics", diagnostics)):
            for seg, recs in table.items():
                t0 = time.time()
                result["comparisons"][name][group][seg] = compare(recs, base, test, seg) if recs else None
                c = result["comparisons"][name][group][seg]
                if c:
                    print(f"{name} {seg}: dBrier {c['blend']['brier'] - c['seat']['brier']:+.5f} {c['panel_ci95']['brier']} "
                          f"dLogScore {c['blend']['nll'] - c['seat']['nll']:+.5f} {c['panel_ci95']['nll']} holds={c['holds']} ({time.time() - t0:.0f}s)", flush=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print("updates", result["updates"], "stations", result["stations"], "days", result["event_days"],
          "zero-P(truth) updates", result["zero_truth_probability_updates"])
    for name, comp in result["comparisons"].items():
        print(f"\n=== {name}: {comp['base']} -> {comp['test']}")
        for group in ("segments", "diagnostics"):
            for seg, c in comp[group].items():
                if not c:
                    continue
                ci, s_, b_ = c["panel_ci95"], c["seat"], c["blend"]
                print(f"  {'' if group == 'segments' else '[诊断] '}{seg:24s} {c['station_days']:5d} 站日 {c['updates']:6d} 更新 | "
                      f"Brier {s_['brier']:.5f}->{b_['brier']:.5f} ({b_['brier'] - s_['brier']:+.5f} [{ci['brier'][0]:+.5f},{ci['brier'][1]:+.5f}]) | "
                      f"LogScore {s_['nll']:.5f}->{b_['nll']:.5f} ({b_['nll'] - s_['nll']:+.5f} [{ci['nll'][0]:+.5f},{ci['nll'][1]:+.5f}]) | "
                      f"REL {1e3 * (b_['rel'] - s_['rel']):+.3f}e-3 RES {1e3 * (b_['res'] - s_['res']):+.3f}e-3 | "
                      f"RMSE {b_['rmse'] - s_['rmse']:+.4f} CRPS {b_['crps'] - s_['crps']:+.5f} | holds={c['holds']}")


if __name__ == "__main__":
    main()
