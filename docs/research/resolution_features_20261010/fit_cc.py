#!/usr/bin/env python3
"""PROTOCOL.md deviation 1: complete-case comparison per group (B0 refitted on the
same decisions) and a one-feature-at-a-time screen (candidates only)."""
import importlib.util, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
src = (HERE / "fit.py").read_text()
src = src[: src.index("result = {}")]          # reuse the definitions, not the run
ns = {"__file__": str(HERE / "fit.py"), "__name__": "fit_defs"}
exec(compile(src, "fit.py", "exec"), ns)
rows, GROUPS, walk_forward, bll, murphy, block_boot = (ns[k] for k in ("rows", "GROUPS", "walk_forward", "bll", "murphy", "block_boot"))
base, day_terms, bucket_terms, BONF, SEED = ns["base"], ns["day_terms"], ns["bucket_terms"], ns["BONF"], ns["SEED"]

NEED = {
    "G2 模型与市场相对位置": (["z", "is_model_mode"], ["dmu", "adm", "dmode", "meanbucket_eq_M"]),
    "G3 历史温度": (["d_last", "d_last3"], ["last_lag", "model_err", "anom30"]),
    "G4 云雨辐射": ([], ["rad_z", "cloud", "precip", "wind"]),
    "G5 入座权重": ([], ["neff", "nseat", "disp", "wmax", "ai", "agree_sgn"]),
    "G6 时效与盘口": ([], ["hours", "spread", "snap_age", "midsum_dev"]),
    "G1 模型概率": ([], []),
}

def complete(r, g):
    b, d = NEED[g]
    return all(r["bucket"].get(k) is not None for k in b) and all(r["day"].get(k) is not None for k in d)

def compare(sub, builder):
    pb, pg = walk_forward(sub, GROUPS["B0"]), walk_forward(sub, builder)
    ok = ~np.isnan(pb) & ~np.isnan(pg)
    y = np.array([r["y"] for r in sub]); dec = np.array([r["dec"] for r in sub]); dd = np.array([r["d"] for r in sub])
    if ok.sum() == 0:
        return None
    diff = bll(pg[ok], y[ok]) - bll(pb[ok], y[ok])
    per = defaultdict(float); dday = {}
    for k, v, day in zip(dec[ok], diff, dd[ok]):
        per[k] += v; dday[k] = day
    keys = sorted(per); vals = [per[k] for k in keys]; days_k = [dday[k] for k in keys]
    tdays = sorted(set(days_k)); half = tdays[len(tdays) // 2]
    lo, hi, p_one = block_boot(days_k, vals, np.random.default_rng(SEED))
    h1 = float(np.mean([v for v, d in zip(vals, days_k) if d < half])); h2 = float(np.mean([v for v, d in zip(vals, days_k) if d >= half]))
    relb, resb = murphy(pb[ok], y[ok]); relg, resg = murphy(pg[ok], y[ok])
    verdict = "有" if (hi < 0 and h1 < 0 and h2 < 0 and p_one < BONF) else ("苗头" if hi < 0 and h1 < 0 and h2 < 0 else "没有")
    return {"decisions": len(keys), "test_days": [tdays[0], tdays[-1], len(tdays)], "mean": float(np.mean(vals)), "ci95": [lo, hi],
            "halves": [h1, h2], "p_one_sided": p_one, "RES_B0": resb, "RES_g": resg, "REL_B0": relb, "REL_g": relg, "verdict": verdict}

out = {"complete_case": {}, "single_feature_screen": {}}
for fam in ("P0>36h", "P0-M", "P1"):
    out["complete_case"][fam] = {}
    for g in NEED:
        if g == "G5 入座权重" and fam != "P0>36h":
            continue
        sub = [r for r in rows if r["fam"] == fam and complete(r, g)]
        if len({r["d"] for r in sub}) < 16:
            out["complete_case"][fam][g] = {"state": "样本不足", "event_days": len({r["d"] for r in sub})}
            continue
        out["complete_case"][fam][g] = compare(sub, GROUPS[g])
    # single features (candidates only)
    out["single_feature_screen"][fam] = {}
    feats = [("bucket", k) for k in ("z", "is_model_mode", "d_last", "d_last3")] + \
            [("day", k) for k in ("dmu", "adm", "dmode", "meanbucket_eq_M", "last_lag", "model_err", "anom30", "rad_z", "cloud",
                                  "precip", "wind", "neff", "nseat", "disp", "wmax", "ai", "agree_sgn", "hours", "spread", "snap_age", "midsum_dev")]
    feats = [("p", "logit_p")] + feats
    for kind, k in feats:
        if kind == "p":
            sub = [r for r in rows if r["fam"] == fam]; builder = GROUPS["G1 模型概率"]
        elif kind == "bucket":
            sub = [r for r in rows if r["fam"] == fam and r["bucket"].get(k) is not None]
            builder = (lambda kk: lambda r: base(r) + [float(r["bucket"][kk]), float(r["bucket"][kk]) * r["pos"]])(k)
        else:
            sub = [r for r in rows if r["fam"] == fam and r["day"].get(k) is not None]
            builder = (lambda kk: lambda r: base(r) + [float(r["day"][kk]) * r["pos"], float(r["day"][kk]) * (1.0 if r["pos"] == 0 else 0.0)])(k)
        if len({r["d"] for r in sub}) < 16:
            continue
        res = compare(sub, builder)
        if res:
            out["single_feature_screen"][fam][k] = res
(HERE / "results_cc.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
for fam, gs in out["complete_case"].items():
    print(f"== 完整样本 {fam}")
    for g, r in gs.items():
        if "state" in r:
            print(f"   {g}: {r['state']}"); continue
        print(f"   {g:12s} {r['decisions']:4d}决策 {r['test_days'][2]:2d}检验日 Δ {r['mean']:+.4f} [{r['ci95'][0]:+.4f},{r['ci95'][1]:+.4f}] 两半 {r['halves'][0]:+.4f}/{r['halves'][1]:+.4f} RES {r['RES_B0']*1e3:.2f}→{r['RES_g']*1e3:.2f}e-3 → {r['verdict']}")
for fam, fs in out["single_feature_screen"].items():
    best = sorted(fs.items(), key=lambda kv: kv[1]["mean"])[:6]
    print(f"== 单特征筛查 {fam}（最好的 6 个，候选）")
    for k, r in best:
        print(f"   {k:16s} {r['decisions']:4d}决策 Δ {r['mean']:+.4f} [{r['ci95'][0]:+.4f},{r['ci95'][1]:+.4f}] 两半 {r['halves'][0]:+.4f}/{r['halves'][1]:+.4f} p={r['p_one_sided']:.3f} → {r['verdict']}")
