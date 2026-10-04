#!/usr/bin/env python3
"""PROTOCOL.md: the readings and the verdict.

    python3 judge.py
"""
import json
import os
import pickle
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
ACCEPT = os.path.join(ROOT, "docs", "research", "final_vector_acceptance_20261004")
sys.path.insert(0, ROOT)
sys.path.insert(0, ACCEPT)
sys.path.insert(0, HERE)
import common as c  # noqa: E402
import layer as L  # noqa: E402

BINS = [0, 0.005, 0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0001]
MIN_BUCKETS = 200
BOOT = 2000
SEED = 20261005
FEE = 0.05


def boot(values, weights, codes, k):
    values, weights = np.asarray(values, float), np.asarray(weights, float)
    if len(values) == 0:
        return None
    s = np.bincount(codes, values * weights, k)
    w = np.bincount(codes, weights, k)
    idx = np.random.default_rng(SEED).integers(0, k, size=(BOOT, k))
    with np.errstate(invalid="ignore", divide="ignore"):
        b = s[idx].sum(1) / w[idx].sum(1)
    lo, hi = np.nanpercentile(b, [2.5, 97.5])
    return (float(s.sum() / w.sum()), float(lo), float(hi))


def boot_ratio(num, den, weights, codes, k):
    s = np.bincount(codes, num * weights, k)
    w = np.bincount(codes, den * weights, k)
    idx = np.random.default_rng(SEED).integers(0, k, size=(BOOT, k))
    with np.errstate(invalid="ignore", divide="ignore"):
        b = s[idx].sum(1) / w[idx].sum(1)
    lo, hi = np.nanpercentile(b, [2.5, 97.5])
    return (float(s.sum() / w.sum()), float(lo), float(hi))


def f(t, scale=1.0, digits=4):
    if t is None:
        return "无"
    star = "*" if (t[1] > 0 or t[2] < 0) else " "
    return f"{t[0]*scale:+.{digits}f} [{t[1]*scale:+.{digits}f},{t[2]*scale:+.{digits}f}]{star}"


def flat(rec, idx, vectors):
    cols = {n: [] for n in ("i", "b", "y", "dist", "wt", "bid", "ask", "lo", "hi")}
    for i in idx:
        x = rec[i]
        n = x["k"]
        pos = np.arange(n)
        cols["i"].append(np.full(n, i)); cols["b"].append(vectors["b"][i]); cols["y"].append((pos == x["y"]).astype(float))
        cols["dist"].append(np.abs(pos - x["mode"])); cols["wt"].append(np.full(n, x["wt"]))
        cols["bid"].append(x["bid"]); cols["ask"].append(x["ask"])
        cols["lo"].append(vectors["lo"][i] if vectors.get("lo") else vectors["b"][i])
        cols["hi"].append(vectors["hi"][i] if vectors.get("hi") else vectors["b"][i])
    return {n: np.concatenate(v) for n, v in cols.items()}


def reliability(fr, codes, k, mask, label):
    rows, passed, judged = [], 0, 0
    for lo, hi in zip(BINS[:-1], BINS[1:]):
        m = mask & (fr["b"] >= lo) & (fr["b"] < hi)
        if m.sum() == 0:
            continue
        gap = boot(fr["y"][m] - fr["b"][m], fr["wt"][m], codes[m], k)
        ok = gap[1] <= 0 <= gap[2]
        if m.sum() >= MIN_BUCKETS:
            judged += 1
            passed += ok
        rows.append({"bin": [lo, hi], "n": int(m.sum()), "pred": float(np.average(fr["b"][m], weights=fr["wt"][m])),
                     "hit": float(np.average(fr["y"][m], weights=fr["wt"][m])), "gap": gap, "judged": bool(m.sum() >= MIN_BUCKETS)})
    return {"label": label, "rows": rows, "judged": judged, "passed": int(passed)}


def calibration_line(fr, codes, k):
    from analysis import boot_fit, logit, wlogistic  # noqa: PLC0415
    X = np.column_stack([np.ones(len(fr["b"])), logit(fr["b"])])
    return boot_fit(lambda m: wlogistic(X, fr["y"], fr["wt"] * m[codes]), codes, k)


def edges(fr, codes, k, belief_key="b"):
    """Taker legs: YES where b - ask - fee > 0, NO where bid - b - fee > 0.
    Confident: the same with b's 10% point (YES) / 90% point (NO)."""
    out = {}
    b, lo, hi = fr["b"], fr["lo"], fr["hi"]
    ask, bid = fr["ask"], fr["bid"]
    fee_yes = FEE * ask * (1 - ask)
    fee_no = FEE * bid * (1 - bid)
    yes = ~np.isnan(ask) & (b - ask - fee_yes > 0)
    no = ~np.isnan(bid) & (bid > 0) & (bid - b - fee_no > 0)
    sure_yes = yes & (lo - ask - fee_yes > 0)
    sure_no = no & (bid - hi - fee_no > 0)
    for name, m, side in (("YES 全部", yes, 1), ("YES 有把握", sure_yes, 1), ("YES 没把握", yes & ~sure_yes, 1),
                          ("NO 全部", no, -1), ("NO 有把握", sure_no, -1), ("NO 没把握", no & ~sure_no, -1)):
        if m.sum() < 20:
            out[name] = {"legs": int(m.sum())}
            continue
        price = ask[m] if side == 1 else bid[m]
        fee = fee_yes[m] if side == 1 else fee_no[m]
        claimed = side * (b[m] - price) - fee
        realised = side * (fr["y"][m] - price) - fee
        cost = price if side == 1 else 1 - price
        w = fr["wt"][m]
        out[name] = {"legs": int(m.sum()), "station_day_weight": float(w.sum()),
                     "price": float(np.average(price, weights=w)), "belief": float(np.average(b[m], weights=w)),
                     "hit": float(np.average(fr["y"][m], weights=w)),
                     "belief_minus_hit": boot(b[m] - fr["y"][m], w, codes[m], k),
                     "claimed": boot(claimed, w, codes[m], k), "realised": boot(realised, w, codes[m], k),
                     "roi": boot_ratio(realised, cost, w, codes[m], k)}
    return out


def main():
    rec = c.load()
    fits = {}
    for model in L.MODELS + ("M4i", "M5", "M1p"):
        with open(os.path.join(HERE, "fits", f"{model}.pkl"), "rb") as fh:
            fits[model] = pickle.load(fh)
    idx = sorted(fits["M1"]["b"])
    days = sorted({rec[i]["d"] for i in idx})
    code = {d: i for i, d in enumerate(days)}
    k = len(days)
    vectors = {"M0": {"b": {i: rec[i]["q"] for i in idx}}, "P": {"b": {i: rec[i]["p"] for i in idx}}, **fits}
    names = {"P": "模型原样 p", "M0": "M0 市场原样", "M1": "M1 市场自校准", "M2": "M2 市场+模型整体", "M3": "M3 市场+模型分项", "M4": "M4 分项+中心条件", "M4i": "M4i 再加保序映射", "M5": "M5 分项+条件+档距截距", "M1p": "M1p 市场自校准+档距截距"}
    wt = np.array([rec[i]["wt"] for i in idx])
    dcode = np.array([code[rec[i]["d"]] for i in idx])
    result = {"scored_decisions": len(idx), "station_days": len({(rec[i]["st"], rec[i]["d"]) for i in idx}), "event_days": k,
              "theta_last": {m: fits[m]["theta"][days[-1]] for m in L.MODELS}}
    print(f"计分决策 {len(idx)}  站日 {result['station_days']}  事件日 {k}  {days[0]}..{days[-1]}")
    for m in L.MODELS:
        print(f"   {names[m]} 末日参数 {np.round(fits[m]['theta'][days[-1]], 3)}")

    # LogScore as the protocol reads it: the negative log of the truth bucket, lower is better.
    ls = {m: np.array([-np.log(vectors[m]["b"][i][rec[i]["y"]]) for i in idx]) for m in vectors}
    br = {m: np.array([((vectors[m]["b"][i] - (np.arange(rec[i]["k"]) == rec[i]["y"])) ** 2).sum() for i in idx]) for m in vectors}
    top = {m: np.array([float(vectors[m]["b"][i].max()) for i in idx]) for m in vectors}

    result["models"] = {}
    for m in ("P", "M0", "M1", "M1p", "M2", "M3", "M4", "M4i", "M5"):
        fr = flat(rec, idx, vectors[m])
        codes = np.array([code[rec[i]["d"]] for i in fr["i"].astype(int)])
        everything = np.ones(len(fr["b"]), bool)
        rel = [reliability(fr, codes, k, everything, "全部桶")]
        for dc, dn in enumerate(("市场众数桶", "±1档", "≥2档")):
            rel.append(reliability(fr, codes, k, np.minimum(fr["dist"], 2) == dc, dn))
        line = calibration_line(fr, codes, k)
        ybar = np.average(fr["y"], weights=fr["wt"])
        res = sum(fr["wt"][(fr["b"] >= r["bin"][0]) & (fr["b"] < r["bin"][1])].sum() * (r["hit"] - ybar) ** 2 for r in rel[0]["rows"]) / fr["wt"].sum()
        rl = sum(fr["wt"][(fr["b"] >= r["bin"][0]) & (fr["b"] < r["bin"][1])].sum() * (r["hit"] - r["pred"]) ** 2 for r in rel[0]["rows"]) / fr["wt"].sum()
        out = {"reliability": rel, "calibration_line": line, "rel": float(rl), "res": float(res),
               "logscore": boot(ls[m], wt, dcode, k), "brier": boot(br[m], wt, dcode, k), "top": float(np.average(top[m], weights=wt)),
               "logscore_vs_M0": boot(ls[m] - ls["M0"], wt, dcode, k), "logscore_vs_M1": boot(ls[m] - ls["M1"], wt, dcode, k),
               "brier_vs_M0": boot(br[m] - br["M0"], wt, dcode, k), "brier_vs_M1": boot(br[m] - br["M1"], wt, dcode, k),
               "edges": edges(fr, codes, k)}
        result["models"][m] = out
        print(f"\n== {names[m]}")
        print(f"   校准  " + "  ".join(f"{r['label']} {r['passed']}/{r['judged']} 档过" for r in rel)
              + f"   校准线 截距 {f(line[0],1,3)} 斜率 {line[1][0]:.3f} [{line[1][1]:.3f},{line[1][2]:.3f}]   REL {rl:.5f}")
        for r in rel[0]["rows"]:
            mark = "" if not r["judged"] else ("" if r["gap"][1] <= 0 <= r["gap"][2] else "  ← 不过")
            print(f"     {r['bin'][0]:5.3f}-{min(r['bin'][1],1):5.3f}  桶 {r['n']:6d}  预测 {r['pred']:.4f}  命中 {r['hit']:.4f}  差 {f(r['gap'],100,2)}{mark}")
        for r in rel[1:]:
            bad = [f"{x['bin'][0]:.3f}-{min(x['bin'][1],1):.3f} 差 {f(x['gap'],100,1)}" for x in r["rows"] if x["judged"] and not (x["gap"][1] <= 0 <= x["gap"][2])]
            if bad:
                print(f"     [{r['label']}] 不过的档：" + "；".join(bad))
        print(f"   清晰度  RES {res:.5f}  平均最高桶 {out['top']:.3f}  LogScore {out['logscore'][0]:.4f}  Brier {out['brier'][0]:.4f}")
        print(f"           对市场原样 ΔLogScore {f(out['logscore_vs_M0'])}  ΔBrier {f(out['brier_vs_M0'])}")
        print(f"           对市场自校准 ΔLogScore {f(out['logscore_vs_M1'])}  ΔBrier {f(out['brier_vs_M1'])}")
        print("   edge（吃单，扣费后；¢/份）  腿数 / 均价 / 平均 b / 命中 / b−命中 / 声称 / 兑现 / 每元回报")
        for name, e in out["edges"].items():
            if "price" not in e:
                print(f"     {name:<8} 腿 {e['legs']}")
                continue
            print(f"     {name:<8} {e['legs']:6d}  {e['price']:.3f}  {e['belief']:.3f}  {e['hit']:.3f}  {f(e['belief_minus_hit'],100,1)}"
                  f"  {e['claimed'][0]*100:+.2f}  {f(e['realised'],100,2)}  {f(e['roi'],100,1)}%")

    # 3. the information increment over the market's own recalibration, by condition
    print("\n== 市场条件信息增量（各层对 M1 的 ΔLogScore；负＝模型在市场之外还带信息）")
    gap = np.array([abs(c.moments(rec[i]["p"])[0] - c.moments(rec[i]["q"])[0]) for i in idx])
    hours = np.array([rec[i]["h"] for i in idx])
    unit = np.array([rec[i]["unit"] for i in idx])
    conditions = [("全部", np.ones(len(idx), bool)), ("中心一致 ≤0.25 档", gap <= 0.25), ("中心不一致 >0.25 档", gap > 0.25),
                  ("36–48h", hours <= 48), ("48–60h", (hours > 48) & (hours <= 60)), (">60h", hours > 60),
                  ("C 站", unit == "C"), ("F 站", unit == "F")]
    result["increment"] = {}
    for cname, m in conditions:
        row = {"decisions": int(m.sum())}
        parts = []
        for model in ("M2", "M3", "M4", "M4i", "M5"):
            row[model] = boot((ls[model] - ls["M1"])[m], wt[m], dcode[m], k)
            parts.append(f"{model} {f(row[model])}")
        row["M5_vs_M1p"] = boot((ls["M5"] - ls["M1p"])[m], wt[m], dcode[m], k)
        parts.append(f"｜M5 对 M1p {f(row['M5_vs_M1p'])}")
        row["M1_vs_M0"] = boot((ls["M1"] - ls["M0"])[m], wt[m], dcode[m], k)
        result["increment"][cname] = row
        print(f"   {cname:<14} 决策 {int(m.sum()):5d}  " + "  ".join(parts) + f"   （M1 对市场原样 {f(row['M1_vs_M0'])}）")

    # verdict
    print("\n== 判词")
    result["verdict"] = {}
    for m in ("M1", "M1p", "M2", "M3", "M4", "M4i", "M5"):
        out = result["models"][m]
        calibrated = all(r["passed"] == r["judged"] for r in out["reliability"])
        legs_ok = all(e.get("belief_minus_hit") is None or (e["belief_minus_hit"][1] <= 0 <= e["belief_minus_hit"][2])
                      for name, e in out["edges"].items() if name in ("YES 全部", "NO 全部"))
        more = out["logscore_vs_M1"][2] < 0 if m not in ("M1", "M1p") else None
        if m == "M5":
            more = bool(result["increment"]["全部"]["M5_vs_M1p"][2] < 0)
        sure = [e["realised"][1] > 0 for name, e in out["edges"].items() if name.endswith("有把握") and "realised" in e]
        judged = sum(r["judged"] for r in out["reliability"])
        failed = judged - sum(r["passed"] for r in out["reliability"])
        result["verdict"][m] = {"bins_judged": int(judged), "bins_failed": int(failed), "usable": bool(calibrated and legs_ok), "calibrated_bins": calibrated, "selected_legs_calibrated": legs_ok,
                                "more_than_market_recalibration": more, "confident_edge_realised": bool(sure and all(sure))}
        print(f"   {names[m]:<14} 层可用 {'是' if (calibrated and legs_ok) else '否'}（各档校准 {'是' if calibrated else '否'}，被选中腿说多少中多少 {'是' if legs_ok else '否'}）"
              f"  比市场自校准多带信息 {'—' if more is None else ('是' if more else '否')}  有把握的 edge 扣费后兑现 {'是' if (sure and all(sure)) else '否'}"
              f"  不过的档 {failed}/{judged}（5% 假设下期望 {0.05*judged:.1f}）")
    with open(os.path.join(HERE, "results.json"), "w") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1, default=float)


if __name__ == "__main__":
    main()
