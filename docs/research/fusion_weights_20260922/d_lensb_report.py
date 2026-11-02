"""Lens B report: arm - equal (and arm - prodrule60) per station-day, event-day bootstrap."""
import json, math, pickle, statistics as S, sys
from collections import defaultdict, Counter
sys.path.insert(0, "/Users/xiong/weather/docs/research/fusion_weights_20260922")
from stats_lib import boot_day, boot_station, fmt

IN = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw/lensb.pkl"
OUTJ = sys.argv[1] if len(sys.argv) > 1 else None
D = pickle.load(open(IN, "rb"))
cells = D["cells"]
ARMS = ["equal", "prodrule60",
        "fleet_mse_3", "fleet_mse_5", "fleet_mse_7", "fleet_mse_14", "fleet_mse_30", "fleet_mse_60",
        "fleet_crps_3", "fleet_crps_7", "fleet_crps_14",
        "st_mse_3", "st_mse_5", "st_mse_7", "st_mse_14", "st_mse_30", "st_mse_60", "st_crps_7", "st_crps_14",
        "stlead_eb7", "stlead_eb14", "st_mse7_shr", "stlead_eb14_shr",
        "skillperm", "oracle"]
FROM = sys.argv[2] if len(sys.argv) > 2 else "2026-08-24"
cells = [c for c in cells if c["d"] >= FROM]
out = {"n_cells": len(cells), "from": FROM}


def station_day_table(cells, arms, idx):
    sd = defaultdict(lambda: defaultdict(list))
    for c in cells:
        for a in arms:
            sd[(c["st"], c["d"])][a].append(c["scores"][a][idx])
    return {k: {a: sum(v) / len(v) for a, v in d.items()} for k, d in sd.items()}


def compare(cells, base, label, lead=None):
    sub = [c for c in cells if lead is None or c["lead"] == lead]
    # zero-bucket-free NLL: drop cells where any arm puts <1e-4 on the truth bucket
    nz = [c for c in sub if min(c["scores"][a][2] for a in ARMS) >= 1e-4]
    T = station_day_table(sub, ARMS, 0)
    N = station_day_table(nz, ARMS, 1)
    res = {}
    print(f"\n=== Lens B {label}: cells {len(sub)}, station-days {len(T)}, days {len({k[1] for k in T})}, "
          f"base={base} mean CRPS {S.mean(v[base] for v in T.values()):.4f}; NLL cells kept {len(nz)}/{len(sub)}")
    for a in ARMS:
        if a == base:
            continue
        bd = defaultdict(list); bs = defaultdict(list)
        for (st, d), v in T.items():
            bd[d].append(v[a] - v[base]); bs[st].append(v[a] - v[base])
        m, lo, hi = boot_day(bd)
        _, slo, shi = boot_station(bs)
        nd = defaultdict(list)
        for (st, d), v in N.items():
            nd[d].append(v[a] - v[base])
        nm, nlo, nhi = boot_day(nd)
        win = sum(1 for v in T.values() if v[a] < v[base] - 1e-12)
        los = sum(1 for v in T.values() if v[a] > v[base] + 1e-12)
        dw = sum(1 for d, xs in bd.items() if sum(xs) < 0)
        rel = m / S.mean(v[base] for v in T.values()) * 100
        print(f"  {a:18s} dCRPS {fmt(m, lo, hi)} ({rel:+.2f}%)  st-boot [{slo:+.4f},{shi:+.4f}]  "
              f"sd better/worse {win}/{los}  days better {dw}/{len(bd)}  dNLL(no-zero) {fmt(nm, nlo, nhi)}")
        res[a] = dict(d_crps=m, lo=lo, hi=hi, rel_pct=rel, st_lo=slo, st_hi=shi, sd_better=win, sd_worse=los,
                      days_better=dw, days=len(bd), d_nll=nm, nll_lo=nlo, nll_hi=nhi)
    return res


out["vs_equal"] = compare(cells, "equal", "all leads")
out["vs_prodrule60"] = compare(cells, "prodrule60", "all leads, base prodrule60")
out["by_lead"] = {}
for lead in ("h00_12", "h12_24", "h24_48"):
    out["by_lead"][lead] = compare(cells, "equal", f"lead {lead}", lead)

# weight dispersion + fallbacks + chosen parameters
print("\n=== weight dispersion (mean max weight / mean |w - equal|), fallback rate")
disp = {}
for a in ARMS:
    mx = [max(c["weights"][a].values()) for c in cells]
    dev = [sum(abs(c["weights"][a][s] - 1 / len(c["systems"])) for s in c["systems"]) / 2 for c in cells]
    fbr = sum(1 for c in cells if c["fallback"].get(a)) / len(cells)
    disp[a] = dict(mean_max_w=S.mean(mx), mean_tv_from_equal=S.mean(dev), fallback=fbr)
    print(f"  {a:18s} max_w {S.mean(mx):.3f}  TV-from-equal {S.mean(dev):.3f}  fallback {fbr:.3f}")
out["dispersion"] = disp
ch = defaultdict(Counter)
for c in cells:
    for k, v in c.get("chosen", {}).items():
        ch[k][v] += 1
print("\n=== rolling choices:")
for k, v in ch.items():
    print(" ", k, dict(v.most_common()))
out["chosen"] = {k: dict(v) for k, v in ch.items()}
# single-system skill (paired cells with all 4)
four = [c for c in cells if len(c["systems"]) == 4]
print(f"\n=== single-system CRPS on {len(four)} four-system cells:")
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.native_ensemble_oos import _metrics
ss = defaultdict(list)
for c in four:
    for s in c["systems"]:
        ss[s].append(_metrics(c["probs"][s], c["truth"])["crps"])
for s, v in sorted(ss.items(), key=lambda x: S.mean(x[1])):
    print(f"  {s:26s} {S.mean(v):.4f}")
out["single_system_crps_four"] = {s: S.mean(v) for s, v in ss.items()}
# persistence: does the best system over the last 7 days (station) beat others today?
if OUTJ:
    json.dump(out, open(OUTJ, "w"), indent=1, default=float)
print("\n=== persistence on four-system cells: P(arm's top-weighted system is today's closest centre) [random 0.25]; pairwise concordance")
import itertools
pers = {}
for a in ["prodrule60", "fleet_mse_7", "fleet_mse_14", "st_mse_3", "st_mse_7", "st_mse_14", "st_crps_14", "stlead_eb14", "skillperm"]:
    hit = 0; conc = disc = 0
    for c in four:
        w = c["weights"][a]
        err = {s: abs(c["centres"][s] - c["truth"]) for s in c["systems"]}
        top = max(w, key=w.get)
        hit += top == min(err, key=err.get)
        for x, y in itertools.combinations(c["systems"], 2):
            dw = w[x] - w[y]; de = err[x] - err[y]
            if abs(dw) < 1e-9 or abs(de) < 1e-9:
                continue
            if (dw > 0) == (de < 0): conc += 1
            else: disc += 1
    pers[a] = dict(top_hit=hit / len(four), concordance=conc / max(1, conc + disc))
    print(f"  {a:14s} top-is-best {hit/len(four):.3f}  pairwise concordance {conc/max(1,conc+disc):.3f}")
out["persistence_four"] = pers
if OUTJ:
    json.dump(out, open(OUTJ, "w"), indent=1, default=float)
