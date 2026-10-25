#!/usr/bin/env python3
"""TAF physics retest (PROTOCOL.md): arms R, P and the ablations, walk-forward, new and old roots.

    python3 docs/research/taf_physics_20261025/run.py
"""

from __future__ import annotations

import json
import pickle
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import ndtr

HERE = Path(__file__).resolve().parent
FLOOR = 1e-3
SCORE_FROM = "2026-10-01"
MIN_TRAIN_DAYS = 7
DRAWS = 2000
CENTER = ("cloud", "low_cloud", "precip", "precip_tempo", "convection", "wspd", "gust", "fog",
          "tx_warm", "tx_cold", "tx_hour", "dtr")
WIDTH = ("n_change", "convection", "precip_tempo", "tx_abs")
GROUPS = {
    "P": {"center": CENTER, "width": WIDTH, "wind_dir": True},
    "cloud_rain": {"center": ("cloud", "low_cloud", "precip", "precip_tempo"), "width": (), "wind_dir": False},
    "convection": {"center": ("convection",), "width": (), "wind_dir": False},
    "wind_speed": {"center": ("wspd", "gust"), "width": (), "wind_dir": False},
    "fog": {"center": ("fog",), "width": (), "wind_dir": False},
    "tx_direction": {"center": ("tx_warm", "tx_cold", "tx_hour", "dtr"), "width": (), "wind_dir": False},
    "wind_direction": {"center": (), "width": (), "wind_dir": True},
    "width_only": {"center": (), "width": WIDTH, "wind_dir": False},
    # A reading beside the protocol: the normal with only the two intercepts, so a
    # physics arm can be read against the same shape (the old roots are not normal).
    "intercept": {"center": (), "width": (), "wind_dir": False},
}
L2, L2_STATION = 0.01, 0.1


def _phi(z):
    return np.where(np.isfinite(z), np.exp(-0.5 * np.where(np.isfinite(z), z, 0.0) ** 2) / np.sqrt(2 * np.pi), 0.0)


class Data:
    def __init__(self, cases, root, stations, dtr_mean, spec, stats=None):
        n = len(cases)
        K = max(len(c["ordered"]) for c in cases)
        self.n = n
        self.mu = np.array([c["roots"][root]["mu"] for c in cases])
        self.sd = np.array([c["roots"][root]["sd"] for c in cases])
        self.tlo = np.full((n, K), np.inf)
        self.thi = np.full((n, K), np.inf)
        self.mask = np.zeros((n, K), bool)
        self.excl = np.zeros((n, K), bool)
        self.rootp = np.zeros((n, K))
        self.win = np.array([c["winner"] for c in cases])
        self.lowest_live = np.zeros(n, int)
        for i, c in enumerate(cases):
            k = len(c["ordered"])
            self.tlo[i, :k], self.thi[i, :k], self.mask[i, :k] = c["tlo"], c["thi"], True
            self.rootp[i, :k] = c["roots"][root]["p"]
            for j in c["excluded"]:
                self.excl[i, j] = True
            live = [j for j in range(k) if j not in c["excluded"]]
            self.lowest_live[i] = live[0]
        # the winner's effective interval: the lowest live bucket takes the excluded mass below it
        idx = np.arange(n)
        self.a = self.tlo[idx, self.win].copy()
        self.b = self.thi[idx, self.win].copy()
        merged = (self.win == self.lowest_live) & self.excl.any(1)
        self.a[merged] = -np.inf

        def feat(c, name):
            f = c["features"][root]
            if name == "dtr":
                return (f["dtr_raw"] - dtr_mean) if f["dtr_raw"] is not None else 0.0
            return f[name]

        xc = np.array([[feat(c, nme) for nme in spec["center"]] for c in cases]).reshape(n, len(spec["center"]))
        xw = np.array([[feat(c, nme) for nme in spec["width"]] for c in cases]).reshape(n, len(spec["width"]))
        if stats is None:
            stats = (xc.mean(0), xc.std(0) + 1e-9, xw.mean(0), xw.std(0) + 1e-9)
        self.stats = stats
        xc = (xc - stats[0]) / stats[1]
        xw = (xw - stats[2]) / stats[3]
        cols = [np.ones((n, 1)), xc]
        pen = [0.0] + [L2] * xc.shape[1]
        if spec["wind_dir"]:
            S = len(stations)
            U = np.zeros((n, 2 * S))
            for i, c in enumerate(cases):
                s = stations.get(c["station"])
                if s is not None:
                    U[i, 2 * s] = c["features"][root]["wind_u"]
                    U[i, 2 * s + 1] = c["features"][root]["wind_v"]
            cols.append(U)
            pen += [L2_STATION] * (2 * S)
        self.Xc = np.hstack(cols)
        self.Xw = np.hstack([np.ones((n, 1)), xw])
        self.pen = np.array(pen + [0.0] + [L2] * xw.shape[1])
        self.nc = self.Xc.shape[1]

    def mu_sd(self, theta):
        mu = self.mu + self.Xc @ theta[: self.nc]
        sd = self.sd * np.exp(np.clip(self.Xw @ theta[self.nc:], -2.0, 2.0))
        return mu, sd

    def objective(self, theta):
        mu, sd = self.mu_sd(theta)
        za, zb = (self.a - mu) / sd, (self.b - mu) / sd
        q = np.clip(ndtr(zb) - ndtr(za), 1e-12, None)
        pa, pb = _phi(za), _phi(zb)
        za0 = np.where(np.isfinite(za), za, 0.0)
        zb0 = np.where(np.isfinite(zb), zb, 0.0)
        dmu = (pa - pb) / (sd * q)            # d log q / d mu
        dls = (za0 * pa - zb0 * pb) / q        # d log q / d log sd
        nll = -np.log(q).mean() + (self.pen * theta ** 2).sum()
        g_c = -(self.Xc * dmu[:, None]).mean(0)
        g_w = -(self.Xw * dls[:, None]).mean(0)
        return nll, np.concatenate([g_c, g_w]) + 2 * self.pen * theta

    def vectors(self, theta=None):
        if theta is None:
            q = self.rootp.copy()
        else:
            mu, sd = self.mu_sd(theta)
            lo = np.where(np.isfinite(self.tlo), (self.tlo - mu[:, None]) / sd[:, None], -np.inf)
            hi = np.where(np.isfinite(self.thi), (self.thi - mu[:, None]) / sd[:, None], np.inf)
            q = np.where(self.mask, np.clip(ndtr(hi) - ndtr(lo), 0.0, None), 0.0)
        q = q / q.sum(1, keepdims=True)
        lost = (q * self.excl).sum(1)
        q = np.where(self.excl, 0.0, q)
        q[np.arange(self.n), self.lowest_live] += lost
        q = np.where(self.mask & ~self.excl, np.maximum(q, FLOOR), 0.0)
        return q / q.sum(1, keepdims=True)

    def scores(self, q):
        idx = np.arange(self.n)
        o = np.zeros_like(q)
        o[idx, self.win] = 1.0
        return {"logscore": -np.log(q[idx, self.win]), "brier": ((q - o) ** 2).sum(1),
                "crps": ((np.cumsum(q, 1) - np.cumsum(o, 1)) ** 2 * self.mask).sum(1)}


def fit(train, root, stations, dtr_mean, spec, warm):
    d = Data(train, root, stations, dtr_mean, spec)
    x0 = np.zeros(d.nc + d.Xw.shape[1]) if warm is None or len(warm) != d.nc + d.Xw.shape[1] else warm
    res = minimize(d.objective, x0, jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return res.x, d.stats


def boot(per_day):
    days = sorted(per_day)
    sums = np.array([sum(per_day[x]) for x in days])
    cnts = np.array([len(per_day[x]) for x in days])
    rng = np.random.default_rng(281)
    picks = rng.integers(0, len(days), (DRAWS, len(days)))
    st = sums[picks].sum(1) / cnts[picks].sum(1)
    return float(np.percentile(st, 2.5)), float(np.percentile(st, 97.5))


def compare(rows, hi, lo, keep=lambda r: True):
    out = {}
    for sc in ("logscore", "crps", "brier"):
        per_day = defaultdict(list)
        for r in rows:
            if keep(r):
                per_day[r["day"]].append(r[f"{hi}_{sc}"] - r[f"{lo}_{sc}"])
        if not per_day:
            return {"n": 0}
        allv = [v for vs in per_day.values() for v in vs]
        ci = boot(per_day)
        out[sc] = {"delta": round(statistics.mean(allv), 5), "ci": [round(ci[0], 5), round(ci[1], 5)]}
        if sc == "logscore":
            out["n_days"], out["n"] = len(per_day), len(allv)
            out["days_better"] = sum(1 for v in per_day.values() if statistics.mean(v) < 0)
    ls = out["logscore"]
    out["verdict"] = ("INSUFFICIENT" if out["n_days"] < 20 else
                      "PASS" if ls["ci"][1] < 0 and out["crps"]["ci"][0] <= 0 and out["brier"]["ci"][0] <= 0 else "REFUSED")
    return out


def run(cases, root, arms):
    cases = [c for c in cases if root in c["roots"]]
    stations = {s: i for i, s in enumerate(sorted({c["station"] for c in cases}))}
    days = sorted({c["day"] for c in cases})
    rows, warm, last = [], {a: None for a in arms}, {}
    for day in days:
        if day < SCORE_FROM:
            continue
        train = [c for c in cases if c["day"] < day]
        if len({c["day"] for c in train}) < MIN_TRAIN_DAYS:
            continue
        test = [c for c in cases if c["day"] == day]
        dtr = [c["features"][root]["dtr_raw"] for c in train if c["features"][root]["dtr_raw"] is not None]
        dtr_mean = statistics.mean(dtr) if dtr else 0.0
        base = Data(test, root, stations, dtr_mean, GROUPS["width_only"])
        out = {"R": base.scores(base.vectors())}
        for arm in arms:
            theta, stats = fit(train, root, stations, dtr_mean, GROUPS[arm], warm[arm])
            warm[arm] = theta
            last[arm] = theta
            dt = Data(test, root, stations, dtr_mean, GROUPS[arm], stats)
            out[arm] = dt.scores(dt.vectors(theta))
        for i, c in enumerate(test):
            row = {"day": day, "station": c["station"], "has_tx": c["features"][root]["has_tx"]}
            for a, s in out.items():
                for k, v in s.items():
                    row[f"{a}_{k}"] = float(v[i])
            rows.append(row)
        print(root, day, len(test), {a: round(float(s["logscore"].mean()), 4) for a, s in out.items()}, flush=True)
    return rows, last


def summarise(rows, arms):
    days = sorted({r["day"] for r in rows})
    half = days[len(days) // 2]
    res = {"days": [days[0], days[-1]], "n_days": len(days), "split": half, "vs_R": {}}
    for a in arms:
        res["vs_R"][a] = {
            "all": compare(rows, a, "R"),
            "first_half": compare(rows, a, "R", lambda r: r["day"] < half),
            "second_half": compare(rows, a, "R", lambda r: r["day"] >= half),
            "tx_days": compare(rows, a, "R", lambda r: r["has_tx"] > 0),
            "vs_intercept": compare(rows, a, "intercept") if a != "intercept" else None,
        }
    return res


def main() -> int:
    cases = pickle.loads((HERE / "cases.pkl").read_bytes())
    arms = list(GROUPS)
    out = {}
    for root, use in (("new", arms), ("old", arms)):
        rows, last = run(cases, root, use)
        out[root] = summarise(rows, use)
        out[root]["theta_last_P"] = [round(float(x), 4) for x in last["P"][:1 + len(CENTER)]]
    (HERE / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    for root, res in out.items():
        print("==", root, res["days"], res["n_days"], "days")
        for a, sl in res["vs_R"].items():
            v = sl["all"]
            print(f"  {a:15s} {v['logscore']['delta']:+.4f} [{v['logscore']['ci'][0]:+.4f},{v['logscore']['ci'][1]:+.4f}]"
                  f" rps{v['crps']['delta']:+.4f}{v['crps']['ci']} br{v['brier']['delta']:+.4f}{v['brier']['ci']}"
                  f" {v['n_days']}d/{v['days_better']} {v['verdict'][:4]} | 1st {sl['first_half']['logscore']['delta']:+.4f} 2nd {sl['second_half']['logscore']['delta']:+.4f}"
                  f" | TX days {sl['tx_days'].get('logscore', {}).get('delta')}"
                  + (f" | vs intercept {sl['vs_intercept']['logscore']['delta']:+.4f} [{sl['vs_intercept']['logscore']['ci'][0]:+.4f},{sl['vs_intercept']['logscore']['ci'][1]:+.4f}]" if sl.get('vs_intercept') else ""))
        print("  P centre coefs (std units; intercept, " + ", ".join(CENTER) + "):", res["theta_last_P"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
