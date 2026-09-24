"""Walk-forward replay of the Bayes chain x utility-optimal basket (PROTOCOL.md).

    python3 replay.py dataset.jsonl.gz results.json [--gamma 4] [--variants SERVED,CHAIN_T,...]
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
from scipy.optimize import minimize

F = 1e-6
ETA = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25)
A_GRID = (0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0)
B_GRID = (0.0, 0.5, 0.75, 1.0, 1.25)
KAPPA = (1e-6, 1e-3, 1e-2, 3e-2, 0.1, 1.0)
TRAIN_GAP_DAYS = 3
MIN_TRAIN_DAYS = 8
W0 = 100.0
MAX_PRICE = 0.95
BOOT = 1000
rng = np.random.default_rng(20260924)


def bin_of(h: int) -> str:
    return "far" if h >= 24 else ("mid" if h >= 10 else "near")


def logv(vec: dict, names: list[str]) -> np.ndarray:
    return np.log(np.maximum(np.array([float(vec.get(b, 0.0)) for b in names]), F))


def norm_log(x: np.ndarray) -> np.ndarray:
    finite = np.isfinite(x)
    mx = np.max(x[finite])
    p = np.where(finite, np.exp(x - mx), 0.0)
    return p / p.sum()


class Row:
    __slots__ = ("st", "day", "H", "bin", "names", "t", "truth", "lr", "ltaf", "lobs", "alive", "lm", "served", "asks", "bids", "minorder", "key")

    def __init__(self, raw: dict):
        self.st, self.day, self.H = raw["st"], raw["day"], raw["H"]
        self.bin = bin_of(self.H)
        m = raw["m"]
        names = [b for b in raw["served"] if b in m]
        self.names = names
        self.truth = raw["truth"]
        self.t = names.index(raw["truth"]) if raw["truth"] in names else -1
        root = raw["root"] or raw["served"]
        self.lr = logv(root, names)
        self.ltaf = logv(raw["taf"], names) - self.lr if raw["taf"] else np.zeros(len(names))
        mask = set(raw["mask"] or [])
        self.alive = np.array([b not in mask for b in names])
        if raw["stage"] == "P1" and raw["pred_root"]:
            pr = np.array([float(raw["pred_root"].get(b, 0.0)) for b in names]) * self.alive
            pr = pr / pr.sum() if pr.sum() > 0 else pr
            self.lobs = np.where(self.alive, logv(raw["served"], names) - np.log(np.maximum(pr, F)), 0.0)
        else:
            self.lobs = np.zeros(len(names))
        self.lm = logv(m, names)
        self.served = np.array([float(raw["served"].get(b, 0.0)) for b in names])
        self.asks = [raw["asks"].get(b) or [] for b in names]
        self.bids = [raw["bids"].get(b) or [] for b in names]
        self.minorder = [float(raw["min_order"].get(b, 5.0)) for b in names]
        self.key = (self.st, self.day)

    def chain(self, e1: float, e2: float) -> np.ndarray:
        x = self.lr + e1 * self.ltaf + e2 * self.lobs
        return norm_log(np.where(self.alive, x, -np.inf))

    def market(self) -> np.ndarray:
        return norm_log(self.lm.copy())

    def pooled(self, e1, e2, a, b) -> np.ndarray:
        x = a * np.log(np.maximum(self.chain(e1, e2), F)) + b * self.lm
        return norm_log(np.where(self.alive, x, -np.inf))

    def pooled_soft(self, e1, e2, a, b, kappa) -> np.ndarray:
        """Exploratory (not pre-registered): the chain without its hard mask,
        pooled with the price; a masked bucket is penalised by log kappa
        instead of being set to zero."""
        free = norm_log(self.lr + e1 * self.ltaf + e2 * self.lobs)
        x = a * np.log(np.maximum(free, F)) + b * self.lm + np.where(self.alive, 0.0, math.log(kappa))
        return norm_log(x)

    def belief(self, variant: str, params: dict) -> np.ndarray:
        if variant == "MARKET":
            return self.market()
        if variant == "SERVED":
            s = self.served / self.served.sum()
            return s
        e1, e2 = params["eta"]
        if variant == "CHAIN_T":
            return self.chain(e1, e2)
        a, b = params["ab"]
        if variant == "CHAIN_T_PRICE_SOFT":
            return self.pooled_soft(e1, e2, a, b, params["kappa"])
        return self.pooled(e1, e2, a, b)


def score(p: np.ndarray, t: int) -> float:
    return math.log(max(float(p[t]), F))


class Matrix:
    """All rows padded to one width so a grid point is scored in one pass."""

    def __init__(self, rows):
        k = max(len(r.names) for r in rows)
        n = len(rows)
        pad = lambda: np.full((n, k), -np.inf)
        self.LR, self.LTAF, self.LOBS, self.LM = pad(), np.zeros((n, k)), np.zeros((n, k)), pad()
        self.ALIVE = np.zeros((n, k), dtype=bool)
        self.T = np.array([r.t for r in rows])
        for i, r in enumerate(rows):
            m = len(r.names)
            self.LR[i, :m] = r.lr
            self.LTAF[i, :m] = r.ltaf
            self.LOBS[i, :m] = r.lobs
            self.LM[i, :m] = r.lm
            self.ALIVE[i, :m] = r.alive
        self.LMN = self._lognorm(self.LM, np.isfinite(self.LM))
        sd_ids = {}
        self.SD = np.array([sd_ids.setdefault(r.key, len(sd_ids)) for r in rows])
        self.DAY = np.array([r.day for r in rows])
        self.idx = np.arange(n)

    @staticmethod
    def _lognorm(x, ok):
        x = np.where(ok, x, -np.inf)
        mx = np.max(x, axis=1, keepdims=True)
        lse = mx[:, 0] + np.log(np.sum(np.exp(x - mx), axis=1))
        return x - lse[:, None]

    def chain_log(self, e1, e2):
        return self._lognorm(self.LR + e1 * self.LTAF + e2 * self.LOBS, self.ALIVE)

    def pooled_log(self, e1, e2, a, b):
        lc = np.maximum(self.chain_log(e1, e2), math.log(F))
        return self._lognorm(a * lc + b * self.LM, self.ALIVE & np.isfinite(self.LM))

    def pooled_soft_log(self, e1, e2, a, b, kappa):
        finite = np.isfinite(self.LR)
        free = np.maximum(self._lognorm(self.LR + e1 * self.LTAF + e2 * self.LOBS, finite), math.log(F))
        x = a * free + b * self.LM + np.where(self.ALIVE, 0.0, math.log(kappa))
        return self._lognorm(x, finite & np.isfinite(self.LM))

    def truth_score(self, logp):
        return np.maximum(logp[self.idx, self.T], math.log(F))


def sd_weighted_mean(values, sel, sd):
    ids = sd[sel]
    uniq, inv = np.unique(ids, return_inverse=True)
    sums = np.bincount(inv, weights=values[sel])
    cnt = np.bincount(inv)
    return float(np.mean(sums / cnt))


def day_boot(values_by_day: dict, n=BOOT, seed=None):
    days = sorted(values_by_day)
    if not days:
        return (float("nan"),) * 3
    gen = np.random.default_rng(seed) if seed is not None else np.random.default_rng(20260924)
    means = []
    for _ in range(n):
        pick = gen.choice(len(days), len(days))
        vals = [v for i in pick for v in values_by_day[days[i]]]
        means.append(np.mean(vals))
    allv = [v for d in days for v in values_by_day[d]]
    return float(np.mean(allv)), float(np.percentile(means, 5)), float(np.percentile(means, 95))


def fit(M, sel, variant, eta_cache):
    if variant in ("MARKET", "SERVED"):
        return {}
    if "eta" not in eta_cache:
        eta_cache["eta"] = max(((e1, e2) for e1 in ETA for e2 in ETA),
                               key=lambda e: sd_weighted_mean(M.truth_score(M.chain_log(*e)), sel, M.SD))
    best = eta_cache["eta"]
    params = {"eta": best}
    if variant == "CHAIN_T_PRICE":
        params["ab"] = max(((a, b) for a in A_GRID for b in B_GRID if a + b > 0),
                           key=lambda ab: sd_weighted_mean(M.truth_score(M.pooled_log(*best, *ab)), sel, M.SD))
    if variant == "CHAIN_T_PRICE_SOFT":
        combo = max(((a, b, k) for a in A_GRID for b in B_GRID for k in KAPPA if a + b > 0),
                    key=lambda abk: sd_weighted_mean(M.truth_score(M.pooled_soft_log(*best, *abk)), sel, M.SD))
        params["ab"], params["kappa"] = combo[:2], combo[2]
    return params


def variant_log(M, variant, params, served_log):
    if variant == "MARKET":
        return M.LMN
    if variant == "SERVED":
        return served_log
    if variant == "CHAIN_T":
        return M.chain_log(*params["eta"])
    if variant == "CHAIN_T_PRICE_SOFT":
        return M.pooled_soft_log(*params["eta"], *params["ab"], params["kappa"])
    return M.pooled_log(*params["eta"], *params["ab"])


def gate_open(M, sel, variant, params, served_log) -> bool:
    diff = M.truth_score(variant_log(M, variant, params, served_log)) - M.truth_score(M.LMN)
    ids = M.SD[sel]
    uniq, inv = np.unique(ids, return_inverse=True)
    sd_mean = np.bincount(inv, weights=diff[sel]) / np.bincount(inv)
    sd_day = {}
    for u, dd in zip(ids, M.DAY[sel]):
        sd_day[u] = dd
    by_day = defaultdict(list)
    for u, v in zip(uniq, sd_mean):
        by_day[sd_day[u]].append(float(v))
    # Seeded by what is being judged, so a gate does not depend on which
    # other variants happened to run before it in the same process.
    seed = int.from_bytes(f"{variant}|{sorted(set(M.DAY[sel]))[-1]}|{int(sel.sum())}".encode()[:8].ljust(8, b"0"), "little") ^ len(by_day)
    _, lo, _ = day_boot(by_day, n=500, seed=seed)
    return lo > 0


# ---------------------------------------------------------------- the solver
def solve_step(p, row: Row, cash, hold, gamma):
    """One re-solve: CRRA over terminal wealth by bucket, buys walk the asks,
    sells walk the bids.  Returns (new_cash, new_hold, traded)."""
    n = len(row.names)
    hold = np.array(hold, dtype=float)
    w_now = (cash + hold) / W0
    mu = p * w_now ** (-gamma)
    emu = mu.sum()
    buys, sells = [], []
    for k in range(n):
        for price, shares in row.asks[k]:
            if price < MAX_PRICE and mu[k] > price * emu * (1 + 1e-9):
                buys.append((k, price, shares))
        if hold[k] > 0:
            for price, shares in row.bids[k]:
                if mu[k] < price * emu * (1 - 1e-9):
                    sells.append((k, price, min(shares, hold[k])))
    if not buys and not sells:
        return cash, hold, False
    nb, ns = len(buys), len(sells)
    kb = np.array([b[0] for b in buys], dtype=int); pb = np.array([b[1] for b in buys]); cb = np.array([b[2] for b in buys]) / W0
    ks = np.array([s[0] for s in sells], dtype=int); ps = np.array([s[1] for s in sells]); cs = np.array([s[2] for s in sells]) / W0
    c0, h0 = cash / W0, hold / W0

    def wealth(z):
        x, y = z[:nb], z[nb:]
        c = c0 - pb @ x + ps @ y
        h = h0.copy()
        np.add.at(h, kb, x)
        np.subtract.at(h, ks, y)
        return c + h, c

    def obj(z):
        w, _ = wealth(z)
        w = np.maximum(w, 1e-9)
        u = np.log(w) if gamma == 1 else (w ** (1 - gamma) - 1) / (1 - gamma)
        return -float(p @ u)

    def jac(z):
        w, _ = wealth(z)
        w = np.maximum(w, 1e-9)
        g = p * w ** (-gamma)          # d u / d w_k, weighted
        tot = g.sum()                  # cash enters every scenario
        gx = -(g[kb] - pb * tot) if nb else np.zeros(0)
        gy = -(-g[ks] + ps * tot) if ns else np.zeros(0)
        return np.concatenate([gx, gy])

    cons = [{"type": "ineq", "fun": lambda z: wealth(z)[1],
             "jac": lambda z: np.concatenate([-pb, ps])}]
    bounds = [(0.0, c) for c in cb] + [(0.0, c) for c in cs]
    r = minimize(obj, np.zeros(nb + ns), jac=jac, bounds=bounds, constraints=cons,
                 method="SLSQP", options={"maxiter": 300, "ftol": 1e-12})
    z = r.x if r.success else np.zeros(nb + ns)
    x, y = z[:nb] * W0, z[nb:] * W0
    net = np.zeros(n)
    np.add.at(net, kb, x)
    np.subtract.at(net, ks, y)
    keep = np.array([abs(net[k]) >= row.minorder[k] for k in range(n)])
    x = np.where(keep[kb], x, 0.0) if nb else x
    y = np.where(keep[ks], y, 0.0) if ns else y
    new_cash = cash - float(pb @ x) + float(ps @ y)
    new_hold = hold.copy()
    np.add.at(new_hold, kb, x)
    np.subtract.at(new_hold, ks, y)
    traded = bool(keep.any())
    if new_cash < -1e-6:
        return cash, hold, False
    return new_cash, new_hold, traded


def main():
    src, out = sys.argv[1], sys.argv[2]
    gamma = float(sys.argv[sys.argv.index("--gamma") + 1]) if "--gamma" in sys.argv else 4.0
    variants = (sys.argv[sys.argv.index("--variants") + 1].split(",") if "--variants" in sys.argv
                else ["MARKET", "SERVED", "CHAIN_T", "CHAIN_T_PRICE"])
    raw = [json.loads(l) for l in gzip.open(src, "rt")]
    if "--truth-override" in sys.argv:
        ov = json.load(open(sys.argv[sys.argv.index("--truth-override") + 1]))["overrides"]
        for r in raw:
            r["truth"] = ov.get(f"{r['st']}|{r['day']}", r["truth"])
    rows = [Row(r) for r in raw if r["m"] and r["truth"] in r["m"] and r["truth"] in r["served"]]
    days = sorted({r.day for r in rows})
    by_day_bin = defaultdict(list)
    for r in rows:
        by_day_bin[(r.day, r.bin)].append(r)
    # ---- walk-forward parameters and gates per (test day, bin, variant)
    M = Matrix(rows)
    served_log = np.full_like(M.LR, -np.inf)
    for i, r in enumerate(rows):
        s_ = r.served / r.served.sum()
        served_log[i, :len(s_)] = np.log(np.maximum(s_, F))
    BIN = np.array([r.bin for r in rows])
    params, gates = {}, {}
    for d in days:
        cut = (date.fromisoformat(d) - timedelta(days=TRAIN_GAP_DAYS)).isoformat()
        for b in ("far", "mid", "near"):
            sel = (BIN == b) & (M.DAY <= cut)
            if len(set(M.DAY[sel])) < MIN_TRAIN_DAYS:
                continue
            eta_cache: dict = {}
            for v in variants:
                pr = fit(M, sel, v, eta_cache)
                params[(d, b, v)] = pr
                gates[(d, b, v)] = (v != "MARKET") and gate_open(M, sel, v, pr, served_log)
        print("fitted", d, {b: params.get((d, b, variants[-1])) for b in ("far", "mid", "near")}, flush=True)
    # ---- reading 1: log score difference against the price, test rows only
    ls = {}
    for v in variants:
        for b in ("far", "mid", "near", "all"):
            vals = defaultdict(lambda: defaultdict(list))
            for r in rows:
                if b != "all" and r.bin != b:
                    continue
                key = (r.day, r.bin, v)
                if key not in params:
                    continue
                q = r.belief(v, params[key])
                vals[r.day][r.key].append(score(q, r.t) - score(r.market(), r.t))
            by_day = {d: [float(np.mean(x)) for x in sd.values()] for d, sd in vals.items()}
            mean, lo, hi = day_boot(by_day)
            half = sorted(by_day)
            h1 = [x for d in half[: len(half) // 2] for x in by_day[d]]
            h2 = [x for d in half[len(half) // 2:] for x in by_day[d]]
            ls[f"{v}|{b}"] = {"diff": mean, "lo90": lo, "hi90": hi,
                              "first_half": float(np.mean(h1)) if h1 else None,
                              "second_half": float(np.mean(h2)) if h2 else None,
                              "station_days": sum(len(x) for x in by_day.values()), "event_days": len(by_day)}
    # ---- reading 2: realized growth, holdings carried across the grid
    by_sd = defaultdict(list)
    for r in rows:
        by_sd[r.key].append(r)
    growth = {}
    for v in variants:
        for gated in (False, True):
            if v == "MARKET" and gated:
                continue
            per_day = defaultdict(list)
            per_sd = []
            traded_days, traded_sd, pnl, spent = set(), 0, 0.0, 0.0
            for key, rs in by_sd.items():
                rs = sorted(rs, key=lambda r: -r.H)
                names = rs[0].names
                cash, hold, any_trade, evaluable = W0, None, False, False
                for r in rs:
                    pk = (r.day, r.bin, v)
                    if pk not in params:
                        continue
                    evaluable = True
                    if hold is None or r.names != names:
                        # a ladder change mid-day re-keys holdings by bucket name
                        old = dict(zip(names, hold)) if hold is not None else {}
                        names = r.names
                        hold = np.array([old.get(b, 0.0) for b in names])
                    if gated and not gates.get(pk, False):
                        continue
                    before = cash
                    cash, hold, traded = solve_step(r.belief(v, params[pk]), r, cash, hold, gamma)
                    spent += max(0.0, before - cash)
                    any_trade |= traded
                if not evaluable:
                    continue
                truth = rs[-1].truth
                final = cash + (float(hold[names.index(truth)]) if hold is not None and truth in names else 0.0)
                g = math.log(max(final, 1e-9) / W0)
                per_day[key[1]].append(g)
                per_sd.append([key[0], key[1], round(final, 4), bool(any_trade)])
                pnl += final - W0
                if any_trade:
                    traded_sd += 1
                    traded_days.add(key[1])
            mean, lo, hi = day_boot(per_day)
            half = sorted(per_day)
            h1 = [x for d in half[: len(half) // 2] for x in per_day[d]]
            h2 = [x for d in half[len(half) // 2:] for x in per_day[d]]
            growth[f"{v}|{'gated' if gated else 'ungated'}"] = {
                "mean_log_growth": mean, "lo90": lo, "hi90": hi,
                "first_half": float(np.mean(h1)) if h1 else None, "second_half": float(np.mean(h2)) if h2 else None,
                "station_days": sum(len(x) for x in per_day.values()), "traded_station_days": traded_sd,
                "traded_event_days": len(traded_days), "pnl_usdc": pnl, "spent_usdc": spent,
                "per_station_day": per_sd,
            }
            print("growth", v, gated, {k: x for k, x in growth[f"{v}|{'gated' if gated else 'ungated'}"].items() if k != "per_station_day"}, flush=True)
    last = {f"{b}|{v}": params.get((days[-1], b, v)) for b in ("far", "mid", "near") for v in variants}
    gate_open_share = {f"{b}|{v}": float(np.mean([gates[(d, b, v)] for d in days if (d, b, v) in gates]))
                       for b in ("far", "mid", "near") for v in variants if v != "MARKET"}
    json.dump({"gamma": gamma, "log_score": ls, "growth": growth, "last_params": last,
               "gate_open_share": gate_open_share, "rows": len(rows), "event_days": len(days)},
              open(out, "w"), ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
