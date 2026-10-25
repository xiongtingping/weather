#!/usr/bin/env python3
"""Arms A/B/C/D (PROTOCOL.md): walk-forward fit and scores on records.pkl.

    python3 docs/research/det_ens_blend_20261025/run.py [--train-from 2026-10-01]

Arm A is the ensemble-only P0 vector as served.  B, C and D remap it: the
vector is read as a piecewise-uniform density (an open bucket cut at one bucket
width), mapped by x -> mu' + s * (x - mu_A) and integrated back onto the same
buckets; the 1e-3 floor last on every arm.  Parameters are fitted on the mean
bucket LogScore of the event days before each scored day.
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import statistics
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
ROSTER = ("AIFS", "IFS9KM", "UKMO10", "CMA", "IFS_HRES", "GFS", "AIGFS", "GDPS", "ICON_GLOBAL", "UKMO_GLOBAL")
FLOOR = 1e-3
SCORE_FROM = "2026-10-01"
MIN_TRAIN_DAYS = 8
DRAWS = 2000
ARMS = ("A", "B", "C", "D")
N_PARAMS = {"B": 3, "C": 5, "D": 6}


def load(path: Path) -> list[dict]:
    recs = pickle.loads(path.read_bytes())
    out = []
    for r in recs:
        sys_vals = {k: v for k, v in r["systems"].items() if k in ROSTER}
        if len(sys_vals) < 3:
            continue
        r = dict(r)
        r["systems"] = sys_vals
        out.append(r)
    return out


class Batch:
    """Padded arrays for a set of records (max K buckets)."""

    def __init__(self, recs: list[dict]):
        K = max(len(r["p"]) for r in recs)
        n = len(recs)
        self.p = np.zeros((n, K))
        self.slo = np.zeros((n, K))
        self.shi = np.ones((n, K))
        self.tlo = np.full((n, K), np.inf)
        self.thi = np.full((n, K), np.inf)
        self.mask = np.zeros((n, K), bool)
        self.win = np.zeros(n, int)
        self.y = np.array([r["y_c"] for r in recs])
        self.L = np.array([(r["hours"] - 36.0) / 24.0 for r in recs])
        for i, r in enumerate(recs):
            iv = r["intervals"]
            widths = [hi - lo for lo, hi in iv if lo is not None and hi is not None]
            w = statistics.median(widths) if widths else 1.0
            for j, (lo, hi) in enumerate(iv):
                slo = lo if lo is not None else hi - w
                shi = hi if hi is not None else lo + w
                self.slo[i, j], self.shi[i, j] = slo, shi
                self.tlo[i, j] = lo if lo is not None else -np.inf
                self.thi[i, j] = hi if hi is not None else np.inf
                self.mask[i, j] = True
            self.p[i, : len(r["p"])] = r["p"]
            self.win[i] = r["winner"]
        self.p /= self.p.sum(1, keepdims=True)
        self.mid = (self.slo + self.shi) / 2.0
        self.mu = (self.p * self.mid).sum(1)
        self.sd = np.sqrt((self.p * (self.mid - self.mu[:, None]) ** 2).sum(1) + (self.shi - self.slo).mean(1) ** 2 / 12.0)

    def remap(self, mu_new: np.ndarray, s: np.ndarray) -> np.ndarray:
        a = mu_new[:, None] + s[:, None] * (self.slo - self.mu[:, None])
        b = mu_new[:, None] + s[:, None] * (self.shi - self.mu[:, None])
        span = b - a  # (n, K) source
        lo = np.maximum(a[:, :, None], self.tlo[:, None, :])
        hi = np.minimum(b[:, :, None], self.thi[:, None, :])
        frac = np.clip(hi - lo, 0.0, None) / span[:, :, None]
        q = (self.p[:, :, None] * frac).sum(1)
        return q

    def floor(self, q: np.ndarray) -> np.ndarray:
        q = np.where(self.mask, np.maximum(q, FLOOR), 0.0)
        return q / q.sum(1, keepdims=True)

    def scores(self, q: np.ndarray) -> dict[str, np.ndarray]:
        idx = np.arange(len(q))
        ls = -np.log(q[idx, self.win])
        o = np.zeros_like(q)
        o[idx, self.win] = 1.0
        brier = ((q - o) ** 2).sum(1)
        rps = ((np.cumsum(q, 1) - np.cumsum(o, 1)) ** 2 * self.mask).sum(1)
        return {"logscore": ls, "brier": brier, "crps": rps}


def det_features(recs: list[dict], bias: dict[str, float]) -> tuple[np.ndarray, np.ndarray]:
    F, Z = [], []
    for r in recs:
        vals = [v + bias.get(k, 0.0) for k, v in r["systems"].items()]
        F.append(statistics.median(vals))
        Z.append(math.log(float(np.std(vals)) + 0.2))
    return np.array(F), np.array(Z)


def fit_bias(recs: list[dict]) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for r in recs:
        for k, v in r["systems"].items():
            acc.setdefault(k, []).append(r["y_c"] - v)
    return {k: statistics.mean(v) for k, v in acc.items()}


def arm_vector(arm: str, theta: np.ndarray, B: Batch, F: np.ndarray, Z: np.ndarray) -> np.ndarray:
    if arm == "A":
        return B.floor(B.p.copy())
    a, w0, w1 = theta[:3]
    mu = B.mu + a + (w0 + w1 * B.L) * (F - B.mu)
    if arm == "B":
        s = np.ones_like(mu)
    else:
        logs = theta[3] + theta[4] * B.L + (theta[5] * Z if arm == "D" else 0.0)
        s = np.exp(np.clip(logs, -1.5, 1.5))
    return B.floor(B.remap(mu, s))


def fit_arm(arm: str, B: Batch, F: np.ndarray, Z: np.ndarray, x0: np.ndarray | None) -> np.ndarray:
    k = N_PARAMS[arm]
    start = np.zeros(k) if x0 is None else np.asarray(x0, float)[:k]
    if len(start) < k:
        start = np.concatenate([start, np.zeros(k - len(start))])

    def obj(theta):
        q = arm_vector(arm, theta, B, F, Z)
        return float(B.scores(q)["logscore"].mean())

    res = minimize(obj, start, method="L-BFGS-B", options={"maxiter": 200})
    return res.x


def boot(per_day: dict[str, list[float]], seed: int = 279) -> tuple[float, float]:
    days = sorted(per_day)
    rng = np.random.default_rng(seed)
    sums = np.array([sum(per_day[d]) for d in days])
    cnts = np.array([len(per_day[d]) for d in days])
    stats = []
    for _ in range(DRAWS):
        pick = rng.integers(0, len(days), len(days))
        stats.append(sums[pick].sum() / cnts[pick].sum())
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def run(recs: list[dict], train_from: str | None) -> dict:
    days = sorted({r["day"] for r in recs})
    scored_days = [d for d in days if d >= SCORE_FROM]
    rows: dict[str, list[dict]] = {}
    params: dict[str, list] = {a: [] for a in ARMS[1:]}
    warm: dict[str, np.ndarray | None] = {a: None for a in ARMS[1:]}
    for day in scored_days:
        train = [r for r in recs if r["day"] < day and (train_from is None or r["day"] >= train_from)]
        if len({r["day"] for r in train}) < MIN_TRAIN_DAYS:
            continue
        test = [r for r in recs if r["day"] == day and r["decision_at_utc"] >= SCORE_FROM]
        if not test:
            continue
        bias = fit_bias(train)
        Btr, Bte = Batch(train), Batch(test)
        Ftr, Ztr = det_features(train, bias)
        Fte, Zte = det_features(test, bias)
        out = {"A": Bte.scores(arm_vector("A", None, Bte, Fte, Zte))}
        qs = {"A": arm_vector("A", None, Bte, Fte, Zte)}
        for arm in ("B", "C", "D"):
            theta = fit_arm(arm, Btr, Ftr, Ztr, warm[arm])
            warm[arm] = theta
            params[arm].append({"day": day, "theta": [round(float(t), 4) for t in theta]})
            qs[arm] = arm_vector(arm, theta, Bte, Fte, Zte)
            out[arm] = Bte.scores(qs[arm])
        for i, r in enumerate(test):
            mid = Bte.mid[i]
            row = {"day": day, "station": r["station"], "family": r["family"]}
            for arm in ARMS:
                for sc in ("logscore", "brier", "crps"):
                    row[f"{arm}_{sc}"] = float(out[arm][sc][i])
                q = qs[arm][i]
                mu = float((q * mid).sum())
                sd = float(math.sqrt(max((q * (mid - mu) ** 2).sum() + (Bte.shi[i] - Bte.slo[i]).mean() ** 2 / 12.0, 1e-6)))
                row[f"{arm}_z2"] = ((r["y_c"] - mu) / sd) ** 2
            rows.setdefault(day, []).append(row)
        print(day, len(test), {a: round(float(out[a]["logscore"].mean()), 4) for a in ARMS}, flush=True)
    return {"rows": rows, "params": params}


def compare(rows: dict[str, list[dict]], hi: str, lo: str, keep=lambda r: True) -> dict:
    res = {}
    n = 0
    for sc in ("logscore", "crps", "brier"):
        per_day = {}
        for d, rs in rows.items():
            vals = [r[f"{hi}_{sc}"] - r[f"{lo}_{sc}"] for r in rs if keep(r)]
            if vals:
                per_day[d] = vals
        allv = [v for vs in per_day.values() for v in vs]
        n = len(allv)
        lo_ci, hi_ci = boot(per_day)
        res[sc] = {"delta": round(statistics.mean(allv), 5), "ci": [round(lo_ci, 5), round(hi_ci, 5)]}
        if sc == "logscore":
            res["n_days"] = len(per_day)
            res["days_better"] = sum(1 for v in per_day.values() if statistics.mean(v) < 0)
    res["n"] = n
    ls, cr, br = res["logscore"], res["crps"], res["brier"]
    res["verdict"] = ("INSUFFICIENT" if res["n_days"] < 20 else
                      "PASS" if ls["ci"][1] < 0 and cr["ci"][0] <= 0 and br["ci"][0] <= 0 else "REFUSED")
    return res


def summarise(result: dict) -> dict:
    rows = result["rows"]
    days = sorted(rows)
    half = days[len(days) // 2] if days else ""
    pairs = [("B", "A"), ("C", "B"), ("D", "C"), ("C", "A"), ("D", "A")]
    slices = {
        "all": lambda r: True,
        "far": lambda r: r["family"] == "P0_far",
        "near": lambda r: r["family"] == "P0_near",
        "first_half": lambda r: r["day"] < half,
        "second_half": lambda r: r["day"] >= half,
    }
    out = {"days": [days[0], days[-1]] if days else [], "split_day": half, "pairs": {}}
    for hi, lo in pairs:
        out["pairs"][f"{hi}-{lo}"] = {name: compare(rows, hi, lo, keep) for name, keep in slices.items()}
    flat = [r for rs in rows.values() for r in rs]
    out["mean"] = {a: {sc: round(statistics.mean(r[f"{a}_{sc}"] for r in flat), 5)
                       for sc in ("logscore", "brier", "crps", "z2")} for a in ARMS}
    by_station = {}
    for r in flat:
        by_station.setdefault(r["station"], []).append(r)
    out["stations_better"] = {
        f"{hi}-{lo}": sum(1 for rs in by_station.values()
                          if statistics.mean(x[f"{hi}_logscore"] - x[f"{lo}_logscore"] for x in rs) < 0)
        for hi, lo in pairs}
    out["n_stations"] = len(by_station)
    out["params_last"] = {a: (v[-1] if v else None) for a, v in result["params"].items()}
    out["params_path"] = result["params"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-from", default=None)
    args = parser.parse_args()
    recs = load(HERE / "records.pkl")
    print("records", len(recs), "days", len({r["day"] for r in recs}), flush=True)
    result = run(recs, args.train_from)
    summary = summarise(result)
    tag = "train_from_" + args.train_from if args.train_from else "primary"
    (HERE / f"results_{tag}.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    print(json.dumps({k: summary[k] for k in ("days", "mean", "stations_better", "params_last")}, ensure_ascii=False))
    for pair, sl in summary["pairs"].items():
        a = sl["all"]
        print(pair, "LS", a["logscore"], "CRPS", a["crps"], "Br", a["brier"], a["n_days"], a["days_better"], a["verdict"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
