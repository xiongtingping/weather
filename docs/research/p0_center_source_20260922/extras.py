"""P0 centre source, step 5: recoverable headroom inside the correction, the
gap-heavy stations system by system, and concrete station-day examples."""
import collections, json, pickle
import numpy as np
SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/"
X = pickle.load(open(SP + "csrc_cells.pkl", "rb")); C, E = X["C"], X["E"]
rows = pickle.load(open(SP + "csrc_rows.pkl", "rb"))["rows"]
LONG = ("12-24", "24-36", "36-48", "48+")
OUT = {}
L = [z for z in C if z["tier"] in LONG]
se_prod = np.mean([z["sep"] for z in L]); eb2 = np.mean([z["b"] ** 2 for z in L])
# oracle: replace b by its station x system window mean (upper bound, uses the window itself)
g = collections.defaultdict(list)
for z in L: g[(z["st"], z["sys"])].append(z["b"])
bb = {k: np.mean(v) for k, v in g.items()}
se_bar = np.mean([(z["e0"] + bb[(z["st"], z["sys"])]) ** 2 for z in L])
# global shrink to k*
num = sum(-z["e0"] * z["b"] for z in L); den = sum(z["b"] ** 2 for z in L); k = num / den
se_k = np.mean([(z["e0"] + k * z["b"]) ** 2 for z in L])
# per-system shrink
se_ks = []
for s in set(z["sys"] for z in L):
    sub = [z for z in L if z["sys"] == s]
    ks = sum(-z["e0"] * z["b"] for z in sub) / sum(z["b"] ** 2 for z in sub)
    se_ks += [(z["e0"] + ks * z["b"]) ** 2 for z in sub]
OUT["system_mse"] = dict(prod=float(se_prod), E_b2=float(eb2), oracle_station_mean_shift=float(se_bar),
                         global_kstar=float(k), shrink_global=float(np.mean(se_k)) if False else float(se_k), shrink_per_system=float(np.mean(se_ks)))
print("system-level MSE prod %.3f | oracle constant-per-station-system shift %.3f (%.1f%%) | shrink k*=%.3f %.3f (%.2f%%) | per-system k* %.3f (%.2f%%)" % (
    se_prod, se_bar, 100 * (se_bar / se_prod - 1), k, se_k, 100 * (se_k / se_prod - 1), np.mean(se_ks), 100 * (np.mean(se_ks) / se_prod - 1)))
# pooled level shrink headroom
P = [z for z in E if z["tier"] in LONG and z["era"] == "pool"]
kp = sum(z["num"] for z in P) / sum(z["den"] for z in P)
print("pooled k* %.3f; (1-k)^2 E[shift^2] = %.4f C^2 vs pooled MSE ~ %.3f" % (kp, (1 - kp) ** 2 * np.mean([z["shift"] ** 2 for z in P]), np.mean([z["eprod_s"] ** 2 for z in P])))
OUT["pooled_shrink_headroom_c2"] = float((1 - kp) ** 2 * np.mean([z["shift"] ** 2 for z in P]))
# gap-heavy stations: MAE by system (prod, pool era, paired envelopes, >=12h) vs market
gap = collections.defaultdict(list)
for z in P:
    if z["paired"]: gap[z["st"]].append(z["prod_p"] - z["mk"])
top = sorted(gap, key=lambda s: -np.mean(gap[s]))[:8]
per = collections.defaultdict(lambda: collections.defaultdict(list))
for r in rows:
    if r["era"] != "pool" or r["tier"] not in LONG or r["mv"] is None or r["st"] not in top: continue
    T = r["tmax"]
    for s, x in r["systems"].items():
        per[r["st"]][s].append(abs(x["raw"] + x["b"] - T))
    W = sum(x["w"] for x in r["systems"].values())
    per[r["st"]]["POOLED"].append(abs(sum(x["w"] * (x["raw"] + x["b"]) for x in r["systems"].values()) / W - T))
mk = collections.defaultdict(list)
for z in P:
    if z["paired"] and z["st"] in top: mk[z["st"]].append(z["mk"])
OUT["gap_stations"] = {}
print("gap-heavy stations: corrected MAE by system (envelope mean) vs pooled vs market (cell mean)")
for st in top:
    d = {s[:5]: round(float(np.mean(v)), 2) for s, v in sorted(per[st].items())}
    d["MARKET"] = round(float(np.mean(mk[st])), 2); d["gap"] = round(float(np.mean(gap[st])), 2)
    OUT["gap_stations"][st] = d
    print("  ", st, d)
# fleet: best single system at each station chosen on first half, scored on second half vs pooled
days = sorted(set(z["day"] for z in P)); h1 = set(days[: len(days) // 2])
acc = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(list)))
for r in rows:
    if r["era"] != "pool" or r["tier"] not in LONG: continue
    half = "h1" if r["day"] in h1 else "h2"; T = r["tmax"]
    W = sum(x["w"] for x in r["systems"].values())
    acc[r["st"]]["POOLED"][half].append(abs(sum(x["w"] * (x["raw"] + x["b"]) for x in r["systems"].values()) / W - T))
    for s, x in r["systems"].items():
        acc[r["st"]][s][half].append(abs(x["raw"] + x["b"] - T))
pick, pooled = [], []
for st, d in acc.items():
    cand = {s: np.mean(v["h1"]) for s, v in d.items() if s != "POOLED" and v["h1"] and v["h2"]}
    if not cand or not d["POOLED"]["h2"]: continue
    best = min(cand, key=cand.get)
    pick.append(np.mean(d[best]["h2"])); pooled.append(np.mean(d["POOLED"]["h2"]))
print("per-station best system (picked on first half) scored on second half: MAE %.3f vs pooled %.3f over %d stations" % (np.mean(pick), np.mean(pooled), len(pick)))
OUT["best_system_pick_oos"] = dict(pick=float(np.mean(pick)), pooled=float(np.mean(pooled)), n=len(pick))
json.dump(OUT, open("/Users/xiong/weather/docs/research/p0_center_source_20260922/results_extras.json", "w"), indent=1)

# ---- implementable: smooth the served shift over the station x system's own past served shifts
#      (known at decision time; no refit, no truth read)
Lall = sorted([z for z in C if z["tier"] in LONG], key=lambda z: z["day"])
hist = collections.defaultdict(list)   # (st, sys) -> [(day, b)]
for z in Lall: hist[(z["st"], z["sys"])].append((z["day"], z["b"]))
res = {}
for lab, back in (("today+prev3d", 3), ("today+prev7d", 7), ("expanding", 99)):
    d_se, keys = [], []
    for z in Lall:
        if z["era"] != "pool": continue
        from datetime import date
        dd = date.fromisoformat(z["day"])
        bs = [b for (d, b) in hist[(z["st"], z["sys"])] if 0 <= (dd - date.fromisoformat(d)).days <= back]
        bsm = float(np.mean(bs))
        d_se.append((z["e0"] + bsm) ** 2 - (z["e0"] + z["b"]) ** 2); keys.append((z["st"], z["day"]))
    g2 = collections.defaultdict(list)
    for k_, v in zip(keys, d_se): g2[k_].append(v)
    G = [np.array(v) for v in g2.values()]; sums = np.array([x.sum() for x in G]); cnt = np.array([len(x) for x in G])
    rng = np.random.default_rng(5); idx = rng.integers(0, len(G), (3000, len(G))); bt = sums[idx].sum(1) / cnt[idx].sum(1)
    res[lab] = [float(np.mean(d_se)), float(np.percentile(bt, 2.5)), float(np.percentile(bt, 97.5))]
    print(f"smoothed shift ({lab}) minus prod, system MSE (pool era >=12h): {res[lab][0]:+.4f} [{res[lab][1]:+.4f},{res[lab][2]:+.4f}] C^2")
OUT["smoothed_shift_minus_prod_mse"] = res
json.dump(OUT, open("/Users/xiong/weather/docs/research/p0_center_source_20260922/results_extras.json", "w"), indent=1)
