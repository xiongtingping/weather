"""Diagnostic: where does Lens B's small skill-weight gain go in Lens A?
Same Lens A envelopes and weights, but scored as the plain linear pool of the
served per-system ladder vectors (no bridge, no downstream layers)."""
import pickle, statistics as S, sys
from collections import defaultdict
sys.path.insert(0, "/Users/xiong/weather/docs/research/fusion_weights_20260922")
sys.path.insert(0, "/Users/xiong/weather")
from stats_lib import order, rps, boot_day, fmt
from bayes_basket_target.native_ensemble_p0 import _bucket_for_value
SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw"
ok = pickle.load(open(f"{SP}/lensa_scored.pkl", "rb"))
ARMS = ["prod", "equal", "perm", "prodrule60", "fleet_mse_7", "fleet_mse_14", "st_mse_3", "st_mse_7", "st_mse_14",
        "stlead_eb14", "st_mse7_shr", "skillperm", "oracle"]
for base in ("equal",):
    T = defaultdict(lambda: defaultdict(list)); F = defaultdict(lambda: defaultdict(list))
    for r in ok:
        iv = r["intervals"]; ordk = order(iv); tb = _bucket_for_value(r["truth"], iv)
        for a in ARMS:
            w = r["weights"][a]
            mix = {b: sum(w[s] * r["sp"][s].get(b, 0.0) for s in w) for b in ordk}
            t = sum(mix.values()); mix = {b: v / t for b, v in mix.items()}
            T[(r["station"], r["day"])][a].append(rps(mix, ordk, tb))
            F[(r["station"], r["day"])][a].append(r["rps"][a])
    T = {k: {a: S.mean(v) for a, v in d.items()} for k, d in T.items()}
    F = {k: {a: S.mean(v) for a, v in d.items()} for k, d in F.items()}
    print(f"plain pool of served per-system ladder vectors vs full compile; base={base}; station-days {len(T)}")
    print(f"  mean RPS equal: plain {S.mean(v['equal'] for v in T.values()):.4f}  full {S.mean(v['equal'] for v in F.values()):.4f}")
    for a in ARMS:
        if a == base: continue
        for lab, tab in (("plain", T), ("full ", F)):
            bd = defaultdict(list)
            for (st, d), v in tab.items(): bd[d].append(v[a] - v[base])
            m, lo, hi = boot_day(bd)
            print(f"  {a:14s} {lab} dRPS {fmt(m, lo, hi)}")
