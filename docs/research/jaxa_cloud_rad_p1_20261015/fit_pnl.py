"""Judge the JAXA cloud / sunshine / rain tilts by money, not by LogScore
(user 2026-10-15 「为什么一定要用ΔLogScore评分」→「宪章 211/240 定的主裁判并不一定正确！」).

Against market-relative log score the market term cancels, so only a trading
rule can rank differently.  Rule (taker, the Proper line's sizing): at each
anchor, with the official WS snapshot the market showed then (<= 30 min old),
for every bucket

    b > ask      buy YES  w = ln(b / ask)          shares, pay ask
    b < bid      buy NO   w = ln((1-b) / (1-bid))   shares, pay 1 - bid

plus the taker fee 0.05 * price * (1 - price) a share; P&L at settlement.
The same rule runs on the served P1 and on the tilted P1; the strength of
each tilt is picked on earlier days from {0, .25, .5, 1, 2} by training
P&L, then the test day is scored.  Differences by event-day block bootstrap.
"""

from __future__ import annotations

import gzip
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
import fit_jaxa as J  # noqa: E402
import fit_rain as R  # noqa: E402

GRID = (0.0, 0.25, 0.5, 1.0, 2.0)
FEE = 0.05
MAX_AGE = timedelta(minutes=30)


def quotes(conn, station, day, at):
    hi = at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"
    lo_t = at - MAX_AGE
    lo = lo_t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{lo_t.microsecond // 1000:03d}Z"
    row = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
        " AND state='COMMITTED' AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",
        (station, day, hi, lo)).fetchone()
    if row is None:
        return None
    out = {}
    for b in json.loads(row[0] or "[]"):
        ask = b.get("best_ask") if b.get("ask_available") else None
        bid = b.get("bid_price") if b.get("bid_available") else None
        out[str(b.get("bucket"))] = (float(ask) if ask else None, float(bid) if bid else None)
    return out


def attach(rows, data, conn):
    """Each prepared row gets the market's ask/bid per bucket index."""

    by_key = {}
    for r in rows:
        iv = r["intervals"]
        if not iv:
            continue
        labels = sorted(iv, key=lambda b: (iv[b][0] if iv[b][0] is not None else -1e9))
        by_key[(r["station"], r["day"], r["local_hour"])] = (labels, r["decided_at"])
    kept = []
    for x in data:
        labels, at = by_key[(x["station"], x["day"], x["hour"])]
        q = quotes(conn, x["station"], x["day"], J.utc(at))
        if not q:
            continue
        x["ask"] = np.array([q.get(l, (None, None))[0] or np.nan for l in labels])
        x["bid"] = np.array([q.get(l, (None, None))[1] or np.nan for l in labels])
        kept.append(x)
    return kept


def pnl(x, b):
    win = np.zeros(len(b)); win[x["k"]] = 1.0
    total = 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        yes = np.where(np.isfinite(x["ask"]) & (b > x["ask"]), np.log(b / x["ask"]), 0.0)
        no = np.where(np.isfinite(x["bid"]) & (b < x["bid"]), np.log((1 - b) / (1 - x["bid"])), 0.0)
    for i in range(len(b)):
        if yes[i] > 0:
            p = x["ask"][i]
            total += yes[i] * (win[i] - p - FEE * p * (1 - p))
        if no[i] > 0:
            p = 1 - x["bid"][i]
            total += no[i] * ((1 - win[i]) - p - FEE * p * (1 - p))
    return total


def run(label, data, tilt):
    days = sorted({x["day"] for x in data})
    base_by = defaultdict(float)
    for x in data:
        base_by[(x["day"], x["station"])] += pnl(x, x["p"])
    cand_cache = {s: {id(x): pnl(x, tilt(x, s)) for x in data} for s in GRID}
    per_sd = defaultdict(lambda: [0.0, 0.0])
    chosen = []
    for i, day in enumerate(days):
        if i < J.MIN_TRAIN_DAYS:
            continue
        train = [x for x in data if x["day"] < day]
        best = max(GRID, key=lambda s: sum(cand_cache[s][id(x)] for x in train))
        chosen.append(best)
        for x in (y for y in data if y["day"] == day):
            per_sd[(day, x["station"])][0] += pnl(x, x["p"])
            per_sd[(day, x["station"])][1] += cand_cache[best][id(x)]
    by_day = defaultdict(list)
    for (day, _st), (b0, c0) in per_sd.items():
        by_day[day].append(c0 - b0)
    rng = random.Random(7)
    days_t = sorted(by_day)
    point = np.mean([v for d in days_t for v in by_day[d]])
    boots = [np.mean([v for d in (rng.choice(days_t) for _ in days_t) for v in by_day[d]]) for _ in range(2000)]
    base_total = sum(v[0] for v in per_sd.values())
    fixed = {s: sum(cand_cache[s][id(x)] for x in data if x["day"] >= days[J.MIN_TRAIN_DAYS]) - sum(
        pnl(x, x["p"]) for x in data if x["day"] >= days[J.MIN_TRAIN_DAYS]) for s in GRID[1:]}
    print(f"{label:6s} rows {len(data):5d} sd {len(per_sd):4d}  base P&L {base_total:+9.2f} share-units  "
          f"chosen {sorted(set(chosen))} (last {chosen[-1] if chosen else None})  "
          f"dP&L/station-day {point:+.4f} [{np.percentile(boots, 2.5):+.4f},{np.percentile(boots, 97.5):+.4f}]  "
          f"fixed-strength total d: " + " ".join(f"{s}:{v:+.1f}" for s, v in fixed.items()), flush=True)


def main() -> None:
    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=30)
    rows = [json.loads(l) for l in gzip.open(HERE / "rows.jsonl.gz", "rt")]
    swr, clp = J.load_jaxa()
    dj = attach(rows, J.prepare(rows, swr, clp), conn)
    run("日照", dj, lambda x, s: J.tilted(x, s, 0.0))
    run("云", dj, lambda x, s: J.tilted(x, 0.0, s))
    rr = [json.loads(l) for l in gzip.open(HERE / "rows_gsmap.jsonl.gz", "rt")]
    dr = attach(rr, R.prepare(rr, R.load_gsmap(), "mm"), conn)
    run("雨", dr, lambda x, s: R.tilted(x, s))


if __name__ == "__main__":
    main()
