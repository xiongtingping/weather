"""Jitter report: weight-only centre step and weight stability per arm."""
import glob, json, pickle, statistics as S, sys
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.served_shape import model_center_c

SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw"
OUTJ = sys.argv[1] if len(sys.argv) > 1 else None
sds = []
for f in sorted(glob.glob(f"{SP}/jitter_*of8.pkl")):
    sds += pickle.load(open(f, "rb"))
ARMS = list(sds[0]["rows"][0]["w"])
nenv = sum(len(x["rows"]) for x in sds)
print("station-days", len(sds), "envelopes", nenv)
steps = {a: [] for a in ARMS}
dws = {a: [] for a in ARMS}
changed = {a: 0 for a in ARMS}
worst = {a: (0.0, None) for a in ARMS}
per_sd_max = {a: [] for a in ARMS}
npairs = 0
for x in sds:
    rows = x["rows"]
    mx = {a: 0.0 for a in ARMS}
    for a_, b_ in zip(rows, rows[1:]):
        if a_["systems"] != b_["systems"]:
            continue
        npairs += 1
        ids = list(b_["systems"])
        for a in ARMS:
            wa, wb = a_["w"][a], b_["w"][a]
            step = abs(model_center_c(wb, b_["centres"], ids) - model_center_c(wa, b_["centres"], ids))
            dw = max(abs(wb[s] - wa[s]) for s in ids)
            steps[a].append(step)
            dws[a].append(dw)
            if dw > 1e-9:
                changed[a] += 1
            mx[a] = max(mx[a], step)
            if step > worst[a][0]:
                worst[a] = (step, (x["station"], x["day"], a_["decision"], b_["decision"], a_["lead"], b_["lead"],
                                   {s: round(wa[s], 3) for s in ids}, {s: round(wb[s], 3) for s in ids},
                                   {s: round(b_["centres"][s], 2) for s in ids}))
    for a in ARMS:
        per_sd_max[a].append(mx[a])


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))]


print(f"same-set consecutive pairs: {npairs}")
print(f"{'arm':16s} {'changed%':>8s} {'med|dw|':>8s} {'p90|dw|':>8s} {'max|dw|':>8s} | weight-only centre step C: "
      f"{'mean':>6s} {'p90':>6s} {'p99':>6s} {'max':>6s} {'>0.25':>6s} {'>0.5':>6s} | per-sd max med")
res = {}
for a in ARMS:
    st, dw = steps[a], dws[a]
    ch = [d for d in dw if d > 1e-9]
    r = dict(changed_frac=changed[a] / npairs, dw_median_when_changed=q(ch, .5) if ch else 0.0,
             dw_p90=q(dw, .9), dw_max=max(dw), step_mean=S.mean(st), step_p90=q(st, .9), step_p99=q(st, .99),
             step_max=max(st), gt025=sum(s > .25 for s in st), gt05=sum(s > .5 for s in st),
             per_sd_max_median=S.median(per_sd_max[a]), worst=worst[a][1])
    res[a] = r
    print(f"{a:16s} {100*r['changed_frac']:7.1f}% {r['dw_median_when_changed']:8.3f} {r['dw_p90']:8.3f} {r['dw_max']:8.3f} | "
          f"{r['step_mean']:6.3f} {r['step_p90']:6.3f} {r['step_p99']:6.3f} {r['step_max']:6.3f} {r['gt025']:6d} {r['gt05']:6d} | "
          f"{r['per_sd_max_median']:.3f}")
print("\nworst pair per arm:")
for a in ARMS:
    print(f"  {a:16s} {worst[a][0]:.3f}C  {worst[a][1]}")
# the audit's specimen: KLGA 2026-09-20
print("\nKLGA 2026-09-20, all arms, weight-only steps > 0.05C:")
for x in sds:
    if x["station"] == "KLGA" and x["day"] == "2026-09-20":
        rows = x["rows"]
        for a_, b_ in zip(rows, rows[1:]):
            if a_["systems"] != b_["systems"]:
                continue
            ids = list(b_["systems"])
            line = []
            for a in ("prod", "equal", "prodrule60", "fleet_mse_7", "st_mse_7", "stlead_eb14"):
                s = abs(model_center_c(b_["w"][a], b_["centres"], ids) - model_center_c(a_["w"][a], b_["centres"], ids))
                line.append(f"{a}={s:.2f}")
            if any(float(t.split("=")[1]) > 0.05 for t in line):
                print("  ", a_["decision"][:16], "->", b_["decision"][:16], a_["lead"], "->", b_["lead"], len(ids), "sys", " ".join(line),
                      "| prod w", {s[:5]: round(a_["w"]["prod"][s], 3) for s in ids}, "->", {s[:5]: round(b_["w"]["prod"][s], 3) for s in ids},
                      "| centres", {s[:5]: round(b_["centres"][s], 2) for s in ids})
if OUTJ:
    json.dump(dict(station_days=len(sds), envelopes=nenv, pairs=npairs, arms=res), open(OUTJ, "w"), indent=1, default=str)
