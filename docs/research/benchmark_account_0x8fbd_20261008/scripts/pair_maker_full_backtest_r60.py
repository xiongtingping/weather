"""Round 60 (熊先生 10-12 ruling: 成形即挂 / 众数 NO 单边可暴露 / pair_cost 0.95 作挂价上限):
the pair maker lane backtested on every station-day with an official WS book and a truth.

Start = the first 10-minute snapshot at which the book is formed (every bucket two-sided,
mids sum 0.7-1.4), searched up to 48 h after the first book.  Targets: the market mode and the
market mean bucket at formation.  Quotes (post-only, static at the formation mid m):
3 YES rungs x_k = m - d - {0, 0.02, 0.05}, 3 NO rungs y_k = PC - (m - d) - {0, 0.02, 0.05}
(so every same-index pair costs <= PC = 0.95), 5 shares each; live for W hours, then the
unfilled rungs are cancelled; unmatched shares are held to settlement (naked YES and NO both
allowed on the mode, per the ruling; reported for the mean bucket too).
Fill models: touch (YES rung fills when best ask <= x; NO rung when best YES bid >= 1 - y) and
through (strictly through the level by a tick: ask <= x - 0.001 / bid >= 1 - y + 0.001), the
latter a stand-in for queue position.  P&L per station-day, return on filled capital, matched
share, unmatched ratio; 95% intervals by event-day block bootstrap; both halves of the dates.
Cached per station-day series in <bench>/pair_series_r60.json (resume with --budget).
"""
import collections
import json
import sqlite3
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import listing_forecast_f as F  # noqa: E402

B = Path(sys.argv[1])
BUDGET = float(sys.argv[sys.argv.index("--budget") + 1]) if "--budget" in sys.argv else 500.0
CACHE = B / "pair_series_r60.json"
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
TRUTH = F.formal_truths(conn, since=date(2026, 7, 1), until=date(2026, 10, 11))
cache = json.load(open(CACHE)) if CACHE.exists() else {}


def ladder(st, d):
    rows = conn.execute("SELECT outcome, registry_json, settlement_unit FROM basket_target_market_registry"
                        " WHERE station_id=? AND event_date=? ORDER BY refreshed_at DESC LIMIT 60", (st, d)).fetchall()
    seen = {}
    for outcome, rj, unit in rows:
        if outcome in seen:
            continue
        try:
            r = json.loads(rj)
        except ValueError:
            continue
        lo = None if r.get("lower") is None else float(r["lower"]) - 0.5
        hi = None if r.get("upper") is None else float(r["upper"]) + 0.5
        if str(unit).upper().startswith("F"):
            lo = None if lo is None else (lo - 32) * 5 / 9
            hi = None if hi is None else (hi - 32) * 5 / 9
        seen[outcome] = [lo, hi]
    if len(seen) < 5:
        return None
    items = sorted(seen.items(), key=lambda kv: kv[1][0] if kv[1][0] is not None else -1e9)
    return [k for k, _ in items], [v for _, v in items]


def truth_label(labels, edges, t):
    for lab, (lo, hi) in zip(labels, edges):
        if (lo is None or t >= lo) and (hi is None or t < hi):
            return lab
    return None


def build(st, d):
    lad = ladder(st, d)
    t = TRUTH.get((st, d))
    if lad is None or t is None:
        return None
    labels, edges = lad
    tl = truth_label(labels, edges, t)
    first = conn.execute("SELECT min(observed_at) FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'", (st, d)).fetchone()[0]
    if not first:
        return None
    t0 = datetime.fromisoformat(first.replace("Z", "+00:00"))
    rows = conn.execute("SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                        " AND observed_at<=? GROUP BY substr(observed_at,1,15) ORDER BY observed_at",
                        (st, d, (t0 + timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))).fetchall()
    ser, start = [], None
    for obs, bj in rows:
        book = {}
        for b in json.loads(bj):
            lab = str(b.get("bucket"))
            asks = sorted(float(x["price"]) for x in (b.get("ask_levels") or []) if float(x.get("shares", 0)) >= 5)
            bid = float(b["bid_price"]) if b.get("bid_available") and b.get("bid_price") else None
            book[lab] = [bid, asks[0] if asks else None]
        ts = datetime.fromisoformat(obs.replace("Z", "+00:00")).timestamp()
        if start is None:
            mids = [None if book.get(l, [None, None])[0] is None or book.get(l, [None, None])[1] is None else (book[l][0] + book[l][1]) / 2 for l in labels]
            if all(m is not None for m in mids) and 0.7 <= sum(mids) <= 1.4 and ts - t0.timestamp() <= 48 * 3600:
                tot = sum(mids)
                centres = [((lo if lo is not None else hi - 1) + (hi if hi is not None else lo + 1)) / 2 for lo, hi in edges]
                mean_c = sum(m * c for m, c in zip(mids, centres)) / tot
                mean_lab = next((l for l, (lo, hi) in zip(labels, edges) if (lo is None or mean_c >= lo) and (hi is None or mean_c < hi)), labels[-1])
                mode_lab = labels[int(np.argmax(mids))]
                start = {"ts": ts, "mode": mode_lab, "mean": mean_lab, "mid": {l: m for l, m in zip(labels, mids)}}
        if start is not None:
            ser.append([ts, {k: book.get(k) for k in {start["mode"], start["mean"]}}])
            if ts - start["ts"] > 13 * 3600:
                break
    if start is None:
        return {"formed": False}
    return {"formed": True, "start": start, "ser": ser, "truth": tl, "first_book_ts": t0.timestamp()}


keys = [f"{s}|{d}" for s, d in conn.execute("SELECT DISTINCT station_id, event_date FROM basket_official_ws_snapshot WHERE event_date<='2026-10-11'")
        if (s, d) in TRUTH]
t_start = time.time()
todo = [k for k in keys if k not in cache]
print(f"{len(keys)} station-days with a book and a truth; {len(todo)} to build", file=sys.stderr)
for k in todo:
    if time.time() - t_start > BUDGET:
        json.dump(cache, open(CACHE, "w"))
        print(f"budget reached; {len([k for k in keys if k not in cache])} left -- rerun", file=sys.stderr)
        sys.exit(0)
    s, d = k.split("|")
    cache[k] = build(s, d) or {"formed": False, "skip": True}
json.dump(cache, open(CACHE, "w"))

PC = 0.95
OFFS = (0.0, 0.02, 0.05)
SH = 5.0


def run(entry, target, d, w, model):
    st = entry["start"]
    lab = st[target]
    m = st["mid"][lab]
    xs = [round(m - d - o, 3) for o in OFFS]
    ys = [round(PC - (m - d) - o, 3) for o in OFFS]
    tick = 0.001 if model == "through" else 0.0
    yf = [False] * 3
    nf = [False] * 3
    end = st["ts"] + w * 3600
    for ts, book in entry["ser"]:
        if ts > end:
            break
        bid, ask = (book.get(lab) or [None, None])
        for k in range(3):
            if not yf[k] and xs[k] > 0 and ask is not None and ask <= xs[k] - tick:
                yf[k] = True
            if not nf[k] and ys[k] > 0 and bid is not None and bid >= 1 - ys[k] + tick:
                nf[k] = True
    ysh = SH * sum(yf)
    nsh = SH * sum(nf)
    ycost = SH * sum(x for x, f in zip(xs, yf) if f)
    ncost = SH * sum(y for y, f in zip(ys, nf) if f)
    hit = lab == entry["truth"]
    pnl = ysh * (1.0 if hit else 0.0) + nsh * (0.0 if hit else 1.0) - ycost - ncost
    return pnl, ycost + ncost, min(ysh, nsh), ysh, nsh


formed = {k: v for k, v in cache.items() if v.get("formed") and k in set(keys)}
days = sorted({k.split("|")[1] for k in formed})
mid_day = days[len(days) // 2]
print(f"\n{len(formed)} station-days formed within 48 h of the first book (of {len(keys)}); event days {days[0]}..{days[-1]}")
lat = [(v["start"]["ts"] - v["first_book_ts"]) / 3600 for v in formed.values()]
print(f"formation hours after the first book: median {statistics.median(lat):.1f}; mode verifies {statistics.mean(v['start']['mode'] == v['truth'] for v in formed.values()):.1%},"
      f" mean bucket verifies {statistics.mean(v['start']['mean'] == v['truth'] for v in formed.values()):.1%}; mode == mean {statistics.mean(v['start']['mode'] == v['start']['mean'] for v in formed.values()):.0%}")
rng = np.random.default_rng(60)
print(f"\n{'target':6s} {'fill':7s} {'W':>3s} {'d':>5s} {'P&L/sd':>8s} {'95% CI':>18s} {'ret/cap':>8s} {'halves ret':>14s} {'filled sd':>9s} {'matched':>8s} {'naked YES':>9s} {'naked NO':>8s} {'cap/sd':>7s}")
for target in ("mode", "mean"):
    for model in ("touch", "through"):
        for w in (3, 6, 12):
            for d in (0.01, 0.03, 0.05):
                res = {k: run(v, target, d, w, model) for k, v in formed.items()}
                pn = np.array([r[0] for r in res.values()])
                cp = np.array([r[1] for r in res.values()])
                by = collections.defaultdict(list)
                for k, r in res.items():
                    by[k.split("|")[1]].append(r[0])
                bs = []
                for _ in range(500):
                    pick = rng.choice(days, size=len(days), replace=True)
                    v = [x for dd in pick for x in by[dd]]
                    bs.append(np.mean(v))
                h = {}
                for half in (True, False):
                    sel = [r for k, r in res.items() if (k.split("|")[1] < mid_day) == half]
                    c = sum(r[1] for r in sel)
                    h[half] = sum(r[0] for r in sel) / c if c else float("nan")
                filled = [r for r in res.values() if r[1] > 0]
                ysh = sum(r[3] for r in filled)
                nsh = sum(r[4] for r in filled)
                mt = sum(r[2] for r in filled)
                print(f"{target:6s} {model:7s} {w:3d} {d:5.2f} {pn.mean():+8.3f} [{np.percentile(bs, 2.5):+.3f}, {np.percentile(bs, 97.5):+.3f}] {pn.sum() / cp.sum():+8.1%}"
                      f" {h[True]:+6.1%}/{h[False]:+6.1%} {len(filled) / len(res):9.0%} {2 * mt / max(1e-9, ysh + nsh):8.0%}"
                      f" {(ysh - mt) / max(1e-9, ysh + nsh):9.0%} {(nsh - mt) / max(1e-9, ysh + nsh):8.0%} {cp.mean():7.2f}")
