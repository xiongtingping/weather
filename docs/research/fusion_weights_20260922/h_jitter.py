"""Centre jitter and weight stability of every arm over ALL post-pool P0
envelopes of settled station-days (no recompile needed: weights x the served
calibrated system centres from lineage, through production's
``served_shape.model_center_c``).

Weight-only step between consecutive envelopes a -> b of one station-day with
the same seated set:  |centre(w_b, c_b) - centre(w_a, c_b)|  -- how far the
blend temperature moves because the weights moved, holding b's system
centres fixed (the audit's "无信息中心步长").
"""
import pickle, sys, time
from collections import defaultdict
sys.path.insert(0, "/Users/xiong/weather/docs/research/fusion_weights_20260922")
sys.path.insert(0, "/Users/xiong/weather")
import weights_lib as W
import arms_lib as A
from bayes_basket_target.served_shape import model_center_c

SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw"
K, N = int(sys.argv[1]), int(sys.argv[2])
ARMS = ["prod", "equal", "prodrule60", "fleet_mse_3", "fleet_mse_7", "fleet_mse_14", "st_mse_3", "st_mse_7",
        "st_mse_14", "st_mse_30", "stlead_eb7", "stlead_eb14", "st_mse7_shr", "stlead_eb14_shr"]
D = pickle.load(open(f"{SP}/index.pkl", "rb"))
by = defaultdict(list)
for r in D["recs"]:
    if r["weights"] and r["centers"] and set(r["weights"]) <= set(r["centers"]):
        by[(r["station"], r["day"])].append(r)
keys = sorted(by)[K::N]
S = W.Skill()
sel = A.Selector(S.first_commit)
t0 = time.time()
out = []
for i, k in enumerate(keys):
    seq = sorted(by[k], key=lambda r: (r["decision"], r["created"]))
    rows = []
    for r in seq:
        systems = sorted(r["weights"])
        ctx = dict(st=r["station"], d=r["day"], t=r["decision"], systems=systems,
                   family={s: r["families"][s] for s in systems}, caps=r["caps"],
                   leads={s: (r["hour_lead"] or {}).get(s) or r["lead"] for s in systems},
                   key=f'{r["station"]}|{r["day"]}|{r["lead"]}')
        arms, fb = A.all_arms(S, sel, ctx, prod=r["weights"])
        rows.append(dict(decision=r["decision"], lead=r["lead"], systems=tuple(systems),
                         centres={s: r["centers"][s] for s in systems}, selection=r["selection"],
                         w={a: arms[a] for a in ARMS}))
    out.append(dict(station=k[0], day=k[1], rows=rows))
    if i % 20 == 0:
        print(K, i, len(keys), round(time.time() - t0), flush=True)
pickle.dump(out, open(f"{SP}/jitter_{K}of{N}.pkl", "wb"))
print("done", K, len(out), round(time.time() - t0))
