#!/usr/bin/env python3
"""Veto replay (PROTOCOL_VETO.md): does "centres far apart and the families
agree" cut losing legs from the two live lanes?  The lanes' rules are the
production functions; the veto is analysis.py's own features.

    python3 veto.py
"""
import collections
import gzip
import json
import math
import os
import sqlite3
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import analysis as an  # noqa: E402
from bayes_basket_target import proper_bet_lane as pb  # noqa: E402
from bayes_basket_target import tail_short_lane as ts  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
SPLIT = "2026-09-24"
FEE = 0.05
VETOES = {
    "V1": lambda f: abs(f["dmu"]) >= 1.0 and f["consist"] is not None and f["consist"] >= 0.8,
    "V2": lambda f: abs(f["dmu"]) >= 0.3 and f["consist"] is not None and f["consist"] >= 0.8,
    "V3": lambda f: abs(f["dmu"]) >= 1.0,
}


def load_books():
    books = {}
    with gzip.open(os.path.join(HERE, "books.jsonl.gz"), "rt") as fh:
        for line in fh:
            r = json.loads(line)
            books[r["sid"]] = r["buckets"]
    return books


def p_dict(row):
    return {n: float(v or 0.0) for n, v in zip(row["names"], row["p"])}


def served(row):
    raw = p_dict(row)
    total = sum(raw.values())
    floored, _ = floor_probabilities({b: v / total for b, v in raw.items()})
    return {"state": "ok", "probabilities": {b: float(v) for b, v in floored.items()}}


def side_of_shift(f, bucket_index, row):
    """+1: the leg's bucket lies on the side the model moved towards; -1 opposite; 0 centre."""
    t = row["temps"][bucket_index]
    if t is None or f["dmu"] == 0:
        return 0
    return 1 if (t - f["mu_q"]) * f["dmu"] > 0 else -1


def replay(rows, books, feats):
    proper, proper_reentry, tail_v1, tail_v2 = [], [], [], []
    by_sd = collections.defaultdict(list)
    for r in rows:
        if r["stage"] == "P0" and (r["h"] or 0) > 36 and r["sid"] in books:
            by_sd[(r["st"], r["d"])].append(r)
    for (st, d), group in by_sd.items():
        group.sort(key=lambda r: r["t"])
        proper_done = reentry_done = False
        judged_v1, judged_v2 = set(), set()
        for r in group:
            f = feats.get((st, d, r["t"]))
            buckets = books[r["sid"]]
            names = [str(b.get("bucket")) for b in buckets]
            y = r["y"]
            if y not in names:
                continue
            # Proper YES, charter 249
            if not (proper_done and reentry_done):
                res = pb.evaluate_ladder(
                    p_dict(r), buckets, cash_usdc=None, min_shares={}, ticks={},
                    sides=frozenset({pb.SIDE_YES}), buckets=pb.BUCKETS_MARKET_MODE, stake_usdc=1.0,
                )
                if res.get("refusal") is None and res.get("legs"):
                    leg = res["legs"][0]
                    row_b = next(b for b in buckets if str(b.get("bucket")) == leg["bucket"])
                    _bid, ask = pb.side_book(row_b, pb.SIDE_YES, float(row_b.get("price_tick_size") or 0.01))
                    hit = 1.0 if leg["bucket"] == y else 0.0
                    rec = {"st": st, "d": d, "t": r["t"], "bucket": leg["bucket"], "price": float(leg["price"]),
                           "ask": ask, "hit": hit, "maker": hit - float(leg["price"]),
                           "taker": (hit - ask - FEE * ask * (1 - ask)) if ask is not None else None,
                           "p": float(leg["p"]), "f": f,
                           "side": side_of_shift(f, names.index(leg["bucket"]), r) if f else None}
                    if not proper_done:
                        proper.append(rec)
                        proper_done = True
                    if not reentry_done and f is not None and not VETOES["V1"](f):
                        proper_reentry.append(rec)
                        reentry_done = True
            # far-tail NO, charters 234 (v1) / 251 (v2)
            tail = ts.far_tail_legs(buckets, max_market_price=0.05, min_rungs_from_mode=3, min_yes_bid=0.02)
            if tail.get("state") != "complete":
                continue
            sv = None
            for leg in tail["legs"]:
                b = leg["bucket"]
                no_price = round(1.0 - float(leg["bid"]), 6)
                hit = 1.0 if b == y else 0.0
                rec = {"st": st, "d": d, "t": r["t"], "bucket": b, "price": no_price, "hit": hit,
                       "pnl": (1.0 - hit) - no_price - FEE * no_price * (1 - no_price),
                       "q": leg["q"], "rungs": leg["rungs_from_mode"], "f": f,
                       "side": side_of_shift(f, names.index(b), r) if f else None}
                if b not in judged_v1:
                    judged_v1.add(b)
                    tail_v1.append(rec)
                if b not in judged_v2:
                    judged_v2.add(b)
                    sv = sv or served(r)
                    mf = ts.model_filter(sv, b, float(leg["bid"]))
                    if mf["refusal"] is None:
                        tail_v2.append(dict(rec, model_p=mf["p"]))
    return {"proper": proper, "proper_reentry": proper_reentry, "tail_v1": tail_v1, "tail_v2": tail_v2}


def summarise(legs, value, days_all):
    """Per-leg mean with a 3-day block interval, overall and by half."""
    out = {}
    for name, keep in (("all", lambda d: True), ("H1", lambda d: d < SPLIT), ("H2", lambda d: d >= SPLIT)):
        sub = [x for x in legs if keep(x["d"]) and value(x) is not None]
        days = sorted({d for d in days_all if keep(d)})
        if not sub or not days:
            out[name] = {"legs": 0}
            continue
        boot = an.Boot(days)
        num, den = an.day_sums(sub, days, value, lambda x: 1.0)
        s = boot.summarise(num, den) or {}
        out[name] = {"legs": len(sub), "days": len({x["d"] for x in sub}),
                     "station_days": len({(x["st"], x["d"]) for x in sub}),
                     "sum": float(sum(value(x) for x in sub)), **s}
    return out


def verdict(cut):
    a, h1, h2 = cut["all"], cut["H1"], cut["H2"]
    if a.get("legs", 0) < 30 or a.get("days", 0) < 8:
        return "insufficient"
    if a.get("hi") is not None and a["hi"] < 0 and (h1.get("mean") or 0) < 0 and (h2.get("mean") or 0) < 0:
        return "adopt"
    return "no"


def live_tail(feats_by_sd):
    conn = sqlite3.connect(DB, uri=True, isolation_level=None, timeout=30)
    labels = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l"
        " WHERE truth_revision_no=(SELECT MAX(truth_revision_no) FROM basket_station_day_settlement_label x"
        " WHERE x.station_id=l.station_id AND x.event_date=l.event_date) AND event_date>='2026-09-28'")}
    legs = []
    for st, d, b, at, rv, price, shares, fee in conn.execute(
        "SELECT station_id, event_date, bucket, evaluated_at_utc, rule_version, no_price, filled_shares, fee_usdc_est"
        " FROM tail_short_lane_evaluation WHERE filled_shares>0"):
        y = labels.get((st, d))
        if y is None or price is None:
            continue
        hit = 1.0 if b == y else 0.0
        fee_ps = (float(fee) / float(shares)) if fee and shares else FEE * price * (1 - price)
        f = latest_feature(feats_by_sd, st, d, at)
        legs.append({"st": st, "d": d, "bucket": b, "rule": rv, "pnl": (1 - hit) - float(price) - fee_ps,
                     "hit": hit, "f": f})
    return legs


def live_proper(feats_by_sd):
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "proper_bet_attribution.py"), "--json"],
                         capture_output=True, text=True, timeout=600)
    rows = json.loads(out.stdout)["rows"]
    conn = sqlite3.connect(DB, uri=True, isolation_level=None, timeout=30)
    legs = []
    for r in rows:
        for lg in r.get("legs") or []:
            if not lg.get("shares"):
                continue
            at = conn.execute(
                "SELECT decision_at_utc FROM proper_bet_lane_evaluation WHERE station_id=? AND event_date=? AND stage='P0'",
                (r["station_id"], r["event_date"])).fetchone()
            f = latest_feature(feats_by_sd, r["station_id"], r["event_date"], at[0] if at else None)
            sh = float(lg["shares"])
            legs.append({"st": r["station_id"], "d": r["event_date"], "bucket": lg["bucket"],
                         "pnl": (float(lg["payout_usdc"]) - float(lg["paid_usdc"])) / sh, "f": f})
    return legs


def latest_feature(feats_by_sd, st, d, at):
    cands = [f for f in feats_by_sd.get((st, d), []) if at is None or f["t"] <= at]
    return cands[-1] if cands else None


def main():
    rows = an.load()
    books = load_books()
    feats_list = [f for f in an.featurise(rows) if f["stage"] == "P0"]
    feats = {(f["st"], f["d"], f["t"]): f for f in feats_list}
    feats_by_sd = collections.defaultdict(list)
    for f in sorted(feats_list, key=lambda f: f["t"]):
        feats_by_sd[(f["st"], f["d"])].append(f)
    rep = replay(rows, books, feats)
    days_all = sorted({r["d"] for r in rows})
    results = {"replay": {}, "live": {}}
    lanes = {"proper_maker": ("proper", lambda x: x["maker"]), "proper_taker": ("proper", lambda x: x["taker"]),
             "tail_v1": ("tail_v1", lambda x: x["pnl"]), "tail_v2": ("tail_v2", lambda x: x["pnl"])}
    for lane, (key, value) in lanes.items():
        legs = rep[key]
        lane_res = {"base": summarise(legs, value, days_all), "no_features": sum(1 for x in legs if x["f"] is None)}
        for vname, rule in VETOES.items():
            cut = [x for x in legs if x["f"] is not None and rule(x["f"])]
            kept = [x for x in legs if not (x["f"] is not None and rule(x["f"]))]
            entry = {"cut": summarise(cut, value, days_all), "kept": summarise(kept, value, days_all)}
            entry["verdict"] = verdict(entry["cut"]) if vname == "V1" else "report_only"
            for side_name, sv in (("same_side", 1), ("opposite_side", -1)):
                entry[f"cut_{side_name}"] = summarise([x for x in cut if x["side"] == sv], value, days_all)
            lane_res[vname] = entry
        results["replay"][lane] = lane_res
    results["replay"]["proper_reentry_V1_maker"] = summarise(rep["proper_reentry"], lambda x: x["maker"], days_all)
    for lane, legs in (("tail", live_tail(feats_by_sd)), ("proper", live_proper(feats_by_sd))):
        lane_res = {"base": summarise(legs, lambda x: x["pnl"], sorted({x["d"] for x in legs}) or days_all),
                    "no_features": sum(1 for x in legs if x["f"] is None)}
        for vname, rule in VETOES.items():
            cut = [x for x in legs if x["f"] is not None and rule(x["f"])]
            lane_res[vname] = {"cut_legs": len(cut), "cut_pnl_sum": float(sum(x["pnl"] for x in cut)),
                               "cut_hits": int(sum(1 for x in cut if x.get("hit", 1.0 if x["pnl"] > 0 else 0.0))),
                               "kept_legs": len(legs) - len(cut),
                               "kept_pnl_sum": float(sum(x["pnl"] for x in legs if x not in cut))}
        results["live"][lane] = lane_res
    json.dump(results, open(os.path.join(HERE, "results_veto.json"), "w"), ensure_ascii=False, indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main()
