"""Round 33 part B -- why the cluster's first YES (placed before the market
formed) so often sits one bucket ABOVE the bias-corrected multi-model forecast
bucket F.  Protocol committed in README (0d45cf32) before scoring.
Inputs (bench): first_rows.json.gz, first_runs.json.  Research <= 10-03, forward >= 10-04.
"""
import collections
import gzip
import json
import math
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

B = Path(sys.argv[1])
R = json.load(gzip.open(B / "first_rows.json.gz", "rt"))
RUNS = {k: v for k, v in json.load(open(B / "first_runs.json")).items() if "error" not in v}
SPLIT = "2026-10-03"
LAG = {"gfs_global": 4.0, "icon_global": 3.5, "ecmwf_ifs025": 7.0, "gem_global": 4.5, "ukmo_global_deterministic_10km": 5.5,
       "ecmwf_aifs025_single": 6.0, "meteofrance_arpege_world": 4.0, "jma_gsm": 4.0, "cma_grapes_global": 7.0, "ncep_aigfs025": 5.0}
SH = {"gfs_global": "GFS", "icon_global": "ICON", "ecmwf_ifs025": "IFS", "gem_global": "GEM", "ukmo_global_deterministic_10km": "UKMO",
      "ecmwf_aifs025_single": "AIFS", "meteofrance_arpege_world": "ARPEGE", "jma_gsm": "JMA", "cma_grapes_global": "CMA", "ncep_aigfs025": "AIGFS"}


def formed(r):
    v = list(r["now"].values())
    return all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4


def edges(r):
    w = r["width"]
    out = []
    for lab in r["labels"]:
        lo, hi = r["bounds"][lab]
        out.append([lo, hi])
    out[0][0] = out[0][1] - w
    out[-1][1] = out[-1][0] + w
    return out


def bucket_i(r, x):
    e = edges(r)
    if x < e[0][1]:
        return 0
    for i, (lo, hi) in enumerate(e):
        if lo <= x < hi:
            return i
    return len(e) - 1


def frac_in(r, x):
    e = edges(r)
    i = bucket_i(r, x)
    lo, hi = e[i]
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


def fc(r, m, at):
    ed = datetime.fromisoformat(r["ed"]).replace(tzinfo=timezone.utc)
    d2 = ed - timedelta(days=2)
    best = None
    for run in (d2 - timedelta(hours=6), d2, d2 + timedelta(hours=6), d2 + timedelta(hours=12)):
        v = RUNS.get(f"{r['st']}|{run.strftime('%Y-%m-%dT%H:%M')}", {}).get("tmax", {}).get(m, {}).get(r["ed"])
        if v is not None and run.timestamp() + LAG[m] * 3600 <= at and (best is None or run > best[0]):
            best = (run, v)
    return None if best is None else best[1]


# forecast in hand at each station-day's first fill; bias history from all station-days (truth - forecast), as of d-2
FC = {}
for r in R:
    for m in LAG:
        FC[(r["st"], r["ed"], m)] = fc(r, m, r["ts"])
TRUTH = {(r["st"], r["ed"]): r["truth_c"] for r in R if r["truth_c"] is not None}


def bias(st, ed, m):
    cut = (date.fromisoformat(ed) - timedelta(days=2)).isoformat()
    v = [TRUTH[(s, d)] - x for (s, d, mm), x in FC.items() if s == st and mm == m and d <= cut and x is not None and (s, d) in TRUTH]
    return statistics.mean(v) if len(v) >= 3 else 0.0


cases = []
for r in R:
    if formed(r) or r["first"]["side"] != "Yes":
        continue
    raw = {m: FC[(r["st"], r["ed"], m)] for m in LAG if FC[(r["st"], r["ed"], m)] is not None}
    if len(raw) < 3:
        continue
    cor = {m: v + bias(r["st"], r["ed"], m) for m, v in raw.items()}
    med_c = statistics.median(cor.values())
    med_r = statistics.median(raw.values())
    F = bucket_i(r, med_c)
    ch = r["labels"].index(r["first"]["bucket"])
    srt = sorted(cor.values())
    p75 = srt[int(round(0.75 * (len(srt) - 1)))]
    sv = r["served"]["p"] if r.get("served") and r["served"].get("p") else None
    our_mode = r["labels"].index(max(sv, key=lambda b: sv[b])) if sv and max(sv, key=lambda b: sv[b]) in r["labels"] else None
    cases.append({
        "r": r, "set": "research" if r["ed"] <= SPLIT else "forward", "st": r["st"], "ed": r["ed"], "unit": r["unit"],
        "k": ch - F, "F": F, "ch": ch, "med_c": med_c, "med_r": med_r, "frac": frac_in(r, med_c),
        "corr": med_c - med_r, "raw_off": bucket_i(r, med_r) - F, "spread": statistics.pstdev(cor.values()),
        "n_up": sum(1 for v in cor.values() if bucket_i(r, v) > F), "n_dn": sum(1 for v in cor.values() if bucket_i(r, v) < F),
        "warm_off": bucket_i(r, max(cor.values())) - F, "p75_off": bucket_i(r, p75) - F,
        "raw_i": {m: bucket_i(r, v) for m, v in raw.items()}, "cor_i": {m: bucket_i(r, v) for m, v in cor.items()},
        "our_off": None if our_mode is None else our_mode - F, "M": r.get("M"),
        "truth_off": None if r["truth"] is None or r["truth"] not in r["labels"] else r["labels"].index(r["truth"]) - F,
        "week": date.fromisoformat(r["ed"]).isocalendar()[1], "hour": datetime.fromtimestamp(r["listed"], timezone.utc).hour,
        "price": r["first"]["price"], "width": r["width"], "cor": cor, "raw": raw,
    })
res = [c for c in cases if c["set"] == "research"]
fwd = [c for c in cases if c["set"] == "forward"]
print(f"early first YES with >=3 models: research {len(res)}, forward {len(fwd)}")
print("offset k = chosen - F:  research " + " ".join(f"{k:+d}:{sum(c['k'] == k for c in res)}" for k in range(-2, 4)) +
      "   forward " + " ".join(f"{k:+d}:{sum(c['k'] == k for c in fwd)}" for k in range(-2, 4)))

# ---------------------------------------------------------------- H1 calibration constant
print("\n== H1 a constant added to the forecast: exact hit of bucket(median + c) (research picks c; forward reads it)")
for name, key in (("corrected median", "med_c"), ("raw median (no station bias)", "med_r")):
    best = None
    grid = [round(x * 0.1, 1) for x in range(-15, 26)]
    for c in grid:
        h = sum(bucket_i(x["r"], x[key] + c) == x["ch"] for x in res) / len(res)
        if best is None or h > best[0]:
            best = (h, c)
    h0r = sum(bucket_i(x["r"], x[key]) == x["ch"] for x in res) / len(res)
    h0f = sum(bucket_i(x["r"], x[key]) == x["ch"] for x in fwd) / len(fwd)
    hf = sum(bucket_i(x["r"], x[key] + best[1]) == x["ch"] for x in fwd) / len(fwd)
    curve = "  ".join(f"{c:+.1f}:{sum(bucket_i(x['r'], x[key] + c) == x['ch'] for x in res) / len(res):.2f}" for c in (-0.5, 0.0, 0.3, 0.5, 0.7, 0.9, 1.1, 1.5))
    print(f"   {name:30s} c=0: {h0r:.3f} | {h0f:.3f}   best c={best[1]:+.1f}: {best[0]:.3f} | {hf:.3f}    research curve {curve}")
print("   P(k >= +1) by where the corrected median sits inside its bucket (a constant shift must make this rise):")
for S, nm in ((res, "research"), (fwd, "forward")):
    cells = []
    for lo, hi in ((0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)):
        sel = [c for c in S if lo <= c["frac"] < hi]
        cells.append(f"{lo:.2f}-{min(hi, 1):.2f}: {sum(c['k'] >= 1 for c in sel) / len(sel):.2f} (n={len(sel)})" if sel else "-")
    print(f"     {nm:8s} " + "   ".join(cells))

# ---------------------------------------------------------------- H2 season
print("\n== H2 by event week (ISO): n, P(k>=+1), P(k=0), mean k")
for wk in sorted({c["week"] for c in cases}):
    sel = [c for c in cases if c["week"] == wk]
    print(f"   week {wk} ({min(c['ed'] for c in sel)}..{max(c['ed'] for c in sel)}): n={len(sel):3d}  P(k>=1) {sum(c['k'] >= 1 for c in sel) / len(sel):.2f}"
          f"  P(k=0) {sum(c['k'] == 0 for c in sel) / len(sel):.2f}  mean k {statistics.mean(c['k'] for c in sel):+.2f}")

# ---------------------------------------------------------------- H3 station
print("\n== H3 station: P(k>=+1) research vs forward, per station with >=3 cases in both")
pairs = []
for st in sorted({c["st"] for c in cases}):
    a = [c for c in res if c["st"] == st]
    b = [c for c in fwd if c["st"] == st]
    if len(a) >= 3 and len(b) >= 3:
        pairs.append((st, sum(c["k"] >= 1 for c in a) / len(a), sum(c["k"] >= 1 for c in b) / len(b), len(a), len(b)))
if len(pairs) >= 4:
    ra = np.array([p[1] for p in pairs]); fa = np.array([p[2] for p in pairs])
    print(f"   stations {len(pairs)}; correlation of P(k>=1) research vs forward {np.corrcoef(ra, fa)[0, 1]:+.2f}")
    print("   " + "  ".join(f"{st} {a:.2f}->{b:.2f}" for st, a, b, na, nb in pairs))
# station constant c_s fitted on research, applied forward
hits_s = hits_g = n = 0
cg = None
grid = [round(x * 0.1, 1) for x in range(-15, 26)]
best = max(grid, key=lambda c: sum(bucket_i(x["r"], x["med_c"] + c) == x["ch"] for x in res))
for st in {c["st"] for c in fwd}:
    a = [c for c in res if c["st"] == st]
    cs = max(grid, key=lambda c: (sum(bucket_i(x["r"], x["med_c"] + c) == x["ch"] for x in a), -abs(c - best))) if len(a) >= 3 else best
    for x in (c for c in fwd if c["st"] == st):
        n += 1
        hits_s += bucket_i(x["r"], x["med_c"] + cs) == x["ch"]
        hits_g += bucket_i(x["r"], x["med_c"] + best) == x["ch"]
print(f"   forward exact: per-station c_s {hits_s / n:.3f}  vs fleet c={best:+.1f} {hits_g / n:.3f}  (n={n})")

# ---------------------------------------------------------------- H4 unit
print("\n== H4 unit: offset distribution")
for u in ("C", "F"):
    sel = [c for c in cases if c["unit"] == u]
    if sel:
        print(f"   {u}: n={len(sel)}  " + " ".join(f"{k:+d}:{100 * sum(c['k'] == k for c in sel) / len(sel):.0f}%" for k in range(-2, 4)))

# ---------------------------------------------------------------- H5 which forecast it took
print("\n== H5 the chosen bucket is the bucket of ... (research | forward)")
cands = {"corrected median": lambda c: c["F"], "corrected 75th pct": lambda c: c["F"] + c["p75_off"], "warmest corrected model": lambda c: c["F"] + c["warm_off"],
         "raw median": lambda c: c["F"] + c["raw_off"], "our served mode": lambda c: None if c["our_off"] is None else c["F"] + c["our_off"]}
for m in LAG:
    cands[f"{SH[m]} raw"] = (lambda c, m=m: c["raw_i"].get(m))
    cands[f"{SH[m]} corrected"] = (lambda c, m=m: c["cor_i"].get(m))
for nm, f in cands.items():
    cells = []
    for S in (res, fwd):
        sel = [(f(c), c["ch"]) for c in S if f(c) is not None]
        cells.append(f"{sum(a == b for a, b in sel) / len(sel):.3f} ({len(sel)})" if sel else "-")
    print(f"   {nm:26s} {cells[0]} | {cells[1]}")

# ---------------------------------------------------------------- H6 was it right
print("\n== H6 where the truth landed, relative to F, by the cluster's offset (settled)")
for k in (0, 1, 2, -1):
    sel = [c for c in cases if c["k"] == k and c["truth_off"] is not None]
    if not sel:
        continue
    t = collections.Counter(max(-2, min(3, c["truth_off"])) for c in sel)
    print(f"   cluster k={k:+d} (n={len(sel)}): truth at " + " ".join(f"{j:+d}:{100 * t[j] / len(sel):.0f}%" for j in range(-2, 4)) +
          f"   cluster right {sum(c['truth_off'] == k for c in sel) / len(sel):.2f}  F right {sum(c['truth_off'] == 0 for c in sel) / len(sel):.2f}")

# ---------------------------------------------------------------- the deviating cases' features
print("\n== features by offset group (all early first YES)")
FEATS = [("pos of corrected median in its bucket", "frac"), ("station-bias correction (C)", "corr"), ("model spread (C)", "spread"),
         ("models in F+1 or above", "n_up"), ("models in F-1 or below", "n_dn"), ("warmest model offset", "warm_off"),
         ("75th pct offset", "p75_off"), ("raw median bucket offset", "raw_off"), ("our served mode offset", "our_off"),
         ("momentum M", "M"), ("listing hour UTC", "hour"), ("price paid", "price"), ("F unit share", None)]
groups = [("k=-1", lambda c: c["k"] == -1), ("k=0", lambda c: c["k"] == 0), ("k=+1", lambda c: c["k"] == 1), ("k=+2+", lambda c: c["k"] >= 2)]
print("   " + " " * 40 + "".join(f"{g:>12s}" for g, _ in groups))
for nm, key in FEATS:
    vals = []
    for g, f in groups:
        sel = [c for c in cases if f(c)]
        if key is None:
            v = sum(c["unit"] == "F" for c in sel) / len(sel) if sel else float("nan")
        else:
            xs = [c[key] for c in sel if c[key] is not None]
            v = statistics.mean(xs) if xs else float("nan")
        vals.append(v)
    print(f"   {nm:40s}" + "".join(f"{v:12.2f}" for v in vals))
print("   n" + " " * 39 + "".join(f"{sum(1 for c in cases if f(c)):12d}" for g, f in groups))

# logistic k>=+1 vs k=0, research -> forward
XK = ["frac", "corr", "spread", "n_up", "n_dn", "warm_off", "raw_off"]


def xv(c):
    return [1.0] + [float(c[k]) for k in XK] + [1.0 if c["unit"] == "F" else 0.0]


tr = [c for c in res if c["k"] >= 0]
te = [c for c in fwd if c["k"] >= 0]
X = np.array([xv(c) for c in tr]); y = np.array([1.0 if c["k"] >= 1 else 0.0 for c in tr])
mu, sd = X[:, 1:].mean(0), X[:, 1:].std(0) + 1e-9


def z(A):
    A = A.copy()
    A[:, 1:] = (A[:, 1:] - mu) / sd
    return A


def nll(w):
    s = z(X) @ w
    return float(np.sum(np.logaddexp(0, s) - y * s)) + 0.5 * float(w[1:] @ w[1:])


w = minimize(nll, np.zeros(X.shape[1]), method="L-BFGS-B").x
Xt = z(np.array([xv(c) for c in te])); yt = np.array([1.0 if c["k"] >= 1 else 0.0 for c in te])
pt = 1 / (1 + np.exp(-(Xt @ w)))
acc = float(np.mean((pt >= 0.5) == (yt == 1)))
base = max(yt.mean(), 1 - yt.mean())
order = np.argsort(pt)
ranks = np.empty(len(pt)); ranks[order] = np.arange(1, len(pt) + 1)
npos = yt.sum(); nneg = len(yt) - npos
auc = (ranks[yt == 1].sum() - npos * (npos + 1) / 2) / (npos * nneg) if npos and nneg else float("nan")
print(f"\n== logistic k>=+1 vs k=0 (research n={len(tr)} -> forward n={len(te)}): forward accuracy {acc:.3f} (base {base:.3f}), AUC {auc:.3f}")
print("   standardized weights: " + "  ".join(f"{k} {wi:+.2f}" for k, wi in zip(XK + ["F unit"], w[1:])))

# ---------------------------------------------------------------- the cluster's correction: fleet constant + part of the station bias?
print("\n== the cluster's implied correction: bucket(raw median + c + lam x our station correction); research picks (c, lam), forward reads")
best = None
for c in [round(x * 0.1, 1) for x in range(-5, 16)]:
    for lam in [round(x * 0.1, 1) for x in range(0, 11)]:
        h = sum(bucket_i(x["r"], x["med_r"] + c + lam * x["corr"]) == x["ch"] for x in res) / len(res)
        if best is None or h > best[0]:
            best = (h, c, lam)
h, c, lam = best
hf = sum(bucket_i(x["r"], x["med_r"] + c + lam * x["corr"]) == x["ch"] for x in fwd) / len(fwd)
print(f"   best c={c:+.1f}, lam={lam:.1f}: research {h:.3f}  forward {hf:.3f}")
for c2, lam2 in ((0.8, 0.0), (0.0, 1.0), (0.4, 1.0), (0.5, 0.5)):
    a = sum(bucket_i(x["r"], x["med_r"] + c2 + lam2 * x["corr"]) == x["ch"] for x in res) / len(res)
    b = sum(bucket_i(x["r"], x["med_r"] + c2 + lam2 * x["corr"]) == x["ch"] for x in fwd) / len(fwd)
    print(f"   c={c2:+.1f}, lam={lam2:.1f}: research {a:.3f}  forward {b:.3f}")
# the fleet's standing bias of the raw median (truth - raw median) on the same station-days, as a reference for c
tb = [x["r"]["truth_c"] - x["med_r"] for x in cases if x["r"]["truth_c"] is not None]
print(f"   fleet mean (truth - raw median) on these station-days: {statistics.mean(tb):+.2f} C (n={len(tb)}); our station corrections mean {statistics.mean(x['corr'] for x in cases):+.2f} C")
