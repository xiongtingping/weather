"""C: which side of the distribution did the posted window cover?

Reads only. Every number printed here is computed from
paper_trade_position (posted_buckets_json, probabilities_json,
ladder_center_bucket) joined to paper_trade_settlement.
Ordinal order == key order of probabilities_json, which the ledger
builds by iterating basket_probability_bridge_bucket_probability
ORDER BY ordinal (scripts/paper_trading_ledger.py _stage_entry_candidates).
"""
import json, sqlite3, sys, time, collections

DB = "file:data/bayes_basket_target_ssot.db?mode=ro"

def connect():
    for _ in range(20):
        try:
            c = sqlite3.connect(DB, uri=True, timeout=30)
            c.execute("select count(*) from paper_trade_settlement").fetchone()
            return c
        except sqlite3.OperationalError as e:
            print("retry:", e, file=sys.stderr); time.sleep(10)
    raise SystemExit("db unavailable")

conn = connect()
pos = {}
for r in conn.execute(
    "select station_id,event_date,stage,decision_at_utc,ladder_center_bucket,"
    " posted_buckets_json,window_buckets_json,probabilities_json,"
    " leg_shares_json,win_probability,center_c from paper_trade_position"
):
    pos.setdefault((r[0], r[1]), []).append(r)
setl = {}
for r in conn.execute(
    "select station_id,event_date,stage,settled_bucket,settled_tmax_c,hit,"
    " pnl_usdc,stake_usdc from paper_trade_settlement"
):
    setl[(r[0], r[1])] = r

def analyse(row, settled_bucket):
    (_st, _ed, stage, dec, ladder_c, posted_j, win_j, prob_j,
     legs_j, winp, center_c) = row
    p = json.loads(prob_j)
    order = list(p.keys())
    idx = {b: i for i, b in enumerate(order)}
    posted = json.loads(posted_j or "[]") or json.loads(win_j or "[]")
    posted = [b for b in posted if b in idx]
    if not posted or ladder_c not in idx:
        return None
    lo, hi = min(idx[b] for b in posted), max(idx[b] for b in posted)
    c = idx[ladder_c]
    argmax_b = max(p, key=lambda b: p[b])
    a = idx[argmax_b]
    mass = sum(p[b] for b in posted)
    below = sum(v for b, v in p.items() if idx[b] < lo)
    above = sum(v for b, v in p.items() if idx[b] > hi)
    n_below_c = sum(1 for b in posted if idx[b] < c)
    n_above_c = sum(1 for b in posted if idx[b] > c)
    if n_below_c == 0 and n_above_c == 0:
        side = "single"          # window is the centre alone
    elif n_below_c == 0:
        side = "up_only"
    elif n_above_c == 0:
        side = "down_only"
    elif n_above_c > n_below_c:
        side = "up_tilt"
    elif n_below_c > n_above_c:
        side = "down_tilt"
    else:
        side = "balanced"
    endpoint = (c == lo or c == hi)
    s = idx.get(settled_bucket)
    out = {
        "stage": stage, "decision_at_utc": dec,
        "posted": posted, "n": len(posted),
        "mass": mass, "mass_below": below, "mass_above": above,
        "ladder_centre": ladder_c, "argmax": argmax_b,
        "centre_is_argmax": ladder_c == argmax_b,
        "centre_at_endpoint": endpoint,
        "centre_index_in_window": c - lo,
        "side": side, "n_below_c": n_below_c, "n_above_c": n_above_c,
        "settled_idx": s, "centre_idx": c, "lo": lo, "hi": hi,
        "settled_in_window": (s is not None and lo <= s <= hi),
        "centre_mass": p[ladder_c], "argmax_mass": p[argmax_b],
        "dist_settled_to_centre": (None if s is None else s - c),
    }
    if s is not None:
        # abandoned side = the side of the CENTRE the window did not extend to
        if side in ("up_only", "single"):
            out["truth_on_abandoned_side"] = s < c
            out["abandoned"] = "down"
        elif side == "down_only":
            out["truth_on_abandoned_side"] = s > c
            out["abandoned"] = "up"
        else:
            out["truth_on_abandoned_side"] = None
            out["abandoned"] = "none"
        out["truth_outside_side"] = (
            "below" if s < lo else ("above" if s > hi else "in")
        )
    return out

def pct(x, n):
    return "n/a" if not n else "%.1f%% (%d/%d)" % (100.0 * x / n, x, n)

for lane, pick in (
    ("ENTRY stage (first decision of the chain)", "first"),
    ("SETTLED stage (the row settlement points at)", "settled"),
):
    rows = []
    for key, sr in sorted(setl.items()):
        cands = pos.get(key) or []
        if not cands:
            continue
        if pick == "first":
            row = sorted(cands, key=lambda r: r[3])[0]
        else:
            row = next((r for r in cands if r[2] == sr[2]), None)
            if row is None:
                continue
        a = analyse(row, sr[3])
        if a is None:
            continue
        a["key"] = key
        a["hit"] = sr[5]
        a["pnl"] = sr[6]
        rows.append(a)
    n = len(rows)
    print("\n=== %s  |  settled station-days analysed: %d" % (lane, n))
    if not n:
        continue
    covered = sorted(r["mass"] for r in rows)
    print("posted-window posterior mass: median %.3f  mean %.3f  p10 %.3f p90 %.3f"
          % (covered[n//2], sum(covered)/n, covered[max(0,int(.1*n))], covered[min(n-1,int(.9*n))]))
    print("window size: " + str(collections.Counter(r["n"] for r in rows).most_common()))
    print("ladder centre AT window endpoint: " + pct(sum(r["centre_at_endpoint"] for r in rows), n))
    multi = [r for r in rows if r["n"] > 1]
    print("  (windows with n>1 only): " + pct(sum(r["centre_at_endpoint"] for r in multi), len(multi)))
    print("ladder centre == argmax of entry distribution: " + pct(sum(r["centre_is_argmax"] for r in rows), n))
    print("side of centre the window extends to: " + str(collections.Counter(r["side"] for r in rows).most_common()))
    onesided = [r for r in rows if r["side"] in ("up_only","down_only","single")]
    print("one-sided (incl. n=1) windows: " + pct(len(onesided), n))
    tos = [r for r in onesided if r.get("truth_on_abandoned_side") is not None]
    print("  truth on the ABANDONED side of the centre: " + pct(sum(bool(r["truth_on_abandoned_side"]) for r in tos), len(tos)))
    misses = [r for r in tos if not r["hit"]]
    print("  ... among MISSES only: " + pct(sum(bool(r["truth_on_abandoned_side"]) for r in misses), len(misses)))
    print("settled bucket OUTSIDE posted window: " + pct(sum(not r["settled_in_window"] for r in rows), n))
    print("  of those, side: " + str(collections.Counter(r["truth_outside_side"] for r in rows if not r["settled_in_window"]).most_common()))
    print("mass abandoned below window: mean %.3f | above window: mean %.3f"
          % (sum(r["mass_below"] for r in rows)/n, sum(r["mass_above"] for r in rows)/n))
    # abandoned mass on the side truth actually landed
    print("hit rate: " + pct(sum(r["hit"] for r in rows), n))
    print("hit rate | centre at endpoint: " + pct(
        sum(r["hit"] for r in rows if r["centre_at_endpoint"]),
        sum(1 for r in rows if r["centre_at_endpoint"])))
    print("hit rate | centre interior: " + pct(
        sum(r["hit"] for r in rows if not r["centre_at_endpoint"]),
        sum(1 for r in rows if not r["centre_at_endpoint"])))
    d = collections.Counter(r["dist_settled_to_centre"] for r in rows if r["dist_settled_to_centre"] is not None)
    print("settled minus ladder-centre index: " + str(sorted(d.items())))

json.dump(
    [dict(r, key=list(r["key"])) for r in rows],
    open("tmp/window_side_coverage_probe_C.json", "w"), ensure_ascii=False, indent=1)
