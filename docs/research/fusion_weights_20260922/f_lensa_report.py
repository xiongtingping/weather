"""Lens A report: served-ladder RPS and zero-bucket-free NLL per station-day,
arm - prod and arm - equal, event-day bootstrap (station bootstrap beside it).

Samples:
  A2 "等权快照": first envelope of each (station, day, lead); a station-day's
     score is the plain mean over its lead buckets.
  A1 "首决策": the station-day's first post-pool envelope only.
"""
import glob, json, pickle, statistics as S, sys
from collections import defaultdict, Counter
sys.path.insert(0, "/Users/xiong/weather/docs/research/fusion_weights_20260922")
sys.path.insert(0, "/Users/xiong/weather")
from stats_lib import order, rps, nll, boot_day, boot_station, fmt
from bayes_basket_target.native_ensemble_p0 import _bucket_for_value
import arms_lib as A

SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw"
OUTJ = sys.argv[1] if len(sys.argv) > 1 else None
rows, errs = [], Counter()
for f in sorted(glob.glob(f"{SP}/lensa_*of6.pkl")):
    d = pickle.load(open(f, "rb"))
    rows += d["rows"]
    errs.update(d["errs"])
ARMS = [a for a in A.ARMS_A]
ok = [r for r in rows if all(a in r["prob"] for a in ARMS)]
print("rows", len(rows), "complete", len(ok), "errors", dict(errs))
out = {"rows": len(rows), "complete": len(ok), "errors": dict(errs)}

# fidelity of the prod replay to the served vector (downstream code moved since serving)
fid = [max(abs(r["prob"]["prod"].get(b, 0) - r["served"].get(b, 0)) for b in r["served"]) for r in ok]
fid.sort()
print("prod replay vs served max|dp|: median %.4f p90 %.4f max %.4f; <1e-6 %d/%d" % (
    fid[len(fid) // 2], fid[int(.9 * len(fid))], fid[-1], sum(x < 1e-6 for x in fid), len(fid)))
out["fidelity"] = dict(median=fid[len(fid) // 2], p90=fid[int(.9 * len(fid))], max=fid[-1])

for r in ok:
    iv = r["intervals"]
    ordk = order(iv)
    tb = _bucket_for_value(r["truth"], iv)
    r["rps"] = {a: rps(r["prob"][a], ordk, tb) for a in ARMS}
    r["pt"] = {a: float(r["prob"][a].get(tb, 0.0)) for a in ARMS}
    r["rps"]["served"] = rps(r["served"], ordk, tb)


def sd_table(sub, key):
    sd = defaultdict(lambda: defaultdict(list))
    for r in sub:
        for a in ARMS:
            sd[(r["station"], r["day"])][a].append(key(r, a))
    return {k: {a: S.mean(v) for a, v in d.items()} for k, d in sd.items()}


def compare(sub, base, label):
    T = sd_table(sub, lambda r, a: r["rps"][a])
    nz = [r for r in sub if min(r["pt"].values()) >= 1e-4]
    N = sd_table(nz, lambda r, a: nll(r["pt"][a]))
    mb = S.mean(v[base] for v in T.values())
    print(f"\n=== Lens A {label}: envelopes {len(sub)}, station-days {len(T)}, days {len({k[1] for k in T})}, "
          f"base={base} mean RPS {mb:.4f}; NLL envelopes kept {len(nz)}/{len(sub)}")
    res = {}
    for a in ARMS:
        if a == base:
            continue
        bd, bs, nd = defaultdict(list), defaultdict(list), defaultdict(list)
        for (st, d), v in T.items():
            bd[d].append(v[a] - v[base]); bs[st].append(v[a] - v[base])
        for (st, d), v in N.items():
            nd[d].append(v[a] - v[base])
        m, lo, hi = boot_day(bd)
        _, slo, shi = boot_station(bs)
        nm, nlo, nhi = boot_day(nd)
        win = sum(1 for v in T.values() if v[a] < v[base] - 1e-12)
        los = sum(1 for v in T.values() if v[a] > v[base] + 1e-12)
        dw = sum(1 for xs in bd.values() if sum(xs) < 0)
        print(f"  {a:16s} dRPS {fmt(m, lo, hi)} ({m / mb * 100:+.2f}%)  st-boot [{slo:+.4f},{shi:+.4f}]  "
              f"sd better/worse {win}/{los}  days better {dw}/{len(bd)}  dNLL(no-zero) {fmt(nm, nlo, nhi)}")
        res[a] = dict(d_rps=m, lo=lo, hi=hi, rel_pct=m / mb * 100, st_lo=slo, st_hi=shi, sd_better=win,
                      sd_worse=los, days_better=dw, days=len(bd), d_nll=nm, nll_lo=nlo, nll_hi=nhi, base_mean=mb)
    return res


out["A2_vs_prod"] = compare(ok, "prod", "A2 等权快照 (first per lead)")
out["A2_vs_equal"] = compare(ok, "equal", "A2 等权快照, base equal")
first = [r for r in ok if r["first_of_day"]]
out["A1_vs_prod"] = compare(first, "prod", "A1 首决策")
out["A1_vs_equal"] = compare(first, "equal", "A1 首决策, base equal")
out["by_lead"] = {}
for lead in ("h00_12", "h12_24", "h24_48"):
    out["by_lead"][lead] = compare([r for r in ok if r["lead"] == lead], "prod", f"A2 lead {lead}")
out["four_only"] = compare([r for r in ok if r["n"] == 4], "prod", "A2 four-system envelopes")
# weight dispersion and parameter choices
print("\n=== weight dispersion on Lens A (mean max w, TV from equal)")
disp = {}
for a in ARMS:
    mx = [max(r["weights"][a].values()) for r in ok]
    tv = [sum(abs(r["weights"][a][s] - 1 / r["n"]) for s in r["weights"][a]) / 2 for r in ok]
    fb = sum(1 for r in ok if r["fb"].get(a)) / len(ok)
    disp[a] = dict(max_w=S.mean(mx), tv=S.mean(tv), fallback=fb)
    print(f"  {a:16s} max_w {S.mean(mx):.3f} TV {S.mean(tv):.3f} fallback {fb:.3f}")
out["dispersion"] = disp
print("EB k chosen (7,14):", Counter(r["k_eb"] for r in ok).most_common(6))
print("kappa chosen (st_mse7_shr, stlead_eb14_shr):", Counter(tuple(r["kappa"]) for r in ok).most_common(6))
print("n systems:", Counter(r["n"] for r in ok))
out["k_eb"] = [list(map(str, k)) + [v] for k, v in Counter(r["k_eb"] for r in ok).most_common()]
out["kappa"] = [list(map(str, k)) + [v] for k, v in Counter(tuple(r["kappa"]) for r in ok).most_common()]
if OUTJ:
    json.dump(out, open(OUTJ, "w"), indent=1, default=float)
pickle.dump(ok, open(f"{SP}/lensa_scored.pkl", "wb"))
