"""Round 32 scorer: what decides the cluster's FIRST leg on a station-day
(first_rows.json.gz from first_trade_build.py).  Research = event days up to
10-03, forward = from 10-04; every rule is set on research and read forward.

(1) centres: how often the first YES sits in the bucket of each centre known at
    the fill -- market mode / market mean, our served mode, each raw native
    ensemble's mean (raw, and with the station x system bias from the station's
    other research days), the raw consensus; how often the first NO sits on each.
(2) edge: the chosen leg's rank among all priced legs by (centre probability -
    price), per probability source.
(3) direction of off-mode first legs vs each centre's side of the market mean.
(4) the deterministic rule: gate + integer points (conditional logit on research,
    rounded) + the forward top-1 / top-3, and an ordered list.
"""
import collections
import gzip
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

B = Path(sys.argv[1])
R = json.load(gzip.open(B / "first_rows.json.gz", "rt"))
SPLIT = "2026-10-03"
SYS = ("ifs", "aifs", "icon", "gefs", "gem", "aigefs")


def bucket_of(r, x):
    for lab in r["labels"]:
        lo, hi = r["bounds"][lab]
        if (lo is None or x >= lo) and (hi is None or x < hi):
            return lab
    return None


rows = []
for r in R:
    if not all(v is not None for v in r["now"].values()):
        continue
    s = sum(r["now"].values())
    if not 0.7 <= s <= 1.4:
        continue
    q = {k: v / s for k, v in r["now"].items()}
    labs = r["labels"]
    mo = max(labs, key=lambda b: q[b])
    mean_ord = sum(labs.index(b) * q[b] for b in labs)
    r.update(q=q, mode=mo, mode_i=labs.index(mo), mean_ord=mean_ord, set="research" if r["ed"] <= SPLIT else "forward")
    rows.append(r)
res = [r for r in rows if r["set"] == "research"]
fwd = [r for r in rows if r["set"] == "forward"]
print(f"first legs with a full ladder before the fill: research {len(res)} (event days to {SPLIT}), forward {len(fwd)}")

# station x system bias from the station's research days (truth - raw mean)
bias = collections.defaultdict(list)
for r in res:
    if r["truth_c"] is None:
        continue
    for k, v in r["raw"].items():
        bias[(r["st"], k)].append((r["ed"], r["truth_c"] - v["mean"]))


def b_of(r, k):
    vals = [b for d, b in bias.get((r["st"], k), []) if d != r["ed"]]
    return statistics.mean(vals) if len(vals) >= 3 else 0.0


for r in rows:
    r["centre"] = {"market mode": r["mode"], "market mean": r["labels"][min(len(r["labels"]) - 1, max(0, round(r["mean_ord"])))]}
    sv = r["served"]["p"] if r.get("served") and r["served"].get("p") else None
    if sv:
        r["centre"]["our served mode"] = max(sv, key=lambda b: sv[b])
    ms, mb = [], []
    for k in SYS:
        if k in r["raw"]:
            m = r["raw"][k]["mean"]
            r["centre"][f"{k} raw mean"] = bucket_of(r, m)
            r["centre"][f"{k} mean+bias"] = bucket_of(r, m + b_of(r, k))
            ms.append(m)
            mb.append(m + b_of(r, k))
    if len(ms) >= 2:
        r["centre"]["raw consensus"] = bucket_of(r, statistics.mean(ms))
        r["centre"]["consensus+bias"] = bucket_of(r, statistics.mean(mb))

print("\n== (1) the first leg sits in the bucket of ... (research | forward; n = legs where the centre exists)")
names = sorted({k for r in rows for k in r["centre"]}, key=lambda k: (k.split()[0] not in ("market", "our"), k))
for side in ("Yes", "No"):
    print(f"   first {side.upper()}:")
    for nm in names:
        cells = []
        for S in (res, fwd):
            sel = [r for r in S if r["first"]["side"] == side and r["centre"].get(nm)]
            cells.append(f"{sum(r['first']['bucket'] == r['centre'][nm] for r in sel) / len(sel):.3f} ({len(sel)})" if sel else "-")
        print(f"     {nm:22s} {cells[0]} | {cells[1]}")


def probs(r, src):
    if src == "market":
        return r["q"]
    if src == "our served":
        return r["served"]["p"] if r.get("served") and r["served"].get("p") else None
    if src == "raw pool":
        ps = [r["raw"][k]["probs"] for k in SYS if k in r["raw"]]
        if len(ps) < 2:
            return None
        return {b: sum(p.get(b, 0.0) for p in ps) / len(ps) for b in r["labels"]}
    if src in r["raw"]:
        return r["raw"][src]["probs"]
    return None


def legs(r):
    out = []
    for b in r["labels"]:
        y = r["now"][b]
        for side, price in (("Yes", y), ("No", 1 - y)):
            if 0.0 < price < 1.0:
                out.append((b, side, price))
    return out


print("\n== (2) the chosen leg's rank among all legs by (source probability - price): top-1 / top-3 (research | forward)")
for src in ("market", "our served", "raw pool") + SYS:
    cells = []
    for S in (res, fwd):
        t1 = t3 = n = 0
        for r in S:
            p = probs(r, src)
            if not p:
                continue
            sc = sorted(((p.get(b, 0.0) if side == "Yes" else 1 - p.get(b, 0.0)) - price, b, side) for b, side, price in legs(r))[::-1]
            ranks = [i for i, (_, b, side) in enumerate(sc) if b == r["first"]["bucket"] and side == r["first"]["side"]]
            if not ranks:
                continue
            n += 1
            t1 += ranks[0] == 0
            t3 += ranks[0] < 3
        cells.append(f"{t1 / n:.3f} / {t3 / n:.3f} (n={n})" if n else "-")
    print(f"   {src:12s} {cells[0]} | {cells[1]}")

print("\n== (3) off-mode first legs: its side of the market mode vs each centre's side of the market mean (agreement, research | forward)")


def leg_dir(r):
    d = r["labels"].index(r["first"]["bucket"]) - r["mode_i"]
    if d == 0:
        return 0
    return (1 if d > 0 else -1) * (1 if r["first"]["side"] == "Yes" else -1)


def centre_dir(r, k):
    if k == "our served":
        p = probs(r, "our served")
        if not p:
            return None
        m = sum(r["labels"].index(b) * v for b, v in p.items() if b in r["labels"])
    elif k == "M":
        return None if r.get("M") is None or abs(r["M"]) < 1e-9 else (1 if r["M"] > 0 else -1)
    else:
        if k not in r["raw"]:
            return None
        lab = bucket_of(r, r["raw"][k]["mean"] + b_of(r, k))
        if lab is None:
            return None
        m = r["labels"].index(lab)
    return 0 if abs(m - r["mean_ord"]) < 0.25 else (1 if m > r["mean_ord"] else -1)


for k in ("our served", "M") + SYS:
    cells = []
    for S in (res, fwd):
        sel = [(leg_dir(r), centre_dir(r, k)) for r in S if leg_dir(r) != 0]
        sel = [(a, b) for a, b in sel if b not in (None, 0)]
        cells.append(f"{sum(a == b for a, b in sel) / len(sel):.3f} (n={len(sel)})" if sel else "-")
    print(f"   {k:12s} {cells[0]} | {cells[1]}")

# ------------------------------------------------------------------ (4) deterministic rule
COND = [
    ("YES on the mode", lambda r, c: c["side"] == "Yes" and c["d"] == 0),
    ("YES one below", lambda r, c: c["side"] == "Yes" and c["d"] == -1),
    ("YES one above", lambda r, c: c["side"] == "Yes" and c["d"] == 1),
    ("YES two away", lambda r, c: c["side"] == "Yes" and abs(c["d"]) == 2),
    ("NO on the mode", lambda r, c: c["side"] == "No" and c["d"] == 0),
    ("NO one below", lambda r, c: c["side"] == "No" and c["d"] == -1),
    ("NO one above", lambda r, c: c["side"] == "No" and c["d"] == 1),
    ("NO two away", lambda r, c: c["side"] == "No" and abs(c["d"]) == 2),
    ("YES priced 0.10-0.50", lambda r, c: c["side"] == "Yes" and 0.10 <= c["price"] < 0.50),
    ("NO priced 0.30-0.75", lambda r, c: c["side"] == "No" and 0.30 <= c["price"] < 0.75),
    ("the market's second bucket (YES)", lambda r, c: c["side"] == "Yes" and c["second"]),
    ("leg got >1c cheaper in the hour", lambda r, c: c["mom"] is not None and c["mom"] < -0.01),
    ("leg got >1c dearer in the hour", lambda r, c: c["mom"] is not None and c["mom"] > 0.01),
    ("raw pool says >5c cheap", lambda r, c: c["edge_raw"] is not None and c["edge_raw"] > 0.05),
    ("our belief says >5c cheap", lambda r, c: c["edge_our"] is not None and c["edge_our"] > 0.05),
    ("leg on the momentum side", lambda r, c: c["mdir"] != 0 and r.get("M") is not None and abs(r["M"]) >= 1 and (c["mdir"] > 0) == (r["M"] > 0)),
    ("leg against the momentum side", lambda r, c: c["mdir"] != 0 and r.get("M") is not None and abs(r["M"]) >= 1 and (c["mdir"] > 0) != (r["M"] > 0)),
    ("consensus+bias bucket (YES)", lambda r, c: c["side"] == "Yes" and r["centre"].get("consensus+bias") == c["b"]),
]


def cands(r):
    labs = r["labels"]
    order = sorted(labs, key=lambda b: -r["q"][b])
    second = order[1] if len(order) > 1 else None
    pr = probs(r, "raw pool")
    po = probs(r, "our served")
    out = []
    for b, side, price in legs(r):
        i = labs.index(b)
        d = max(-3, min(3, i - r["mode_i"]))
        ago = r["ago"].get(b)
        mom = None if ago is None else (r["now"][b] - ago) * (1 if side == "Yes" else -1)
        pv = (lambda p: (p.get(b, 0.0) if side == "Yes" else 1 - p.get(b, 0.0)) - price)
        mdir = 0 if d == 0 else (1 if d > 0 else -1) * (1 if side == "Yes" else -1)
        out.append({"b": b, "side": side, "price": price, "d": d, "second": b == second, "mom": mom,
                    "edge_raw": pv(pr) if pr else None, "edge_our": pv(po) if po else None, "mdir": mdir,
                    "chosen": b == r["first"]["bucket"] and side == r["first"]["side"]})
    return out


def gate(c):
    return abs(c["d"]) <= 2 and ((c["side"] == "Yes" and 0.03 <= c["price"] <= 0.65) or (c["side"] == "No" and 0.25 <= c["price"] <= 0.90))


print("\n== (4) the deterministic rule for the first leg")
EV = {"research": [], "forward": []}
for r in rows:
    cs = cands(r)
    if sum(c["chosen"] for c in cs) == 1:
        EV[r["set"]].append((r, cs))
for nm in ("research", "forward"):
    S = EV[nm]
    cov = sum(1 for r, cs in S if any(c["chosen"] and gate(c) for c in cs)) / len(S)
    left = statistics.mean(sum(1 for c in cs if gate(c)) for r, cs in S)
    print(f"   gate (mode +-2, YES 0.03-0.65, NO 0.25-0.90): covers {cov:.3f} of {nm} first legs, {left:.1f} legs left (of {statistics.mean(len(cs) for r, cs in S):.1f})")


def X(r, cs):
    return np.array([[1.0 if f(r, c) else 0.0 for _, f in COND] for c in cs])


train = []
for r, cs in EV["research"]:
    inside = [c for c in cs if gate(c)]
    k = [i for i, c in enumerate(inside) if c["chosen"]]
    if k:
        train.append((X(r, inside), k[0]))


def nll(w):
    tot, g = 0.0, np.zeros_like(w)
    for x, k in train:
        z = x @ w
        p = np.exp(z - z.max())
        p /= p.sum()
        tot -= math.log(max(p[k], 1e-300))
        g -= x[k] - p @ x
    return tot + 0.05 * float(w @ w), g + 0.1 * w


w = minimize(nll, np.zeros(len(COND)), jac=True, method="L-BFGS-B").x
pts = np.round(w * 4.0 / max(abs(w))).astype(int)
for (nm, _), wi, pi in sorted(zip(COND, w, pts), key=lambda t: -t[2]):
    print(f"   {pi:+d}  {nm:34s} (logit {wi:+.2f})")


def topk(S, weights):
    t1 = t3 = 0
    for r, cs in S:
        inside = [c for c in cs if gate(c)]
        if not inside:
            continue
        s = X(r, inside) @ weights
        order = sorted(range(len(inside)), key=lambda i: (-s[i], 0 if inside[i]["side"] == "Yes" else 1, abs(inside[i]["d"]), -inside[i]["price"]))
        t1 += inside[order[0]]["chosen"]
        t3 += any(inside[i]["chosen"] for i in order[:3])
    return t1 / len(S), t3 / len(S)


uni = [statistics.mean(1 / max(1, sum(1 for c in cs if gate(c))) for r, cs in EV[nm]) for nm in ("research", "forward")]
a = topk(EV["research"], w), topk(EV["forward"], w)
b = topk(EV["research"], pts.astype(float)), topk(EV["forward"], pts.astype(float))
print(f"\n   uniform inside the gate top-1 {uni[0]:.3f} | {uni[1]:.3f}")
print(f"   logit weights   top-1 {a[0][0]:.3f} | {a[1][0]:.3f}   top-3 {a[0][1]:.3f} | {a[1][1]:.3f}")
print(f"   integer points  top-1 {b[0][0]:.3f} | {b[1][0]:.3f}   top-3 {b[0][1]:.3f} | {b[1][1]:.3f}")

# what each first-leg type earned (settled), per share at its price
print("\n== first-leg result per share at its own price (settled), research | forward")
for typ in ("mode YES", "off YES", "mode NO", "off NO"):
    cells = []
    for S in (res, fwd):
        sel = []
        for r in S:
            if r["truth"] is None:
                continue
            on = r["first"]["bucket"] == r["mode"]
            t = ("mode " if on else "off ") + r["first"]["side"].upper()
            if t != typ:
                continue
            win = (r["truth"] == r["first"]["bucket"]) == (r["first"]["side"] == "Yes")
            sel.append((1.0 if win else 0.0) - r["first"]["price"])
        cells.append(f"n={len(sel)} {100 * statistics.mean(sel):+.1f} c" if sel else "-")
    print(f"   {typ:9s} {cells[0]} | {cells[1]}")

# ------------------------------------------------------------------ (5) first legs placed before the market formed
# A bucket with no book shows a 0.5 mid; a ladder whose mids sum far above 1 (or has no prior price) had not formed.
print("\n== (5) first legs placed before the market formed (no prior price, or ladder mids summing outside 0.7-1.4)")
early = []
for r in R:
    vals = list(r["now"].values())
    formed = all(v is not None for v in vals) and 0.7 <= sum(vals) <= 1.4
    if not formed:
        early.append(r)
for r in early:
    r["set"] = "research" if r["ed"] <= SPLIT else "forward"
    if "centre" not in r:
        r["centre"] = {}
        ms, mb = [], []
        for k in SYS:
            if k in r["raw"]:
                m = r["raw"][k]["mean"]
                r["centre"][f"{k} mean+bias"] = bucket_of(r, m + b_of(r, k))
                ms.append(m)
                mb.append(m + b_of(r, k))
        if len(mb) >= 2:
            r["centre"]["consensus+bias"] = bucket_of(r, statistics.mean(mb))
        sv = r["served"]["p"] if r.get("served") and r["served"].get("p") else None
        if sv:
            r["centre"]["our served mode"] = max(sv, key=lambda b: sv[b])
age = sorted((r["ts"] - r["listed"]) / 60 for r in early)
print(f"   n={len(early)}; minutes after listing median {age[len(age) // 2]:.1f}, p75 {age[3 * len(age) // 4]:.1f}; YES share"
      f" {sum(r['first']['side'] == 'Yes' for r in early) / len(early):.2f}; taker share {sum(r['first']['taker'] for r in early) / len(early):.2f}")
pos = collections.Counter()
for r in early:
    n = len(r["labels"])
    pos[(r["first"]["side"], r["labels"].index(r["first"]["bucket"]) - (n - 1) // 2)] += 1
tot = sum(pos.values())
for side in ("Yes", "No"):
    print(f"   {side.upper()} by position on the ladder (0 = middle of {len(early[0]['labels'])}): " +
          "  ".join(f"{k:+d}: {100 * pos[(side, k)] / tot:.1f}%" for k in range(-5, 6) if pos[(side, k)]))
print("   first YES sits in the bucket of ... (research | forward):")
for nm in ["our served mode", "consensus+bias"] + [f"{k} mean+bias" for k in SYS]:
    cells = []
    for s in ("research", "forward"):
        sel = [r for r in early if r["set"] == s and r["first"]["side"] == "Yes" and r["centre"].get(nm)]
        cells.append(f"{sum(r['first']['bucket'] == r['centre'][nm] for r in sel) / len(sel):.3f} ({len(sel)})" if sel else "-")
    print(f"     {nm:22s} {cells[0]} | {cells[1]}")

# where the market settled its mode later: the event-day 06:00-local mode from round 30's paths (station-days to 10-10)
R30 = Path("/private/tmp/claude-501/-Users-xiong-weather/8e8e0e0f-2636-45a0-a0af-fe34e79a733b/scratchpad/r30")
P30 = json.load(open(R30 / "paths.json"))


def later_mode(r, hour=6):
    idx = (hour - 4) * 6
    vals = {}
    for lab in r["labels"]:
        v = P30.get(f"{r['st']}|{r['ed']}|{lab}")
        if not v or "g" not in v or v["g"][idx] is None:
            return None
        vals[lab] = v["g"][idx]
    s = sum(vals.values())
    if not 0.7 <= s <= 1.4:
        return None
    return max(vals, key=lambda b: vals[b])


print("   vs the market's mode on the event day at 06:00 local (formed by then):")
for grp, S in (("before the market formed", early), ("after it formed", rows)):
    yes = [(r, later_mode(r)) for r in S if r["first"]["side"] == "Yes"]
    yes = [(r, m) for r, m in yes if m]
    no = [(r, later_mode(r)) for r in S if r["first"]["side"] == "No"]
    no = [(r, m) for r, m in no if m]
    dy = collections.Counter(max(-2, min(2, r["labels"].index(r["first"]["bucket"]) - r["labels"].index(m))) for r, m in yes)
    dn = collections.Counter(max(-2, min(2, r["labels"].index(r["first"]["bucket"]) - r["labels"].index(m))) for r, m in no)
    ny, nn = max(1, len(yes)), max(1, len(no))
    print(f"     {grp:26s} YES (n={len(yes)}): " + " ".join(f"{k:+d} {100 * dy[k] / ny:.0f}%" for k in range(-2, 3)) +
          f"   NO (n={len(no)}): " + " ".join(f"{k:+d} {100 * dn[k] / nn:.0f}%" for k in range(-2, 3)))
print("   result per share at its own price (settled):")
for side in ("Yes", "No"):
    sel = [((1.0 if (r["truth"] == r["first"]["bucket"]) == (side == "Yes") else 0.0) - r["first"]["price"]) for r in early
           if r["first"]["side"] == side and r["truth"]]
    print(f"     {side.upper()} n={len(sel)} {100 * statistics.mean(sel):+.1f} c   (price mean {statistics.mean(r['first']['price'] for r in early if r['first']['side'] == side):.3f})")

# ------------------------------------------------------------------ (6) the deterministic forecasts in hand at the first fill
RUNS_PATH = B / "first_runs.json"
if RUNS_PATH.exists():
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    RUNS = {k: v for k, v in json.load(open(RUNS_PATH)).items() if "error" not in v}
    LAG = {"gfs_global": 4.0, "icon_global": 3.5, "ecmwf_ifs025": 7.0, "gem_global": 4.5, "ukmo_global_deterministic_10km": 5.5,
           "ecmwf_aifs025_single": 6.0, "meteofrance_arpege_world": 4.0, "jma_gsm": 4.0, "cma_grapes_global": 7.0, "ncep_aigfs025": 5.0}
    SHORTM = {"gfs_global": "GFS", "icon_global": "ICON", "ecmwf_ifs025": "IFS", "gem_global": "GEM", "ukmo_global_deterministic_10km": "UKMO",
              "ecmwf_aifs025_single": "AIFS", "meteofrance_arpege_world": "ARPEGE", "jma_gsm": "JMA", "cma_grapes_global": "CMA", "ncep_aigfs025": "AIGFS"}

    def runs_for(r):
        ed = _dt.fromisoformat(r["ed"]).replace(tzinfo=_tz.utc)
        d2 = ed - _td(days=2)
        return [d2 - _td(hours=6), d2, d2 + _td(hours=6), d2 + _td(hours=12)]

    def fc(r, model, mode):
        """mode 'latest': newest run whose init + lag <= first fill; or a fixed run label."""
        best = None
        for run in runs_for(r):
            key = f"{r['st']}|{run.strftime('%Y-%m-%dT%H:%M')}"
            v = RUNS.get(key, {}).get("tmax", {}).get(model, {}).get(r["ed"])
            if v is None:
                continue
            if mode == "latest":
                if run.timestamp() + LAG[model] * 3600 <= r["ts"] and (best is None or run > best[0]):
                    best = (run, v)
            elif run.strftime("%HZ") == mode and (best is None):
                best = (run, v)
        return None if best is None else best[1]

    allr = rows + early
    for r in allr:
        r["formed"] = r in rows
    print("\n== (6) first YES in the bucket of each deterministic model's forecast in hand at the fill (Open-Meteo single runs; research days only so far)")
    for grp, sel_fn in (("before the market formed", lambda r: not r["formed"]), ("after it formed", lambda r: r["formed"])):
        print(f"   {grp}:")
        for mode in ("latest", "00Z", "18Z"):
            cells = []
            for m in list(LAG) + ["median"]:
                hits = n = near = 0
                for r in allr:
                    if not sel_fn(r) or r["first"]["side"] != "Yes":
                        continue
                    if m == "median":
                        vs = [fc(r, mm, mode) for mm in LAG]
                        vs = [v for v in vs if v is not None]
                        if len(vs) < 3:
                            continue
                        v = statistics.median(vs)
                    else:
                        v = fc(r, m, mode)
                    if v is None:
                        continue
                    lab = bucket_of(r, v)
                    if lab is None:
                        continue
                    n += 1
                    hits += lab == r["first"]["bucket"]
                    near += abs(r["labels"].index(lab) - r["labels"].index(r["first"]["bucket"])) <= 1
                if n >= 20:
                    cells.append(f"{SHORTM.get(m, m)} {hits / n:.2f}/{near / n:.2f} ({n})")
            print(f"     run {mode:6s} exact/within-1: " + "  ".join(cells))
    # rounding convention: settlement is integer C / 2F buckets; does the cluster round the forecast up?
    print("   where the first YES sits relative to the GFS forecast in hand (bucket offset, before the market formed):")
    off = collections.Counter()
    for r in allr:
        if r["formed"] or r["first"]["side"] != "Yes":
            continue
        v = fc(r, "gfs_global", "latest")
        lab = bucket_of(r, v) if v is not None else None
        if lab:
            off[max(-3, min(3, r["labels"].index(r["first"]["bucket"]) - r["labels"].index(lab)))] += 1
    n = sum(off.values())
    if n:
        print("     " + "  ".join(f"{k:+d}: {100 * off[k] / n:.0f}%" for k in range(-3, 4)) + f"  (n={n})")
    # station x model bias from the station's other days (truth - forecast in hand), then the corrected bucket
    print("\n   with each station x model's standing bias removed (mean truth - forecast over the station's other days, >=3):")
    fcv = {}
    for r in allr:
        for m in LAG:
            fcv[(r["st"], r["ed"], m)] = fc(r, m, "latest")
    tr = {(r["st"], r["ed"]): r["truth_c"] for r in allr if r["truth_c"] is not None}

    def corrected(r, m):
        v = fcv.get((r["st"], r["ed"], m))
        if v is None:
            return None
        other = [tr[(s, d)] - x for (s, d, mm), x in fcv.items() if s == r["st"] and mm == m and d != r["ed"] and x is not None and (s, d) in tr]
        return v + (statistics.mean(other) if len(other) >= 3 else 0.0)

    for grp, sel_fn in (("before the market formed", lambda r: not r["formed"]), ("after it formed", lambda r: r["formed"])):
        cells = []
        for m in list(LAG) + ["median"]:
            hits = n = near = 0
            for r in allr:
                if not sel_fn(r) or r["first"]["side"] != "Yes":
                    continue
                if m == "median":
                    vs = [corrected(r, mm) for mm in LAG]
                    vs = [v for v in vs if v is not None]
                    if len(vs) < 3:
                        continue
                    v = statistics.median(vs)
                else:
                    v = corrected(r, m)
                if v is None:
                    continue
                lab = bucket_of(r, v)
                if lab is None:
                    continue
                n += 1
                hits += lab == r["first"]["bucket"]
                near += abs(r["labels"].index(lab) - r["labels"].index(r["first"]["bucket"])) <= 1
            if n >= 20:
                cells.append(f"{SHORTM.get(m, m)} {hits / n:.2f}/{near / n:.2f} ({n})")
        print(f"     {grp:26s} " + "  ".join(cells))
    # and against the truth and the later market mode, for the same early first YES: cluster vs the best corrected forecast
    print("\n   early first YES vs corrected-median forecast bucket: hit the truth / hit the event-day 06:00 market mode")
    a = collections.Counter()
    for r in allr:
        if r["formed"] or r["first"]["side"] != "Yes" or r["truth"] is None:
            continue
        vs = [corrected(r, mm) for mm in LAG]
        vs = [v for v in vs if v is not None]
        if len(vs) < 3:
            continue
        lab = bucket_of(r, statistics.median(vs))
        lm = later_mode(r)
        a["n"] += 1
        a["cl_truth"] += r["first"]["bucket"] == r["truth"]
        a["fc_truth"] += lab == r["truth"]
        if lm:
            a["n_lm"] += 1
            a["cl_lm"] += r["first"]["bucket"] == lm
            a["fc_lm"] += lab == lm
    if a["n"]:
        print(f"     n={a['n']}: truth  cluster {a['cl_truth'] / a['n']:.3f}  forecast {a['fc_truth'] / a['n']:.3f}   |   later market mode (n={a['n_lm']}):"
              f" cluster {a['cl_lm'] / max(1, a['n_lm']):.3f}  forecast {a['fc_lm'] / max(1, a['n_lm']):.3f}")
    print("\n   early first legs by bucket offset from the corrected-median forecast bucket:")
    for side in ("Yes", "No"):
        off = collections.Counter()
        pnl = collections.defaultdict(list)
        for r in allr:
            if r["formed"] or r["first"]["side"] != side:
                continue
            vs = [corrected(r, mm) for mm in LAG]
            vs = [v for v in vs if v is not None]
            if len(vs) < 3:
                continue
            lab = bucket_of(r, statistics.median(vs))
            if lab is None:
                continue
            k = max(-3, min(3, r["labels"].index(r["first"]["bucket"]) - r["labels"].index(lab)))
            off[k] += 1
            if r["truth"]:
                win = (r["truth"] == r["first"]["bucket"]) == (side == "Yes")
                pnl[k].append((1.0 if win else 0.0) - r["first"]["price"])
        n = sum(off.values())
        if n:
            print(f"     {side.upper():3s} (n={n}) " + "  ".join(f"{k:+d}: {100 * off[k] / n:.0f}%" + (f" [{100 * statistics.mean(pnl[k]):+.0f}c]" if pnl[k] else "") for k in range(-3, 4)))

    # ------------------------------------------------------------------ (7) forward check of the early-first-YES rule
    # Rule read off the research days in (6): the first YES sits in the corrected-median forecast bucket or one above,
    # one above most often.  Here the bias uses only days <= d-2 (as of the trade), and the split is by event day.
    from datetime import date as _date

    def corrected_asof(r, m):
        v = fcv.get((r["st"], r["ed"], m))
        if v is None:
            v = fc(r, m, "latest")
        if v is None:
            return None
        cut = (_date.fromisoformat(r["ed"]) - _td(days=2)).isoformat()
        other = []
        for x in allr:
            if x["st"] != r["st"] or x["ed"] > cut or x["truth_c"] is None:
                continue
            fx = fcv.get((x["st"], x["ed"], m))
            if fx is not None:
                other.append(x["truth_c"] - fx)
        return v + (statistics.mean(other) if len(other) >= 3 else 0.0)

    for r in allr:
        for m in LAG:
            if (r["st"], r["ed"], m) not in fcv:
                fcv[(r["st"], r["ed"], m)] = fc(r, m, "latest")
    print("\n== (7) early first YES (before the market formed) vs the corrected-median forecast bucket (bias from days <= d-2), research | forward")
    stat = {s: collections.Counter() for s in ("research", "forward")}
    for r in allr:
        if r["formed"] or r["first"]["side"] != "Yes":
            continue
        vs = [corrected_asof(r, mm) for mm in LAG]
        vs = [v for v in vs if v is not None]
        if len(vs) < 3:
            continue
        lab = bucket_of(r, statistics.median(vs))
        if lab is None:
            continue
        s = r["set"]
        k = r["labels"].index(r["first"]["bucket"]) - r["labels"].index(lab)
        st_ = stat[s]
        st_["n"] += 1
        st_[f"off{max(-2, min(2, k))}"] += 1
        if r["truth"]:
            st_["nt"] += 1
            st_["cl_truth"] += r["first"]["bucket"] == r["truth"]
            st_["fc_truth"] += lab == r["truth"]
            up = r["labels"][min(len(r["labels"]) - 1, r["labels"].index(lab) + 1)]
            st_["up_truth"] += up == r["truth"]
        lm = later_mode(r)
        if lm:
            st_["nl"] += 1
            st_["cl_lm"] += r["first"]["bucket"] == lm
            st_["fc_lm"] += lab == lm
    for s in ("research", "forward"):
        c = stat[s]
        if not c["n"]:
            continue
        n = c["n"]
        print(f"   {s:8s} n={n}: offset " + "  ".join(f"{k:+d} {100 * c[f'off{k}'] / n:.0f}%" for k in range(-2, 3)) +
              f"   rule 'forecast bucket' exact {c['off0'] / n:.3f}, 'one above' exact {c['off1'] / n:.3f}, either {(c['off0'] + c['off1']) / n:.3f}")
        if c["nt"]:
            print(f"            truth hit: cluster {c['cl_truth'] / c['nt']:.3f}  forecast bucket {c['fc_truth'] / c['nt']:.3f}  one above {c['up_truth'] / c['nt']:.3f} (n={c['nt']})"
                  f"   later market mode hit: cluster {c['cl_lm'] / max(1, c['nl']):.3f}  forecast {c['fc_lm'] / max(1, c['nl']):.3f} (n={c['nl']})")
