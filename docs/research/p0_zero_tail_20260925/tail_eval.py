#!/usr/bin/env python3
"""Fit the P0 tail on the training period, score it once on the test period (PROTOCOL.md).

Two data sets: the six-family replay's ``ll_on`` served vectors (integer
buckets, today's arithmetic) and every settled live P0 envelope (market
ladder).  Writes tail_eval.json.
"""
from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import norm, t as student_t

HERE = Path(__file__).resolve().parent
REPLAY = HERE.parent / "p0_replay_219x216_20260925" / "pass1"
EPS = 1e-12                      # native_ensemble_oos._metrics' log floor
NU = 3.0
EPSILONS = (0.002, 0.005, 0.01, 0.02, 0.03, 0.05)
SCALES = (1.5, 2.0, 3.0, 4.0, 5.0)
BLOCK, BOOT = 3, 2000
WINDOW = 25                      # integer buckets either side of the rounded mean


def load_replay():
    recs = []
    for path in sorted(REPLAY.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                arm = r["ll_on"]
                recs.append((r["station"], r["event_date"], float(r["actual"]), float(arm["mean"]),
                             {int(k): float(v) for k, v in arm["vec"].items()}))
    n = len(recs)
    width = 2 * WINDOW + 1
    lo = np.zeros((n, width)); hi = np.zeros((n, width)); p = np.zeros((n, width)); y = np.zeros((n, width))
    mu = np.zeros(n); days = []; sd = []
    for i, (st, day, actual, mean, vec) in enumerate(recs):
        centre = int(round(mean))
        keys = np.arange(centre - WINDOW, centre + WINDOW + 1)
        lo[i] = keys - 0.5; hi[i] = keys + 0.5
        p[i] = [vec.get(int(k), 0.0) for k in keys]
        total = p[i].sum()
        if total > 0:
            p[i] /= total
        truth = math.floor(actual + 0.5)
        if not keys[0] <= truth <= keys[-1]:
            raise RuntimeError(f"truth outside window {st} {day}")
        y[i, truth - keys[0]] = 1.0
        mu[i] = mean
        days.append(day); sd.append((st, day))
    return {"lo": lo, "hi": hi, "p": p, "y": y, "mu": mu, "day": np.array(days), "sd": sd}


def load_live():
    recs = []
    with gzip.open(HERE / "p0_envelopes.jsonl.gz", "rt") as handle:
        for line in handle:
            recs.append(json.loads(line))
    width = max(len(r["keys"]) for r in recs)
    n = len(recs)
    lo = np.full((n, width), np.nan); hi = np.full((n, width), np.nan)
    p = np.zeros((n, width)); y = np.zeros((n, width)); mu = np.zeros(n)
    days = []; sd = []
    for i, r in enumerate(recs):
        k = len(r["keys"])
        los = [(-np.inf if v is None else float(v)) for v in r["lo"]]
        his = [(np.inf if v is None else float(v)) for v in r["hi"]]
        lo[i, :k] = los; hi[i, :k] = his
        pv = np.array(r["p"], dtype=float)
        pv = pv / pv.sum()
        p[i, :k] = pv
        y[i, r["truth_index"]] = 1.0
        closed = [h - l for l, h in zip(los, his) if np.isfinite(l) and np.isfinite(h)]
        step = float(np.median(closed)) if closed else 1.0
        centres = [((l + h) / 2 if np.isfinite(l) and np.isfinite(h) else (h - step / 2 if np.isfinite(h) else l + step / 2))
                   for l, h in zip(los, his)]
        mu[i] = float(np.dot(pv, centres))
        days.append(r["event_date"]); sd.append((r["station"], r["event_date"]))
    return {"lo": lo, "hi": hi, "p": p, "y": y, "mu": mu, "day": np.array(days), "sd": sd}


def tail_q(data, scale, kind):
    dist = student_t(NU) if kind == "t" else norm()
    z_hi = (data["hi"] - data["mu"][:, None]) / scale
    z_lo = (data["lo"] - data["mu"][:, None]) / scale
    q = np.nan_to_num(dist.cdf(z_hi) - dist.cdf(z_lo), nan=0.0)
    return q / q.sum(axis=1, keepdims=True)


def scores(p, y):
    brier = ((p - y) ** 2).sum(axis=1)
    pt = (p * y).sum(axis=1)
    nll = -np.log(np.maximum(pt, EPS))
    return brier, nll, pt


def weights(sd):
    count = defaultdict(int)
    for key in sd:
        count[key] += 1
    return np.array([1.0 / count[key] for key in sd])


def boot_ci(delta, w, days, seed=20260925):
    uniq = sorted(set(days.tolist()))
    idx_by_day = defaultdict(list)
    for i, d in enumerate(days.tolist()):
        idx_by_day[d].append(i)
    idx_by_day = {d: np.array(v) for d, v in idx_by_day.items()}
    rng = np.random.default_rng(seed)
    n_blocks = math.ceil(len(uniq) / BLOCK)
    out = []
    for _ in range(BOOT):
        idx = np.concatenate([idx_by_day[uniq[(s + j) % len(uniq)]]
                              for s in rng.integers(0, len(uniq), n_blocks) for j in range(BLOCK)])
        ww = w[idx]
        out.append(float((ww * delta[idx]).sum() / ww.sum()))
    return [float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))]


def subset(data, mask):
    return {k: (v[mask] if isinstance(v, np.ndarray) else [x for x, m in zip(v, mask) if m]) for k, v in data.items()}


def run(name, data, train_mask, kind):
    train, test = subset(data, train_mask), subset(data, ~train_mask)
    w_tr, w_te = weights(train["sd"]), weights(test["sd"])
    grid = []
    for s in SCALES:
        q = tail_q(train, s, kind)
        for e in EPSILONS:
            b, n, _ = scores((1 - e) * train["p"] + e * q, train["y"])
            grid.append({"eps": e, "scale": s, "train_nll": float((w_tr * n).sum() / w_tr.sum()),
                         "train_brier": float((w_tr * b).sum() / w_tr.sum())})
    best = min(grid, key=lambda g: g["train_nll"])
    b0, n0, p0 = scores(test["p"], test["y"])
    q = tail_q(test, best["scale"], kind)
    pt_ = (1 - best["eps"]) * test["p"] + best["eps"] * q
    b1, n1, p1 = scores(pt_, test["y"])
    mean = lambda v: float((w_te * v).sum() / w_te.sum())
    out = {
        "data": name, "kind": kind, "chosen": best,
        "train": {"updates": int(train_mask.sum()), "station_days": len(set(train["sd"]))},
        "test": {"updates": int((~train_mask).sum()), "station_days": len(set(test["sd"])),
                 "days": len(set(test["day"].tolist()))},
        "brier": {"base": mean(b0), "tail": mean(b1), "delta": mean(b1 - b0), "ci95": boot_ci(b1 - b0, w_te, test["day"])},
        "logscore": {"base": mean(n0), "tail": mean(n1), "delta": mean(n1 - n0), "ci95": boot_ci(n1 - n0, w_te, test["day"])},
        "zero_truth_updates": {"base": int((p0 <= 0).sum()), "tail": int((p1 <= 0).sum())},
        "min_bucket_probability_tail": float(pt_[~np.isnan(test["lo"])].min()),
        "median_truth_probability": {"base": float(np.median(p0)), "tail": float(np.median(p1))},
    }
    out["holds"] = bool(out["brier"]["ci95"][1] < 0 and out["logscore"]["ci95"][1] < 0)
    return out


def main():
    results = []
    replay = load_replay()
    live = load_live()
    for kind in ("t", "normal"):
        results.append(run("replay_ll_on", replay, replay["day"] <= "2026-08-31", kind))
        results.append(run("live_p0", live, live["day"] <= "2026-09-08", kind))
    (HERE / "tail_eval.json").write_text(json.dumps(results, indent=1))
    for r in results:
        c = r["chosen"]
        print(f"{r['data']:13s} {r['kind']:6s} eps={c['eps']} s={c['scale']} | test {r['test']['station_days']} 站日 {r['test']['updates']} 更新 | "
              f"Brier {r['brier']['base']:.5f}->{r['brier']['tail']:.5f} ({r['brier']['delta']:+.5f} [{r['brier']['ci95'][0]:+.5f},{r['brier']['ci95'][1]:+.5f}]) | "
              f"LogScore {r['logscore']['base']:.5f}->{r['logscore']['tail']:.5f} ({r['logscore']['delta']:+.5f} [{r['logscore']['ci95'][0]:+.5f},{r['logscore']['ci95'][1]:+.5f}]) | "
              f"zeros {r['zero_truth_updates']} | holds={r['holds']}", flush=True)


if __name__ == "__main__":
    main()
