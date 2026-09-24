"""Event-time replay (PROTOCOL_EVENT.md): price prior x our P1 update, minutes after a report.

    python3 replay_events.py events.jsonl.gz OUT.json [--gamma 4] [--primary-lag 5] [--prior PRE]
"""
from __future__ import annotations

import gzip
import json
import math
import sys
import zlib
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, __import__("os").path.dirname(__file__))
from replay import day_boot, F, W0, MAX_PRICE  # noqa: E402

BETA = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5)
KAPPA = 0.01
LAGS = ("2", "5", "15")
TRAIN_GAP_DAYS, MIN_TRAIN_DAYS = 3, 8


def bin_of(h: float) -> str:
    return "10-24h" if h >= 10 else ("6-10h" if h >= 6 else "0-6h")


class Ev:
    def __init__(self, r: dict, truth: str):
        self.st, self.day, self.truth = r["st"], r["day"], truth
        self.key = (self.st, self.day)
        self.t_ours = r["t_ours"]
        self.bin = bin_of(r["hours_to_end"])
        self.books = r["books"]
        self.priors = {"PRE": r["m_pre"], "NOW": r["m_now"]}
        p1, base = r["p1"], r["base"] or {}
        new_mask = set(r["mask"]) - set(r["prev_mask"])
        self.names = [b for b in p1]
        # Our increment: this P1 over the belief just before it.  A bucket this
        # report newly excludes is penalised by kappa instead of zeroed.
        lr = {}
        for b in self.names:
            if b in new_mask:
                lr[b] = math.log(KAPPA)
            else:
                lr[b] = math.log(max(float(p1.get(b, 0.0)), F)) - math.log(max(float(base.get(b, 0.0)), F))
        self.dl = lr

    def belief(self, prior: str, beta: float, names: list[str]) -> np.ndarray | None:
        m = self.priors[prior]
        if not m:
            return None
        x = np.array([math.log(max(float(m.get(b, 0.0)), F)) + beta * self.dl.get(b, 0.0) for b in names])
        x -= x.max()
        p = np.exp(x)
        return p / p.sum()


def common(ev: Ev, lag: str, prior: str):
    bk = ev.books.get(lag)
    m_pr = ev.priors[prior]
    if not bk or not bk["m"] or not m_pr:
        return None
    names = [b for b in ev.names if b in bk["m"] and b in m_pr]
    if ev.truth not in names:
        return None
    return names


def score(p, names, truth):
    return math.log(max(float(p[names.index(truth)]), F))


def market_vec(ev, lag, names):
    m = np.array([float(ev.books[lag]["m"].get(b, 0.0)) for b in names])
    return m / m.sum()


def fit_beta(train, prior):
    def obj(beta):
        by_sd = defaultdict(list)
        for ev in train:
            names = [b for b in ev.names if ev.priors[prior] and b in ev.priors[prior]]
            if ev.truth not in names:
                continue
            by_sd[ev.key].append(score(ev.belief(prior, beta, names), names, ev.truth))
        return np.mean([np.mean(v) for v in by_sd.values()]) if by_sd else -np.inf
    return max(BETA, key=obj)


def diff_by_day(evs, prior, lag, beta_of):
    by_sd = defaultdict(list)
    for ev in evs:
        names = common(ev, lag, prior)
        beta = beta_of(ev)
        if names is None or beta is None:
            continue
        q = ev.belief(prior, beta, names)
        by_sd[ev.key].append(score(q, names, ev.truth) - score(market_vec(ev, lag, names), names, ev.truth))
    by_day = defaultdict(list)
    for (st, d), v in by_sd.items():
        by_day[d].append(float(np.mean(v)))
    return by_day


def solve_on_book(p, names, book, cash, hold, gamma):
    """The same CRRA re-solve as replay.solve_step, on one event's book."""
    from replay import solve_step

    class _R:
        pass
    row = _R()
    row.names = names
    row.asks = [book["asks"].get(b) or [] for b in names]
    row.bids = [book["bids"].get(b) or [] for b in names]
    row.minorder = [float(book["min_order"].get(b, 5.0)) for b in names]
    return solve_step(p, row, cash, hold, gamma)


def main():
    src, out = sys.argv[1], sys.argv[2]
    gamma = float(sys.argv[sys.argv.index("--gamma") + 1]) if "--gamma" in sys.argv else 4.0
    PRIORS = tuple(sys.argv[sys.argv.index("--priors") + 1].split(",")) if "--priors" in sys.argv else ("PRE", "NOW")
    ov = json.load(open("venue_truth_overrides.json"))["overrides"]
    evs = []
    for line in gzip.open(src, "rt"):
        r = json.loads(line)
        evs.append(Ev(r, ov.get(f"{r['st']}|{r['day']}", r["truth"])))
    days = sorted({e.day for e in evs})
    by_bin_day = defaultdict(list)
    for e in evs:
        by_bin_day[(e.bin, e.day)].append(e)
    betas, gates = {}, {}
    for d in days:
        cut = (date.fromisoformat(d) - timedelta(days=TRAIN_GAP_DAYS)).isoformat()
        for b in ("10-24h", "6-10h", "0-6h"):
            train = [e for (bb, dd), es in by_bin_day.items() if bb == b and dd <= cut for e in es]
            if len({e.day for e in train}) < MIN_TRAIN_DAYS:
                continue
            for prior in PRIORS:
                beta = fit_beta(train, prior)
                betas[(d, b, prior)] = beta
                for lag in LAGS:
                    bd = diff_by_day(train, prior, lag, lambda ev, beta=beta: beta)
                    seed = zlib.crc32(f"{d}|{b}|{prior}|{lag}".encode())
                    _, lo, _ = day_boot(bd, n=500, seed=seed) if bd else (0, -1, 0)
                    gates[(d, b, prior, lag)] = lo > 0
        print("fitted", d, {k[1] + "|" + k[2]: v for k, v in betas.items() if k[0] == d}, flush=True)
    beta_of = lambda prior: (lambda ev: betas.get((ev.day, ev.bin, prior)))
    ls = {}
    for prior in PRIORS:
        for lag in LAGS:
            for b in ("10-24h", "6-10h", "0-6h", "all"):
                sel = [e for e in evs if b == "all" or e.bin == b]
                bd = diff_by_day(sel, prior, lag, beta_of(prior))
                mean, lo, hi = day_boot(bd)
                half = sorted(bd)
                h1 = [x for d in half[: len(half) // 2] for x in bd[d]]
                h2 = [x for d in half[len(half) // 2:] for x in bd[d]]
                ls[f"{prior}|L{lag}|{b}"] = {"diff": mean, "lo90": lo, "hi90": hi,
                                             "first_half": float(np.mean(h1)) if h1 else None,
                                             "second_half": float(np.mean(h2)) if h2 else None,
                                             "station_days": sum(len(v) for v in bd.values()), "event_days": len(bd)}
                print("logscore", prior, lag, b, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in ls[f"{prior}|L{lag}|{b}"].items()}, flush=True)
    growth = {}
    for prior in PRIORS:
        for lag in LAGS:
            for gated in (False, True):
                by_sd = defaultdict(list)
                for e in evs:
                    by_sd[e.key].append(e)
                per_day, traded_days, traded_sd, pnl, spent = defaultdict(list), set(), 0, 0.0, 0.0
                for key, es in by_sd.items():
                    es.sort(key=lambda e: e.t_ours)
                    cash, hold, any_trade, evaluable = W0, {}, False, False
                    for e in es:
                        beta = betas.get((e.day, e.bin, prior))
                        names = common(e, lag, prior)
                        if beta is None or names is None:
                            continue
                        evaluable = True
                        if gated and not gates.get((e.day, e.bin, prior, lag), False):
                            continue
                        h = np.array([hold.get(b, 0.0) for b in names])
                        before = cash
                        cash, h2, traded = solve_on_book(e.belief(prior, beta, names), names, e.books[lag], cash, h, gamma)
                        for b, v in zip(names, h2):
                            hold[b] = float(v)
                        spent += max(0.0, before - cash)
                        any_trade |= traded
                    if not evaluable:
                        continue
                    final = cash + hold.get(es[-1].truth, 0.0)
                    per_day[key[1]].append(math.log(max(final, 1e-9) / W0))
                    pnl += final - W0
                    if any_trade:
                        traded_sd += 1
                        traded_days.add(key[1])
                mean, lo, hi = day_boot(per_day)
                half = sorted(per_day)
                h1 = [x for d in half[: len(half) // 2] for x in per_day[d]]
                h2 = [x for d in half[len(half) // 2:] for x in per_day[d]]
                growth[f"{prior}|L{lag}|{'gated' if gated else 'ungated'}"] = {
                    "mean_log_growth": mean, "lo90": lo, "hi90": hi,
                    "first_half": float(np.mean(h1)) if h1 else None, "second_half": float(np.mean(h2)) if h2 else None,
                    "station_days": sum(len(v) for v in per_day.values()), "traded_station_days": traded_sd,
                    "traded_event_days": len(traded_days), "pnl_usdc": pnl, "spent_usdc": spent}
                print("growth", prior, lag, gated, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in growth[f"{prior}|L{lag}|{'gated' if gated else 'ungated'}"].items()}, flush=True)
    json.dump({"gamma": gamma, "kappa": KAPPA, "log_score": ls, "growth": growth,
               "betas_last_day": {f"{b}|{p}": betas.get((days[-1], b, p)) for b in ("10-24h", "6-10h", "0-6h") for p in PRIORS},
               "gate_open_share": {f"{b}|{p}|L{l}": float(np.mean([gates[(d, b, p, l)] for d in days if (d, b, p, l) in gates] or [0]))
                                   for b in ("10-24h", "6-10h", "0-6h") for p in PRIORS for l in LAGS},
               "events": len(evs), "event_days": len(days)}, open(out, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
