"""Round 59 (熊先生 10-12, design of a YES+NO pair lane): post-only pair feasibility on the
official WS book history.

Per station-day (first-rows universe, a book within 2 h of listing), the official CLOB WS
snapshots (``basket_official_ws_snapshot``, YES token book per bucket) sampled one per 10
minutes from the first book to the end of the event's local day.  A post-only YES bid at x
is taken as filled at the first snapshot whose best YES ask (>= 5 shares) is <= x (a seller
was there); a post-only NO bid at y as filled at the first snapshot whose best YES bid is
>= 1 - y (a YES buyer at that price matches a NO bid by minting).  Size and queue are not
modelled (small orders; optimistic on queue).  Target buckets: F's bucket at listing (the
market has no mode before it forms) and, once the book forms (sum of mids 0.7-1.4), the
market's mode bucket and its mean bucket at formation.
Policies for pair cost PC (x + y = PC):
  SYM   both bids posted at the first book
  YES1  YES bid first; the NO bid only after the YES fills, at y = PC - x (no naked NO)
Outcomes: both filled (locked 1 - PC a pair), YES only (settles 1 if the truth bucket),
NO only (settles 1 if not), none.  Reported per policy x grid: fill shares, time to pair,
P&L per pair-unit of capital, the unmatched rate and its P&L.
"""
import collections
import gzip
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

B = Path(sys.argv[1])
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 260
FR = json.load(gzip.open(B / "first_rows.json.gz", "rt"))
CACHE = json.load(open(B / "f_v3_backtest_r57.json"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
ZONE = {s: z for s, z in conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}


def edges_of(r):
    out = [[*r["bounds"][lab]] for lab in r["labels"]]
    w = float(r.get("width") or 1.0)
    if out[0][0] is None:
        out[0][0] = out[0][1] - w
    if out[-1][1] is None:
        out[-1][1] = out[-1][0] + w
    return out


def bucket(edges, x):
    if x < edges[0][1]:
        return 0
    for i, (lo, hi) in enumerate(edges):
        if lo <= x < hi:
            return i
    return len(edges) - 1


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def series(r):
    """[(ts, {label: (best_bid, best_ask)})] one per 10 minutes, listing .. end of the local event day."""
    zone = ZONE.get(r["st"])
    if not zone:
        return []
    end = datetime.fromisoformat(r["ed"]).replace(tzinfo=ZoneInfo(zone)) + timedelta(days=1)
    rows = conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at>=? AND observed_at<? GROUP BY substr(observed_at,1,15) ORDER BY observed_at",
        (r["st"], r["ed"], iso(float(r["listed"])), end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))).fetchall()
    out = []
    for obs, bj in rows:
        book = {}
        for b in json.loads(bj):
            lab = str(b.get("bucket"))
            asks = sorted(float(x["price"]) for x in (b.get("ask_levels") or []) if float(x.get("shares", 0)) >= 5)
            bid = float(b["bid_price"]) if b.get("bid_available") and b.get("bid_price") else None
            book[lab] = (bid, asks[0] if asks else None)
        out.append((datetime.fromisoformat(obs.replace("Z", "+00:00")).timestamp(), book))
    return out


def formed(book, labels):
    mids = []
    for lab in labels:
        bid, ask = book.get(lab, (None, None))
        if bid is None or ask is None:
            return None
        mids.append((bid + ask) / 2)
    s = sum(mids)
    return mids if 0.7 <= s <= 1.4 else None


def simulate(ser, lab, t_start, x, y, policy):
    """(state, t_yes, t_no): state in BOTH / YES / NO / NONE."""
    t_yes = t_no = None
    for ts, book in ser:
        if ts < t_start:
            continue
        bid, ask = book.get(lab, (None, None))
        if t_yes is None and ask is not None and ask <= x:
            t_yes = ts
        no_live = policy == "SYM" or (t_yes is not None and ts >= t_yes)
        if t_no is None and no_live and bid is not None and bid >= 1 - y:
            t_no = ts
        if t_yes is not None and t_no is not None:
            break
    if t_yes and t_no:
        return "BOTH", t_yes, t_no
    return ("YES" if t_yes else "NO" if t_no else "NONE"), t_yes, t_no


cands = [r for r in FR if r.get("truth") in r["labels"] and len(r["labels"]) >= 5]
cands.sort(key=lambda r: (r["ed"], r["st"]))
step = max(1, len(cands) // LIMIT)
sample = cands[::step][:LIMIT]
data = []
for r in sample:
    ser = series(r)
    if len(ser) < 6 or ser[0][0] - float(r["listed"]) > 7200:
        continue
    edges = edges_of(r)
    p = CACHE.get(str((int(r["listed"]) + 300) // 600 * 600), {}).get("station_days", {}).get(f"{r['st']}|{r['ed']}")
    if not p:
        continue
    labels = r["labels"]
    targets = {"F bucket at listing": (labels[bucket(edges, p["f"])], ser[0][0])}
    for ts, book in ser:
        mids = formed(book, labels)
        if mids:
            tot = sum(mids)
            mode = int(np.argmax(mids))
            centres = [(lo + hi) / 2 for lo, hi in edges]
            mean_c = sum(m * c for m, c in zip(mids, centres)) / tot
            targets["market mode at formation"] = (labels[mode], ts)
            targets["market mean bucket at formation"] = (labels[bucket(edges, mean_c)], ts)
            break
    data.append({"r": r, "ser": ser, "targets": targets, "truth": r["truth"]})
print(f"{len(data)} station-days with a book from listing (sample of {len(sample)}); formed books in {sum('market mode at formation' in d['targets'] for d in data)}")
first = [d["ser"][0][1].get(d["targets"]["F bucket at listing"][0], (None, None)) for d in data]
fb = [b for b, a in first if b is not None]
fa = [a for b, a in first if a is not None]
print(f"F bucket's first book: best YES bid median {statistics.median(fb) if fb else float('nan'):.3f} (present {len(fb)}/{len(data)}), best YES ask median {statistics.median(fa):.3f};"
      f" implied NO ask median {1 - statistics.median(fb) if fb else float('nan'):.3f}")

for tname in ("F bucket at listing", "market mode at formation", "market mean bucket at formation"):
    rows = [d for d in data if tname in d["targets"]]
    print(f"\n== target: {tname} (n {len(rows)}); truth = target bucket {statistics.mean(d['targets'][tname][0] == d['truth'] for d in rows):.0%}")
    print(f"   {'policy':5s} {'PC':>4s} {'x':>5s} {'y':>5s} {'both':>5s} {'YES only':>8s} {'NO only':>7s} {'none':>5s} {'hrs to pair':>11s} {'YES-only hit':>12s} {'P&L / pair-unit':>15s} {'P&L / filled cap':>16s}")
    for pc in (0.95, 0.90):
        for policy in ("SYM", "YES1"):
            for x in (0.25, 0.30, 0.35, 0.40, 0.45):
                y = round(pc - x, 3)
                st = collections.Counter()
                hrs, yes_hit, pnl, cap = [], [], 0.0, 0.0
                for d in rows:
                    lab, t0 = d["targets"][tname]
                    state, ty, tn = simulate(d["ser"], lab, t0, x, y, policy)
                    st[state] += 1
                    hit = lab == d["truth"]
                    if state == "BOTH":
                        hrs.append((max(ty, tn) - t0) / 3600)
                        pnl += 1 - pc
                        cap += pc
                    elif state == "YES":
                        yes_hit.append(hit)
                        pnl += (1.0 if hit else 0.0) - x
                        cap += x
                    elif state == "NO":
                        pnl += (0.0 if hit else 1.0) - y
                        cap += y
                n = len(rows)
                print(f"   {policy:5s} {pc:4.2f} {x:5.2f} {y:5.2f} {st['BOTH'] / n:5.0%} {st['YES'] / n:8.0%} {st['NO'] / n:7.0%} {st['NONE'] / n:5.0%}"
                      f" {statistics.median(hrs) if hrs else float('nan'):11.1f} {statistics.mean(yes_hit) if yes_hit else float('nan'):12.0%}"
                      f" {pnl / n:+15.3f} {pnl / cap if cap else float('nan'):+16.1%}")

# ---------------------------------------------------------------- time-boxed quoting
# Quotes live only for W hours from the target's start; unfilled legs are cancelled at W;
# an unmatched leg is held to settlement.  (Static quotes held to settlement fill the losing
# leg by construction: a bucket going to 0 hits the YES bid, one going to 1 the NO bid.)
def simulate_w(ser, lab, t_start, x, y, policy, w_hours):
    t_end = t_start + w_hours * 3600
    t_yes = t_no = None
    for ts, book in ser:
        if ts < t_start:
            continue
        if ts > t_end:
            break
        bid, ask = book.get(lab, (None, None))
        if t_yes is None and ask is not None and ask <= x:
            t_yes = ts
        no_live = policy == "SYM" or (t_yes is not None and ts >= t_yes)
        if t_no is None and no_live and bid is not None and bid >= 1 - y:
            t_no = ts
        if t_yes is not None and t_no is not None:
            break
    if t_yes and t_no:
        return "BOTH"
    return "YES" if t_yes else "NO" if t_no else "NONE"


print("\n\n==== time-boxed quoting: unfilled legs cancelled after W hours; unmatched legs held to settlement")
for tname in ("F bucket at listing", "market mode at formation"):
    rows = [d for d in data if tname in d["targets"]]
    print(f"\n== target: {tname} (n {len(rows)})")
    print(f"   {'policy':5s} {'W h':>4s} {'PC':>4s} {'x':>5s} {'y':>5s} {'both':>5s} {'YES only':>8s} {'NO only':>7s} {'none':>5s} {'YES-only hit':>12s} {'NO-only win':>11s} {'P&L / station-day':>17s} {'P&L / filled cap':>16s}")
    for policy in ("SYM", "YES1"):
        for w in (1, 3, 6, 12, 24):
            for pc in (0.95,):
                for x in (0.30, 0.35, 0.40):
                    y = round(pc - x, 3)
                    st = collections.Counter()
                    yes_hit, no_win, pnl, cap = [], [], 0.0, 0.0
                    for d in rows:
                        lab, t0 = d["targets"][tname]
                        state = simulate_w(d["ser"], lab, t0, x, y, policy, w)
                        st[state] += 1
                        hit = lab == d["truth"]
                        if state == "BOTH":
                            pnl += 1 - pc
                            cap += pc
                        elif state == "YES":
                            yes_hit.append(hit)
                            pnl += (1.0 if hit else 0.0) - x
                            cap += x
                        elif state == "NO":
                            no_win.append(not hit)
                            pnl += (0.0 if hit else 1.0) - y
                            cap += y
                    n = len(rows)
                    print(f"   {policy:5s} {w:4d} {pc:4.2f} {x:5.2f} {y:5.2f} {st['BOTH'] / n:5.0%} {st['YES'] / n:8.0%} {st['NO'] / n:7.0%} {st['NONE'] / n:5.0%}"
                          f" {statistics.mean(yes_hit) if yes_hit else float('nan'):12.0%} {statistics.mean(no_win) if no_win else float('nan'):11.0%}"
                          f" {pnl / n:+17.4f} {pnl / cap if cap else float('nan'):+16.1%}")

# ---------------------------------------------------------------- when does the book form?
lat = [(d["targets"]["market mode at formation"][1] - float(d["r"]["listed"])) / 3600 for d in data if "market mode at formation" in d["targets"]]
print(f"\nbook formation (all buckets two-sided, sum of mids 0.7-1.4): {len(lat)}/{len(data)} station-days within the window;"
      f" hours after listing median {statistics.median(lat):.1f}, p25 {np.percentile(lat, 25):.1f}, p75 {np.percentile(lat, 75):.1f}")
same = [d["targets"]["market mode at formation"][0] == d["targets"]["F bucket at listing"][0] for d in data if "market mode at formation" in d["targets"]]
print(f"market mode at formation = F's bucket at listing on {statistics.mean(same):.0%}")
