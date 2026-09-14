#!/usr/bin/env python3
"""Charter 134: score the nested replay and apply the pre-declared gate.

Declared before any fleet result was read (2026-09-14, committed with this
file).  Candidate = the stable pool with kappa and width lambda chosen on
earlier days; baseline = the subset contest with its lambda chosen the same
way.  Both sit under the band table that was active at each update.

  G1  CRPS non-inferiority: mean relative CRPS (candidate - baseline) /
      baseline, 95% station-cluster upper bound <= +1.0%.
  G2  NLL non-inferiority: mean NLL difference, 95% upper bound <= +0.01 nat.
  G3  calibration: |80% central coverage - 0.80| no worse than the
      baseline's by more than 2 points.
  G4  stability: mean |change in served mean| between consecutive updates of
      a station-day lower than the baseline's, 95% upper bound < 0, and
      fewer >=1C steps.

Nested selection: for an update made at time t, kappa and lambda are the
fleet-wide CRPS minimisers over updates whose event day is at least two UTC
days before t's date (every such truth was committed before t), requiring
at least 300 inner updates on 3 event days; earlier updates are not scored.
Only updates where the contest could pick (three shared settled days) are
compared, so both arms answer the same questions.

New-run latency is identical by construction: every arm updates at the same
arrival moments.  The band is not refitted here; if G1-G4 pass, a band refit
on the chosen pool is the next step before any switch.
"""
from __future__ import annotations

import gzip
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "tmp" / "stable_pool_replay_20260914"
LAMBDA_GRID = (0.6, 0.7, 0.75, 0.8, 0.9, 1.0, 1.1, 1.2)
KAPPAS = ("C0", "C0.25", "C0.5", "C0.75", "C1")
MIN_INNER_UPDATES = 300
MIN_INNER_DAYS = 3
G1_MAX_REL_CRPS = 0.010
G2_MAX_NLL = 0.010
G3_MAX_EXTRA_MISCOVERAGE = 0.02


def load() -> list[dict]:
    records = []
    for path in sorted((BASE / "updates").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            records.extend(json.loads(line) for line in handle)
    return records


def variants(scheme: dict) -> dict[str, list[float]]:
    out = {"served": scheme["served"], "raw": scheme["raw"]}
    for lam, score in zip(LAMBDA_GRID, scheme["lam"]):
        out[f"lam{lam:g}"] = score
    return out


def mid_pit(score: list[float]) -> float:
    return score[3] + 0.5 * score[4]


def cluster_ci(rows, fn, *, key="station", n=2000, seed=19):
    groups = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    keys = sorted(groups)
    rng = random.Random(seed)
    values = []
    for _ in range(n):
        sample = [row for k in (rng.choice(keys) for _ in keys) for row in groups[k]]
        values.append(fn(sample))
    values.sort()
    return [values[int(0.025 * n)], values[int(0.975 * n)]]


def main() -> int:
    records = [r for r in load() if "A" in r["schemes"]]
    for r in records:
        r["decided"] = r["at"][:10]
    # Candidate configurations, scored per update.
    configs = {}
    for lam_key in ["served"] + [f"lam{lam:g}" for lam in LAMBDA_GRID]:
        configs[("A", lam_key)] = lambda r, k=lam_key: variants(r["schemes"]["A"])[k]
        for kappa in KAPPAS:
            configs[(kappa, lam_key)] = lambda r, k=lam_key, c=kappa: variants(r["schemes"][c])[k]
    by_event_day = defaultdict(list)
    for r in records:
        by_event_day[r["event_date"]].append(r)
    event_days = sorted(by_event_day)
    # Cumulative CRPS sums by event day, per config, for fast inner means.
    totals = {cfg: {} for cfg in configs}
    for cfg, fn in configs.items():
        running = 0.0
        count = 0
        for day in event_days:
            for r in by_event_day[day]:
                running += fn(r)[0]
                count += 1
            totals[cfg][day] = (running, count)

    def inner_best(decided: str, arms) -> tuple | None:
        cutoff = (date.fromisoformat(decided) - timedelta(days=2)).isoformat()
        days = [d for d in event_days if d <= cutoff]
        if len(days) < MIN_INNER_DAYS:
            return None
        best = None
        for cfg in arms:
            total, count = totals[cfg][days[-1]]
            if count < MIN_INNER_UPDATES:
                return None
            score = total / count
            if best is None or score < best[0]:
                best = (score, cfg)
        return best[1]

    a_arms = [cfg for cfg in configs if cfg[0] == "A" and cfg[1] != "raw"]
    c_arms = [cfg for cfg in configs if cfg[0] != "A" and cfg[1] != "raw"]
    scored = []
    chosen = defaultdict(int)
    for r in records:
        a_cfg = inner_best(r["decided"], a_arms)
        c_cfg = inner_best(r["decided"], c_arms)
        if a_cfg is None or c_cfg is None:
            continue
        chosen[("A",) + a_cfg[1:]] += 1
        chosen[c_cfg] += 1
        a = configs[a_cfg](r)
        c = configs[c_cfg](r)
        a_served = configs[("A", "served")](r)
        pool_served = configs[("C1", "served")](r)
        scored.append({
            "station": r["station"], "event_date": r["event_date"], "at": r["at"],
            "lead": r["lead"], "pool_n": len(r["pool"]), "a_n": len(r["schemes"]["A"]["w"]),
            "A": a, "C": c, "A_served": a_served, "C1_served": pool_served,
            "a_cfg": a_cfg, "c_cfg": c_cfg,
            "A_mean": r["schemes"]["A"]["mean"], "C_mean": r["schemes"][c_cfg[0]]["mean"],
            "A_sigma": r["schemes"]["A"]["sigma"], "C_sigma": r["schemes"][c_cfg[0]]["sigma"],
            "actual": r["actual"],
        })

    def rel_crps(rows, x="C", y="A"):
        return sum(row[x][0] - row[y][0] for row in rows) / sum(row[y][0] for row in rows)

    def mean_diff(rows, index, x="C", y="A"):
        return statistics.mean(row[x][index] - row[y][index] for row in rows)

    def coverage(rows, arm, lo=0.1, hi=0.9):
        return statistics.mean(1.0 if lo <= mid_pit(row[arm]) <= hi else 0.0 for row in rows)

    summary = {
        "updates_scored": len(scored),
        "stations": len({row["station"] for row in scored}),
        "event_days": [min(row["event_date"] for row in scored), max(row["event_date"] for row in scored)],
        "chosen_configs": {f"{k[0]}|{k[1]}": v for k, v in sorted(chosen.items(), key=lambda kv: -kv[1])},
    }
    arms = {"A_nested": "A", "C_nested": "C", "A_served": "A_served", "C1_served": "C1_served"}
    summary["arms"] = {
        name: {
            "crps": statistics.mean(row[key][0] for row in scored),
            "nll": statistics.mean(row[key][1] for row in scored),
            "brier": statistics.mean(row[key][2] for row in scored),
            "coverage80": coverage(scored, key),
            "coverage50": coverage(scored, key, 0.25, 0.75),
        }
        for name, key in arms.items()
    }
    comparisons = {}
    for label, x, y in (("C_nested_vs_A_nested", "C", "A"), ("C_nested_vs_A_served", "C", "A_served"), ("C1_served_vs_A_served", "C1_served", "A_served")):
        comparisons[label] = {
            "rel_crps": rel_crps(scored, x, y),
            "rel_crps_ci95": cluster_ci(scored, lambda rows, x=x, y=y: rel_crps(rows, x, y)),
            "nll_diff": mean_diff(scored, 1, x, y),
            "nll_diff_ci95": cluster_ci(scored, lambda rows, x=x, y=y: mean_diff(rows, 1, x, y)),
        }
    summary["comparisons"] = comparisons
    # Stability along each station-day's update sequence.
    sequences = defaultdict(list)
    for row in scored:
        sequences[(row["station"], row["event_date"])].append(row)
    steps = []
    for (station, _day), rows in sequences.items():
        rows.sort(key=lambda row: row["at"])
        for prev, cur in zip(rows, rows[1:]):
            steps.append({
                "station": station,
                "A": abs(cur["A_mean"] - prev["A_mean"]),
                "C": abs(cur["C_mean"] - prev["C_mean"]),
                "A_err": abs(cur["A_mean"] - cur["actual"]) - abs(prev["A_mean"] - prev["actual"]),
                "C_err": abs(cur["C_mean"] - cur["actual"]) - abs(prev["C_mean"] - prev["actual"]),
            })
    summary["stability"] = {
        "steps": len(steps),
        "mean_abs_step_A": statistics.mean(s["A"] for s in steps),
        "mean_abs_step_C": statistics.mean(s["C"] for s in steps),
        "diff_ci95": cluster_ci(steps, lambda rows: statistics.mean(s["C"] - s["A"] for s in rows)),
        "steps_ge_1C_A": sum(s["A"] >= 1.0 for s in steps),
        "steps_ge_1C_C": sum(s["C"] >= 1.0 for s in steps),
        "mean_abs_err_change_per_step_A": statistics.mean(s["A_err"] for s in steps),
        "mean_abs_err_change_per_step_C": statistics.mean(s["C_err"] for s in steps),
    }
    by = defaultdict(list)
    for row in scored:
        by[("lead", row["lead"])].append(row)
        by[("pool_n", row["pool_n"])].append(row)
    summary["breakdown"] = {
        f"{k[0]}={k[1]}": {"n": len(rows), "rel_crps_C_vs_A": rel_crps(rows)}
        for k, rows in sorted(by.items(), key=lambda kv: str(kv[0])) if len(rows) >= 100
    }
    a80 = abs(summary["arms"]["A_nested"]["coverage80"] - 0.80)
    c80 = abs(summary["arms"]["C_nested"]["coverage80"] - 0.80)
    head = comparisons["C_nested_vs_A_nested"]
    stab = summary["stability"]
    summary["gate"] = {
        "G1_crps_noninferior": head["rel_crps_ci95"][1] <= G1_MAX_REL_CRPS,
        "G2_nll_noninferior": head["nll_diff_ci95"][1] <= G2_MAX_NLL,
        "G3_calibration": c80 <= a80 + G3_MAX_EXTRA_MISCOVERAGE,
        "G4_stability": stab["diff_ci95"][1] < 0 and stab["steps_ge_1C_C"] < stab["steps_ge_1C_A"],
    }
    summary["gate"]["pass"] = all(summary["gate"].values())
    (BASE / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True, default=str))
    print(json.dumps(summary, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
