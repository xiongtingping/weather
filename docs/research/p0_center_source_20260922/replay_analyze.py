"""P0 centre source, step 4: score the replay arms (RPS vs truth, gap to market),
and split each arm's gap to market into centre and shape with the audit's own
rule: apply_center_width_calibration(lambda=1) moves the model distribution to
the market mean (|delta| <= 1.5C, as the audit ran it); what that removes is
the centre part, what remains is the shape part."""
import collections, json, pickle, sys
import numpy as np
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.center_width_calibration import served_bucket_axis, apply_center_width_calibration
from bayes_basket_target.delta_v2_settlement_label import _winning_bucket

SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/"
sel = {r["eid"]: r for r in pickle.load(open(SP + "csrc_replay_sel.pkl", "rb"))}
rep = pickle.load(open(SP + "csrc_replay_0of1.pkl", "rb"))
rng = np.random.default_rng(99)
ARMS = ["prod", "raw", "flip", "rsA", "rsB"]


def rps(p, order, tb):
    k = order.index(tb); cp = 0.0; s = 0.0
    for i in range(len(order) - 1):
        cp += p[order[i]]; s += (cp - (1.0 if i >= k else 0.0)) ** 2
    return s


def mean_on(p, order, cen):
    return sum(p[k] * c for k, c in zip(order, cen))


def to_mean(p, order, cen, target):
    d = max(-1.5, min(1.5, target - mean_on(p, order, cen)))
    return apply_center_width_calibration(p, delta_c=d, width_lambda=1.0, ordered_keys=order, key_centers_c=cen)[0]


def boot(keys, vals, n=4000):
    g = collections.defaultdict(list)
    for k, v in zip(keys, vals): g[k].append(v)
    G = [np.array(v) for v in g.values()]
    sums = np.array([x.sum() for x in G]); cnt = np.array([len(x) for x in G])
    idx = rng.integers(0, len(G), (n, len(G)))
    b = sums[idx].sum(1) / cnt[idx].sum(1)
    return [float(np.mean(vals)), float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))]


def f(t): return f"{t[0]:+.4f} [{t[1]:+.4f},{t[2]:+.4f}]"


fid = []; R = []; ladder_mismatch = 0
for x in rep:
    s = sel[x["eid"]]
    if x["err"] or "prod" not in x["arms"]:
        continue
    iv = x["arms"]["prod"]["iv"]
    if set(x["arms"]["prod"]["prob"]) != set(s["served"]) or (iv is not None and iv != s["intervals"]):
        ladder_mismatch += 1
        continue
    order = s["order"]
    fid.append(max(abs(x["arms"]["prod"]["prob"][b] - s["served"][b]) for b in order))
    tb = _winning_bucket(official_tmax_c=s["tmax"], bucket_intervals=s["intervals"])
    cen, _ = served_bucket_axis(s["intervals"], order)
    mk = mean_on(s["mv"], order, cen)
    row = dict(st=s["st"], day=s["day"], tier=s["tier"], mkt=rps(s["mv"], order, tb), served=rps(s["served"], order, tb),
               T=s["tmax"], mk_mean=mk)
    for a in ARMS:
        p = x["arms"][a]["prob"]
        row[a] = rps(p, order, tb)
        row[a + "_atm"] = rps(to_mean(p, order, cen, mk), order, tb)
        row[a + "_mean"] = mean_on(p, order, cen)
    row["rs"] = 0.5 * (row["rsA"] + row["rsB"]); row["rs_atm"] = 0.5 * (row["rsA_atm"] + row["rsB_atm"])
    row["served_atm"] = rps(to_mean(s["served"], order, cen, mk), order, tb)
    R.append(row)
fid = np.array(fid)
print(f"rows {len(R)} of {len(rep)}; ladder/support mismatch {ladder_mismatch}; fidelity prod vs served max|dp|: median {np.median(fid):.2e} p95 {np.percentile(fid, 95):.2e} max {fid.max():.2e}; exact(<1e-9) {np.mean(fid < 1e-9):.1%}")
OUT = {"n_rows": len(R), "n_station_days": len({(r['st'], r['day']) for r in R}), "ladder_mismatch": ladder_mismatch,
       "fidelity_max_abs_dp": {"median": float(np.median(fid)), "p95": float(np.percentile(fid, 95)), "max": float(fid.max()),
                               "share_exact": float(np.mean(fid < 1e-9))}}


def sdavg(sub, fn):
    g = collections.defaultdict(list)
    for r in sub: g[(r["st"], r["day"])].append(fn(r))
    ks = sorted(g); return ks, [float(np.mean(g[k])) for k in ks]


def block(sub, label):
    o = {"n_station_days": len({(r['st'], r['day']) for r in sub}), "n_rows": len(sub)}
    for lab, fn in [("served_minus_mkt", lambda r: r["served"] - r["mkt"]),
                    ("served_atm_minus_mkt", lambda r: r["served_atm"] - r["mkt"]),
                    ("prod_minus_mkt", lambda r: r["prod"] - r["mkt"]),
                    ("raw_minus_mkt", lambda r: r["raw"] - r["mkt"]),
                    ("rs_minus_mkt", lambda r: r["rs"] - r["mkt"]),
                    ("flip_minus_mkt", lambda r: r["flip"] - r["mkt"]),
                    ("prod_minus_raw", lambda r: r["prod"] - r["raw"]),
                    ("rs_minus_raw", lambda r: r["rs"] - r["raw"]),
                    ("prod_atm_minus_mkt", lambda r: r["prod_atm"] - r["mkt"]),
                    ("raw_atm_minus_mkt", lambda r: r["raw_atm"] - r["mkt"]),
                    ("prod_atm_minus_raw_atm", lambda r: r["prod_atm"] - r["raw_atm"])]:
        k, v = sdavg(sub, fn); o[lab] = boot(k, v)
    o["centre_share_served"] = 1 - o["served_atm_minus_mkt"][0] / o["served_minus_mkt"][0]
    o["centre_share_prod"] = 1 - o["prod_atm_minus_mkt"][0] / o["prod_minus_mkt"][0]
    o["centre_share_raw"] = 1 - o["raw_atm_minus_mkt"][0] / o["raw_minus_mkt"][0]
    o["correction_closes_share_of_raw_gap"] = 1 - o["prod_minus_mkt"][0] / o["raw_minus_mkt"][0]
    o["mae_centre"] = {a: float(np.mean([abs(r[a + "_mean"] - r["T"]) for r in sub])) for a in ("prod", "raw", "flip")}
    o["mae_centre"]["market"] = float(np.mean([abs(r["mk_mean"] - r["T"]) for r in sub]))
    print(f"{label:10s} SERVED gap {f(o['served_minus_mkt'])} at market mean {f(o['served_atm_minus_mkt'])} centre share {o['centre_share_served']:.1%}")
    print(f"{label:10s} n_sd={o['n_station_days']:3d} gap to market: prod {f(o['prod_minus_mkt'])} raw {f(o['raw_minus_mkt'])} rsign {f(o['rs_minus_mkt'])} flip {f(o['flip_minus_mkt'])}")
    print(f"{'':10s} prod-raw {f(o['prod_minus_raw'])} rsign-raw {f(o['rs_minus_raw'])} | at market mean: prod {f(o['prod_atm_minus_mkt'])} raw {f(o['raw_atm_minus_mkt'])} "
          f"| centre share prod {o['centre_share_prod']:.1%} raw {o['centre_share_raw']:.1%} | correction closes {o['correction_closes_share_of_raw_gap']:.1%} of raw gap")
    print(f"{'':10s} served-mean MAE: prod {o['mae_centre']['prod']:.3f} raw {o['mae_centre']['raw']:.3f} flip {o['mae_centre']['flip']:.3f} market {o['mae_centre']['market']:.3f}")
    return o


OUT["all_ge12h"] = block(R, ">=12h")
for t in ("12-24", "24-36", "36-48", "48+"):
    OUT[t] = block([r for r in R if r["tier"] == t], t)
# per station: raw vs prod RPS, within-station
g = collections.defaultdict(list)
for r in R: g[r["st"]].append(r["prod"] - r["raw"])
st = {s: float(np.mean(v)) for s, v in g.items()}
OUT["stations_prod_better_than_raw"] = [sum(v < 0 for v in st.values()), len(st)]
OUT["stations_worst"] = sorted(st.items(), key=lambda kv: -kv[1])[:6]
print("stations where prod RPS beats raw:", OUT["stations_prod_better_than_raw"], "worst:", [(s, round(v, 3)) for s, v in OUT["stations_worst"]])
json.dump(OUT, open("/Users/xiong/weather/docs/research/p0_center_source_20260922/results_replay.json", "w"), indent=1)
