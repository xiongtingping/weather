"""P0 centre source, step 2: does the single-system bias correction move each
system's centre toward truth, and does it move the pooled centre toward truth
and toward the market?

Arms on each seated system (raw member mean m, production shift b):
  raw    m                    (no correction)
  prod   m + b                (production: _resolved_bias_c on the lineage artifact)
  const  m + b_pooled         (same resolver, constant form = the artifact's pooled H5 shift)
  half   m + b/2
  rsign  m +/- b, EXACT expectation over both signs (per system, independent)
k* = E[(T-m) b] / E[b^2]: the least-squares multiplier on the served shift
(1 = right-sized, 0 = no information, <0 = wrong way).

Unit: envelopes are averaged inside (station, day, tier); every interval is a
cluster bootstrap over station-days (and, separately, event days and
stations).  Within-station checks: per-station effect, split-half persistence
(first vs second half of event days), and the shift split into its
station-average part and its within-station day-to-day part.
"""
import collections, itertools, json, math, pickle, sys
import numpy as np
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.center_width_calibration import served_bucket_axis

SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/"
D = pickle.load(open(SP + "csrc_rows.pkl", "rb"))
rows = D["rows"]
rng = np.random.default_rng(20260922)
LONG = ("12-24", "24-36", "36-48", "48+")
RES = {"stats": D["stats"], "window": [D["D0"], D["D1"]]}


def boot(keys, vals, n=3000):
    """cluster bootstrap of the mean over the given cluster keys"""
    g = collections.defaultdict(list)
    for k, v in zip(keys, vals):
        g[k].append(v)
    G = [np.array(v) for v in g.values()]
    sums = np.array([x.sum() for x in G]); cnts = np.array([len(x) for x in G])
    idx = rng.integers(0, len(G), (n, len(G)))
    b = sums[idx].sum(1) / cnts[idx].sum(1)
    return float(np.mean(vals)), float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))


def kstar(keys, num, den, n=3000):
    g = collections.defaultdict(lambda: [0.0, 0.0])
    for k, a, b in zip(keys, num, den):
        g[k][0] += a; g[k][1] += b
    A = np.array([v[0] for v in g.values()]); B = np.array([v[1] for v in g.values()])
    idx = rng.integers(0, len(A), (n, len(A)))
    ks = A[idx].sum(1) / B[idx].sum(1)
    return float(A.sum() / B.sum()), float(np.percentile(ks, 2.5)), float(np.percentile(ks, 97.5))


def fmt(t):
    return f"{t[0]:+.3f} [{t[1]:+.3f},{t[2]:+.3f}]"


# ---------------------------------------------------------------- system level
# cell = (st, day, tier, sys): mean over envelopes
cells = collections.defaultdict(list)
for r in rows:
    T = r["tmax"]
    for s, x in r["systems"].items():
        e0 = x["raw"] - T
        b = x["b"]
        cells[(r["st"], r["day"], r["tier"], s, r["era"])].append((
            abs(e0), abs(e0 + b), abs(e0 + x["b_const"]), abs(e0 + 0.5 * b),
            0.5 * (abs(e0 + b) + abs(e0 - b)),
            e0 * e0, (e0 + b) ** 2, (-e0) * b, b * b, b, e0, x["b_const"]))
C = []
for (st, day, t, s, era), v in cells.items():
    a = np.mean(np.array(v), 0)
    C.append(dict(st=st, day=day, tier=t, sys=s, era=era, raw=a[0], prod=a[1], const=a[2], half=a[3], rsign=a[4],
                  se0=a[5], sep=a[6], num=a[7], den=a[8], b=a[9], e0=a[10], bconst=a[11]))
print("system cells", len(C))


def sys_block(sub, label):
    keys = [(z["st"], z["day"]) for z in sub]
    out = {"n_cells": len(sub), "n_station_days": len(set(keys)), "n_stations": len(set(z["st"] for z in sub)),
           "mae_raw": float(np.mean([z["raw"] for z in sub])), "mae_prod": float(np.mean([z["prod"] for z in sub])),
           "mean_abs_b": float(np.mean([abs(z["b"]) for z in sub]))}
    for arm in ("prod", "const", "half", "rsign"):
        out["d_" + arm] = boot(keys, [z[arm] - z["raw"] for z in sub])
    out["d_prod_minus_rsign"] = boot(keys, [z["prod"] - z["rsign"] for z in sub])
    out["d_mse_prod"] = boot(keys, [z["sep"] - z["se0"] for z in sub])
    out["kstar"] = kstar(keys, [z["num"] for z in sub], [z["den"] for z in sub])
    out["d_prod_by_event_day"] = boot([z["day"] for z in sub], [z["prod"] - z["raw"] for z in sub])
    out["d_prod_by_station"] = boot([z["st"] for z in sub], [z["prod"] - z["raw"] for z in sub])
    print(f"{label:34s} n_sd={out['n_station_days']:4d} MAE raw {out['mae_raw']:.3f} prod {out['mae_prod']:.3f} |b| {out['mean_abs_b']:.2f} | "
          f"prod-raw {fmt(out['d_prod'])} rsign-raw {fmt(out['d_rsign'])} const-raw {fmt(out['d_const'])} half-raw {fmt(out['d_half'])} k* {fmt(out['kstar'])}")
    return out


RES["system"] = {}
for era in ("pool", "pre", "all"):
    for tiers, tl in ((LONG, ">=12h"), (("0-12",), "0-12h")):
        sub = [z for z in C if z["tier"] in tiers and (era == "all" or z["era"] == era)]
        if sub:
            RES["system"][f"{era}|{tl}|ALL"] = sys_block(sub, f"{era} {tl} all systems")
print()
for s in sorted(set(z["sys"] for z in C)):
    for era in ("all",):
        sub = [z for z in C if z["tier"] in LONG and z["sys"] == s]
        if len(sub) > 30:
            RES["system"][f"{era}|>=12h|{s}"] = sys_block(sub, f"{s[:26]}")
print()
for t in ("0-12",) + LONG:
    sub = [z for z in C if z["tier"] == t]
    RES["system"][f"all|{t}|ALL"] = sys_block(sub, f"tier {t}")

# ---- within-station: per-station effect, split-half persistence, decomposition
L = [z for z in C if z["tier"] in LONG]
days = sorted(set(z["day"] for z in L)); half1 = set(days[: len(days) // 2])
per = collections.defaultdict(lambda: collections.defaultdict(list))
for z in L:
    per[z["st"]]["all"].append(z["prod"] - z["raw"])
    per[z["st"]]["h1" if z["day"] in half1 else "h2"].append(z["prod"] - z["raw"])
st_eff = {s: float(np.mean(v["all"])) for s, v in per.items()}
helped = sum(v < 0 for v in st_eff.values()); hurt = sum(v > 0 for v in st_eff.values())
pairs = [(np.mean(v["h1"]), np.mean(v["h2"])) for v in per.values() if v["h1"] and v["h2"]]
r_half = float(np.corrcoef(np.array(pairs).T)[0, 1])
same_sign = sum((a > 0) == (b > 0) for a, b in pairs)
print(f"\nwithin-station (>=12h, all eras): stations helped {helped} / hurt {hurt} of {len(st_eff)}; "
      f"split-half r={r_half:+.2f}, same sign {same_sign}/{len(pairs)}")
# station x system persistence
per2 = collections.defaultdict(lambda: collections.defaultdict(list))
for z in L:
    per2[(z["st"], z["sys"])]["h1" if z["day"] in half1 else "h2"].append(z["prod"] - z["raw"])
pairs2 = [(np.mean(v["h1"]), np.mean(v["h2"])) for v in per2.values() if len(v["h1"]) >= 3 and len(v["h2"]) >= 3]
r_half2 = float(np.corrcoef(np.array(pairs2).T)[0, 1])
same2 = sum((a > 0) == (b > 0) for a, b in pairs2)
print(f"station x system split-half r={r_half2:+.2f}, same sign {same2}/{len(pairs2)}")
RES["within_station"] = dict(helped=helped, hurt=hurt, n=len(st_eff), split_half_r=r_half, split_half_same_sign=[same_sign, len(pairs)],
                             stsys_split_half_r=r_half2, stsys_same_sign=[same2, len(pairs2)],
                             per_station=dict(sorted(st_eff.items(), key=lambda kv: kv[1])))
# decomposition of the served shift into station-average and within-station deviation (per system)
sb = collections.defaultdict(list)
for z in L:
    sb[(z["st"], z["sys"])].append(z["b"])
sbar = {k: float(np.mean(v)) for k, v in sb.items()}
keys = [(z["st"], z["day"]) for z in L]
num_bar = [(-z["e0"]) * sbar[(z["st"], z["sys"])] for z in L]; den_bar = [sbar[(z["st"], z["sys"])] ** 2 for z in L]
num_dev = [(-z["e0"]) * (z["b"] - sbar[(z["st"], z["sys"])]) for z in L]; den_dev = [(z["b"] - sbar[(z["st"], z["sys"])]) ** 2 for z in L]
k_bar = kstar(keys, num_bar, den_bar); k_dev = kstar(keys, num_dev, den_dev)
# within-station: demean truth error too (does the day-to-day movement of b track the day-to-day movement of the error?)
e0bar = collections.defaultdict(list)
for z in L:
    e0bar[(z["st"], z["sys"])].append(z["e0"])
e0m = {k: float(np.mean(v)) for k, v in e0bar.items()}
num_dev2 = [(-(z["e0"] - e0m[(z["st"], z["sys"])])) * (z["b"] - sbar[(z["st"], z["sys"])]) for z in L]
k_dev2 = kstar(keys, num_dev2, den_dev)
share_dev = float(np.sum(den_dev) / (np.sum(den_dev) + np.sum(den_bar)))
print(f"shift decomposition: station-average part k*={fmt(k_bar)}; day-to-day part k*={fmt(k_dev)} (demeaned error {fmt(k_dev2)}); "
      f"day-to-day share of E[b^2] {share_dev:.1%}")
RES["decomposition"] = dict(k_station_average=k_bar, k_day_to_day=k_dev, k_day_to_day_demeaned=k_dev2, day_to_day_share_of_b2=share_dev)
# within-station |b| halves: is a big correction better or worse than a small one AT THE SAME STATION?
bh = []
for (st, s), v in sb.items():
    zs = [z for z in L if z["st"] == st and z["sys"] == s]
    if len(zs) < 6:
        continue
    med = np.median([abs(z["b"]) for z in zs])
    hi = [z["prod"] - z["raw"] for z in zs if abs(z["b"]) > med]; lo = [z["prod"] - z["raw"] for z in zs if abs(z["b"]) <= med]
    if hi and lo:
        bh.append((st, s, np.mean(hi), np.mean(lo)))
bh_d = [a - b for _, _, a, b in bh]
bh_t = boot([x[0] for x in bh], bh_d)
print(f"within station x system: (prod-raw | big |b| half) - (small half) = {fmt(bh_t)} over {len(bh)} station-systems")
RES["within_station_big_vs_small_b"] = bh_t

# ------------------------------------------------------------- pooled centre
env_cells = collections.defaultdict(list)
for r in rows:
    T = r["tmax"]; S = r["systems"]
    W = sum(x["w"] for x in S.values())
    craw = sum(x["w"] * x["raw"] for x in S.values()) / W
    cprod = sum(x["w"] * (x["raw"] + x["b"]) for x in S.values()) / W
    cconst = sum(x["w"] * (x["raw"] + x["b_const"]) for x in S.values()) / W
    chalf = sum(x["w"] * (x["raw"] + 0.5 * x["b"]) for x in S.values()) / W
    ks = list(S)
    rs = []
    for signs in itertools.product((1.0, -1.0), repeat=len(ks)):
        rs.append(abs(sum(S[k]["w"] * (S[k]["raw"] + sg * S[k]["b"]) for k, sg in zip(ks, signs)) / W - T))
    e_rs = float(np.mean(rs))
    cen, _edges = served_bucket_axis(r["intervals"], r["order"])
    smean = sum(r["served"][k] * c for k, c in zip(r["order"], cen))
    mmean = sum(r["mv"][k] * c for k, c in zip(r["order"], cen)) if r["mv"] else None
    shift = cprod - craw
    env_cells[(r["st"], r["day"], r["tier"], r["era"])].append(dict(
        raw=abs(craw - T), prod=abs(cprod - T), const=abs(cconst - T), half=abs(chalf - T), rsign=e_rs,
        served=abs(smean - T), num=(T - craw) * shift, den=shift * shift, shift=shift,
        mk=None if mmean is None else abs(mmean - T),
        raw_to_m=None if mmean is None else abs(craw - mmean), prod_to_m=None if mmean is None else abs(cprod - mmean),
        served_to_m=None if mmean is None else abs(smean - mmean),
        eraw_s=craw - T, eprod_s=cprod - T, emk_s=None if mmean is None else mmean - T,
        paired=mmean is not None))
E = []
for (st, day, t, era), v in env_cells.items():
    d = {"st": st, "day": day, "tier": t, "era": era, "n_env": len(v)}
    for k in ("raw", "prod", "const", "half", "rsign", "served", "num", "den", "shift", "eraw_s", "eprod_s"):
        d[k] = float(np.mean([x[k] for x in v]))
    pv = [x for x in v if x["paired"]]
    d["paired"] = bool(pv)
    if pv:
        for k in ("mk", "raw_to_m", "prod_to_m", "served_to_m", "emk_s"):
            d[k] = float(np.mean([x[k] for x in pv]))
        for k in ("raw", "prod", "served", "rsign"):
            d[k + "_p"] = float(np.mean([x[k] for x in pv]))
    E.append(d)


def pool_block(sub, label):
    keys = [(z["st"], z["day"]) for z in sub]
    o = {"n_station_days": len(set(keys)), "n_cells": len(sub),
         "mae_raw": float(np.mean([z["raw"] for z in sub])), "mae_prod": float(np.mean([z["prod"] for z in sub])),
         "mae_served": float(np.mean([z["served"] for z in sub])), "mean_abs_shift": float(np.mean([abs(z["shift"]) for z in sub]))}
    for arm in ("prod", "const", "half", "rsign"):
        o["d_" + arm] = boot(keys, [z[arm] - z["raw"] for z in sub])
    o["kstar"] = kstar(keys, [z["num"] for z in sub], [z["den"] for z in sub])
    o["bias_raw"] = float(np.mean([z["eraw_s"] for z in sub])); o["bias_prod"] = float(np.mean([z["eprod_s"] for z in sub]))
    ps = [z for z in sub if z["paired"]]
    if ps:
        pk = [(z["st"], z["day"]) for z in ps]
        o["paired_n_station_days"] = len(set(pk))
        o["paired_mae"] = {k: float(np.mean([z[k] for z in ps])) for k in ("raw_p", "prod_p", "served_p", "mk")}
        o["gap_to_market_truth_raw"] = boot(pk, [z["raw_p"] - z["mk"] for z in ps])
        o["gap_to_market_truth_prod"] = boot(pk, [z["prod_p"] - z["mk"] for z in ps])
        o["gap_to_market_truth_served"] = boot(pk, [z["served_p"] - z["mk"] for z in ps])
        o["dist_to_market_raw"] = float(np.mean([z["raw_to_m"] for z in ps]))
        o["dist_to_market_prod"] = float(np.mean([z["prod_to_m"] for z in ps]))
        o["dist_to_market_served"] = float(np.mean([z["served_to_m"] for z in ps]))
        o["d_dist_to_market_prod_minus_raw"] = boot(pk, [z["prod_to_m"] - z["raw_to_m"] for z in ps])
        o["market_bias"] = float(np.mean([z["emk_s"] for z in ps]))
    pm = o.get("paired_mae", {})
    print(f"{label:26s} n_sd={o['n_station_days']:4d} MAE raw {o['mae_raw']:.3f} prod {o['mae_prod']:.3f} served {o['mae_served']:.3f} |shift| {o['mean_abs_shift']:.2f} "
          f"prod-raw {fmt(o['d_prod'])} rsign-raw {fmt(o['d_rsign'])} k* {fmt(o['kstar'])}")
    if ps:
        print(f"{'':26s} paired n_sd={o['paired_n_station_days']}: MAE raw {pm['raw_p']:.3f} prod {pm['prod_p']:.3f} served {pm['served_p']:.3f} market {pm['mk']:.3f} | "
              f"gap to mkt raw {fmt(o['gap_to_market_truth_raw'])} prod {fmt(o['gap_to_market_truth_prod'])} | dist to mkt raw {o['dist_to_market_raw']:.3f} prod {o['dist_to_market_prod']:.3f} served {o['dist_to_market_served']:.3f} "
              f"d {fmt(o['d_dist_to_market_prod_minus_raw'])}")
    return o


print("\n---- pooled centre (lineage weights)")
RES["pooled"] = {}
for era in ("pool", "pre", "all"):
    sub = [z for z in E if z["tier"] in LONG and (era == "all" or z["era"] == era)]
    RES["pooled"][f"{era}|>=12h"] = pool_block(sub, f"{era} >=12h")
for t in ("0-12",) + LONG:
    sub = [z for z in E if z["tier"] == t and z["era"] == "pool"]
    RES["pooled"][f"pool|{t}"] = pool_block(sub, f"pool {t}")
json.dump(RES, open("/Users/xiong/weather/docs/research/p0_center_source_20260922/results_centre.json", "w"), indent=1, default=float)
pickle.dump(dict(C=C, E=E), open(SP + "csrc_cells.pkl", "wb"))
