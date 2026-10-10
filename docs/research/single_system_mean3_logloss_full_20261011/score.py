#!/usr/bin/env python3
"""Score the four arms on the market mode +-1 and mean +-1 (PROTOCOL.md).

Binary log loss per rung in the window, YES and NO both: -[y log p + (1-y) log(1-p)],
averaged over the window's rungs.  Two levels: each seated system's own
calibrated ladder vector (systems weighted equally), and the served P0 vector.
The market vector of the same instant on the same window is the reference.
Every probability is floored at charter 224's 1e-3 (and capped at 1 - 1e-3).
Representative decisions: per station-day the latest decision in each of five
hours-to-settlement bins, averaged within the station-day; differences are
bootstrapped over station-days (2000 draws).

    python3 score.py
"""
from __future__ import annotations

import gzip
import json
import math
import random
import statistics as st
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARMS = ("cur", "full", "mode3", "mean3")
WINDOWS = ("mode", "mean")
FLOOR = 1e-3
BINS = ((48, 1e9, ">=48h"), (36, 48, "36-48h"), (24, 36, "24-36h"), (12, 24, "12-24h"), (0, 12, "<12h"))
SPLIT = "2026-09-20"


def bll(p, y):
    p = min(max(float(p), FLOOR), 1.0 - FLOOR)
    return -(y * math.log(p) + (1 - y) * math.log(1.0 - p))


def window_loss(vec, rungs, hit):
    return st.mean(bll(vec.get(k, 0.0), 1 if k == hit else 0) for k in rungs)


def sides(vec, rungs, hit):
    yes = [bll(vec.get(k, 0.0), 1) for k in rungs if k == hit]
    no = [bll(vec.get(k, 0.0), 0) for k in rungs if k != hit]
    return sum(yes), sum(no), len(rungs)


def load():
    rows = []
    for path in sorted((HERE / "pass1").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def representatives(rows):
    picked = {}
    for r in rows:
        for lo, hi, label in BINS:
            if lo <= r["hours"] < hi:
                key = (r["station"], r["event_date"], label)
                if key not in picked or r["at"] > picked[key]["at"]:
                    picked[key] = r
                break
    return picked


def bootstrap(per_day, draws=2000, seed=11):
    days = sorted(per_day)
    values = [per_day[d] for d in days]
    rng = random.Random(seed)
    means = sorted(st.mean(rng.choice(values) for _ in values) for _ in range(draws))
    return st.mean(values), means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def main() -> int:
    rows = load()
    reps = representatives(rows)
    # per decision: losses per (window, level, arm|market)
    scored = []
    for (station, day, label), r in reps.items():
        hit = r["hit"]
        entry = {"station": station, "day": day, "bin": label, "hours": r["hours"], "loss": {}, "sides": {}}
        for kind in WINDOWS:
            rungs = r["windows"][kind]["rungs"]
            entry["loss"][(kind, "market")] = window_loss(r["q"], rungs, hit)
            entry["sides"][(kind, "market")] = sides(r["q"], rungs, hit)
            entry["loss"][(kind, "all", "market")] = st.mean(bll(r["q"].get(k, 0.0), 1 if k == hit else 0) for k in r["ordered"])
            for arm in ARMS:
                entry["loss"][(kind, "p0", arm)] = window_loss(r[arm]["p"], rungs, hit)
                entry["sides"][(kind, "p0", arm)] = sides(r[arm]["p"], rungs, hit)
                entry["loss"][(kind, "sys", arm)] = st.mean(
                    window_loss(vec, rungs, hit) for vec in r[arm]["sys"].values())
                entry["loss"][(kind, "all", arm)] = st.mean(
                    bll(r[arm]["p"].get(k, 0.0), 1 if k == hit else 0) for k in r["ordered"])
        scored.append(entry)

    def per_day(fn, subset=None):
        groups = defaultdict(list)
        for e in subset if subset is not None else scored:
            value = fn(e)
            if value is not None:
                groups[(e["station"], e["day"])].append(value)
        return {k: st.mean(v) for k, v in groups.items()}

    out = {"station_days": len({(e["station"], e["day"]) for e in scored}), "decisions": len(scored),
           "stations": sorted({e["station"] for e in scored}), "windows": {}}
    print(f"stations {len(out['stations'])}, station-days {out['station_days']}, representative decisions {out['decisions']}")
    for kind in WINDOWS:
        block = {}
        print(f"\n=== window: market {kind} bucket +-1 (binary log loss per rung, YES+NO) ===")
        for level in ("p0", "sys"):
            print(f"  [{'served P0' if level == 'p0' else 'single-system (systems equal)'}]")
            for arm in ARMS:
                v = per_day(lambda e: e["loss"][(kind, level, arm)])
                block[(level, arm)] = st.mean(v.values())
            mk = per_day(lambda e: e["loss"][(kind, "market")])
            print(f"     market {st.mean(mk.values()):.4f}   " + "  ".join(f"{a} {block[(level, a)]:.4f}" for a in ARMS))
            for arm in ARMS[1:]:
                d = per_day(lambda e: e["loss"][(kind, level, arm)] - e["loss"][(kind, level, "cur")])
                m, lo, hi = bootstrap(d)
                halves = [st.mean(v for k, v in d.items() if (k[1] < SPLIT) == first) for first in (True, False)]
                print(f"     {arm:5s} - cur  {m:+.4f} [{lo:+.4f}, {hi:+.4f}]  halves {halves[0]:+.4f}/{halves[1]:+.4f}")
                out["windows"].setdefault(kind, {}).setdefault(level, {})[f"{arm}-cur"] = {"mean": m, "ci": [lo, hi], "halves": halves}
            for arm in ARMS:
                d = per_day(lambda e: e["loss"][(kind, level, arm)] - e["loss"][(kind, "market")])
                m, lo, hi = bootstrap(d)
                print(f"     {arm:5s} - market {m:+.4f} [{lo:+.4f}, {hi:+.4f}]")
                out["windows"][kind][level][f"{arm}-market"] = {"mean": m, "ci": [lo, hi]}
        # YES / NO split, served P0
        print("  [served P0, YES side (the hit rung if in window) / NO side (the other rungs), per decision sums]")
        for who in ("market",) + ARMS:
            key = (kind, "market") if who == "market" else (kind, "p0", who)
            ys = st.mean(e["sides"][key][0] for e in scored)
            ns = st.mean(e["sides"][key][1] for e in scored)
            inwin = st.mean(1.0 if e["sides"][key][0] > 0 else 0.0 for e in scored)
            print(f"     {who:6s} YES {ys:.4f}  NO {ns:.4f}   (truth inside window {inwin:.0%})")
        # by hours bin, served P0
        print("  [served P0 by hours to settlement: arm - cur]")
        for _lo, _hi, label in BINS:
            sub = [e for e in scored if e["bin"] == label]
            parts = []
            for arm in ARMS[1:]:
                d = per_day(lambda e: e["loss"][(kind, "p0", arm)] - e["loss"][(kind, "p0", "cur")], sub)
                if len(d) >= 20:
                    m, lo, hi = bootstrap(d)
                    parts.append(f"{arm} {m:+.4f} [{lo:+.4f},{hi:+.4f}]")
            print(f"     {label:7s} n={len(sub):4d}  " + "  ".join(parts))
    # all-bucket binary log loss, served P0 (secondary)
    print("\n=== all rungs (secondary), served P0 ===")
    for arm in ARMS[1:]:
        d = per_day(lambda e: e["loss"][("mode", "all", arm)] - e["loss"][("mode", "all", "cur")])
        m, lo, hi = bootstrap(d)
        print(f"  {arm:5s} - cur  {m:+.5f} [{lo:+.5f}, {hi:+.5f}]")
    # fitted parameters at the representative decisions
    print("\n=== fitted parameters (median over seated systems at representative decisions) ===")
    fits = defaultdict(list)
    for (station, day, label), r in reps.items():
        for f in r["fit"].values():
            for key in ("cur_shift", "full_shift", "mode3_shift", "mean3_shift", "moment_s", "cur_s", "full_s", "mode3_s", "mean3_s"):
                if isinstance(f.get(key), (int, float)):
                    fits[key].append(float(f[key]))
            for key in ("mode3_fallback", "mean3_fallback"):
                if key in f:
                    fits[key].append(1.0 if f[key] else 0.0)
    for key, values in fits.items():
        print(f"  {key:14s} median {st.median(values):+.3f}  p10 {sorted(values)[len(values) // 10]:+.3f}  p90 {sorted(values)[9 * len(values) // 10]:+.3f}" if "fallback" not in key
              else f"  {key:14s} share {st.mean(values):.1%}")
    (HERE / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
