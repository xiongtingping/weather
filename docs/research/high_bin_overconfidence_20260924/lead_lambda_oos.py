#!/usr/bin/env python3
"""Does a lead-dependent width lambda earn its keep out of sample?

Reads the five-family stable-pool replay (``data/aifs_ens_research/replay/
with_aifs_allruns``, production functions, 2026-07-20..09-19): every update of
the served seat (C0.5) carries its [CRPS, NLL, Brier, below, at] under each
lambda of ``replay_p0_stable_pool.LAMBDA_GRID`` with the rest of the served
tables unchanged.  Rolling origin by event day: lambda is chosen on event
days settled at least three days before the test day (>= 10 of them), then
the test day is scored.  Three rules: the incumbent 0.75, one pooled lambda,
one lambda per lead band (hours from the decision to the end of the local
event day).  Station-day weights; 3-day circular block bootstrap.  Research
only; writes lead_lambda_oos.json.
"""
from __future__ import annotations

import gzip
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import replay_p0_stable_pool as RP  # noqa: E402

REPLAY = ROOT / "data" / "aifs_ens_research" / "replay" / "with_aifs_allruns"
SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
GRID = list(RP.LAMBDA_GRID)
INCUMBENT = GRID.index(0.75)
LEADS = (("<12h", 0, 12), ("12-24h", 12, 24), ("24-36h", 24, 36), ("36-48h", 36, 48), (">=48h", 48, 9999))
GAP_DAYS = 3
MIN_TRAIN_DAYS = 10
BOOT = 2000
BLOCK = 3


def band_of(hours: float) -> str:
    return next(name for name, lo, hi in LEADS if lo <= hours < hi)


def main() -> int:
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ).fetchall())
    rows = []
    for path in sorted(REPLAY.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                scheme = (r.get("schemes") or {}).get("C0.5")
                if not scheme or len(scheme.get("lam") or []) != len(GRID):
                    continue
                tz = zones.get(r["station"])
                if not tz:
                    continue
                end = datetime.fromisoformat(r["event_date"]).replace(tzinfo=ZoneInfo(tz)) + timedelta(days=1)
                hours = (end - datetime.fromisoformat(r["at"].replace("Z", "+00:00"))).total_seconds() / 3600.0
                if hours <= 0:
                    continue
                rows.append({
                    "station": r["station"], "day": r["event_date"], "band": band_of(hours),
                    "nll": [float(x[1]) for x in scheme["lam"]], "crps": [float(x[0]) for x in scheme["lam"]],
                })
    copies = Counter((r["station"], r["day"]) for r in rows)
    for r in rows:
        r["w"] = 1.0 / copies[(r["station"], r["day"])]
    days = sorted({r["day"] for r in rows})
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)

    def best(train, metric, band=None):
        members = [r for r in train if band is None or r["band"] == band]
        if not members:
            return INCUMBENT
        w = np.array([r["w"] for r in members])
        scores = np.array([r[metric] for r in members])
        return int(np.argmin((w[:, None] * scores).sum(axis=0)))

    out_rows = []
    choices = defaultdict(list)
    for day in days:
        cutoff = (date.fromisoformat(day) - timedelta(days=GAP_DAYS)).isoformat()
        train_days = [d for d in days if d <= cutoff]
        if len(train_days) < MIN_TRAIN_DAYS:
            continue
        train = [r for d in train_days for r in by_day[d]]
        for metric in ("nll", "crps"):
            pooled = best(train, metric)
            per_band = {name: best(train, metric, name) for name, _lo, _hi in LEADS}
            choices[metric].append({"day": day, "pooled": GRID[pooled], "per_band": {k: GRID[v] for k, v in per_band.items()}})
            for r in by_day[day]:
                out_rows.append({
                    "metric": metric, "day": day, "band": r["band"], "w": r["w"],
                    "incumbent": r[metric][INCUMBENT], "pooled": r[metric][pooled],
                    "per_band": r[metric][per_band[r["band"]]],
                })
    rng = np.random.default_rng(20260924)
    result = {"grid": GRID, "gap_days": GAP_DAYS, "min_train_days": MIN_TRAIN_DAYS, "metrics": {}}
    for metric in ("nll", "crps"):
        subset = [r for r in out_rows if r["metric"] == metric]
        test_days = sorted({r["day"] for r in subset})
        idx_by_day = defaultdict(list)
        for i, r in enumerate(subset):
            idx_by_day[r["day"]].append(i)
        w = np.array([r["w"] for r in subset])
        arms = {k: np.array([r[k] for r in subset]) for k in ("incumbent", "pooled", "per_band")}
        mean = {k: float((w * v).sum() / w.sum()) for k, v in arms.items()}
        n_blocks = math.ceil(len(test_days) / BLOCK)
        diffs = defaultdict(list)
        for _ in range(BOOT):
            idx = np.concatenate([idx_by_day[test_days[(s + j) % len(test_days)]]
                                  for s in rng.integers(0, len(test_days), n_blocks) for j in range(BLOCK)])
            ww = w[idx]
            for a, b in (("per_band", "incumbent"), ("per_band", "pooled"), ("pooled", "incumbent")):
                diffs[f"{a}-{b}"].append(float((ww * (arms[a][idx] - arms[b][idx])).sum() / ww.sum()))
        by_band = {}
        for name, _lo, _hi in LEADS:
            members = [i for i, r in enumerate(subset) if r["band"] == name]
            if not members:
                continue
            ww = w[members]
            by_band[name] = {
                "station_days": len({(subset[i]["day"],) for i in members}),
                "incumbent": float((ww * arms["incumbent"][members]).sum() / ww.sum()),
                "per_band": float((ww * arms["per_band"][members]).sum() / ww.sum()),
            }
        full = defaultdict(lambda: np.zeros(len(GRID)))
        full_w = defaultdict(float)
        for r in rows:
            full[r["band"]] += r["w"] * np.array(r[metric])
            full_w[r["band"]] += r["w"]
        result["metrics"][metric] = {
            "test_days": len(test_days), "first_test_day": test_days[0], "last_test_day": test_days[-1],
            "mean": mean,
            "ci95": {k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] for k, v in diffs.items()},
            "by_band_oos": by_band,
            "full_sample_curve": {band: (full[band] / full_w[band]).round(5).tolist() for band in full},
            "full_sample_best": {band: GRID[int(np.argmin(full[band]))] for band in full},
            "last_choice": choices[metric][-1],
        }
        m = result["metrics"][metric]
        print(f"[{metric}] test days {m['test_days']} ({m['first_test_day']}..{m['last_test_day']})")
        print("   mean", {k: round(v, 5) for k, v in mean.items()})
        print("   ci  ", {k: [round(x, 5) for x in v] for k, v in m["ci95"].items()})
        print("   full-sample best λ by lead", m["full_sample_best"])
        for band, v in by_band.items():
            print(f"   {band}: incumbent {v['incumbent']:.4f} per-band {v['per_band']:.4f}")
        print("   last choice", m["last_choice"])
    result["rows"] = len(rows)
    result["station_days"] = len(copies)
    (HERE / "lead_lambda_oos.json").write_text(json.dumps(result, indent=1))
    print("rows", len(rows), "station-days", len(copies))
    return 0


if __name__ == "__main__":
    sys.exit(main())
