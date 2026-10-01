#!/usr/bin/env python3
"""PROTOCOL.md: a separate width lambda for P0 more than 36h before settlement.

Reads the two committed replays, nothing else (no SSOT):
  width_lambda_recheck_20260925/pass1  exact (bucket Brier, LogScore) per grid lambda
  p0_replay_219x216_20260925/pass1     timezone, truth arrival, ll_on as-served vector

    python3 far_lead.py        -> results.json and a printed summary
"""

from __future__ import annotations

import gzip
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

LAMBDA_DIR = ROOT / "docs" / "research" / "width_lambda_recheck_20260925" / "pass1"
REPLAY_DIR = ROOT / "docs" / "research" / "p0_replay_219x216_20260925" / "pass1"
GRID = ("0.60", "0.70", "0.75", "0.80", "0.85", "0.90", "1.00")
BASE = GRID.index("0.75")
FAR_H = 36.0
TEST = ("2026-09-01", "2026-09-23")
HALVES = (("2026-09-01", "2026-09-12"), ("2026-09-13", "2026-09-23"))
HALF, BLOCK, BOOT, SEED = 5, 3, 2000, 20260927
PIT_BINS, REL_BINS = 10, 11
BANDS = {"band_35_50": slice(7, 10), "band_ge50": slice(10, 11)}
TOL = {"logscore": 0.002, "brier": 0.0005}


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ---------------------------------------------------------------- vectors

def rebuild(vec: dict[int, float], lam_rel: float) -> dict[int, float]:
    """The width layer's interior relocation, applied to an as-served vector.

    Same rule as ``center_width_calibration.center_width_adjusted_grid``:
    each rung's mass moves to mean + (centre - mean) * lambda and is split
    linearly between the two neighbouring rungs.  The pivot is the served
    mean, which is the pre-table mean plus the table's delta, so composing
    lam_rel onto the served lambda lands where the new lambda would.
    """

    total = sum(vec.values())
    mean = sum(k * v for k, v in vec.items()) / total
    lo, hi = min(vec) - 8, max(vec) + 8
    out = [0.0] * (hi - lo + 1)
    for k, v in vec.items():
        pos = mean + (k - mean) * lam_rel
        if pos <= lo:
            out[0] += v
        elif pos >= hi:
            out[-1] += v
        else:
            f = math.floor(pos)
            share = pos - f
            out[f - lo] += v * (1.0 - share)
            out[f + 1 - lo] += v * share
    return {lo + i: x / total for i, x in enumerate(out) if x > 0.0}


def fold(vec: dict[int, float], centre: int, actual: float):
    rungs = [0.0] * (2 * HALF + 1)
    for k, v in vec.items():
        rungs[min(max(k - centre, -HALF), HALF) + HALF] += v
    total = sum(rungs)
    floored, _ = floor_probabilities({str(i): x / total for i, x in enumerate(rungs)})
    p = [floored[str(i)] for i in range(2 * HALF + 1)]
    hit = min(max(math.floor(actual + 0.5) - centre, -HALF), HALF) + HALF
    return p, hit


def brier_ls(p, hit):
    return (sum((x - (1.0 if i == hit else 0.0)) ** 2 for i, x in enumerate(p)),
            -math.log(max(p[hit], 1e-12)))


def pit_bins(p, hit):
    lower = sum(p[:hit])
    upper = lower + p[hit]
    bins = [0.0] * PIT_BINS
    if upper - lower <= 1e-12:
        bins[min(PIT_BINS - 1, max(0, int(lower * PIT_BINS)))] = 1.0
    else:
        for i in range(PIT_BINS):
            left, right = max(lower, i / PIT_BINS), min(upper, (i + 1) / PIT_BINS)
            if right > left:
                bins[i] = (right - left) / (upper - lower)
    s = sum(bins)
    return [b / s for b in bins]


def rel_sums(p, hit):
    sums = np.zeros((REL_BINS, 3))
    for i, v in enumerate(p):
        b = min(REL_BINS - 1, max(0, int(v / 0.05)))
        sums[b, 0] += v
        sums[b, 1] += 1.0 if i == hit else 0.0
        sums[b, 2] += 1.0
    return sums


# ---------------------------------------------------------------- data

def load():
    exact = {}
    for path in sorted(LAMBDA_DIR.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                exact[(r["station"], r["event_date"], r["at"])] = r
    rows = []
    for path in sorted(REPLAY_DIR.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                e = exact.pop((r["station"], r["event_date"], r["at"]))
                end = datetime.fromisoformat(r["event_date"]).replace(tzinfo=ZoneInfo(r["tz"])) + timedelta(days=1)
                rows.append({
                    "station": r["station"], "day": r["event_date"], "at": r["at"],
                    "h": (end - _utc(r["at"])).total_seconds() / 3600.0,
                    "truth_known": _utc(r["truth_known_at"]).timestamp(),
                    "actual": float(r["actual"]),
                    "served_lambda": e["served_lambda"],
                    "exact": [tuple(e["scores"][g]) for g in GRID],
                    "exact_served": tuple(e["scores"]["served"]),
                    "vec": {int(k): float(v) for k, v in r["ll_on"]["vec"].items()},
                })
    if exact:
        raise RuntimeError(f"{len(exact)} lambda rows without a replay row")
    first = {}
    for i, r in enumerate(rows):
        key = (r["station"], r["day"])
        if key not in first or r["at"] < rows[first[key]]["at"]:
            first[key] = i
    for i in first.values():
        rows[i]["first"] = True
    by_sd = defaultdict(list)
    for i, r in enumerate(rows):
        by_sd[(r["station"], r["day"])].append(i)
    for idx in by_sd.values():
        # Day-ahead root: the last update in the 24h before the event day's local start
        # (settlement is that day's end, h hours after the update).
        r = rows[idx[0]]
        midnight = _utc(r["at"]) + timedelta(hours=r["h"]) - timedelta(days=1)
        before = [i for i in idx if midnight - timedelta(days=1) <= _utc(rows[i]["at"]) < midnight]
        if before:
            rows[max(before, key=lambda i: rows[i]["at"])]["day_ahead"] = True
    return rows


def rebuild_all(rows):
    """Per row, per grid lambda: folded floored vector and hit, on the recheck's ladder."""

    n = len(rows)
    P = np.zeros((n, len(GRID), 2 * HALF + 1))
    hits = np.zeros(n, dtype=int)
    recon = np.zeros((n, len(GRID), 2))
    for i, r in enumerate(rows):
        centre = int(round(sum(k * v for k, v in r["vec"].items())))
        served = float(r["served_lambda"] or 1.0)
        for g, lam in enumerate(GRID):
            p, hit = fold(rebuild(r["vec"], float(lam) / served), centre, r["actual"])
            P[i, g] = p
            recon[i, g] = brier_ls(p, hit)
        hits[i] = hit
    return P, hits, recon


# ---------------------------------------------------------------- statistics

def weights(rows, idx):
    count = defaultdict(int)
    for i in idx:
        count[(rows[i]["station"], rows[i]["day"])] += 1
    return np.array([1.0 / count[(rows[i]["station"], rows[i]["day"])] for i in idx])


def block_draws(rows, idx, rng):
    days = sorted({rows[i]["day"] for i in idx})
    by_day = defaultdict(list)
    for j, i in enumerate(idx):
        by_day[rows[i]["day"]].append(j)
    n_blocks = math.ceil(len(days) / BLOCK)
    return [np.concatenate([by_day[days[(s + b) % len(days)]]
                            for s in rng.integers(0, len(days), n_blocks) for b in range(BLOCK)])
            for _ in range(BOOT)]


def mean_diff(rows, idx, base, test):
    """Station-day weighted mean of test - base, with its block-bootstrap interval."""

    w = weights(rows, idx)
    d = test - base
    point = float((w * d).sum() / w.sum())
    rng = np.random.default_rng(SEED)
    boots = [float((w[j] * d[j]).sum() / w[j].sum()) for j in block_draws(rows, idx, rng)]
    return {"base": float((w * base).sum() / w.sum()), "test": float((w * test).sum() / w.sum()),
            "delta": point, "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]}


def readings(w, pit, rel):
    W = w.sum()
    hist = (w[:, None] * pit).sum(axis=0) / W
    sums = (w[:, None, None] * rel).sum(axis=0)
    n = sums[:, 2]
    share = n / n.sum()
    with np.errstate(invalid="ignore", divide="ignore"):
        promised = np.where(n > 0, sums[:, 0] / n, 0.0)
        happened = np.where(n > 0, sums[:, 1] / n, 0.0)
    out = {
        "pit_hist": hist,
        "pit_max_dev": float(np.abs(hist - 0.1).max()),
        "pit_ssd": float(((hist - 0.1) ** 2).sum() * PIT_BINS),
        "reliability": float((share * (promised - happened) ** 2).sum()),
        "ece": float((share * np.abs(promised - happened)).sum()),
    }
    for name, sl in BANDS.items():
        s = sums[sl].sum(axis=0)
        out[name] = float(s[1] / s[2] - s[0] / s[2]) if s[2] > 0 else float("nan")
        out[name + "_pairs"] = float(s[2])
    return out


SCALARS = ("pit_max_dev", "pit_ssd", "reliability", "ece", "band_35_50", "band_ge50")


def calibration(rows, idx, P_base, P_test, hits):
    """Readings of two vector sets on the same rows, with block-bootstrap intervals."""

    w = weights(rows, idx)
    arrays = {}
    for name, P in (("base", P_base), ("test", P_test)):
        pit = np.array([pit_bins(list(P[j]), int(hits[i])) for j, i in enumerate(idx)])
        rel = np.array([rel_sums(list(P[j]), int(hits[i])) for j, i in enumerate(idx)])
        arrays[name] = (pit, rel)
    A, B = readings(w, *arrays["base"]), readings(w, *arrays["test"])
    rng = np.random.default_rng(SEED)
    boots = {k: {"base": [], "test": [], "delta": []} for k in SCALARS}
    for j in block_draws(rows, idx, rng):
        a = readings(w[j], arrays["base"][0][j], arrays["base"][1][j])
        b = readings(w[j], arrays["test"][0][j], arrays["test"][1][j])
        for k in SCALARS:
            boots[k]["base"].append(a[k])
            boots[k]["test"].append(b[k])
            boots[k]["delta"].append(b[k] - a[k])
    ci = {k: {s: [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))] for s, v in d.items()}
          for k, d in boots.items()}
    strip = lambda R: {k: (v.round(4).tolist() if isinstance(v, np.ndarray) else v) for k, v in R.items()}  # noqa: E731
    return {"station_days": len({(rows[i]["station"], rows[i]["day"]) for i in idx}), "updates": len(idx),
            "base": strip(A), "test": strip(B), "ci95": ci}


# ---------------------------------------------------------------- the test

def walk_forward(rows, exact_ls, far_only: bool):
    """lambda(d) per test day: grid minimum of station-day weighted LogScore on known truths."""

    at = np.array([_utc(r["at"]).timestamp() for r in rows])
    known = np.array([r["truth_known"] for r in rows])
    far = np.array([r["h"] > FAR_H for r in rows])
    days = sorted({r["day"] for r in rows if TEST[0] <= r["day"] <= TEST[1]})
    chosen = {}
    for d in days:
        t_d = at[[r["day"] == d for r in rows]].min()
        mask = known < t_d
        if far_only:
            mask &= far
        idx = np.nonzero(mask)[0]
        w = weights(rows, idx)
        scores = (w[:, None] * exact_ls[idx]).sum(axis=0) / w.sum()
        best = min(range(len(GRID)), key=lambda g: (round(scores[g], 12), abs(float(GRID[g]) - 0.75)))
        chosen[d] = {"lambda": GRID[best], "train_updates": int(len(idx)),
                     "train_station_days": len({(rows[i]["station"], rows[i]["day"]) for i in idx}),
                     "train_logscore_by_lambda": {GRID[g]: round(float(scores[g]), 5) for g in range(len(GRID))}}
    return chosen


def main():
    rows = load()
    n = len(rows)
    exact = np.array([r["exact"] for r in rows])          # n x grid x (brier, ls)
    P, hits, recon = rebuild_all(rows)
    in_test = np.array([TEST[0] <= r["day"] <= TEST[1] for r in rows])
    far = np.array([r["h"] > FAR_H for r in rows])
    first = np.array([bool(r.get("first")) for r in rows])
    ahead = np.array([bool(r.get("day_ahead")) for r in rows])
    panel = np.nonzero(in_test & far)[0]
    near = np.nonzero(in_test & ~far)[0]
    first_far = np.nonzero(in_test & far & first)[0]
    result = {"rows": n, "panel_updates": int(len(panel))}

    # Sanity: the as-served score is the identity column before the first table and 0.75 after.
    served = np.array([r["exact_served"] for r in rows])
    ident = np.array([r["served_lambda"] is None for r in rows])
    result["served_matches_grid"] = {
        "identity_rows_vs_1.00": float(np.abs(served[ident] - exact[ident, GRID.index("1.00")]).max()),
        "table_rows_vs_0.75": float(np.abs(served[~ident] - exact[~ident, BASE]).max()),
    }

    # Fidelity gate: panel-level delta (lambda - 0.75), rebuilt vs exact.
    w = weights(rows, panel)
    gate, passed = {}, True
    for g, lam in enumerate(GRID):
        entry = {}
        for s, name in ((0, "brier"), (1, "logscore")):
            ex = float((w * (exact[panel, g, s] - exact[panel, BASE, s])).sum() / w.sum())
            rc = float((w * (recon[panel, g, s] - recon[panel, BASE, s])).sum() / w.sum())
            tol = max(0.2 * abs(ex), TOL[name])
            ok = abs(rc - ex) <= tol
            passed &= ok
            entry[name] = {"exact_delta": ex, "rebuilt_delta": rc, "tolerance": tol, "ok": bool(ok),
                           "mean_abs_row_diff": float(np.abs(recon[panel, g, s] - exact[panel, g, s]).mean())}
        gate[lam] = entry
    result["fidelity"] = {"passed": bool(passed), "by_lambda": gate}

    # Walk-forward arms.
    lam_far = walk_forward(rows, exact[:, :, 1], far_only=True)
    lam_all = walk_forward(rows, exact[:, :, 1], far_only=False)
    result["lambda_far_by_day"] = lam_far
    result["lambda_all_by_day"] = lam_all
    g_far = np.full(n, BASE)
    g_all = np.full(n, BASE)
    for i, r in enumerate(rows):
        if TEST[0] <= r["day"] <= TEST[1]:
            if r["h"] > FAR_H:
                g_far[i] = GRID.index(lam_far[r["day"]]["lambda"])
            g_all[i] = GRID.index(lam_all[r["day"]]["lambda"])
    F = exact[np.arange(n), g_far]
    G = exact[np.arange(n), g_all]
    B = exact[:, BASE]

    verdict = {}
    for s, name in ((0, "brier"), (1, "logscore")):
        verdict[name] = mean_diff(rows, panel, B[panel, s], F[panel, s])
        verdict[name]["halves"] = []
        for lo, hi in HALVES:
            half = np.array([i for i in panel if lo <= rows[i]["day"] <= hi])
            wh = weights(rows, half)
            verdict[name]["halves"].append(float((wh * (F[half, s] - B[half, s])).sum() / wh.sum()))
    holds = all(verdict[k]["delta"] < 0 and verdict[k]["ci95"][1] < 0 and all(x < 0 for x in verdict[k]["halves"])
                for k in ("brier", "logscore"))
    result["verdict_panel"] = verdict
    result["verdict"] = "HOLDS" if holds else "DOES_NOT_HOLD"
    result["first_decision_far"] = {name: mean_diff(rows, first_far, B[first_far, s], F[first_far, s])
                                    for s, name in ((0, "brier"), (1, "logscore"))}
    result["near_panel_max_abs_diff"] = float(np.abs(F[near] - B[near]).max())
    all_test = np.nonzero(in_test)[0]
    result["arm_G_all_test"] = {name: mean_diff(rows, all_test, B[all_test, s], G[all_test, s])
                                for s, name in ((0, "brier"), (1, "logscore"))}
    result["arm_G_far_panel"] = {name: mean_diff(rows, panel, B[panel, s], G[panel, s])
                                 for s, name in ((0, "brier"), (1, "logscore"))}

    # Calibration readings (report only), through the fidelity gate.
    if passed:
        P_B = P[:, BASE]
        P_F = P[np.arange(n), g_far]
        result["calibration_F_vs_B"] = {
            "far_panel": calibration(rows, panel, P_B[panel], P_F[panel], hits),
            "first_decision_far": calibration(rows, first_far, P_B[first_far], P_F[first_far], hits),
        }
        # Today's arithmetic (lambda 0.75 on every update) against the as-served replay vectors,
        # which carried no width table before 2026-09-03T22:15Z.
        P_S = np.where(ident[:, None], P[:, GRID.index("1.00")], P[:, BASE])
        every = np.arange(n)
        first_all = np.nonzero(first & far)[0]
        ahead_all = np.nonzero(ahead)[0]
        result["calibration_today_vs_as_served"] = {
            "all_updates": calibration(rows, every, P_S[every], P_B[every], hits),
            "first_decision_far": calibration(rows, first_all, P_S[first_all], P_B[first_all], hits),
            "day_ahead_roots": calibration(rows, ahead_all, P_S[ahead_all], P_B[ahead_all], hits),
        }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    summary(result)


def summary(result):
    print("served column check:", result["served_matches_grid"])
    print("fidelity passed:", result["fidelity"]["passed"])
    for lam, e in result["fidelity"]["by_lambda"].items():
        print(f"  {lam}: " + " | ".join(
            f"{k} exact {v['exact_delta']:+.5f} rebuilt {v['rebuilt_delta']:+.5f} tol {v['tolerance']:.5f} "
            f"{'ok' if v['ok'] else 'FAIL'} rowdiff {v['mean_abs_row_diff']:.5f}" for k, v in e.items()))
    print("lambda_far by day:", {d: v["lambda"] for d, v in result["lambda_far_by_day"].items()})
    print("lambda_all by day:", {d: v["lambda"] for d, v in result["lambda_all_by_day"].items()})
    for k, v in result["verdict_panel"].items():
        print(f"F vs B far panel {k}: {v['base']:.5f}->{v['test']:.5f} ({v['delta']:+.5f} "
              f"[{v['ci95'][0]:+.5f},{v['ci95'][1]:+.5f}]) halves {[round(x, 5) for x in v['halves']]}")
    print("VERDICT:", result["verdict"])
    for label in ("first_decision_far", "arm_G_all_test", "arm_G_far_panel"):
        for k, v in result[label].items():
            print(f"{label} {k}: {v['base']:.5f}->{v['test']:.5f} ({v['delta']:+.5f} [{v['ci95'][0]:+.5f},{v['ci95'][1]:+.5f}])")
    print("near panel max |F-B|:", result["near_panel_max_abs_diff"])
    for block in ("calibration_F_vs_B", "calibration_today_vs_as_served"):
        for seg, res in (result.get(block) or {}).items():
            print(f"{block} {seg} ({res['station_days']} sd, {res['updates']} updates)")
            for k in SCALARS:
                b, t, ci = res["base"][k], res["test"][k], res["ci95"][k]["delta"]
                print(f"   {k:12s} {b:+.4f} -> {t:+.4f}  delta [{ci[0]:+.4f},{ci[1]:+.4f}]")
            print(f"   PIT base {res['base']['pit_hist']}\n   PIT test {res['test']['pit_hist']}")


if __name__ == "__main__":
    main()
