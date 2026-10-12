"""Round 53 (熊先生 10-12 「核心我觉得还是模型的站点校准…也许是对标模型的校准方法问题，
比如用中心校准、logloss等方法」).

The cluster buys the bucket its belief calls most likely.  If its belief is our
six native members under a DIFFERENT station calibration, some calibration
recipe should reproduce its bucket more often than F does and hit the truth at
least as often.  Every recipe below is strictly forward (members as of the
listing instant, truths settled by D-4) and gives one centre (or one most
likely bucket); scored on the 227 pre-formation first YES: agreement with
the pick, truth hits, both halves, paired against production F.
Recipes (per station s, day D, members x_m):
  offset   raw median + c + lambda x corr, corr from per-member bias b_ms by
           estimator E (mean / median / trimmed / exp-weighted half-life h) over
           window W days, >= k days; lambda 0.3..1.5
  pooled   raw median + bias of the median itself (station-level), shrunk
  MOS      per-station regression truth = a + b x raw median (slope shrunk to 1)
  skill    weights 1/MSE_ms over corrected members (mean instead of median);
           best recent member alone
  shape    centre + the station's own empirical residual distribution (or the
           fleet's), P(bucket) -> argmax (a skewed shape can move the most
           likely bucket off the centre's bucket)
  LogLoss  per-station normal (a_s, sigma_s) by LogLoss on the station's own
           unit bins, P(bucket) -> argmax
  hit-max  per-station shift delta maximising historical bucket hits
"""
import collections
import math
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

here = Path(__file__)
src = here.with_name("calibrated_pick_r42.py").read_text()
ns = {"__name__": "r42", "__file__": str(here.with_name("calibrated_pick_r42.py"))}
exec(compile(src.split("def cdf(x):")[0], "r42_head", "exec"), ns)
events, bucket, H, truth, fleet_c, SYSTEMS = (ns[k] for k in ("events", "bucket", "H", "truth", "fleet_c", "SYSTEMS"))
CUT_BACK = 4


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return d, f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


def hist(st, D, window):
    """(day, members, truth) for the station's settled days in [D-window, D-CUT_BACK]."""
    out = []
    for k in range(CUT_BACK, window + 1):
        dd = D - timedelta(days=k)
        if (st, dd) in H and (st, dd) in truth:
            out.append((dd, H[(st, dd)], truth[(st, dd)]))
    return out


def estimate(errs, ages, kind):
    if kind == "mean":
        return statistics.mean(errs)
    if kind == "median":
        return statistics.median(errs)
    if kind == "trimmed":
        s = sorted(errs)
        k = max(0, len(s) // 10)
        return statistics.mean(s[k:len(s) - k] if len(s) - 2 * k >= 1 else s)
    if kind.startswith("ew"):
        h = float(kind[2:])
        w = np.array([0.5 ** (a / h) for a in ages])
        return float(w @ np.array(errs) / w.sum())
    raise ValueError(kind)


def member_bias(st, D, members, *, kind="mean", window=60, min_days=3):
    rows = hist(st, D, window)
    out = {}
    for m in members:
        pairs = [((D - dd).days, t - mem[m]) for dd, mem, t in rows if m in mem]
        if len(pairs) >= min_days:
            out[m] = estimate([e for _a, e in pairs], [a for a, _e in pairs], kind)
        else:
            out[m] = 0.0
    return out


def offset_centre(st, D, members, lam, c, **kw):
    raw = statistics.median(members.values())
    bias = member_bias(st, D, members, **kw)
    corr = statistics.median(members[m] + bias[m] for m in members) - raw
    return raw + c + lam * corr


def corrected(st, D, members, **kw):
    bias = member_bias(st, D, members, **kw)
    return {m: members[m] + bias[m] for m in members}


# ---------------------------------------------------------------- recipes
def recipes(st, D, members, edges, unit_f):
    raw = statistics.median(members.values())
    c = fleet_c(D)
    out = {}
    out["F_base (lambda 0.5, mean 60)"] = raw + c + 0.5 * (statistics.median(corrected(st, D, members).values()) - raw)
    for lam in (0.3, 0.75, 1.0, 1.25, 1.5):
        out[f"offset lambda {lam}"] = offset_centre(st, D, members, lam, c)
    for kind in ("median", "trimmed", "ew10", "ew20"):
        for lam in (0.5, 1.0):
            out[f"offset {kind} lambda {lam}"] = offset_centre(st, D, members, lam, c, kind=kind)
    for window in (14, 30, 90):
        for lam in (0.5, 1.0):
            out[f"offset window {window} lambda {lam}"] = offset_centre(st, D, members, lam, c, window=window)
    out["offset min 7 days lambda 1.0"] = offset_centre(st, D, members, 1.0, c, min_days=7)
    out["raw + full station bias, no c"] = offset_centre(st, D, members, 1.0, 0.0) + 0.0 if False else (
        statistics.median(corrected(st, D, members).values()))
    # pooled: bias of the raw median itself
    rows = hist(st, D, 60)
    pooled = [t - statistics.median(mem.values()) for _d, mem, t in rows]
    if len(pooled) >= 5:
        pb = statistics.mean(pooled)
        out["pooled median bias (kappa 1)"] = raw + pb
        out["pooled median bias (kappa 0.5)"] = raw + c + 0.5 * (pb - c)
        out["pooled median bias, median est"] = raw + statistics.median(pooled)
    # MOS
    if len(rows) >= 15:
        x = np.array([statistics.median(mem.values()) for _d, mem, _t in rows])
        y = np.array([t for _d, _m, t in rows])
        b = float(np.cov(x, y, bias=True)[0, 1] / (x.var() + 1e-9))
        for k_ in (10, 30):
            bs = (len(x) * b + k_ * 1.0) / (len(x) + k_)
            a = y.mean() - bs * x.mean()
            out[f"MOS slope shrink {k_}"] = a + bs * raw
    # skill weights and best member
    corr = corrected(st, D, members)
    mse = {}
    for m in members:
        e = [(t - mem[m] - (corr[m] - members[m])) ** 2 for _d, mem, t in rows if m in mem]
        if len(e) >= 5:
            mse[m] = statistics.mean(e)
    if len(mse) >= 3:
        w = {m: 1.0 / (mse[m] + 0.25) for m in mse}
        out["skill-weighted mean (1/MSE)"] = sum(w[m] * corr[m] for m in w) / sum(w.values())
        best = min(mse, key=mse.get)
        out["best recent member alone"] = corr[best]
    out["mean of corrected members"] = statistics.mean(corr.values())
    # distribution recipes: residuals of the lambda-1 centre over the station's history (as-of each day)
    centres = {}
    resid = []
    for dd, mem, t in rows:
        if len(mem) >= 3:
            cd = offset_centre(st, dd, mem, 1.0, fleet_c(dd))
            resid.append(t - cd)
    today = offset_centre(st, D, members, 1.0, c)
    width = edges[1][1] - edges[1][0]
    unit = 1.0 if not unit_f else 10.0 / 9.0
    if len(resid) >= 15:
        # empirical shape -> most likely bucket on today's ladder
        counts = collections.Counter(bucket(edges, today + r) for r in resid)
        out["shape: station residuals, argmax bucket"] = ("bucket", max(counts, key=lambda j: (counts[j], -abs(j - bucket(edges, today)))))
        # per-station normal by LogLoss on unit bins: a_s, sigma_s maximise sum log P(bin of truth)
        best_ll, best_par = -1e9, None
        for a in np.arange(-1.0, 1.01, 0.1):
            for sg in (0.6, 0.8, 1.0, 1.2, 1.5, 2.0):
                ll = 0.0
                for r in resid:
                    # truth sits at residual r from the centre; its unit bin around r
                    lo = math.floor(r / unit) * unit
                    p = 0.5 * (math.erf((lo + unit - a) / (sg * math.sqrt(2))) - math.erf((lo - a) / (sg * math.sqrt(2))))
                    ll += math.log(max(p, 1e-4))
                if ll > best_ll:
                    best_ll, best_par = ll, (a, sg)
        a, sg = best_par
        probs = []
        for lo, hi in edges:
            lo_ = -1e9 if lo is None else lo
            hi_ = 1e9 if hi is None else hi
            probs.append(0.5 * (math.erf((hi_ - today - a) / (sg * math.sqrt(2))) - math.erf((lo_ - today - a) / (sg * math.sqrt(2)))))
        out["LogLoss normal per station (a, sigma), argmax"] = ("bucket", int(np.argmax(probs)))
        out["LogLoss normal per station, centre only"] = today + a
        # hit-maximising shift on unit bins
        best_hit, best_d = -1, 0.0
        for d_ in np.arange(-1.0, 1.01, 0.1):
            hits = sum(1 for r in resid if math.floor((r - d_) / unit) == 0)
            if hits > best_hit or (hits == best_hit and abs(d_) < abs(best_d)):
                best_hit, best_d = hits, d_
        out["hit-max shift per station"] = today + best_d
    return out


# fleet residual shape (pooled over all stations), built once per event day
_FLEET_RESID = {}


def fleet_resid(D):
    if D not in _FLEET_RESID:
        res = []
        for (st, dd), mem in H.items():
            if D - timedelta(days=45) <= dd <= D - timedelta(days=CUT_BACK) and (st, dd) in truth and len(mem) >= 3:
                res.append(truth[(st, dd)] - offset_centre(st, dd, mem, 1.0, fleet_c(dd)))
        _FLEET_RESID[D] = res
    return _FLEET_RESID[D]


results = collections.defaultdict(list)  # recipe -> list of (ev, bucket)
used = []
for ev in events:
    st, D = ev["st"], date.fromisoformat(ev["ed"])
    members = H.get((st, D))
    if not members or len(members) < 3:
        continue
    used.append(ev)
    rec = recipes(st, D, members, ev["edges"], ev["unit"] == "F")
    today = offset_centre(st, D, members, 1.0, fleet_c(D))
    fr = fleet_resid(D)
    if fr:
        counts = collections.Counter(bucket(ev["edges"], today + r) for r in fr)
        rec["shape: fleet residuals, argmax bucket"] = ("bucket", max(counts, key=lambda j: (counts[j], -abs(j - bucket(ev["edges"], today)))))
    for name, v in rec.items():
        b = v[1] if isinstance(v, tuple) else bucket(ev["edges"], v)
        results[name].append((ev, b))
    results["production F (at the trade)"].append((ev, ev["fb"]))

days = sorted({ev["ed"] for ev in used})
mid = days[len(days) // 2]
base = {id(ev): ev["fb"] == ev["choice"] for ev in used}
print(f"{len(used)} picks with >= 3 listing-time members; cluster hits the truth {statistics.mean(ev['choice'] == ev['truth'] for ev in used if ev['truth'] is not None):.1%}"
      f" on {sum(ev['truth'] is not None for ev in used)} settled; production F = cluster {statistics.mean(base.values()):.1%}")
print(f"{'recipe':48s} {'=cluster':>9s} {'vs F':>24s} {'halves':>13s} {'hits':>6s} {'vs F':>7s}")
table = []
for name, lst in results.items():
    same = [b == ev["choice"] for ev, b in lst]
    bs = [base[id(ev)] for ev, _b in lst]
    h1 = [s for s, (ev, _b) in zip(same, lst) if ev["ed"] < mid]
    h2 = [s for s, (ev, _b) in zip(same, lst) if ev["ed"] >= mid]
    b1 = [s for s, (ev, _b) in zip(bs, lst) if ev["ed"] < mid]
    b2 = [s for s, (ev, _b) in zip(bs, lst) if ev["ed"] >= mid]
    tr = [(ev, b) for ev, b in lst if ev["truth"] is not None]
    hits = statistics.mean(b == ev["truth"] for ev, b in tr)
    fh = statistics.mean(ev["fb"] == ev["truth"] for ev, _b in tr)
    d, ci = paired(same, bs)
    table.append((statistics.mean(same), name, ci, statistics.mean(h1) - statistics.mean(b1), statistics.mean(h2) - statistics.mean(b2), hits, hits - fh, len(lst)))
for agree, name, ci, dh1, dh2, hits, dhit, n in sorted(table, reverse=True):
    both = "**" if dh1 > 0 and dh2 > 0 else "  "
    print(f"{name:48s} {agree:9.1%} {ci:>24s} {dh1:+.3f}/{dh2:+.3f}{both} {hits:6.1%} {dhit:+.3f} (n {n})")
