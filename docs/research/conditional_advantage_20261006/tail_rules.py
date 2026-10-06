#!/usr/bin/env python3
"""Far-tail rule candidates (PROTOCOL_TAIL.md): R0 = charter 251 as served,
R1 = R0 without the V1 station-days, R2 = the filter read on the model vector
moved to the market's centre, R3 = charter 234 v1.  Production
``far_tail_legs`` and ``model_filter``; both windows.

    python3 tail_rules.py
"""
import collections
import gzip
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import analysis as an  # noqa: E402
import veto  # noqa: E402
from bayes_basket_target import realisation_layer as rl  # noqa: E402
from bayes_basket_target import tail_short_lane as ts  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

FEE = 0.05
WINDOWS = {"main": ("rows", "books.jsonl.gz"), "early": ("rows_early", "books_early.jsonl.gz")}


def load_books(path):
    books = {}
    with gzip.open(os.path.join(HERE, path), "rt") as fh:
        for line in fh:
            r = json.loads(line)
            books[r["sid"]] = r["buckets"]
    return books


def centred(row, buckets):
    """The served vector (normalised, floored) moved on the rung axis to the
    market's mean, floored again -- the model's width and shape on the
    market's centre."""
    market = market_reference_v1(buckets)
    if market.get("state") != "complete":
        return None
    names = [str(b.get("bucket")) for b in buckets]
    raw = veto.p_dict(row)
    if set(raw) != set(names):
        return None
    total = sum(raw.values())
    p, _ = floor_probabilities({b: v / total for b, v in raw.items()})
    pv = np.asarray([p[n] for n in names], dtype=float)
    qv = rl.floored([market["vector"][n] for n in names])
    mean_q, _ = rl.moments(qv)
    pc = rl.shifted_to(pv / pv.sum(), mean_q)
    pc_f, _ = floor_probabilities({n: float(v) for n, v in zip(names, pc / pc.sum())})
    return {"state": "ok", "probabilities": {n: float(v) for n, v in pc_f.items()}}


def replay(rows, books, feats):
    legs = {"R0": [], "R1": [], "R2": [], "R3": []}
    by_sd = collections.defaultdict(list)
    for r in rows:
        if r["stage"] == "P0" and (r["h"] or 0) > 36 and r["sid"] in books:
            by_sd[(r["st"], r["d"])].append(r)
    for (st, d), group in by_sd.items():
        group.sort(key=lambda r: r["t"])
        judged = {k: set() for k in legs}
        for r in group:
            buckets = books[r["sid"]]
            names = [str(b.get("bucket")) for b in buckets]
            if r["y"] not in names:
                continue
            tail = ts.far_tail_legs(buckets, max_market_price=0.05, min_rungs_from_mode=3, min_yes_bid=0.02)
            if tail.get("state") != "complete" or not tail["legs"]:
                continue
            f = feats.get((st, d, r["t"]))
            sv = veto.served(r)
            sc = None
            for leg in tail["legs"]:
                b = leg["bucket"]
                no_price = round(1.0 - float(leg["bid"]), 6)
                hit = 1.0 if b == r["y"] else 0.0
                rec = {"st": st, "d": d, "t": r["t"], "bucket": b, "hit": hit, "q": leg["q"],
                       "pnl": (1.0 - hit) - no_price - FEE * no_price * (1 - no_price),
                       "side": veto.side_of_shift(f, names.index(b), r) if f else None}
                if b not in judged["R3"]:
                    judged["R3"].add(b)
                    legs["R3"].append(rec)
                if b not in judged["R0"]:
                    judged["R0"].add(b)
                    m0 = ts.model_filter(sv, b, float(leg["bid"]))
                    if m0["refusal"] is None:
                        legs["R0"].append(dict(rec, p=m0["p"]))
                if b not in judged["R1"]:
                    judged["R1"].add(b)
                    m0 = ts.model_filter(sv, b, float(leg["bid"]))
                    vetoed = f is not None and veto.VETOES["V1"](f)
                    if m0["refusal"] is None and not vetoed:
                        legs["R1"].append(dict(rec, p=m0["p"]))
                if b not in judged["R2"]:
                    judged["R2"].add(b)
                    sc = sc or centred(r, buckets)
                    if sc is not None:
                        m2 = ts.model_filter(sc, b, float(leg["bid"]))
                        if m2["refusal"] is None:
                            legs["R2"].append(dict(rec, p=m2["p"]))
    return legs


def per_leg(legs, days):
    if not legs:
        return {"legs": 0}
    boot = an.Boot(days)
    num, den = an.day_sums(legs, days, lambda x: x["pnl"], lambda x: 1.0)
    s = boot.summarise(num, den) or {}
    return {"legs": len(legs), "days": len({x["d"] for x in legs}), "station_days": len({(x["st"], x["d"]) for x in legs}),
            "sum": float(sum(x["pnl"] for x in legs)), "losses": int(sum(x["hit"] for x in legs)), **s}


def daily_diff(a, b, days):
    boot = an.Boot(days)
    idx = {d: i for i, d in enumerate(days)}
    num = np.zeros(len(days))
    for x in a:
        num[idx[x["d"]]] += x["pnl"]
    for x in b:
        num[idx[x["d"]]] -= x["pnl"]
    den = np.ones(len(days))
    return boot.summarise(num, den) or {}


def main():
    out = {}
    for wname, (rows_dir, books_file) in WINDOWS.items():
        rows = an.load(os.path.join(HERE, rows_dir))
        books = load_books(books_file)
        feats = {(f["st"], f["d"], f["t"]): f for f in an.featurise(rows) if f["stage"] == "P0"}
        legs = replay(rows, books, feats)
        days = sorted({x["d"] for k in legs for x in legs[k]})
        w = {"days": days, "rules": {}}
        for k, lg in legs.items():
            w["rules"][k] = per_leg(lg, days)
            w["rules"][k]["diff_vs_R0_daily"] = daily_diff(lg, legs["R0"], days) if k != "R0" else None
            for side_name, sv in (("same", 1), ("opposite", -1), ("none", 0)):
                w["rules"][k][f"side_{side_name}"] = per_leg([x for x in lg if x["side"] == sv], days)
        # what each candidate adds and drops against R0
        key = lambda x: (x["st"], x["d"], x["bucket"])
        r0 = {key(x): x for x in legs["R0"]}
        for k in ("R1", "R2"):
            ks = {key(x): x for x in legs[k]}
            w["rules"][k]["dropped_from_R0"] = per_leg([x for kk, x in r0.items() if kk not in ks], days)
            w["rules"][k]["added_to_R0"] = per_leg([x for kk, x in ks.items() if kk not in r0], days)
        out[wname] = w
    json.dump(out, open(os.path.join(HERE, "results_tail.json"), "w"), ensure_ascii=False, indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main()
