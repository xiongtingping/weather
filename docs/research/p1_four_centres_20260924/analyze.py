"""P1 four-centre feature search (PROTOCOL.md, 2026-09-24).

    python3 analyze.py [LAG] > results.json      # LAG: "5" (primary) or "15"

Read-only: reads the event dataset built for the Bayes/Kelly replay and the
venue truth overrides beside it.  No store, no network.
"""
from __future__ import annotations

import gzip
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "bayes_kelly_replay_20260924"
LAG = sys.argv[1] if len(sys.argv) > 1 else "5"
TRAIN_END = "2026-09-13"
H_BINS = [("10-24", 10.0, 24.0), ("6-10", 6.0, 10.0), ("0-6", 0.0, 6.0)]
BANDS = [("0.05-0.30", 0.05, 0.30), ("0.30-0.50", 0.30, 0.50), ("0.50-0.70", 0.50, 0.70),
         ("0.70-0.90", 0.70, 0.90), ("0.90-0.99", 0.90, 0.99)]
TARGETS = ("Mo", "Me", "Ko", "Ke")
BOOT = 2000
NULL_SIMS = 200
NULL_BOOT = 400
MIN_N, MIN_DAYS, K_MAX = 30, 8, 10

NUM = re.compile(r"-?\d+(?:\.\d+)?")


def order_key(label: str) -> float:
    m = NUM.search(label)
    return float(m.group()) if m else float("nan")


def centres(vec: dict[str, float], buckets: list[str]):
    p = np.array([max(float(vec.get(b, 0.0)), 0.0) for b in buckets])
    if p.sum() <= 0:
        return None
    p = p / p.sum()
    top = p.max()
    if (p == top).sum() > 1:
        return None
    mode = int(p.argmax())
    mean = int(np.floor(float((p * np.arange(len(p))).sum()) + 0.5))
    return mode, mean, p


def sign3(d: int) -> str:
    return "-" if d <= -1 else ("0" if d == 0 else "+")


def load():
    ov = json.loads((SRC / "venue_truth_overrides.json").read_text())["overrides"]
    rows = []
    ties = missing_m = 0
    with gzip.open(SRC / "events.jsonl.gz", "rt") as fh:
        for line in fh:
            r = json.loads(line)
            h = float(r["hours_to_end"])
            if h >= 24.0 or h < 0.0:
                continue
            m = r.get("m_now")
            if not m:
                missing_m += 1
                continue
            common = sorted(set(r["p1"]) & set(m), key=order_key)
            if len(common) < 2:
                missing_m += 1
                continue
            cm = centres(r["p1"], common)
            ck = centres(m, common)
            if cm is None or ck is None:
                ties += 1
                continue
            mo, me, _ = cm
            ko, ke, q = ck
            d_o, d_e = mo - ko, me - ke
            if d_o == 0 and d_e == 0:
                pattern = "四心同桶" if mo == me else "两边同众数同均值·众≠均"
            else:
                pattern = f"dO{sign3(d_o)} dE{sign3(d_e)}"
            hbin = next(name for name, lo, hi in H_BINS if lo <= h < hi)
            truth = ov.get(f"{r['st']}|{r['day']}", r["truth"])
            book = (r.get("books") or {}).get(LAG) or {}
            rows.append({
                "st": r["st"], "day": r["day"], "t": r["t_ours"], "H": hbin, "P": pattern,
                "buckets": {"Mo": common[mo], "Me": common[me], "Ko": common[ko], "Ke": common[ke]},
                "q": {b: float(q[i]) for i, b in enumerate(common)},
                "truth": truth, "asks": book.get("asks") or {}, "min_order": book.get("min_order") or {},
            })
    return rows, ties, missing_m


def trades(rows):
    """cell -> {station-day: (t, day, hit, ask, q)}, first qualifying event per station-day."""
    cells: dict[tuple, dict] = defaultdict(dict)
    for r in rows:
        for tgt in TARGETS:
            b = r["buckets"][tgt]
            levels = r["asks"].get(b) or []
            if not levels:
                continue
            ask, size = float(levels[0][0]), float(levels[0][1])
            if not (0.0 < ask < 0.99) or size < float(r["min_order"].get(b, 5.0)):
                continue
            band = next((name for name, lo, hi in BANDS if lo <= ask < hi), None)
            if band is None:
                continue
            key = (r["H"], r["P"], tgt, band)
            sd = (r["st"], r["day"])
            prev = cells[key].get(sd)
            if prev is None or r["t"] < prev[0]:
                cells[key][sd] = (r["t"], r["day"], int(b == r["truth"]), ask, r["q"].get(b, 0.0))
    return cells


def arrays(sd_map, lo=None, hi=None):
    items = [v for v in sd_map.values() if (lo is None or v[1] >= lo) and (hi is None or v[1] <= hi)]
    if not items:
        return None
    days = sorted({v[1] for v in items})
    di = {d: i for i, d in enumerate(days)}
    return {
        "day_idx": np.array([di[v[1]] for v in items]), "days": days,
        "hit": np.array([v[2] for v in items], dtype=float),
        "ask": np.array([v[3] for v in items]), "q": np.array([v[4] for v in items]),
        "sd": [k for k, v in sd_map.items() if (lo is None or v[1] >= lo) and (hi is None or v[1] <= hi)],
    }


def day_boot(r, day_idx, n_days, draws, rng):
    s = np.bincount(day_idx, weights=r, minlength=n_days)
    n = np.bincount(day_idx, minlength=n_days).astype(float)
    w = rng.multinomial(n_days, np.full(n_days, 1.0 / n_days), size=draws)
    num, den = w @ s, w @ n
    ok = den > 0
    return num[ok] / den[ok]


def summarise(a, rng, draws=BOOT):
    if a is None:
        return None
    r = a["hit"] / a["ask"] - 1.0
    boots = day_boot(r, a["day_idx"], len(a["days"]), draws, rng)
    half = a["days"][len(a["days"]) // 2] if len(a["days"]) >= 2 else None
    first = r[[a["days"][i] < half for i in a["day_idx"]]] if half else r[:0]
    second = r[[a["days"][i] >= half for i in a["day_idx"]]] if half else r[:0]
    return {
        "n": int(len(r)), "days": len(a["days"]), "hit_rate": float(a["hit"].mean()),
        "avg_ask": float(a["ask"].mean()), "roi": float(r.mean()),
        "ci90": [float(np.quantile(boots, 0.05)), float(np.quantile(boots, 0.95))],
        "p_le_0": float((boots <= 0).mean()),
        "first_half_roi": float(first.mean()) if len(first) else None,
        "second_half_roi": float(second.mean()) if len(second) else None,
    }


def select(cells, rng, hit_override=None, draws=BOOT):
    cand = []
    for key, sd_map in cells.items():
        a = arrays(sd_map, hi=TRAIN_END)
        if a is None or len(a["hit"]) < MIN_N or len(a["days"]) < MIN_DAYS:
            continue
        hit = a["hit"] if hit_override is None else hit_override(a)
        r = hit / a["ask"] - 1.0
        lo = float(np.quantile(day_boot(r, a["day_idx"], len(a["days"]), draws, rng), 0.05))
        if lo > 0:
            cand.append((lo, key, set(a["sd"])))
    cand.sort(key=lambda t: -t[0])
    kept = []
    for lo, key, sds in cand:
        if any(len(sds & k[2]) / len(sds | k[2]) >= 0.8 for k in kept):
            continue
        kept.append((lo, key, sds))
        if len(kept) >= K_MAX:
            break
    return kept, len(cand)


def main() -> None:
    rng = np.random.default_rng(924)
    rows, ties, missing_m = load()
    cells = trades(rows)
    kept, n_pass = select(cells, rng)
    k = max(len(kept), 1)
    out = {
        "lag_minutes": LAG, "events_used": len(rows), "mode_ties_dropped": ties,
        "no_market_dropped": missing_m, "cells_nonempty": len(cells),
        "cells_eligible_train": sum(
            1 for sd in cells.values()
            if (a := arrays(sd, hi=TRAIN_END)) is not None and len(a["hit"]) >= MIN_N and len(a["days"]) >= MIN_DAYS
        ),
        "train_lower_bound_positive": n_pass, "selected": [],
    }
    for lo, key, _ in kept:
        test = summarise(arrays(cells[key], lo="2026-09-14"), rng)
        passed = bool(
            test and test["roi"] > 0 and test["p_le_0"] < 0.10 / k
            and test["n"] >= 10 and test["days"] >= 5
        )
        out["selected"].append({
            "cell": {"H": key[0], "P": key[1], "T": key[2], "B": key[3]},
            "train": summarise(arrays(cells[key], hi=TRAIN_END), rng),
            "test": test, "all": summarise(arrays(cells[key]), rng),
            "bonferroni_alpha": 0.10 / k, "passed": passed,
        })
    concentric = ("6-10", "四心同桶", "Ko", "0.50-0.70")
    out["concentric_cell"] = {
        "train": summarise(arrays(cells.get(concentric, {}), hi=TRAIN_END), rng),
        "test": summarise(arrays(cells.get(concentric, {}), lo="2026-09-14"), rng),
        "all": summarise(arrays(cells.get(concentric, {})), rng),
    }
    # Search noise floor under "the market is right" (amendment 1).
    floor = []
    for i in range(NULL_SIMS):
        sim_rng = np.random.default_rng(10_000 + i)
        kept_null, _ = select(
            cells, sim_rng,
            hit_override=lambda a, g=sim_rng: (g.random(len(a["q"])) < a["q"]).astype(float),
            draws=NULL_BOOT,
        )
        floor.append(kept_null[0][0] if kept_null else float("-inf"))
    fin = [x for x in floor if np.isfinite(x)]
    out["noise_floor"] = {
        "sims": NULL_SIMS, "sims_with_any_selected": len(fin),
        "best_lower_bound_p95": float(np.quantile(floor, 0.95)) if fin else None,
        "best_lower_bound_median": float(np.median(floor)) if fin else None,
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
