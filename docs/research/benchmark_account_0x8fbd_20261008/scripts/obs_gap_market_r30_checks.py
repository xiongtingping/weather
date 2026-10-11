"""Round 30 post-hoc checks (NOT in the committed protocol; reported as checks
on the verdict, not as tests): look-ahead, sample halves, the price source,
and what the leftover is worth.

  A  protocol signals                          B  satellite lagged 1 h (hours ending <= tau-1)
  C  satellite lagged 2 h                      D  no satellite at all (METAR + forecast only)
  halves of the event days for A; for each: P1/P2 slopes (95%), walk-forward
  squared-error change, and the walk-forward log score of the market vector
  shifted by the predicted leftover against the market itself (1e-3 floor).
  Price source: CLOB prices-history value vs our committed WS book mid at the
  same minute (10-03..10-10).  Money: WS spreads near the mode at 10/12 local,
  and the round trip -- buy at tau the bucket the shift favours most, mark at
  tau+2h mid -- in cents per share.
"""
import collections
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

src = Path(__file__).with_name("obs_gap_market_r30.py").read_text()
head = src.split("# walk-forward physics shift S (C) per tau")[0]
ns = {"__name__": "r30", "__file__": str(Path(__file__).with_name("obs_gap_market_r30.py"))}
exec(compile(head, "obs_gap_market_r30_head", "exec"), ns)
exec(compile(src[src.index("def ols("):src.index("# ------------------------------------------------------------------ primary")], "r30_stats", "exec"), ns)
rows, OF, minus2, boot_slope, boot_mean, ols, conn, L, P = (ns[k] for k in (
    "rows", "OF", "minus2", "boot_slope", "boot_mean", "ols", "conn", "L", "P"))
exec(compile(src[src.index("def shifted("):src.index('print("\\n== secondary: trading back-test')], "shifted", "exec"), ns)
shifted = ns["shifted"]


def rad_upto(r, end_hour):
    st, ed = r["st"], r["ed"]
    f, sat = OF[st]["fc"], OF[st].get("sat") or {}
    if not sat.get("time"):
        return None
    idx = {t: i for i, t in enumerate(f["time"])}
    sidx = {t: i for i, t in enumerate(sat["time"])}
    so = sf = 0.0
    for hh in range(7, end_hour + 1):
        k = f"{ed}T{hh:02d}:00"
        i, j = sidx.get(k), idx.get(k)
        if i is None or j is None or sat["sw"][i] is None or f["shortwave_radiation"][j] is None:
            continue
        so += sat["sw"][i]
        sf += f["shortwave_radiation"][j]
    return so / sf - 1.0 if sf > 100 else None


for r in rows:
    r["radB"] = rad_upto(r, r["tau"] - 1)
    r["radC"] = rad_upto(r, r["tau"] - 2)


def xA(r):
    s = r["sig"]
    return [1.0, s["temp"], s["cloud"], s["rain"], s["rad"] or 0.0, 1.0 if s["rad"] is None else 0.0]


def xB(r):
    s = r["sig"]
    return [1.0, s["temp"], s["cloud"], s["rain"], r["radB"] or 0.0, 1.0 if r["radB"] is None else 0.0]


def xC(r):
    s = r["sig"]
    return [1.0, s["temp"], s["cloud"], s["rain"], r["radC"] or 0.0, 1.0 if r["radC"] is None else 0.0]


def xD(r):
    s = r["sig"]
    return [1.0, s["temp"], s["cloud"], s["rain"]]


def fit_shift(sub, xf):
    out = {}
    for d in sorted({r["ed"] for r in sub}):
        cut = minus2(d)
        train = [r for r in sub if r["ed"] <= cut]
        if len({r["ed"] for r in train}) < 10:
            continue
        X = np.array([xf(r) for r in train])
        yv = np.array([r["tmax"] - r["fmax"] - r["beta"] for r in train])
        w, *_ = np.linalg.lstsq(X, yv, rcond=None)
        for r in sub:
            if r["ed"] == d:
                out[id(r)] = float(np.dot(w, xf(r))) / r["width"]
    return out


def evaluate(name, xf, day_filter=None):
    print(f"\n-- {name}")
    for tau in (10, 12):
        sub = [r for r in rows if r["tau"] == tau]
        S = fit_shift(sub, xf)
        sub = [dict(r, S_ord=S[id(r)]) for r in sub if id(r) in S and (day_filter is None or day_filter(r["ed"]))]
        if len({r["ed"] for r in sub}) < 12:
            continue
        b, lo, hi, n, nd = boot_slope(sub, "S_ord", "r", 0.025, 0.975)
        s2 = [r for r in sub if r["move2"] is not None]
        b2, lo2, hi2, *_ = boot_slope(s2, "S_ord", "move2", 0.025, 0.975)
        inc, dls, legs = collections.defaultdict(list), collections.defaultdict(list), []
        for d in sorted({r["ed"] for r in sub}):
            cut = minus2(d)
            train = [r for r in sub if r["ed"] <= cut]
            if len({r["ed"] for r in train}) < 10:
                continue
            bb, aa = ols([r["S_ord"] for r in train], [r["r"] for r in train])
            a0 = statistics.mean(r["r"] for r in train)
            for r in sub:
                if r["ed"] != d:
                    continue
                g = aa + bb * r["S_ord"]
                inc[d].append((r["r"] - g) ** 2 - (r["r"] - a0) ** 2)
                p2 = shifted(r["q"], g)
                dls[d].append(-math.log(max(p2[r["y"]], 1e-3)) + math.log(max(r["q"][r["y"]], 1e-3)))
                legs.append((r, g, p2))
        m, ilo, ihi, *_ = boot_mean(inc) if inc else (float("nan"),) * 3
        ml, llo, lhi, nl, ndl = boot_mean(dls) if dls else (float("nan"),) * 5
        print(f"   {tau}:00  y-M slope {b:+.3f} [{lo:+.3f},{hi:+.3f}]  2h drift {b2:+.3f} [{lo2:+.3f},{hi2:+.3f}]  (n={n}, {nd} days)"
              f"   wf sq-err {m:+.4f} [{ilo:+.4f},{ihi:+.4f}]   wf log score shifted-vs-market {ml:+.4f} [{llo:+.4f},{lhi:+.4f}] (n={nl})")
        if name.startswith("A ") and day_filter is None:
            LEGS[tau] = legs


LEGS = {}
days = sorted({r["ed"] for r in rows})
mid_day = days[len(days) // 2]
evaluate("A protocol signals", xA)
evaluate("B satellite lagged 1 h", xB)
evaluate("C satellite lagged 2 h", xC)
evaluate("D no satellite (METAR + forecast only)", xD)
evaluate(f"A first half (event days < {mid_day})", xA, lambda d: d < mid_day)
evaluate(f"A second half (event days >= {mid_day})", xA, lambda d: d >= mid_day)

# ------------------------------------------------------------ price source and spreads (WS book, 10-03..10-10)
print("\n== price source: CLOB prices-history value vs our WS book mid at the same minute (10-03..10-10, buckets with a two-sided book)")
diffs, spreads = [], collections.defaultdict(list)
for key, lad in L.items():
    st, ed = key.split("|")
    if not "2026-10-03" <= ed <= "2026-10-10" or "error" in lad:
        continue
    t0 = None
    for b in lad["buckets"]:
        v = P.get(f"{key}|{b['label']}")
        if v and "t0" in v:
            t0 = v["t0"]
            break
    if t0 is None:
        continue
    for hour in (10, 12):
        at = t0 + (hour - 4) * 3600
        iso = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lo_iso = datetime.fromtimestamp(at - 300, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        row = conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                           " AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (st, ed, iso, lo_iso)).fetchone()
        if not row:
            continue
        for bk in json.loads(row[0]):
            if not bk.get("bid_available") or not bk.get("ask_available"):
                continue
            bid, ask = float(bk["bid_price"]), float(bk["best_ask"])
            mid = (bid + ask) / 2
            v = P.get(f"{key}|{bk['bucket']}")
            if v and v["g"][(hour - 4) * 6] is not None:
                diffs.append(v["g"][(hour - 4) * 6] - mid)
            if 0.05 <= mid <= 0.95:
                spreads[hour].append(ask - bid)
if diffs:
    ad = sorted(abs(x) for x in diffs)
    print(f"   n={len(diffs)}  median |CLOB - WS mid| {statistics.median(ad):.4f}  p90 {ad[int(0.9 * len(ad))]:.4f}  mean signed {statistics.mean(diffs):+.4f}")
for hour, v in spreads.items():
    v.sort()
    print(f"   {hour}:00 spread (ask-bid) on buckets with mid 0.05-0.95: median {statistics.median(v):.3f}  p25 {v[len(v) // 4]:.3f}  p75 {v[3 * len(v) // 4]:.3f}  (n={len(v)})")

# ------------------------------------------------------------ what the leftover is worth
print("\n== round trip: at tau buy (at mid) the bucket the walk-forward shift favours most, mark at tau+2h mid and at settlement; cents per share")
for tau, legs in LEGS.items():
    for lo_g, hi_g in ((0.0, 0.05), (0.05, 0.1), (0.1, 0.2), (0.2, 9)):
        mid_gain, settle_gain = collections.defaultdict(list), collections.defaultdict(list)
        for r, g, p2 in legs:
            if not lo_g <= abs(g) < hi_g:
                continue
            k = max(range(len(p2)), key=lambda i: p2[i] - r["q"][i])
            v = P.get(f"{r['st']}|{r['ed']}|{r['lad']['labels'][k]}")
            later = v["g"][(tau + 2 - 4) * 6] if v else None
            if later is not None:
                mid_gain[r["ed"]].append(100 * (later - r["raw"][k]))
            settle_gain[r["ed"]].append(100 * ((1.0 if r["y"] == k else 0.0) - r["raw"][k]))
        if not settle_gain:
            continue
        m, lo, hi, n, nd = boot_mean(mid_gain)
        ms, slo, shi, *_ = boot_mean(settle_gain)
        print(f"   {tau}:00 |predicted leftover| {lo_g:.2f}-{hi_g:.2f} buckets: 2h mid change {m:+.2f} [{lo:+.2f},{hi:+.2f}]"
              f"   hold to settlement {ms:+.2f} [{slo:+.2f},{shi:+.2f}]  (n={n}, {nd} days)")

# ------------------------------------------------------------ tilt instead of shift
# The CDF shift moves g of every bucket's mass into its neighbour (uniform density
# inside a bucket), far too much across a peaked mode.  The least-change update
# that moves the mean by g is the exponential tilt p_k ~ q_k exp(lam k).


def tilted(q, g):
    ks = np.arange(len(q), dtype=float)
    qa = np.array(q)
    target = float((qa * ks).sum()) + g

    def mean_at(lam):
        w = qa * np.exp(lam * (ks - ks.mean()))
        w /= w.sum()
        return float((w * ks).sum()), w
    lo, hi = -20.0, 20.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if mean_at(mid)[0] < target:
            lo = mid
        else:
            hi = mid
    return list(mean_at((lo + hi) / 2)[1])


print("\n== tilt (least-change update of the market to the predicted mean) -- walk-forward log score vs market, and taker economics")
print("   (spread from the WS book: median 4 c at 10:00, 5 c at 12:00 -> a taker pays mid + 2 c / + 2.5 c)")
for tau, legs in LEGS.items():
    half = 0.02 if tau == 10 else 0.025
    dls = collections.defaultdict(list)
    yes_pnl, no_pnl = collections.defaultdict(list), collections.defaultdict(list)
    edges = []
    for r, g, _p in legs:
        p2 = tilted(r["q"], g)
        dls[r["ed"]].append(-math.log(max(p2[r["y"]], 1e-3)) + math.log(max(r["q"][r["y"]], 1e-3)))
        k = max(range(len(p2)), key=lambda i: p2[i] - r["q"][i])
        edges.append(p2[k] - r["q"][k])
        if p2[k] - r["q"][k] > half:
            yes_pnl[r["ed"]].append((1.0 if r["y"] == k else 0.0) - (r["raw"][k] + half))
        j = max(range(len(p2)), key=lambda i: r["q"][i] - p2[i])
        if r["q"][j] - p2[j] > half:
            no_pnl[r["ed"]].append((0.0 if r["y"] == j else 1.0) - (1.0 - r["raw"][j] + half))
    m, lo, hi, n, nd = boot_mean(dls)
    edges.sort()
    print(f"   {tau}:00  log score tilted-vs-market {m:+.4f} [{lo:+.4f},{hi:+.4f}] (n={n}, {nd} days)   largest per-bucket edge at mid:"
          f" median {100 * edges[len(edges) // 2]:.2f} c, p90 {100 * edges[int(0.9 * len(edges))]:.2f} c, share above half-spread"
          f" {sum(1 for e in edges if e > half) / len(edges):.1%}")
    for name, book in (("YES on the bucket it raises most", yes_pnl), ("NO on the bucket it lowers most", no_pnl)):
        if not book:
            print(f"     {name}: no trade clears the half-spread")
            continue
        keys = list(book)
        tot = sum(sum(v) for v in book.values())
        cnt = sum(len(v) for v in book.values())
        bs = []
        rng = np.random.default_rng(31)
        for _ in range(2000):
            ks = [keys[i] for i in rng.integers(0, len(keys), len(keys))]
            c = sum(len(book[k]) for k in ks)
            bs.append(100 * sum(sum(book[k]) for k in ks) / c if c else 0.0)
        bs.sort()
        print(f"     {name} when its edge clears the half-spread: {cnt} trades on {len(keys)} days, {100 * tot / cnt:+.2f} c per share"
              f" [{bs[50]:+.2f}, {bs[1949]:+.2f}]")
