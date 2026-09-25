#!/usr/bin/env python3
"""Two readings added after review (2026-09-26, labelled so in REPORT.md).

A. Floor sensitivity.  Re-floor the model vector at eps in {0.001, 0.002, 0.005,
   0.01, 0.02} with the served floor's own semantics (lift below eps, renormalise;
   ``floor_probabilities`` with a different constant), no trading, and re-read
   dS, D = KL(q||p), dS + D and the share of D sitting on floored rungs.
   Served L4 population (already floored at 0.001; lifting again to a higher eps
   equals lifting the raw vector to eps up to the first renormalisation) and
   today's P0 (raw vectors, floored here).

B. L split on a book with both sides and depth: the replay dataset of
   bayes_kelly_replay_20260924 (grid times, served vector by stage priority,
   YES asks and YES bids six levels deep, venue truth overrides).
     Pi_mid     = s . (1_y - q)                         (paper, frictionless)
     Pi_signed  = the paper's own signed position executed: s_k > 0 buys YES
                  walking the asks, s_k < 0 buys NO walking (1 - bid)
     Pi_yes     = the pure-YES shift s' = s - min s, all legs at the asks
     L_market   = Pi_mid - Pi_signed      (the paper's liquidity loss)
     L_constraint = Pi_signed - Pi_yes    (the cost of the BUY-NO ban)
   plus the paper's 3.3.4 no-trade band at executable prices (YES if p_k > ask,
   size log(p_k/ask); NO if p_k < bid, size log(bid/p_k)).
   Rows whose six levels cannot fill a leg are counted and left out of the main
   reading.  No SSOT access.  Writes results_l_split_and_floor.json.

    python3 l_split_and_floor.py POPULATION.pkl DECOMPOSE_CACHE.pkl DATASET.jsonl.gz OVERRIDES.json
"""
from __future__ import annotations

import gzip
import json
import math
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from measure import DRAWS, LABELS, SEED, SPLIT, STAGES, first_decisions, once_per_station_day, wmean_ci  # noqa: E402

EPS = (0.001, 0.002, 0.005, 0.01, 0.02)
H_BUCKETS = {"far>=24h": (24, 30, 36, 44), "mid10-24h": (10, 14, 18), "near<10h": (4, 7)}


def refloor(p, eps):
    lifted = np.maximum(p, eps)
    return lifted / lifted.sum()


def three(p, q, y):
    s = np.log(p / q)
    onehot = np.zeros_like(q)
    onehot[y] = 1.0
    d = q * np.log(q / p)
    at_floor = p <= p.min() * 1.0001
    return {"dS": float(math.log(p[y] / q[y])), "D": float(d.sum()), "profit": float(s @ (onehot - q)),
            "D_floor": float(d[at_floor].sum()), "gross": float(np.abs(s).sum()),
            "units_floor": float(-s[at_floor].sum()), "truth_floored": bool(at_floor[y])}


def floor_block(items, seed):
    rng = np.random.default_rng(seed)
    return {"decisions": len(items), "station_days": len({(d["station"], d["date"]) for d in items}),
            **{f: wmean_ci(items, f, rng) for f in ("dS", "D", "profit", "D_floor", "gross", "units_floor", "truth_floored")}}


def part_a(pop_path, cache_path):
    out = {"served_by_stage_first": {}, "today_p0_opening_24_60h": {}, "today_p0_opening_24_60h_since_0914": {}}
    served = pickle.loads(Path(pop_path).read_bytes())
    for eps in EPS:
        recs = []
        for r in served:
            d = three(refloor(r["p"], eps), r["m"], int(r["hit"]))
            d.update({"stage": r["stage"], "station": r["station"], "date": r["date"], "at": r["at"]})
            recs.append(d)
        out["served_by_stage_first"][str(eps)] = {
            stage: floor_block(first_decisions([d for d in recs if d["stage"] == stage]), SEED) for stage in STAGES}
    del served
    cache = pickle.loads(Path(cache_path).read_bytes())
    today = [r for r in cache[2] if r.get("reading") == "today_p0" and 2 <= int(r["bin"]) <= 4]
    del cache
    zero_truth = sum(1 for r in today if float(r["p"][int(r["hit"])]) <= 0.0)
    out["today_p0_truth_at_raw_zero"] = zero_truth
    out["today_p0_opening_decisions"] = len(today)
    for eps in EPS:
        recs = []
        for r in today:
            p = np.array([float(v) for v in r["p"]])
            q = np.array([float(v) for v in r["m"]])
            if p.sum() <= 0:
                continue
            d = three(refloor(p / p.sum(), eps), q / q.sum(), int(r["hit"]))
            d.update({"station": r["station"], "date": r["date"], "at": r["at"]})
            recs.append(d)
        out["today_p0_opening_24_60h"][str(eps)] = floor_block(once_per_station_day(recs), SEED + 1)
        out["today_p0_opening_24_60h_since_0914"][str(eps)] = floor_block(
            once_per_station_day([d for d in recs if d["date"] >= SPLIT]), SEED + 2)
    return out


def walk(ladder, size):
    """Cost of ``size`` shares walking ``ladder`` = [[price, shares], ...] best first.
    Returns (cost, filled)."""

    cost, left = 0.0, float(size)
    for price, shares in ladder:
        take = min(left, float(shares))
        cost += take * float(price)
        left -= take
        if left <= 1e-12:
            break
    return cost, float(size) - max(left, 0.0)


def exec_row(p, q, y, asks, bids):
    s = np.log(p / q)
    onehot = np.zeros_like(q)
    onehot[y] = 1.0
    pi_mid = float(s @ (onehot - q))
    # the paper's signed position at the book
    pi_signed, filled_ok = 0.0, True
    for k, sk in enumerate(s):
        if sk > 0:
            cost, filled = walk(asks[k], sk)
            pi_signed += sk * onehot[k] - cost
        elif sk < 0:
            size = -sk
            cost, filled = walk([[1.0 - price, shares] for price, shares in bids[k]], size)
            pi_signed += size * (1.0 - onehot[k]) - cost
        else:
            continue
        if filled < abs(sk) - 1e-9:
            filled_ok = False
    # pure-YES shift at the asks
    sp = s - s.min()
    pi_yes, yes_ok = 0.0, True
    for k, sk in enumerate(sp):
        if sk <= 0:
            continue
        cost, filled = walk(asks[k], sk)
        pi_yes += sk * onehot[k] - cost
        if filled < sk - 1e-9:
            yes_ok = False
    # the paper's no-trade band at executable prices
    pi_band, band_legs, pi_band_yes = 0.0, 0, 0.0
    for k in range(len(p)):
        ask1 = asks[k][0][0] if asks[k] else None
        bid1 = bids[k][0][0] if bids[k] else None
        if ask1 is not None and p[k] > ask1:
            size = math.log(p[k] / ask1)
            cost, _ = walk(asks[k], size)
            pi_band += size * onehot[k] - cost
            pi_band_yes += size * onehot[k] - cost
            band_legs += 1
        elif bid1 is not None and p[k] < bid1:
            size = math.log(bid1 / p[k])
            cost, _ = walk([[1.0 - price, shares] for price, shares in bids[k]], size)
            pi_band += size * (1.0 - onehot[k]) - cost
            band_legs += 1
    d = q * np.log(q / p)
    return {"dS": float(math.log(p[y] / q[y])), "D": float(d.sum()), "pi_mid": pi_mid,
            "pi_signed": pi_signed if filled_ok else None, "L_market": (pi_mid - pi_signed) if filled_ok else None,
            "pi_yes": pi_yes if yes_ok else None,
            "L_constraint": (pi_signed - pi_yes) if (filled_ok and yes_ok) else None,
            "L_total": (pi_mid - pi_yes) if yes_ok else None,
            "pi_band": pi_band, "pi_band_yes_only": pi_band_yes, "band_legs": band_legs,
            "signed_filled": filled_ok, "yes_filled": yes_ok, "gross": float(np.abs(s).sum())}


def exec_block(items, seed):
    rng = np.random.default_rng(seed)
    return {"rows": len(items), "station_days": len({(d["station"], d["date"]) for d in items}),
            "days": len({d["date"] for d in items}),
            "signed_unfilled": sum(1 for d in items if not d["signed_filled"]),
            "yes_unfilled": sum(1 for d in items if not d["yes_filled"]),
            **{f: wmean_ci(items, f, rng) for f in ("dS", "D", "pi_mid", "pi_signed", "L_market", "pi_yes",
                                                     "L_constraint", "L_total", "pi_band", "pi_band_yes_only",
                                                     "band_legs", "gross")}}


def part_b(dataset_path, overrides_path):
    from bayes_basket_target.materialization import floor_probabilities  # noqa: PLC0415
    overrides = json.loads(Path(overrides_path).read_text())["overrides"]
    recs, skipped = [], defaultdict(int)
    with gzip.open(dataset_path, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("m_state") != "complete" or not r.get("m") or not r.get("served"):
                skipped["book_incomplete"] += 1
                continue
            keys = list(r["m"])
            if set(keys) != set(r["served"]):
                skipped["rung_mismatch"] += 1
                continue
            truth = overrides.get(f"{r['st']}|{r['day']}", r["truth"])
            if truth not in keys:
                skipped["truth_not_on_ladder"] += 1
                continue
            raw = np.array([float(r["served"][k]) for k in keys])
            if raw.sum() <= 0:
                skipped["zero_vector"] += 1
                continue
            floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, raw / raw.sum())})
            p = np.array([floored[k] for k in keys])
            q = np.array([float(r["m"][k]) for k in keys])
            q = q / q.sum()
            asks = [r["asks"].get(k) or [] for k in keys]
            bids = [r["bids"].get(k) or [] for k in keys]
            d = exec_row(p, q, keys.index(truth), asks, bids)
            d.update({"station": r["st"], "date": r["day"], "H": int(r["H"]), "stage": r["stage"]})
            recs.append(d)
    out = {"rows": len(recs), "skipped": dict(skipped), "by_h_bucket": {}, "by_stage": {}}
    for label, hs in H_BUCKETS.items():
        items = [d for d in recs if d["H"] in hs]
        out["by_h_bucket"][label] = exec_block(once_per_station_day(items), SEED + 3)
    for stage in STAGES:
        items = [d for d in recs if d["stage"] == stage]
        out["by_stage"][stage] = exec_block(once_per_station_day(items), SEED + 4)
    out["all"] = exec_block(once_per_station_day(recs), SEED + 5)
    return out


def main():
    results = {"A_floor_sensitivity": part_a(sys.argv[1], sys.argv[2]), "B_L_split": part_b(sys.argv[3], sys.argv[4])}
    (HERE / "results_l_split_and_floor.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))

    def fmt(x):
        return "—" if x is None else f"{x[0]:+.3f} [{x[1]:+.3f}, {x[2]:+.3f}]"

    a = results["A_floor_sensitivity"]
    print("== A. floor sensitivity, served first decisions ==")
    print("eps | stage | dS | D | dS+D | D on floor | units on floor | truth on floor")
    for eps, by_stage in a["served_by_stage_first"].items():
        for stage, b in by_stage.items():
            print(f"{eps} | {stage} | {fmt(b['dS'])} | {fmt(b['D'])} | {fmt(b['profit'])} | {b['D_floor'][0]:+.3f} | {b['units_floor'][0]:.2f} | {b['truth_floored'][0]:.3f}")
    print("-- today P0 24-60h (truth at raw zero:", a["today_p0_truth_at_raw_zero"], "of", a["today_p0_opening_decisions"], ")")
    for key in ("today_p0_opening_24_60h", "today_p0_opening_24_60h_since_0914"):
        for eps, b in a[key].items():
            print(f"{key} {eps} | {fmt(b['dS'])} | {fmt(b['D'])} | {fmt(b['profit'])} | {b['D_floor'][0]:+.3f} | {b['units_floor'][0]:.2f} | {b['truth_floored'][0]:.3f}")
    b = results["B_L_split"]
    print("\n== B. L split on the replay book (rows", b["rows"], "skipped", b["skipped"], ") ==")
    print("group | sd/days | dS | D | Pi_mid | Pi_signed | L_market | Pi_yes | L_constraint | L_total | band (YES+NO) | band YES only | legs | unfilled signed/yes")
    for key in ("by_h_bucket", "by_stage"):
        for k, e in b[key].items():
            print(f"{k} | {e['station_days']}/{e['days']} | {fmt(e['dS'])} | {fmt(e['D'])} | {fmt(e['pi_mid'])} | {fmt(e['pi_signed'])} | "
                  f"{fmt(e['L_market'])} | {fmt(e['pi_yes'])} | {fmt(e['L_constraint'])} | {fmt(e['L_total'])} | {fmt(e['pi_band'])} | "
                  f"{fmt(e['pi_band_yes_only'])} | {e['band_legs'][0]:.2f} | {e['signed_unfilled']}/{e['yes_unfilled']}")
    e = b["all"]
    print(f"all | {e['station_days']}/{e['days']} | {fmt(e['dS'])} | {fmt(e['D'])} | {fmt(e['pi_mid'])} | {fmt(e['pi_signed'])} | "
          f"{fmt(e['L_market'])} | {fmt(e['pi_yes'])} | {fmt(e['L_constraint'])} | {fmt(e['L_total'])} | {fmt(e['pi_band'])} | "
          f"{fmt(e['pi_band_yes_only'])} | {e['band_legs'][0]:.2f} | {e['signed_unfilled']}/{e['yes_unfilled']}")


if __name__ == "__main__":
    main()
