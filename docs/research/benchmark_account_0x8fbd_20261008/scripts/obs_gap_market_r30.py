"""Round 30 scorer -- exactly the protocol committed in README before scoring
(319aa876; 熊先生 10-11 「实况云雨辐射等，市场并不一定完全在价格上」).

Does the market fully price the day's observed cloud / rain / radiation /
temperature gaps against the day-before forecast?
  P1  truth residual   (y - M(tau)) on the walk-forward physics shift S_ord(tau), tau = 10, 12 local
  P2  market drift     (M(tau+2h) - M(tau)) on S_ord(tau)
4 primary tests, day-clustered bootstrap, 98.75% intervals; P1 also needs the
walk-forward squared-error improvement's 95% interval below zero.
Units are buckets (C 1 C, F 2 F).  Inputs in <bench>: ladders.json,
paths.json (10-minute grid from local 04:00), obsfc.json.
"""
import collections
import json
import math
import random
import re
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import station_day_board as board  # noqa: E402

B = Path(sys.argv[1])
random.seed(30)
L = json.load(open(B / "ladders.json"))
P = json.load(open(B / "paths.json"))
OF = json.load(open(B / "obsfc.json"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
SETTLED = {}
for st, ed, b, t in conn.execute(
        "SELECT station_id, event_date, settled_bucket, settled_tmax_c FROM basket_station_day_settlement_label l"
        " WHERE event_date BETWEEN '2026-08-15' AND '2026-10-10' AND truth_revision_no=(SELECT max(truth_revision_no)"
        " FROM basket_station_day_settlement_label x WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"):
    if b is not None and t is not None:
        SETTLED[(st, ed)] = (str(b), float(t))
HOURS = (9, 10, 11, 12, 13, 14)
PRIMARY = (10, 12)
STEP_PER_HOUR = 6


def f2c(v):
    return (v - 32.0) * 5.0 / 9.0


def bounds(label):
    m = re.match(r"^(-?\d+)°([CF]) or below$", label)
    if m:
        v = float(m.group(1)) + 0.5
        return (-math.inf, v if m.group(2) == "C" else f2c(v)), m.group(2)
    m = re.match(r"^(-?\d+)°([CF]) or higher$", label)
    if m:
        v = float(m.group(1)) - 0.5
        return (v if m.group(2) == "C" else f2c(v), math.inf), m.group(2)
    m = re.match(r"^(-?\d+)-(-?\d+)°F$", label)
    if m:
        return (f2c(float(m.group(1)) - 0.5), f2c(float(m.group(2)) + 0.5)), "F"
    m = re.match(r"^(-?\d+)°C$", label)
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5), "C"
    return None, None


def ladder(key):
    lad = L.get(key)
    if not lad or "error" in lad:
        return None
    rows = []
    for b in lad["buckets"]:
        bd, unit = bounds(b["label"])
        if bd is None:
            return None
        rows.append((bd, unit, b["label"]))
    rows.sort(key=lambda r: r[0][0])
    units = {r[1] for r in rows}
    if len(units) != 1:
        return None
    width = 1.0 if units == {"C"} else 10.0 / 9.0
    edges = [list(r[0]) for r in rows]
    edges[0][0] = edges[0][1] - width
    edges[-1][1] = edges[-1][0] + width
    return {"labels": [r[2] for r in rows], "edges": edges, "width": width, "unit": units.pop()}


def c2ord(x, lad):
    e, w = lad["edges"], lad["width"]
    if x < e[0][0]:
        return -0.5 + (x - e[0][0]) / w
    for k, (lo, hi) in enumerate(e):
        if lo <= x < hi:
            return k - 0.5 + (x - lo) / (hi - lo)
    return len(e) - 0.5 + (x - e[-1][1]) / w


def market(key, lad, hour):
    idx = (hour - 4) * STEP_PER_HOUR
    raw = []
    for label in lad["labels"]:
        v = P.get(f"{key}|{label}")
        if not v or "g" not in v or idx >= len(v["g"]) or v["g"][idx] is None:
            return None
        raw.append(v["g"][idx])
    s = sum(raw)
    if not 0.8 <= s <= 1.3:
        return None
    q = [r / s for r in raw]
    return {"q": q, "raw": raw, "M": sum(k * x for k, x in enumerate(q))}


def fc_max(st, ed):
    f = OF[st]["fc"]
    vals = [f["temperature_2m"][i] for i, t in enumerate(f["time"]) if t.startswith(ed) and f["temperature_2m"][i] is not None]
    return max(vals) if len(vals) >= 20 else None


def signals(st, ed, tau, metar):
    f = OF[st]["fc"]
    idx = {t: i for i, t in enumerate(f["time"])}
    temp_d, cloud_d = [], []
    for m in metar:
        hh = int(m["local"][:2])
        if not 6 <= hh < tau:
            continue
        i = idx.get(f"{ed}T{hh:02d}:00")
        if i is None:
            continue
        if m.get("temp_c") is not None and f["temperature_2m"][i] is not None:
            temp_d.append(m["temp_c"] - f["temperature_2m"][i])
        if m.get("cloud_cover") is not None and f["cloud_cover"][i] is not None:
            cloud_d.append(f["cloud_cover"][i] / 100.0 - m["cloud_cover"])
    if not temp_d or not cloud_d:
        return None
    obs_wet = {int(m["local"][:2]) for m in metar if m.get("precip") and 6 <= int(m["local"][:2]) < tau}
    fc_wet = sum(1 for hh in range(6, tau) if (i := idx.get(f"{ed}T{hh:02d}:00")) is not None
                 and (f["precipitation"][i] or 0.0) >= 0.1)
    out = {"temp": temp_d[-1], "cloud": statistics.mean(cloud_d), "rain": float(fc_wet - len(obs_wet)), "rad": None}
    sat = OF[st].get("sat") or {}
    if sat.get("time"):
        sidx = {t: i for i, t in enumerate(sat["time"])}
        so = sf = 0.0
        for hh in range(7, tau + 1):
            k = f"{ed}T{hh:02d}:00"
            i, j = sidx.get(k), idx.get(k)
            if i is None or j is None or sat["sw"][i] is None or f["shortwave_radiation"][j] is None:
                continue
            so += sat["sw"][i]
            sf += f["shortwave_radiation"][j]
        if sf > 100:
            out["rad"] = so / sf - 1.0
    return out


# ------------------------------------------------------------------ instances
rows, drop = [], collections.Counter()
for key in sorted(L):
    st, ed = key.split("|")
    if (st, ed) not in SETTLED or st not in OF:
        drop["no_truth_or_forecast"] += 1
        continue
    lad = ladder(key)
    if lad is None:
        drop["ladder"] += 1
        continue
    label, tmax = SETTLED[(st, ed)]
    if label not in lad["labels"]:
        drop["truth_not_on_ladder"] += 1
        continue
    fmax = fc_max(st, ed)
    if fmax is None:
        drop["forecast_day"] += 1
        continue
    zone = ZoneInfo(L[key]["tz"])
    try:
        metar = board._metar(st, ed, zone)
    except Exception:
        metar = []
    if not metar:
        drop["no_metar"] += 1
        continue
    m06 = market(key, lad, 6)
    for tau in HOURS:
        mk = market(key, lad, tau)
        if mk is None:
            drop[f"market_{tau}"] += 1
            continue
        sig = signals(st, ed, tau, metar)
        if sig is None:
            drop[f"signals_{tau}"] += 1
            continue
        m2 = market(key, lad, tau + 2)
        rows.append({"st": st, "ed": ed, "tau": tau, "unit": lad["unit"], "width": lad["width"], "lad": lad,
                     "y": lad["labels"].index(label), "tmax": tmax, "fmax": fmax, "q": mk["q"], "raw": mk["raw"],
                     "M": mk["M"], "M2": None if m2 is None else m2["M"], "M06": None if m06 is None else m06["M"], "sig": sig})
print(f"station-days with a ladder {len(L)}; instances {len(rows)}; station-days used {len({(r['st'], r['ed']) for r in rows})}")
print("dropped:", dict(drop))

# station prior bias beta (days <= d-2), fleet fallback
sd_err = {}
for r in rows:
    sd_err[(r["st"], r["ed"])] = r["tmax"] - r["fmax"]


def minus2(ed):
    return (date.fromisoformat(ed) - timedelta(days=2)).isoformat()


beta_cache = {}
for (st, ed) in sd_err:
    cut = minus2(ed)
    own = [v for (s, d), v in sd_err.items() if s == st and d <= cut]
    if len(own) >= 5:
        beta_cache[(st, ed)] = statistics.mean(own)
    else:
        fleet = [v for (s, d), v in sd_err.items() if d <= cut]
        beta_cache[(st, ed)] = statistics.mean(fleet) if fleet else 0.0
for r in rows:
    r["beta"] = beta_cache[(r["st"], r["ed"])]
    r["F_ord"] = c2ord(r["fmax"] + r["beta"], r["lad"])
    r["r"] = r["y"] - r["M"]
    r["move2"] = None if r["M2"] is None else r["M2"] - r["M"]


def xvec(r):
    s = r["sig"]
    return [1.0, s["temp"], s["cloud"], s["rain"], s["rad"] if s["rad"] is not None else 0.0, 1.0 if s["rad"] is None else 0.0]


# walk-forward physics shift S (C) per tau
for tau in HOURS:
    sub = [r for r in rows if r["tau"] == tau]
    for d in sorted({r["ed"] for r in sub}):
        cut = minus2(d)
        train = [r for r in sub if r["ed"] <= cut]
        if len({r["ed"] for r in train}) < 10:
            continue
        X = np.array([xvec(r) for r in train])
        yv = np.array([r["tmax"] - r["fmax"] - r["beta"] for r in train])
        w, *_ = np.linalg.lstsq(X, yv, rcond=None)
        for r in sub:
            if r["ed"] == d:
                r["S"] = float(np.dot(w, xvec(r)))
                r["S_ord"] = r["S"] / r["width"]
rows_s = [r for r in rows if "S_ord" in r]
print(f"instances with a walk-forward physics shift {len(rows_s)} (event days {min(r['ed'] for r in rows_s)}..{max(r['ed'] for r in rows_s)})")


def ols(xs, ys):
    xm, ym = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - xm) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0, ym
    b = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / sxx
    return b, ym - b * xm


RNG = np.random.default_rng(30)


def _day_sums(sel, xk, yk):
    """Per event day: n, sum x, sum y, sum xx, sum xy -- a resampled day set's OLS from sums."""
    days = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])
    for r in sel:
        x = r[xk] if isinstance(xk, str) else xk(r)
        y = r[yk] if isinstance(yk, str) else yk(r)
        a = days[r["ed"]]
        a[0] += 1
        a[1] += x
        a[2] += y
        a[3] += x * x
        a[4] += x * y
    return np.array(list(days.values()))


def _slope(t):
    n, sx, sy, sxx, sxy = t[..., 0], t[..., 1], t[..., 2], t[..., 3], t[..., 4]
    den = n * sxx - sx * sx
    return np.where(den > 0, (n * sxy - sx * sy) / np.where(den > 0, den, 1.0), 0.0)


def boot_slope(sel, xk, yk, lo_q, hi_q, n=2000):
    D = _day_sums(sel, xk, yk)
    b0 = float(_slope(D.sum(axis=0)))
    idx = RNG.integers(0, len(D), size=(n, len(D)))
    bs = np.sort(_slope(D[idx].sum(axis=1)))
    return b0, float(bs[int(lo_q * n)]), float(bs[min(n - 1, int(hi_q * n))]), int(D[:, 0].sum()), len(D)


def boot_mean(vals_by_day, lo_q=0.025, hi_q=0.975, n=2000):
    keys = list(vals_by_day)
    S = np.array([[len(vals_by_day[k]), sum(vals_by_day[k])] for k in keys])
    idx = RNG.integers(0, len(S), size=(n, len(S)))
    T = S[idx].sum(axis=1)
    bs = np.sort(T[:, 1] / T[:, 0])
    tot = S.sum(axis=0)
    return float(tot[1] / tot[0]), float(bs[int(lo_q * n)]), float(bs[min(n - 1, int(hi_q * n))]), int(tot[0]), len(keys)


# ------------------------------------------------------------------ primary
print("\n== PRIMARY (98.75% day-clustered intervals; slopes in buckets per bucket of physics shift)")
wf_pred = {}
for tau in PRIMARY:
    sub = [r for r in rows_s if r["tau"] == tau]
    b, lo, hi, n, nd = boot_slope(sub, "S_ord", "r", 0.00625, 0.99375)
    # walk-forward squared-error improvement
    inc = collections.defaultdict(list)
    for d in sorted({r["ed"] for r in sub}):
        cut = minus2(d)
        train = [r for r in sub if r["ed"] <= cut]
        if len({r["ed"] for r in train}) < 10:
            continue
        bb, aa = ols([r["S_ord"] for r in train], [r["r"] for r in train])
        a0 = statistics.mean(r["r"] for r in train)
        for r in sub:
            if r["ed"] == d:
                pred = aa + bb * r["S_ord"]
                wf_pred[(r["st"], r["ed"], tau)] = pred
                inc[d].append((r["r"] - pred) ** 2 - (r["r"] - a0) ** 2)
    m, ilo, ihi, ni, ndi = boot_mean(inc)
    p1 = (lo > 0 or hi < 0) and ihi < 0
    print(f"   P1 tau={tau}:00  slope {b:+.3f} [{lo:+.3f}, {hi:+.3f}] (n={n}, {nd} days)   walk-forward sq-err change {m:+.4f}"
          f" [{ilo:+.4f}, {ihi:+.4f}] (n={ni}, {ndi} days)   -> {'PASS' if p1 else 'fail'}")
    sub2 = [r for r in sub if r["move2"] is not None]
    b, lo, hi, n, nd = boot_slope(sub2, "S_ord", "move2", 0.00625, 0.99375)
    print(f"   P2 tau={tau}:00  slope {b:+.3f} [{lo:+.3f}, {hi:+.3f}] (n={n}, {nd} days)   -> {'PASS' if (lo > 0 or hi < 0) else 'fail'}")

# ------------------------------------------------------------------ secondary
print("\n== secondary: how much of the physics shift the market already shows (in-sample slopes on S_ord, 95%)")
for tau in HOURS:
    sub = [r for r in rows_s if r["tau"] == tau]
    by, ylo, yhi, n, nd = boot_slope(sub, "S_ord", lambda r: r["y"] - r["F_ord"], 0.025, 0.975, 1000)
    bm, mlo, mhi, *_ = boot_slope(sub, "S_ord", lambda r: r["M"] - r["F_ord"], 0.025, 0.975, 1000)
    s06 = [r for r in sub if r["M06"] is not None]
    b6, *_ = boot_slope(s06, "S_ord", lambda r: r["M06"] - r["F_ord"], 0.025, 0.975, 200)
    print(f"   {tau}:00  truth-forecast {by:+.3f} [{ylo:+.3f},{yhi:+.3f}]   market(tau)-forecast {bm:+.3f} [{mlo:+.3f},{mhi:+.3f}]"
          f"   market(06)-forecast {b6:+.3f}   share priced at tau {bm / by if by else float('nan'):.2f}  (n={n})")

print("\n== secondary: P1/P2 slopes by hour (95%)")
for tau in HOURS:
    sub = [r for r in rows_s if r["tau"] == tau]
    b, lo, hi, n, nd = boot_slope(sub, "S_ord", "r", 0.025, 0.975, 1000)
    sub2 = [r for r in sub if r["move2"] is not None]
    b2, lo2, hi2, n2, _ = boot_slope(sub2, "S_ord", "move2", 0.025, 0.975, 1000)
    print(f"   {tau}:00  truth residual {b:+.3f} [{lo:+.3f},{hi:+.3f}] (n={n})   2h drift {b2:+.3f} [{lo2:+.3f},{hi2:+.3f}] (n={n2})")

print("\n== secondary: physics-shift deciles (mean truth residual y-M and 2h drift, buckets)")
for tau in PRIMARY:
    sub = sorted([r for r in rows_s if r["tau"] == tau], key=lambda r: r["S_ord"])
    n = len(sub)
    print(f"   {tau}:00")
    for k in range(10):
        part = sub[k * n // 10:(k + 1) * n // 10]
        mv = [r["move2"] for r in part if r["move2"] is not None]
        print(f"     decile {k + 1:2d}  S_ord {statistics.mean(r['S_ord'] for r in part):+.2f}  y-M {statistics.mean(r['r'] for r in part):+.3f}"
              f"  drift {statistics.mean(mv) if mv else float('nan'):+.3f}  n={len(part)}")
    for name, part in (("coldest 10%", sub[:n // 10]), ("warmest 10%", sub[-(n // 10):])):
        byd = collections.defaultdict(list)
        for r in part:
            byd[r["ed"]].append(r["r"])
        m, lo, hi, nn, nd = boot_mean(byd)
        print(f"     {name}: y-M {m:+.3f} [{lo:+.3f},{hi:+.3f}] (n={nn}, {nd} days)")

print("\n== secondary: raw signals one at a time (slope of y-M and of 2h drift per unit of signal, 95%)")
for tau in PRIMARY:
    for name in ("temp", "cloud", "rain", "rad"):
        sub = [r for r in rows if r["tau"] == tau and r["sig"][name] is not None]
        b, lo, hi, n, _ = boot_slope(sub, lambda r, nm=name: r["sig"][nm], "r", 0.025, 0.975, 1000)
        sub2 = [r for r in sub if r["move2"] is not None]
        b2, lo2, hi2, n2, _ = boot_slope(sub2, lambda r, nm=name: r["sig"][nm], "move2", 0.025, 0.975, 1000)
        print(f"   {tau}:00 {name:5s} y-M {b:+.3f} [{lo:+.3f},{hi:+.3f}] (n={n})   drift {b2:+.3f} [{lo2:+.3f},{hi2:+.3f}] (n={n2})")

print("\n== secondary: subsets (P1 slope and 2h drift slope, 95%)")
for tau in PRIMARY:
    for name, fn in (("satellite stations", lambda r: r["sig"]["rad"] is not None), ("no satellite", lambda r: r["sig"]["rad"] is None),
                     ("C stations", lambda r: r["unit"] == "C"), ("F stations", lambda r: r["unit"] == "F")):
        sub = [r for r in rows_s if r["tau"] == tau and fn(r)]
        if len(sub) < 60:
            continue
        b, lo, hi, n, _ = boot_slope(sub, "S_ord", "r", 0.025, 0.975, 1000)
        sub2 = [r for r in sub if r["move2"] is not None]
        b2, lo2, hi2, n2, _ = boot_slope(sub2, "S_ord", "move2", 0.025, 0.975, 1000)
        print(f"   {tau}:00 {name:18s} y-M {b:+.3f} [{lo:+.3f},{hi:+.3f}] (n={n})   drift {b2:+.3f} [{lo2:+.3f},{hi2:+.3f}] (n={n2})")

print("\n== secondary: controlling for where the forecast sits against the market (y-M ~ S_ord + (F_ord-M))")
for tau in PRIMARY:
    sub = [r for r in rows_s if r["tau"] == tau]
    X = np.array([[1.0, r["S_ord"], r["F_ord"] - r["M"]] for r in sub])
    yv = np.array([r["r"] for r in sub])
    w, *_ = np.linalg.lstsq(X, yv, rcond=None)
    days = collections.defaultdict(list)
    for i, r in enumerate(sub):
        days[r["ed"]].append(i)
    keys = list(days)
    bs = []
    for _ in range(1000):
        ii = [i for k in (random.choice(keys) for _ in keys) for i in days[k]]
        bs.append(np.linalg.lstsq(X[ii], yv[ii], rcond=None)[0])
    bs = np.array(bs)
    lo, hi = np.percentile(bs, [2.5, 97.5], axis=0)
    print(f"   {tau}:00  S_ord {w[1]:+.3f} [{lo[1]:+.3f},{hi[1]:+.3f}]   forecast-minus-market {w[2]:+.3f} [{lo[2]:+.3f},{hi[2]:+.3f}]  (n={len(sub)})")


def shifted(q, g):
    K = len(q)
    xs = [-0.5] + [k + 0.5 for k in range(K)]
    cs = [0.0]
    for x in q:
        cs.append(cs[-1] + x)

    def F(x):
        if x <= xs[0]:
            return 0.0
        if x >= xs[-1]:
            return 1.0
        return float(np.interp(x, xs, cs))
    p = [F(k + 0.5 - g) - F(k - 0.5 - g) for k in range(K)]
    p[0] += F(-0.5 - g)
    p[-1] += 1.0 - F(K - 0.5 - g)
    return p


print("\n== secondary: trading back-test (walk-forward predicted y-M >= 0.25 buckets; buy the YES with the largest p'-q, one share, hold)")
for tau in PRIMARY:
    for cost in (0.01, 0.02):
        pnl = collections.defaultdict(list)
        spend = collections.defaultdict(list)
        for r in rows_s:
            if r["tau"] != tau:
                continue
            g = wf_pred.get((r["st"], r["ed"], tau))
            if g is None or abs(g) < 0.25:
                continue
            p2 = shifted(r["q"], g)
            k = max(range(len(p2)), key=lambda i: p2[i] - r["q"][i])
            price = r["raw"][k] + cost
            if p2[k] < price + 0.02 or price >= 1:
                continue
            pnl[r["ed"]].append((1.0 if r["y"] == k else 0.0) - price)
            spend[r["ed"]].append(price)
        keys = list(pnl)
        if not keys:
            print(f"   {tau}:00 cost {cost:.2f}: no trades")
            continue
        tot_p = sum(sum(v) for v in pnl.values())
        tot_s = sum(sum(v) for v in spend.values())
        bs = []
        for _ in range(2000):
            ks = [random.choice(keys) for _ in keys]
            sp = sum(sum(spend[k]) for k in ks)
            bs.append(sum(sum(pnl[k]) for k in ks) / sp if sp else 0.0)
        bs.sort()
        print(f"   {tau}:00 cost {cost:.2f}: trades {sum(len(v) for v in pnl.values())} on {len(keys)} days, ROI {100 * tot_p / tot_s:+.1f}%"
              f" [{100 * bs[50]:+.1f}, {100 * bs[1949]:+.1f}]")
