"""Round 35 -- the cluster's first-pick logic before the market forms, exactly
the protocol committed in README (7f3d01fc).  Inputs (bench): first_rows.json.gz,
first_runs.json; WS frames from the SSOT (read-only).
"""
import collections
import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

src = Path(__file__).with_name("why_plus_one_r33.py").read_text()
ns = {"__name__": "r33b", "__file__": str(Path(__file__).with_name("why_plus_one_r33.py"))}
exec(compile(src.split("cases = []")[0], "why_plus_one_head", "exec"), ns)
R, FC, LAG, bias, bucket_i, edges, formed = (ns[k] for k in ("R", "FC", "LAG", "bias", "bucket_i", "edges", "formed"))
SPLIT = "2026-10-03"
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

cases = []
for r in R:
    if formed(r):
        continue
    raw = {m: FC[(r["st"], r["ed"], m)] for m in LAG if FC[(r["st"], r["ed"], m)] is not None}
    if len(raw) < 3:
        continue
    b = {m: bias(r["st"], r["ed"], m) for m in raw}
    cases.append({"r": r, "raw": raw, "b": b, "set": "research" if r["ed"] <= SPLIT else "forward",
                  "ch": r["labels"].index(r["first"]["bucket"]), "side": r["first"]["side"]})
yes = [c for c in cases if c["side"] == "Yes"]
print(f"pre-formation first legs with >=3 models: {len(cases)} (YES {len(yes)}: research {sum(c['set'] == 'research' for c in yes)}, forward {sum(c['set'] == 'forward' for c in yes)})")


def stat(name, vals, r, c):
    v = sorted(vals)
    n = len(v)
    if name == "median":
        return statistics.median(v)
    if name == "mean":
        return statistics.mean(v)
    if name == "trimmed mean":
        return statistics.mean(v[1:-1]) if n >= 5 else statistics.mean(v)
    if name == "max":
        return v[-1]
    if name == "min":
        return v[0]
    if name == "p75":
        return v[int(round(0.75 * (n - 1)))]
    if name == "p25":
        return v[int(round(0.25 * (n - 1)))]
    if name == "midrange":
        return (v[0] + v[-1]) / 2
    return None


STATS = ["median", "mean", "trimmed mean", "max", "min", "p75", "p25", "midrange", "vote mode (warm tie)", "vote mode (cold tie)"]


def pick(case, name, c, lam):
    r = case["r"]
    vals = [case["raw"][m] + lam * case["b"][m] for m in case["raw"]]
    if name.startswith("vote mode"):
        cnt = collections.Counter(bucket_i(r, v + c) for v in vals)
        top = max(cnt.values())
        tied = [k for k, n in cnt.items() if n == top]
        return max(tied) if "warm" in name else min(tied)
    return bucket_i(r, stat(name, vals, r, c) + c)


print("\n== A structure: bucket(statistic(raw + lam x station bias) + c); (c, lam) chosen on research, exact hit research | forward")
res = [c for c in yes if c["set"] == "research"]
fwd = [c for c in yes if c["set"] == "forward"]
GRID_C = [round(x * 0.1, 1) for x in range(-10, 21)]
GRID_L = [round(x * 0.1, 1) for x in range(0, 11)]
table = []
for name in STATS:
    best = None
    for lam in GRID_L:
        for c in GRID_C:
            h = sum(pick(x, name, c, lam) == x["ch"] for x in res)
            if best is None or h > best[0]:
                best = (h, c, lam)
    h, c, lam = best
    hf = sum(pick(x, name, c, lam) == x["ch"] for x in fwd) / len(fwd)
    near = sum(abs(pick(x, name, c, lam) - x["ch"]) <= 1 for x in fwd) / len(fwd)
    table.append((hf, name, c, lam, h / len(res), near))
for hf, name, c, lam, hr, near in sorted(table, reverse=True):
    print(f"   {name:22s} c={c:+.1f} lam={lam:.1f}   research {hr:.3f}   forward {hf:.3f}   forward within 1 bucket {near:.3f}")
ref = sum(pick(x, "median", 0.8, 0.3) == x["ch"] for x in fwd) / len(fwd)
print(f"   reference (round 33: median, c=+0.8, lam=0.3): forward {ref:.3f}")


# ---------------------------------------------------------------- B value
def frame(r):
    row = conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                       " AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (r["st"], r["ed"], iso(r["ts"] - 1), iso(r["ts"] - 600))).fetchone()
    return None if not row else {str(b["bucket"]): b for b in json.loads(row[0])}


def probs(case, sigma):
    r = case["r"]
    e = edges(r)
    xs = [case["raw"][m] + 0.8 + 0.3 * case["b"][m] for m in case["raw"]]
    Phi = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))
    p = []
    for i, (lo, hi) in enumerate(e):
        lo_ = -math.inf if i == 0 else lo
        hi_ = math.inf if i == len(e) - 1 else hi
        p.append(statistics.mean((1.0 if hi_ == math.inf else Phi((hi_ - x) / sigma)) - (0.0 if lo_ == -math.inf else Phi((lo_ - x) / sigma)) for x in xs))
    return p


def legs(case, fr):
    out = []
    for i, lab in enumerate(case["r"]["labels"]):
        b = fr.get(lab)
        if not b:
            continue
        asks = sorted(float(x["price"]) for x in b.get("ask_levels") or [])
        if asks and 0 < asks[0] < 0.99:
            out.append((i, "Yes", asks[0]))
        bid = float(b.get("bid_price") or 0)
        if bid > 0 and 1 - bid < 0.99:
            out.append((i, "No", 1 - bid))
    return out


vcases = []
for c in cases:
    fr = frame(c["r"])
    if fr:
        lg = legs(c, fr)
        if any(i == c["ch"] and s == c["side"] for i, s, _ in lg):
            vcases.append((c, lg))
print(f"\n== B value: pre-formation first legs with a frame <=10 min before and the chosen leg priced: {len(vcases)}"
      f" (first half <=10-07 {sum(c['r']['ed'] <= '2026-10-07' for c, _ in vcases)}, second half {sum(c['r']['ed'] >= '2026-10-08' for c, _ in vcases)})")


def rank_rule(case, lg, sigma, rule):
    p = probs(case, sigma)
    sc = []
    for i, s, price in lg:
        pv = p[i] if s == "Yes" else 1 - p[i]
        if rule == "v2" and s != "Yes":
            continue
        val = (pv - price) / price if rule == "v3" else pv - price
        sc.append((val, i, s))
    sc.sort(reverse=True)
    return [(i, s) for _, i, s in sc]


def score(sel, sigma, rule):
    t1 = t3 = side = 0
    for c, lg in sel:
        rk = rank_rule(c, lg, sigma, rule)
        if not rk:
            continue
        t1 += rk[0] == (c["ch"], c["side"])
        t3 += (c["ch"], c["side"]) in rk[:3]
        side += rk[0][1] == c["side"]
    n = len(sel)
    return t1 / n, t3 / n, side / n


h1 = [x for x in vcases if x[0]["r"]["ed"] <= "2026-10-07"]
h2 = [x for x in vcases if x[0]["r"]["ed"] >= "2026-10-08"]
best = None
for sigma in (0.75, 1.0, 1.5):
    for rule in ("v1", "v2", "v3"):
        a = score(h1, sigma, rule)
        b = score(h2, sigma, rule)
        print(f"   sigma={sigma:.2f} {rule}: first half top-1 {a[0]:.3f} top-3 {a[1]:.3f} side {a[2]:.3f}   |   second half top-1 {b[0]:.3f} top-3 {b[1]:.3f} side {b[2]:.3f}")
        if best is None or a[0] > best[0]:
            best = (a[0], sigma, rule, b)
print(f"   chosen on the first half: sigma={best[1]}, {best[2]} -> second half top-1 {best[3][0]:.3f}, top-3 {best[3][1]:.3f}, side {best[3][2]:.3f}")
for nm, sel in (("first half", h1), ("second half", h2)):
    ys = [(c, lg) for c, lg in sel if c["side"] == "Yes"]
    st_hit = sum(pick(c, "median", 0.8, 0.3) == c["ch"] for c, _ in ys) / max(1, len(ys))
    print(f"   structure rule on the same {nm} YES legs (median+0.8+0.3x): exact {st_hit:.3f} (n={len(ys)}); share of these first legs that are YES {len(ys) / max(1, len(sel)):.2f}")

# for YES picks: is the chosen YES the cheapest of F-1..F+1, or the one with the best p - ask?
print("\n   YES first legs: chosen vs the three buckets around F (F = bucket of median+0.8+0.3x)")
agg = collections.Counter()
for c, lg in vcases:
    if c["side"] != "Yes":
        continue
    F = pick(c, "median", 0.8, 0.3)
    ask = {i: price for i, s, price in lg if s == "Yes"}
    trio = [i for i in (F - 1, F, F + 1) if i in ask]
    if c["ch"] not in trio or len(trio) < 2:
        agg["chosen outside F-1..F+1 or unpriced"] += 1
        continue
    agg["n"] += 1
    agg["chosen is cheapest of the trio"] += c["ch"] == min(trio, key=lambda i: ask[i])
    p = probs(c, 1.0)
    agg["chosen is best p-ask of the trio"] += c["ch"] == max(trio, key=lambda i: p[i] - ask[i])
    agg["chosen is highest p of the trio"] += c["ch"] == max(trio, key=lambda i: p[i])
print("   " + "  ".join(f"{k}: {v}" for k, v in agg.items()))

# ---------------------------------------------------------------- C NO
print("\n== C first NO legs: position vs F (median+0.8+0.3x) and rank under v1 (sigma 1.0)")
pos = collections.Counter()
ranks = []
for c in cases:
    if c["side"] != "No":
        continue
    F = pick(c, "median", 0.8, 0.3)
    pos[max(-3, min(3, c["ch"] - F))] += 1
for c, lg in vcases:
    if c["side"] == "No":
        rk = rank_rule(c, lg, 1.0, "v1")
        if (c["ch"], "No") in rk:
            ranks.append(rk.index((c["ch"], "No")) + 1)
n = sum(pos.values())
print(f"   n={n}: " + "  ".join(f"{k:+d}: {100 * pos[k] / n:.0f}%" for k in range(-3, 4)))
if ranks:
    print(f"   v1 rank of the chosen NO (n={len(ranks)}): 1st {sum(1 for x in ranks if x == 1) / len(ranks):.2f}, top-3 {sum(1 for x in ranks if x <= 3) / len(ranks):.2f}, median rank {statistics.median(ranks)}")

# ---------------------------------------------------------------- post-hoc: a weighted blend of the ten models
# (not in the protocol) target = centre of the chosen YES bucket; non-negative weights summing to 1 plus a constant,
# fitted on research by least squares on the bucket centre, read forward as exact bucket hits.
from scipy.optimize import minimize as _min
import numpy as _np
MODELS = list(LAG)


def centre(r, i):
    e = edges(r)
    return (e[i][0] + e[i][1]) / 2


def feats(c):
    out = []
    for m in MODELS:
        v = c["raw"].get(m)
        out.append(None if v is None else v + 0.3 * c["b"][m])
    return out


def predict(w, c0, c):
    f = feats(c)
    num = sum(wi * fi for wi, fi in zip(w, f) if fi is not None)
    den = sum(wi for wi, fi in zip(w, f) if fi is not None)
    return None if den <= 1e-9 else num / den + c0


def loss(theta, data):
    w = _np.exp(theta[:-1])
    c0 = theta[-1]
    tot = 0.0
    for c in data:
        p = predict(w, c0, c)
        if p is not None:
            tot += (p - centre(c["r"], c["ch"])) ** 2
    return tot


th = _min(lambda t: loss(t, res), _np.zeros(len(MODELS) + 1), method="Nelder-Mead", options={"maxiter": 4000, "xatol": 1e-3, "fatol": 1e-3}).x
w = _np.exp(th[:-1]); w = w / w.sum(); c0 = th[-1]
hr = sum(bucket_i(c["r"], predict(w, c0, c)) == c["ch"] for c in res) / len(res)
hf = sum(bucket_i(c["r"], predict(w, c0, c)) == c["ch"] for c in fwd) / len(fwd)
print("\n== post-hoc: weighted blend fitted on research (target: chosen bucket centre)")
print("   weights: " + "  ".join(f"{ns['SH'][m]} {wi:.2f}" for m, wi in sorted(zip(MODELS, w), key=lambda t: -t[1])) + f"   constant {c0:+.2f} C")
print(f"   exact: research {hr:.3f}  forward {hf:.3f}   (reference median+0.8+0.3x forward {ref:.3f})")
# the highest-probability bucket of the calibrated ten-model distribution (sigma 1.0)
hp = [sum(1 for c in S if max(range(len(c['r']['labels'])), key=lambda i: probs(c, 1.0)[i]) == c["ch"]) / len(S) for S in (res, fwd)]
print(f"   highest-probability bucket of the calibrated distribution (sigma 1.0): research {hp[0]:.3f}  forward {hp[1]:.3f}")

# ---------------------------------------------------------------- post-hoc: what makes the first leg YES or NO
print("\n== post-hoc: share of pre-formation first legs that are YES, by condition (research | forward)")


def share(sel):
    return f"{sum(c['side'] == 'Yes' for c in sel) / len(sel):.2f} ({len(sel)})" if sel else "-"


conds = {
    "wallet 0x8fbd (main)": lambda c: c["r"]["first"]["w"].startswith("0x8fbd"),
    "other wallets": lambda c: not c["r"]["first"]["w"].startswith("0x8fbd"),
    "model spread < 0.8 C": lambda c: statistics.pstdev([c["raw"][m] + c["b"][m] for m in c["raw"]]) < 0.8,
    "model spread >= 1.2 C": lambda c: statistics.pstdev([c["raw"][m] + c["b"][m] for m in c["raw"]]) >= 1.2,
    "top bucket p >= 0.45 (sigma 1)": lambda c: max(probs(c, 1.0)) >= 0.45,
    "top bucket p < 0.35 (sigma 1)": lambda c: max(probs(c, 1.0)) < 0.35,
    "<= 30 min after listing": lambda c: c["r"]["ts"] - c["r"]["listed"] <= 1800,
    "30-120 min after listing": lambda c: 1800 < c["r"]["ts"] - c["r"]["listed"] <= 7200,
    "F unit": lambda c: c["r"]["unit"] == "F",
    "C unit": lambda c: c["r"]["unit"] == "C",
}
for nm, f in conds.items():
    print(f"   {nm:32s} {share([c for c in cases if c['set'] == 'research' and f(c)])} | {share([c for c in cases if c['set'] == 'forward' and f(c)])}")
print("   first NO legs by wallet: " + "  ".join(f"{w} {n}" for w, n in collections.Counter(c["r"]["first"]["w"] for c in cases if c["side"] == "No").most_common()))
print("   first YES legs by wallet: " + "  ".join(f"{w} {n}" for w, n in collections.Counter(c["r"]["first"]["w"] for c in cases if c["side"] == "Yes").most_common()))
# per wallet: where its first NO goes relative to F, and its first-leg result per share
for w in sorted({c["r"]["first"]["w"] for c in cases}):
    sel = [c for c in cases if c["r"]["first"]["w"] == w and c["r"]["truth"]]
    if len(sel) < 15:
        continue
    pnl = [((1.0 if (c["r"]["truth"] == c["r"]["first"]["bucket"]) == (c["side"] == "Yes") else 0.0) - c["r"]["first"]["price"]) for c in sel]
    print(f"   wallet {w}: first legs {len(sel)}, YES share {sum(c['side'] == 'Yes' for c in sel) / len(sel):.2f}, per share {100 * statistics.mean(pnl):+.1f} c")

# ---------------------------------------------------------------- post-hoc (熊先生 10-11 插问): the trio belief rule
# 「先通过校准后的预报桶建立三个原始的桶信念，再挑一个最可能和最不可能，最可能买yes，最不可能买no」
print("\n== post-hoc: trio rule -- beliefs on F-1, F, F+1 (calibrated ten-model distribution, sigma 1.0); YES on the most likely, NO on the least likely")
for S, nm in ((res, "research"), (fwd, "forward")):
    n = intrio = hit = 0
    for c in S:
        F = pick(c, "median", 0.8, 0.3)
        p = probs(c, 1.0)
        trio = [i for i in (F - 1, F, F + 1) if 0 <= i < len(p)]
        n += 1
        intrio += c["ch"] in trio
        hit += c["ch"] == max(trio, key=lambda i: p[i])
    print(f"   YES {nm:8s} n={n}: chosen inside the trio {intrio / n:.2f}; chosen = most likely of the trio {hit / n:.2f}")
nos = [c for c in cases if c["side"] == "No"]
for nm, sel in (("research", [c for c in nos if c["set"] == "research"]), ("forward", [c for c in nos if c["set"] == "forward"])):
    cnt = collections.Counter()
    for c in sel:
        F = pick(c, "median", 0.8, 0.3)
        p = probs(c, 1.0)
        trio = [i for i in (F - 1, F, F + 1) if 0 <= i < len(p)]
        cnt["n"] += 1
        if c["ch"] in trio:
            cnt["inside the trio"] += 1
            cnt["= least likely of the trio"] += c["ch"] == min(trio, key=lambda i: p[i])
            cnt["= most likely of the trio"] += c["ch"] == max(trio, key=lambda i: p[i])
        else:
            cnt["outside, colder"] += c["ch"] < F - 1
            cnt["outside, warmer"] += c["ch"] > F + 1
        # where the NO sits in the whole ladder's belief ranking (1 = most likely bucket)
        order = sorted(range(len(p)), key=lambda i: -p[i])
        cnt["rank_sum"] += order.index(c["ch"]) + 1
    n = cnt["n"]
    print(f"   NO  {nm:8s} n={n}: inside the trio {cnt['inside the trio'] / n:.2f} (of all NO: least likely {cnt['= least likely of the trio'] / n:.2f},"
          f" most likely {cnt['= most likely of the trio'] / n:.2f}); outside colder {cnt['outside, colder'] / n:.2f}, outside warmer {cnt['outside, warmer'] / n:.2f};"
          f" mean belief rank of the shorted bucket {cnt['rank_sum'] / n:.1f} of 11")
# the NO's price: what the cluster paid relative to the belief
pn = [(c["r"]["first"]["price"], 1 - probs(c, 1.0)[c["ch"]], c["r"]["truth"] != c["r"]["first"]["bucket"]) for c in nos if c["r"]["truth"]]
print(f"   NO paid {statistics.mean(x[0] for x in pn):.3f} on average; calibrated belief that the bucket loses {statistics.mean(x[1] for x in pn):.3f}; it actually lost {statistics.mean(1.0 if x[2] else 0.0 for x in pn):.3f} (n={len(pn)})")

# ---------------------------------------------------------------- post-hoc (熊先生 10-11「买yes或者no是用信念减市场价格的edge来决定的？」)
print("\n== post-hoc: edge = belief - price.  (a) the edge of the leg it took, at the price it paid, under our calibrated belief (sigma 1.0)")
for side in ("Yes", "No"):
    sel = [c for c in cases if c["side"] == side]
    e = []
    for c in sel:
        p = probs(c, 1.0)[c["ch"]]
        pv = p if side == "Yes" else 1 - p
        e.append(pv - c["r"]["first"]["price"])
    real = [((1.0 if (c["r"]["truth"] == c["r"]["first"]["bucket"]) == (side == "Yes") else 0.0) - c["r"]["first"]["price"]) for c in sel if c["r"]["truth"]]
    print(f"   {side.upper():3s} n={len(sel)}: belief edge at paid price mean {100 * statistics.mean(e):+.1f} c, positive {sum(1 for x in e if x > 0) / len(e):.2f};"
          f"  realized per share {100 * statistics.mean(real):+.1f} c (n={len(real)})")
print("   (b) does the YES-or-NO decision follow the edges on offer?  frames <=10 min before the fill:")
print("       e_yes = (belief of the most likely bucket) - its YES ask;  e_no = best (1 - belief) - NO price over all buckets")
grp = collections.defaultdict(list)
for c, lg in vcases:
    p = probs(c, 1.0)
    top = max(range(len(p)), key=lambda i: p[i])
    ya = {i: price for i, s, price in lg if s == "Yes"}
    na = {i: price for i, s, price in lg if s == "No"}
    if top not in ya or not na:
        continue
    e_yes = p[top] - ya[top]
    e_no = max((1 - p[i]) - price for i, price in na.items())
    grp[c["side"]].append((e_yes, e_no))
for side, v in grp.items():
    print(f"       first leg {side.upper():3s} n={len(v)}: mean e_yes {100 * statistics.mean(a for a, b in v):+.1f} c, mean e_no {100 * statistics.mean(b for a, b in v):+.1f} c,"
          f" share where e_no > e_yes {sum(1 for a, b in v if b > a) / len(v):.2f}")
allv = [(s, a, b) for s, v in grp.items() for a, b in v]
if allv:
    acc = sum(1 for s, a, b in allv if (("No" if b > a else "Yes") == s)) / len(allv)
    base = max(sum(1 for s, _, _ in allv if s == "Yes"), sum(1 for s, _, _ in allv if s == "No")) / len(allv)
    print(f"       rule 'NO if e_no > e_yes else YES': accuracy {acc:.2f} (always the commoner side {base:.2f}), n={len(allv)}")
print("\n   (c) exploratory: a sharper belief (smaller sigma) -- edge of the legs taken, and the side rule")
for sigma in (0.5, 0.6, 0.75):
    ey = [probs(c, sigma)[c["ch"]] - c["r"]["first"]["price"] for c in cases if c["side"] == "Yes"]
    en = [(1 - probs(c, sigma)[c["ch"]]) - c["r"]["first"]["price"] for c in cases if c["side"] == "No"]
    ok = n = 0
    for c, lg in vcases:
        p = probs(c, sigma)
        top = max(range(len(p)), key=lambda i: p[i])
        ya = {i: price for i, s, price in lg if s == "Yes"}
        na = {i: price for i, s, price in lg if s == "No"}
        if top not in ya or not na:
            continue
        e_yes = p[top] - ya[top]
        e_no = max((1 - p[i]) - price for i, price in na.items())
        n += 1
        ok += ("No" if e_no > e_yes else "Yes") == c["side"]
    print(f"       sigma={sigma}: YES legs belief edge {100 * statistics.mean(ey):+.1f} c (positive {sum(1 for x in ey if x > 0) / len(ey):.2f});"
          f" NO legs {100 * statistics.mean(en):+.1f} c (positive {sum(1 for x in en if x > 0) / len(en):.2f}); side rule accuracy {ok / max(1, n):.2f} (n={n})")

# ---------------------------------------------------------------- post-hoc 35.4 (熊先生 10-11「不要用我目前系统的信念……他是十大确定性模型」)
# The belief of a ten-deterministic-model user: the share of the models whose (calibrated) forecast lands in each bucket.
def votes(case, cal):
    r = case["r"]
    if cal == "raw+0.8+0.3x":
        xs = [case["raw"][m] + 0.8 + 0.3 * case["b"][m] for m in case["raw"]]
    elif cal == "raw+station bias":
        xs = [case["raw"][m] + case["b"][m] for m in case["raw"]]
    else:
        xs = [case["raw"][m] + 0.8 for m in case["raw"]]
    cnt = collections.Counter(bucket_i(r, x) for x in xs)
    n = len(xs)
    return [cnt.get(i, 0) / n for i in range(len(r["labels"]))]


print("\n== post-hoc 35.4: belief = vote share of the ten deterministic models (no kernel), three calibrations")
for cal in ("raw+0.8+0.3x", "raw+0.8", "raw+station bias"):
    ey, en, top_hit, trio_most = [], [], 0, 0
    for c in cases:
        v = votes(c, cal)
        if c["side"] == "Yes":
            ey.append(v[c["ch"]] - c["r"]["first"]["price"])
            top = max(range(len(v)), key=lambda i: (v[i], -abs(i - c["ch"]) * 0))
            top_hit += c["ch"] == max(range(len(v)), key=lambda i: v[i])
        else:
            en.append((1 - v[c["ch"]]) - c["r"]["first"]["price"])
    nY = len(ey)
    ok = n = 0
    for c, lg in vcases:
        v = votes(c, cal)
        top = max(range(len(v)), key=lambda i: v[i])
        ya = {i: price for i, s, price in lg if s == "Yes"}
        na = {i: price for i, s, price in lg if s == "No"}
        if top not in ya or not na:
            continue
        e_yes = v[top] - ya[top]
        e_no = max((1 - v[i]) - price for i, price in na.items())
        n += 1
        ok += ("No" if e_no > e_yes else "Yes") == c["side"]
    print(f"   {cal:18s} YES legs: vote edge at paid price {100 * statistics.mean(ey):+.1f} c (positive {sum(1 for x in ey if x > 0) / nY:.2f}),"
          f" chosen = top-vote bucket {top_hit / nY:.2f} | NO legs: vote edge {100 * statistics.mean(en):+.1f} c (positive {sum(1 for x in en if x > 0) / len(en):.2f})"
          f" | side rule 'NO if e_no > e_yes' {ok / max(1, n):.2f} (n={n})")
# the vote share it bought at: does the YES price paid track the vote share of the bucket?
print("   YES legs by the vote share of the bucket bought (raw+0.8+0.3x): n, mean price paid, realized hit")
by = collections.defaultdict(list)
for c in cases:
    if c["side"] != "Yes" or not c["r"]["truth"]:
        continue
    v = votes(c, "raw+0.8+0.3x")[c["ch"]]
    k = "0" if v == 0 else "0.1-0.2" if v <= 0.2 else "0.3-0.4" if v <= 0.4 else "0.5-0.6" if v <= 0.6 else "0.7+"
    by[k].append((c["r"]["first"]["price"], 1.0 if c["r"]["truth"] == c["r"]["first"]["bucket"] else 0.0))
for k in ("0", "0.1-0.2", "0.3-0.4", "0.5-0.6", "0.7+"):
    if by[k]:
        print(f"     vote share {k:7s} n={len(by[k]):3d}  price {statistics.mean(p for p, h in by[k]):.3f}  hit {statistics.mean(h for p, h in by[k]):.3f}")
print("   NO legs by the vote share of the bucket shorted (raw+0.8+0.3x): n, mean NO price, bucket actually lost")
by = collections.defaultdict(list)
for c in cases:
    if c["side"] != "No" or not c["r"]["truth"]:
        continue
    v = votes(c, "raw+0.8+0.3x")[c["ch"]]
    k = "0" if v == 0 else "0.1-0.2" if v <= 0.2 else "0.3-0.4" if v <= 0.4 else "0.5+"
    by[k].append((c["r"]["first"]["price"], 1.0 if c["r"]["truth"] != c["r"]["first"]["bucket"] else 0.0))
for k in ("0", "0.1-0.2", "0.3-0.4", "0.5+"):
    if by[k]:
        print(f"     vote share {k:7s} n={len(by[k]):3d}  NO price {statistics.mean(p for p, h in by[k]):.3f}  lost {statistics.mean(h for p, h in by[k]):.3f}")
