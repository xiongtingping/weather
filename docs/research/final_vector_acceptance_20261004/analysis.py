#!/usr/bin/env python3
"""Final betting vector acceptance, parts A-E of PROTOCOL.md.

    python3 analysis.py A|B|C|D|E [since]

Everything is weighted one per station-day (its decisions share the weight) and
every interval is an event-day cluster bootstrap.
"""
import json
import os
import sys

import numpy as np
from scipy.optimize import minimize

import common as c

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = "2026-09-12"
LATE = "2026-09-26"
FIT_BOOT = 400
P_BINS_A = [0, 0.005, 0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0001]
P_BINS_B = [0, 0.02, 0.1, 0.3, 0.5, 1.0001]


# ---------------------------------------------------------------------------
# Frames.

def flat(rec, key="p", ref="q"):
    """One row a bucket: the vector ``key`` against ``ref``."""
    cols = {n: [] for n in ("i", "k", "p", "q", "y", "dist", "wt", "bid", "ask", "floored")}
    for i, x in enumerate(rec):
        v = x[key]
        if v is None:
            continue
        n = x["k"]
        idx = np.arange(n)
        cols["i"].append(np.full(n, i))
        cols["k"].append(idx)
        cols["p"].append(v)
        cols["q"].append(x[ref])
        cols["y"].append((idx == x["y"]).astype(float))
        cols["dist"].append(np.abs(idx - x["mode"]))
        cols["wt"].append(np.full(n, x["wt"]))
        cols["bid"].append(x["bid"])
        cols["ask"].append(x["ask"])
        fl = np.zeros(n)
        fl[x["floored"]] = 1.0
        cols["floored"].append(fl)
    out = {n: np.concatenate(v) for n, v in cols.items()}
    out["i"] = out["i"].astype(int)
    return out


def codes_of(rec):
    return c.day_codes([x["d"] for x in rec])


def logit(v):
    v = np.clip(v, 1e-6, 1 - 1e-6)
    return np.log(v / (1 - v))


def wlogistic(X, y, w, offset=None, start=None):
    """Weighted logistic regression by Newton steps; returns the coefficients."""
    beta = np.zeros(X.shape[1]) if start is None else start.copy()
    off = 0.0 if offset is None else offset
    for _ in range(40):
        eta = X @ beta + off
        mu = 1.0 / (1.0 + np.exp(-eta))
        grad = X.T @ (w * (y - mu))
        hess = (X * (w * mu * (1 - mu))[:, None]).T @ X + 1e-9 * np.eye(X.shape[1])
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if np.abs(step).max() < 1e-8:
            break
    return beta


def boot_fit(fit, codes, k, n_boot=FIT_BOOT, seed=c.SEED):
    """``fit(multiplier_per_day)`` -> vector; point estimate and percentile interval."""
    est = np.atleast_1d(fit(np.ones(k)))
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(n_boot):
        mult = np.bincount(rng.integers(0, k, size=k), minlength=k).astype(float)
        try:
            draws.append(np.atleast_1d(fit(mult)))
        except np.linalg.LinAlgError:
            continue
    draws = np.array(draws)
    lo, hi = np.nanpercentile(draws, [2.5, 97.5], axis=0)
    return [(float(e), float(a), float(b)) for e, a, b in zip(est, lo, hi)]


# ---------------------------------------------------------------------------
# Log pools on padded ladders.

def padded(rec, key):
    n, kmax = len(rec), max(x["k"] for x in rec)
    out = np.full((n, kmax), np.nan)
    for i, x in enumerate(rec):
        out[i, :x["k"]] = x[key]
    return out


def pool_fit(base, deltas, y, w, start=None):
    """Multiclass log pool: log b = log base + sum_j w_j * delta_j (+const)."""
    mask = ~np.isnan(base)
    L0 = np.where(mask, np.log(np.where(mask, base, 1.0)), -1e9)
    D = [np.where(mask, d, 0.0) for d in deltas]
    rows = np.arange(len(y))

    def nll(theta):
        z = L0 + sum(t * d for t, d in zip(theta, D))
        z = z - z.max(1, keepdims=True)
        lse = np.log(np.exp(z).sum(1))
        b = np.exp(z - lse[:, None])
        val = -(w * (z[rows, y] - lse)).sum()
        grad = np.array([-(w * (d[rows, y] - (b * d).sum(1))).sum() for d in D])
        return val, grad

    res = minimize(nll, np.zeros(len(D)) if start is None else start, jac=True, method="BFGS")
    return res.x


def pooled(base, deltas, theta):
    mask = ~np.isnan(base)
    z = np.where(mask, np.log(np.where(mask, base, 1.0)), -1e9) + sum(
        t * np.where(mask, d, 0.0) for t, d in zip(theta, deltas))
    z = z - z.max(1, keepdims=True)
    b = np.exp(z)
    b = b / b.sum(1, keepdims=True)
    return np.where(mask, b, np.nan)


def single_w(rec, key="p", ref="q"):
    """One mixing coefficient: log b = log q + w log(p/q), with its interval."""
    rec = [x for x in rec if x[key] is not None]
    if len({x["d"] for x in rec}) < 3:
        return None
    P, Q = padded(rec, key), padded(rec, ref)
    with np.errstate(invalid="ignore"):
        D = np.log(P) - np.log(Q)
    y = np.array([x["y"] for x in rec])
    wt = np.array([x["wt"] for x in rec])
    codes, k = codes_of(rec)
    return boot_fit(lambda m: pool_fit(Q, [D], y, wt * m[codes]), codes, k)[0]


def scores(rec, key):
    ls = np.array([np.log(x[key][x["y"]]) for x in rec])
    onehot = [(np.arange(x["k"]) == x["y"]).astype(float) for x in rec]
    br = np.array([((x[key] - o) ** 2).sum() for x, o in zip(rec, onehot)])
    return ls, br


def mean_ci(values, rec, weights=None):
    codes, k = codes_of(rec)
    wt = np.array([x["wt"] for x in rec]) if weights is None else weights
    return c.boot_ratio(wt * values, wt, codes, k)


# ---------------------------------------------------------------------------
# A. Overall calibration.

def part_a(rec, label):
    out = {"sample": label, "decisions": len(rec), "station_days": len({(x["st"], x["d"]) for x in rec}),
           "event_days": len({x["d"] for x in rec})}
    codes_d, k = codes_of(rec)
    print(f"\n== A 总体校准 [{label}]  决策 {out['decisions']}  站日 {out['station_days']}  事件日 {out['event_days']}")
    for name, key in (("模型 p", "p"), ("市场 q", "q")):
        f = flat(rec, key)
        codes = codes_d[f["i"]]
        table = []
        off = 0
        print(f"-- {name}\n   档            桶数   平均预测  命中率   命中-预测 [95%]")
        for lo, hi in zip(P_BINS_A[:-1], P_BINS_A[1:]):
            m = (f["p"] >= lo) & (f["p"] < hi)
            if m.sum() == 0:
                continue
            w = f["wt"][m]
            pm = (w * f["p"][m]).sum() / w.sum()
            hit = (w * f["y"][m]).sum() / w.sum()
            diff = c.boot_ratio(w * (f["y"][m] - f["p"][m]), w, codes[m], k)
            off += int(diff[1] > 0 or diff[2] < 0)
            table.append({"bin": [lo, hi], "n": int(m.sum()), "mean_pred": pm, "hit": hit, "diff": diff})
            print(f"   {lo:5.3f}-{min(hi,1):5.3f}  {int(m.sum()):6d}  {pm:7.4f}  {hit:7.4f}  {c.fmt(diff)}")
        w = f["wt"]
        ybar = (w * f["y"]).sum() / w.sum()
        rel = sum(f["wt"][(f["p"] >= lo) & (f["p"] < hi)].sum() * (t["hit"] - t["mean_pred"]) ** 2
                  for (lo, hi), t in ((tuple(t["bin"]), t) for t in table)) / w.sum()
        res = sum(f["wt"][(f["p"] >= lo) & (f["p"] < hi)].sum() * (t["hit"] - ybar) ** 2
                  for (lo, hi), t in ((tuple(t["bin"]), t) for t in table)) / w.sum()
        X = np.column_stack([np.ones(len(f["p"])), logit(f["p"])])
        line = boot_fit(lambda mlt: wlogistic(X, f["y"], f["wt"] * mlt[codes]), codes, k)
        print(f"   REL {rel:.5f}  RES {res:.5f}  区间不含0的档 {off}/{len(table)}"
              f"  校准线 截距 {c.fmt(line[0],digits=3)} 斜率 {line[1][0]:.3f} [{line[1][1]:.3f},{line[1][2]:.3f}]")
        out[key] = {"table": table, "rel": rel, "res": res, "bins_off": off, "intercept": line[0], "slope": line[1]}
    lsp, brp = scores(rec, "p")
    lsq, brq = scores(rec, "q")
    out["logscore"] = {"p": mean_ci(lsp, rec), "q": mean_ci(lsq, rec), "diff": mean_ci(lsp - lsq, rec)}
    out["brier"] = {"p": mean_ci(brp, rec), "q": mean_ci(brq, rec), "diff": mean_ci(brp - brq, rec)}
    print(f"   LogScore 模型 {out['logscore']['p'][0]:.4f} 市场 {out['logscore']['q'][0]:.4f} 差 {c.fmt(out['logscore']['diff'])}")
    print(f"   Brier    模型 {out['brier']['p'][0]:.4f} 市场 {out['brier']['q'][0]:.4f} 差 {c.fmt(out['brier']['diff'])}")
    return out


# ---------------------------------------------------------------------------
# B. Inside the same predicted probability, by what the market says.

def part_b(rec, label):
    f = flat(rec)
    codes_d, k = codes_of(rec)
    codes = codes_d[f["i"]]
    s = np.log(f["p"] / f["q"])
    sclass = np.where(s > 0.4, 0, np.where(s < -0.4, 2, 1))
    snames = ["市场低得多(模型说便宜)", "相近", "市场高得多(模型说贵)"]
    dclass = np.minimum(f["dist"], 2).astype(int)
    dnames = ["众数桶", "±1档", "≥2档"]
    sd_key = np.array([hash((rec[i]["st"], rec[i]["d"])) for i in f["i"]])
    out = {"sample": label, "cells": [], "regression": {}}
    print(f"\n== B 同一预测概率内按市场条件 [{label}]")
    print("   p档        市场相对模型            档距    站日  平均p   平均q   命中率   命中-p [95%]                 命中-q [95%]")
    for lo, hi in zip(P_BINS_B[:-1], P_BINS_B[1:]):
        for sc in range(3):
            for dc in range(3):
                m = (f["p"] >= lo) & (f["p"] < hi) & (sclass == sc) & (dclass == dc)
                nsd = len(set(sd_key[m]))
                if nsd < 20:
                    continue
                w = f["wt"][m]
                pm, qm, hit = [(w * f[n][m]).sum() / w.sum() for n in ("p", "q", "y")]
                dp = c.boot_ratio(w * (f["y"][m] - f["p"][m]), w, codes[m], k)
                dq = c.boot_ratio(w * (f["y"][m] - f["q"][m]), w, codes[m], k)
                out["cells"].append({"p_bin": [lo, hi], "market": snames[sc], "distance": dnames[dc],
                                     "station_days": nsd, "p": pm, "q": qm, "hit": hit, "hit_minus_p": dp,
                                     "hit_minus_q": dq})
                print(f"   {lo:4.2f}-{min(hi,1):4.2f}  {snames[sc]:<14}  {dnames[dc]:<4}  {nsd:4d}  {pm:6.3f}  {qm:6.3f}  {hit:6.3f}"
                      f"   {c.fmt(dp,digits=3)}   {c.fmt(dq,digits=3)}")
    print("   回归 logit E[Y] = a + b·logit p + c·(logit q − logit p)")
    for dc in (0, 1, 2, None):
        m = np.ones(len(s), bool) if dc is None else dclass == dc
        X = np.column_stack([np.ones(m.sum()), logit(f["p"][m]), logit(f["q"][m]) - logit(f["p"][m])])
        cm, ym, wm = codes[m], f["y"][m], f["wt"][m]
        coef = boot_fit(lambda mlt: wlogistic(X, ym, wm * mlt[cm]), cm, k)
        name = "全部" if dc is None else dnames[dc]
        out["regression"][name] = {"a": coef[0], "b": coef[1], "c": coef[2]}
        print(f"   {name:<4} a {c.fmt(coef[0],digits=3)}  b {coef[1][0]:.3f} [{coef[1][1]:.3f},{coef[1][2]:.3f}]"
              f"  c {coef[2][0]:.3f} [{coef[2][1]:.3f},{coef[2][2]:.3f}]")
    return out


# ---------------------------------------------------------------------------
# C. The four cells, and centre / width / shape.

CELLS4 = [("YES", "近(众数/±1)"), ("YES", "远(≥2)"), ("NO", "近(众数/±1)"), ("NO", "远(≥2)")]


def cell_frame(rec, key="p", ref="q"):
    f = flat(rec, key, ref)
    side = np.sign(f["p"] - f["q"])
    f["side"] = side
    f["cell4"] = np.where(side > 0, 0, 2) + (f["dist"] >= 2)
    f["cell6"] = np.where(side > 0, 0, 3) + np.minimum(f["dist"], 2).astype(int)
    f["live"] = side != 0
    return f


def cell_table(f, codes, k, weight, cells, names, title):
    rows = []
    print(f"   {title}\n   格                 腿数    声称      兑现 [95%]                     夸大 [95%]                     兑现率 [95%]")
    for ci, name in enumerate(names):
        m = f["live"] & (cells == ci)
        if m.sum() == 0:
            continue
        w = weight[m]
        claim = f["side"][m] * (f["p"][m] - f["q"][m])
        real = f["side"][m] * (f["y"][m] - f["q"][m])
        t_claim = c.boot_ratio(w * claim, w, codes[m], k)
        t_real = c.boot_ratio(w * real, w, codes[m], k)
        t_over = c.boot_ratio(w * (claim - real), w, codes[m], k)
        t_rate = c.boot_ratio(w * real, w * claim, codes[m], k)
        rows.append({"cell": name, "legs": int(m.sum()), "claimed": t_claim, "realised": t_real,
                     "overclaim": t_over, "rate": t_rate})
        print(f"   {name:<16} {int(m.sum()):6d}  {t_claim[0]*100:6.2f}¢  {c.fmt(t_real,100,2)}¢  {c.fmt(t_over,100,2)}¢"
              f"  {t_rate[0]:+.2f} [{t_rate[1]:+.2f},{t_rate[2]:+.2f}]")
    return rows


def part_c(rec, label):
    out = {"sample": label}
    codes_d, k = codes_of(rec)
    f = cell_frame(rec)
    codes = codes_d[f["i"]]
    names4 = [f"{a} {b}" for a, b in CELLS4]
    names6 = [f"{a} {b}" for a in ("YES", "NO") for b in ("众数桶", "±1档", "≥2档")]
    print(f"\n== C 四格 [{label}]")
    out["cells_equal"] = cell_table(f, codes, k, f["wt"], f["cell4"], names4, "每条腿等权，对归一中价 q")
    out["cells6_equal"] = cell_table(f, codes, k, f["wt"], f["cell6"], names6, "2×3 明细")
    s_abs = np.abs(np.log(f["p"] / f["q"]))
    out["cells_s"] = cell_table(f, codes, k, f["wt"] * s_abs, f["cell4"], names4, "按 |s| 加权")
    # Tradeable: YES at the ask, NO at 1 - bid, only legs the rule passes.
    rows = []
    print("   可成交口径（YES 对卖一、NO 对 1−买一，仅 p 过线的腿，未扣费）\n   格                 腿数    声称      兑现 [95%]                     兑现率 [95%]")
    for ci, name in enumerate(names4):
        yes = ci < 2
        far = ci % 2 == 1
        price = f["ask"] if yes else f["bid"]
        m = ~np.isnan(price) & ((f["dist"] >= 2) == far)
        m &= (f["p"] > price) if yes else (f["p"] < price)
        if m.sum() == 0:
            continue
        w = f["wt"][m]
        sgn = 1.0 if yes else -1.0
        claim = sgn * (f["p"][m] - price[m])
        real = sgn * (f["y"][m] - price[m])
        t_claim = c.boot_ratio(w * claim, w, codes[m], k)
        t_real = c.boot_ratio(w * real, w, codes[m], k)
        t_rate = c.boot_ratio(w * real, w * claim, codes[m], k)
        rows.append({"cell": name, "legs": int(m.sum()), "claimed": t_claim, "realised": t_real, "rate": t_rate})
        print(f"   {name:<16} {int(m.sum()):6d}  {t_claim[0]*100:6.2f}¢  {c.fmt(t_real,100,2)}¢  {t_rate[0]:+.2f} [{t_rate[1]:+.2f},{t_rate[2]:+.2f}]")
    out["cells_tradeable"] = rows

    # Three components along q -> centre -> width -> p, and the reverse path.
    for tag, a_key, b_key, title in (("forward", "v1", "v2", "正向：市场 → 换成模型中心 → 换成模型宽度 → 模型"),
                                     ("reverse", "r2", "r1", "反向：市场 → 模型形状 → 换成模型宽度 → 模型（先换形状）")):
        P, Q, A, Bv = padded(rec, "p"), padded(rec, "q"), padded(rec, a_key), padded(rec, b_key)
        with np.errstate(invalid="ignore"):
            deltas = [np.log(A) - np.log(Q), np.log(Bv) - np.log(A), np.log(P) - np.log(Bv)]
        y = np.array([x["y"] for x in rec])
        wt = np.array([x["wt"] for x in rec])
        theta = boot_fit(lambda m: pool_fit(Q, deltas, y, wt * m[codes_d]), codes_d, k)
        comp_names = ["中心", "宽度", "形状/尾部"] if tag == "forward" else ["形状/尾部", "宽度", "中心"]
        print(f"   三项兑现率（{title}）")
        for n, t in zip(comp_names, theta):
            print(f"     {n:<6} w = {t[0]:+.3f} [{t[1]:+.3f},{t[2]:+.3f}]")
        th = [t[0] for t in theta]
        steps = [Q, pooled(Q, deltas[:1], th[:1]), pooled(Q, deltas[:2], th[:2]), pooled(Q, deltas, th)]
        chain = [Q, A, Bv, P]
        res = {"weights": dict(zip(comp_names, theta)), "cells": []}
        print("     格                 声称: " + " / ".join(comp_names) + "      兑现(池): " + " / ".join(comp_names) + "   实际兑现  残差")
        for ci, name in enumerate(names4):
            m = f["live"] & (f["cell4"] == ci)
            w = f["wt"][m]
            ii, kk = f["i"][m], f["k"][m]
            sd = f["side"][m]
            claims = [(w * sd * (chain[j + 1][ii, kk] - chain[j][ii, kk])).sum() / w.sum() for j in range(3)]
            reals = [(w * sd * (steps[j + 1][ii, kk] - steps[j][ii, kk])).sum() / w.sum() for j in range(3)]
            actual = (w * sd * (f["y"][m] - f["q"][m])).sum() / w.sum()
            res["cells"].append({"cell": name, "claimed": claims, "realised_pool": reals, "actual": actual})
            print(f"     {name:<16} " + " / ".join(f"{v*100:+6.2f}" for v in claims) + "¢     "
                  + " / ".join(f"{v*100:+6.2f}" for v in reals) + f"¢   {actual*100:+6.2f}¢  {(actual-sum(reals))*100:+5.2f}¢")
        out[f"components_{tag}"] = res
    out["single_w"] = single_w(rec)
    print(f"   单一混合系数 w = {out['single_w'][0]:.3f} [{out['single_w'][1]:.3f},{out['single_w'][2]:.3f}]")

    # Decision-level readings.
    mp = np.array([c.moments(x["p"]) for x in rec])
    mq = np.array([c.moments(x["q"]) for x in rec])
    yi = np.array([x["y"] for x in rec], float)
    wt = np.array([x["wt"] for x in rec])
    gap = mp[:, 0] - mq[:, 0]
    X = np.column_stack([np.ones(len(gap)), gap])

    def wls(mlt):
        w = wt * mlt[codes_d]
        return np.linalg.solve((X * w[:, None]).T @ X, (X * w[:, None]).T @ (yi - mq[:, 0]))

    slope = boot_fit(wls, codes_d, k)
    out["centre"] = {"intercept": slope[0], "slope": slope[1],
                     "mean_abs_gap": float((wt * np.abs(gap)).sum() / wt.sum()),
                     "bias_p": mean_ci(yi - mp[:, 0], rec), "bias_q": mean_ci(yi - mq[:, 0], rec),
                     "mae_p": mean_ci(np.abs(yi - mp[:, 0]), rec), "mae_q": mean_ci(np.abs(yi - mq[:, 0]), rec)}
    print(f"   中心：真值−市场中心 对 模型中心−市场中心 的斜率 {slope[1][0]:.3f} [{slope[1][1]:.3f},{slope[1][2]:.3f}]"
          f"  截距 {c.fmt(slope[0],digits=3)} 档  平均|中心差| {out['centre']['mean_abs_gap']:.3f} 档")
    print(f"         真值−模型中心 {c.fmt(out['centre']['bias_p'],digits=3)}  真值−市场中心 {c.fmt(out['centre']['bias_q'],digits=3)}"
          f"  |误差| 模型 {out['centre']['mae_p'][0]:.3f} 市场 {out['centre']['mae_q'][0]:.3f} 档")
    terc = np.quantile(np.abs(gap), [1 / 3, 2 / 3])
    out["centre_by_gap"] = []
    for name, m in (("小", np.abs(gap) <= terc[0]), ("中", (np.abs(gap) > terc[0]) & (np.abs(gap) <= terc[1])),
                    ("大", np.abs(gap) > terc[1])):
        sub = [x for x, keep in zip(rec, m) if keep]
        moved = mean_ci(np.sign(gap[m]) * (yi[m] - mq[m, 0]), sub)
        claimed = float((wt[m] * np.abs(gap[m])).sum() / wt[m].sum())
        out["centre_by_gap"].append({"tercile": name, "claimed": claimed, "realised": moved})
        print(f"         |中心差|{name}：模型挪 {claimed:.3f} 档，真值朝模型方向走 {c.fmt(moved,digits=3)} 档")
    ratio = np.log(mp[:, 1] / mq[:, 1])
    zp = ((yi - mp[:, 0]) ** 2 + c.WITHIN) / mp[:, 1] ** 2
    zq = ((yi - mq[:, 0]) ** 2 + c.WITHIN) / mq[:, 1] ** 2
    out["width"] = {"all": {"z2_p": mean_ci(zp, rec), "z2_q": mean_ci(zq, rec),
                            "sd_p": float((wt * mp[:, 1]).sum() / wt.sum()), "sd_q": float((wt * mq[:, 1]).sum() / wt.sum())},
                    "terciles": []}
    print(f"   宽度：E[z²] 模型 {out['width']['all']['z2_p'][0]:.3f} [{out['width']['all']['z2_p'][1]:.3f},{out['width']['all']['z2_p'][2]:.3f}]"
          f"  市场 {out['width']['all']['z2_q'][0]:.3f} [{out['width']['all']['z2_q'][1]:.3f},{out['width']['all']['z2_q'][2]:.3f}]"
          f"  平均σ 模型 {out['width']['all']['sd_p']:.3f} 市场 {out['width']['all']['sd_q']:.3f} 档")
    tq = np.quantile(ratio, [1 / 3, 2 / 3])
    for name, m in (("模型更窄", ratio <= tq[0]), ("相近", (ratio > tq[0]) & (ratio <= tq[1])), ("模型更宽", ratio > tq[1])):
        sub = [x for x, keep in zip(rec, m) if keep]
        a, b = mean_ci(zp[m], sub), mean_ci(zq[m], sub)
        out["width"]["terciles"].append({"tercile": name, "sd_ratio": float(np.exp((wt[m] * ratio[m]).sum() / wt[m].sum())),
                                         "z2_p": a, "z2_q": b})
        print(f"         {name}（σ_p/σ_q={out['width']['terciles'][-1]['sd_ratio']:.2f}）：E[z²] 模型 {a[0]:.3f} [{a[1]:.3f},{a[2]:.3f}]"
              f"  市场 {b[0]:.3f} [{b[1]:.3f},{b[2]:.3f}]")
    out["tail"] = []
    for far in (2, 3):
        pm = np.array([x["p"][np.abs(np.arange(x["k"]) - x["mode"]) >= far].sum() for x in rec])
        qm = np.array([x["q"][np.abs(np.arange(x["k"]) - x["mode"]) >= far].sum() for x in rec])
        hit = np.array([float(abs(x["y"] - x["mode"]) >= far) for x in rec])
        row = {"far": far, "p": mean_ci(pm, rec), "q": mean_ci(qm, rec), "hit": mean_ci(hit, rec),
               "hit_minus_p": mean_ci(hit - pm, rec), "hit_minus_q": mean_ci(hit - qm, rec)}
        out["tail"].append(row)
        print(f"   尾部：离市场众数≥{far}档的总质量 模型 {row['p'][0]:.3f} 市场 {row['q'][0]:.3f} 实际 {row['hit'][0]:.3f}"
              f"  实际−模型 {c.fmt(row['hit_minus_p'],digits=3)}  实际−市场 {c.fmt(row['hit_minus_q'],digits=3)}")
    fl = f["p"] <= 0.002
    out["floor"] = []
    for name, m in (("全部", fl), ("≥2档", fl & (f["dist"] >= 2))):
        w = f["wt"][m]
        row = {"where": name, "buckets": int(m.sum()), "share_of_buckets": float(f["wt"][m].sum() / f["wt"].sum()),
               "p": float((w * f["p"][m]).sum() / w.sum()), "q": float((w * f["q"][m]).sum() / w.sum()),
               "hit": c.boot_ratio(w * f["y"][m], w, codes[m], k)}
        out["floor"].append(row)
        print(f"   下限桶（p≤0.002，{name}）：占全部桶 {row['share_of_buckets']:.1%}，平均 p {row['p']:.4f}，市场 q {row['q']:.4f}，"
              f"命中率 {row['hit'][0]:.4f} [{row['hit'][1]:.4f},{row['hit'][2]:.4f}]")
    return out


# ---------------------------------------------------------------------------
# D. Layer by layer (envelopes that kept each system's vector).

def part_d(rec, label):
    rec = [x for x in rec if x["lp"] is not None]
    out = {"sample": label, "decisions": len(rec), "station_days": len({(x["st"], x["d"]) for x in rec}),
           "event_days": len({x["d"] for x in rec}), "layers": {}}
    codes_d, k = codes_of(rec)
    print(f"\n== D 逐层 [{label}]  决策 {len(rec)}  站日 {out['station_days']}  事件日 {out['event_days']}")
    lsq, brq = scores(rec, "q")
    yi = np.array([x["y"] for x in rec], float)
    wt = np.array([x["wt"] for x in rec])
    mq = np.array([c.moments(x["q"]) for x in rec])
    # every seated system, scored on its own and averaged by its served weight
    ls_sys = np.array([sum(w * np.log(v[x["y"]]) for w, v in x["sys"]) for x in rec])
    br_sys = np.array([sum(w * ((v - (np.arange(x["k"]) == x["y"])) ** 2).sum() for w, v in x["sys"]) for x in rec])
    out["single_systems_weighted"] = {"logscore": mean_ci(ls_sys, rec), "logscore_vs_q": mean_ci(ls_sys - lsq, rec),
                                      "brier": mean_ci(br_sys, rec), "brier_vs_q": mean_ci(br_sys - brq, rec)}
    print(f"   各家单模型（按服务权重平均）：LogScore {out['single_systems_weighted']['logscore'][0]:.4f}"
          f"  对市场 {c.fmt(out['single_systems_weighted']['logscore_vs_q'])}  Brier 对市场 {c.fmt(out['single_systems_weighted']['brier_vs_q'])}")
    names4 = [f"{a} {b}" for a, b in CELLS4]
    for name, key in (("单模型层（权重最大的一家）", "lead"), ("融合层（线性池）", "lp"), ("最终层（服务向量）", "p")):
        ls, br = scores(rec, key)
        mv = np.array([c.moments(x[key]) for x in rec])
        gap = mv[:, 0] - mq[:, 0]
        X = np.column_stack([np.ones(len(gap)), gap])

        def wls(mlt, X=X):
            w = wt * mlt[codes_d]
            return np.linalg.solve((X * w[:, None]).T @ X, (X * w[:, None]).T @ (yi - mq[:, 0]))

        slope = boot_fit(wls, codes_d, k)[1]
        z2 = ((yi - mv[:, 0]) ** 2 + c.WITHIN) / mv[:, 1] ** 2
        far_mass = np.array([x[key][np.abs(np.arange(x["k"]) - x["mode"]) >= 2].sum() for x in rec])
        far_hit = np.array([float(abs(x["y"] - x["mode"]) >= 2) for x in rec])
        row = {"logscore": mean_ci(ls, rec), "logscore_vs_q": mean_ci(ls - lsq, rec),
               "brier": mean_ci(br, rec), "brier_vs_q": mean_ci(br - brq, rec),
               "bias": mean_ci(yi - mv[:, 0], rec), "mae": mean_ci(np.abs(yi - mv[:, 0]), rec),
               "centre_slope": slope, "z2": mean_ci(z2, rec), "sd": float((wt * mv[:, 1]).sum() / wt.sum()),
               "far_mass": mean_ci(far_mass, rec), "far_hit_minus_mass": mean_ci(far_hit - far_mass, rec),
               "single_w": single_w(rec, key)}
        print(f"-- {name}")
        print(f"   LogScore {row['logscore'][0]:.4f}  对市场 {c.fmt(row['logscore_vs_q'])}   Brier {row['brier'][0]:.4f}  对市场 {c.fmt(row['brier_vs_q'])}")
        print(f"   中心：真值−中心 {c.fmt(row['bias'],digits=3)} 档  |误差| {row['mae'][0]:.3f}  对市场分歧的斜率 {slope[0]:.3f} [{slope[1]:.3f},{slope[2]:.3f}]")
        print(f"   宽度：平均σ {row['sd']:.3f} 档  E[z²] {row['z2'][0]:.3f} [{row['z2'][1]:.3f},{row['z2'][2]:.3f}]"
              f"   尾部：≥2档质量 {row['far_mass'][0]:.3f}  实际−质量 {c.fmt(row['far_hit_minus_mass'],digits=3)}"
              f"   混合系数 w {row['single_w'][0]:.3f} [{row['single_w'][1]:.3f},{row['single_w'][2]:.3f}]")
        f = cell_frame(rec, key)
        row["cells"] = cell_table(f, codes_d[f["i"]], k, f["wt"], f["cell4"], names4, "四格（格按本层自己的方向定）")
        out["layers"][key] = row
    mq_far = np.array([x["q"][np.abs(np.arange(x["k"]) - x["mode"]) >= 2].sum() for x in rec])
    print(f"   市场：平均σ {(wt*mq[:,1]).sum()/wt.sum():.3f} 档  ≥2档质量 {(wt*mq_far).sum()/wt.sum():.3f}"
          f"  LogScore {mean_ci(lsq, rec)[0]:.4f}  Brier {mean_ci(brq, rec)[0]:.4f}")
    # the adjustment layers themselves: final against the plain pool
    ml = np.array([c.moments(x["lp"]) for x in rec])
    mp = np.array([c.moments(x["p"]) for x in rec])
    move = mp[:, 0] - ml[:, 0]
    X = np.column_stack([np.ones(len(move)), move])

    def wls2(mlt):
        w = wt * mlt[codes_d]
        return np.linalg.solve((X * w[:, None]).T @ X, (X * w[:, None]).T @ (yi - ml[:, 0]))

    adj = boot_fit(wls2, codes_d, k)[1]
    sdr = mp[:, 1] / ml[:, 1]
    lsl, _ = scores(rec, "lp")
    lsp, _ = scores(rec, "p")
    out["adjustment"] = {
        "centre_move_mean_abs": float((wt * np.abs(move)).sum() / wt.sum()), "centre_move_slope": adj,
        "sd_ratio_quantiles": [float(v) for v in np.quantile(sdr, [0.1, 0.5, 0.9])],
        "logscore_final_minus_pool": mean_ci(lsp - lsl, rec),
        "bridge_lambda_quantiles": [float(v) for v in np.quantile(
            [x["bridge_lambda"] for x in rec if x["bridge_lambda"] is not None] or [np.nan], [0.1, 0.5, 0.9])],
        "width_lambda_values": sorted({round(float(x["width_lambda"]), 3) for x in rec if x["width_lambda"] is not None}),
        "shift_c_mean_abs": float(np.mean([abs(x["shift_c"]) for x in rec if x["shift_c"] is not None] or [np.nan])),
        "floored_share_of_decisions": float(np.mean([bool(x["floored"]) for x in rec])),
    }
    a = out["adjustment"]
    print(f"-- 调整层（最终 对 线性池）\n   中心平均挪动 {a['centre_move_mean_abs']:.3f} 档，真值跟随斜率 {adj[0]:.3f} [{adj[1]:.3f},{adj[2]:.3f}]"
          f"   σ_最终/σ_池 分位 10/50/90% = {a['sd_ratio_quantiles'][0]:.2f}/{a['sd_ratio_quantiles'][1]:.2f}/{a['sd_ratio_quantiles'][2]:.2f}")
    print(f"   LogScore 最终−线性池 {c.fmt(a['logscore_final_minus_pool'])}   共识桥 λ 分位 {a['bridge_lambda_quantiles']}"
          f"   宽度 λ {a['width_lambda_values']}   辐射平移平均 |{a['shift_c_mean_abs']:.3f}|°C   有下限桶的决策 {a['floored_share_of_decisions']:.1%}")
    return out


# ---------------------------------------------------------------------------
# E. Where the model is right against the market (a search; candidates only).

def part_e(rec, label):
    out = {"sample": label, "families": {}}
    print(f"\n== E 条件混合系数 [{label}]  （w=1 模型全对，0 市场全对）")
    # 1. by distance from the market's mode: a binary fit on buckets, logit q as offset
    f = flat(rec)
    codes_d, k = codes_of(rec)
    codes = codes_d[f["i"]]
    d = logit(f["p"]) - logit(f["q"])
    fam = []
    for dc, name in enumerate(("众数桶", "±1档", "≥2档")):
        m = np.minimum(f["dist"], 2) == dc
        X = np.column_stack([np.ones(m.sum()), d[m]])
        cm, ym, wm, om = codes[m], f["y"][m], f["wt"][m], logit(f["q"][m])
        coef = boot_fit(lambda mlt: wlogistic(X, ym, wm * mlt[cm], offset=om), cm, k)
        fam.append({"level": name, "w": coef[1], "intercept": coef[0], "n": int(m.sum())})
    out["families"]["1 档距（桶层面）"] = fam
    mp = np.array([c.moments(x["p"]) for x in rec])
    mq = np.array([c.moments(x["q"]) for x in rec])
    gap = np.abs(mp[:, 0] - mq[:, 0])
    ratio = mp[:, 1] / mq[:, 1]
    ms = np.array([x["ms"] if x["ms"] is not None else np.nan for x in rec])
    h = np.array([x["h"] for x in rec])

    def terciles(v, names):
        t = np.nanquantile(v, [1 / 3, 2 / 3])
        return [(f"{names[0]}(≤{t[0]:.2f})", v <= t[0]), (f"{names[1]}", (v > t[0]) & (v <= t[1])),
                (f"{names[2]}(>{t[1]:.2f})", v > t[1])]

    families = {
        "2 |中心差|（档）": terciles(gap, ("小", "中", "大")),
        "3 入座家数": [("≤3 家", np.array([0 < x["nsys"] <= 3 for x in rec])), ("≥4 家", np.array([x["nsys"] >= 4 for x in rec]))],
        "4 梯子中价合计": terciles(ms, ("紧", "中", "松")),
        "5 距结算": [("36–48h", h <= 48), ("48–60h", (h > 48) & (h <= 60)), (">60h", h > 60)],
        "6 单位": [("C", np.array([x["unit"] == "C" for x in rec])), ("F", np.array([x["unit"] == "F" for x in rec]))],
        "7 σ_p/σ_q": terciles(ratio, ("模型更窄", "相近", "模型更宽")),
    }
    for fname, levels in families.items():
        fam = []
        for name, m in levels:
            sub = [x for x, keep in zip(rec, m) if keep]
            w = single_w(sub) if sub else None
            fam.append({"level": name, "w": w, "n": len(sub),
                        "station_days": len({(x["st"], x["d"]) for x in sub})})
        out["families"][fname] = fam
    for fname, fam in out["families"].items():
        print(f"   {fname}")
        for row in fam:
            w = row["w"]
            if w is None:
                print(f"     {row['level']:<16} 样本不足")
                continue
            print(f"     {row['level']:<16} w = {w[0]:+.3f} [{w[1]:+.3f},{w[2]:+.3f}]  n={row['n']}")
    return out


PARTS = {"A": part_a, "B": part_b, "C": part_c, "D": part_d, "E": part_e}

if __name__ == "__main__":
    part = sys.argv[1]
    results = {}
    for label, since in (("主样本 09-12 起", MAIN), ("敏感性 09-26 起", LATE)):
        results[label] = PARTS[part](c.load(since), label)
    with open(os.path.join(HERE, f"results_{part}.json"), "w") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1, default=float)
