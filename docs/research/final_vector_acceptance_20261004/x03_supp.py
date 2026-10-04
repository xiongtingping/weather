#!/usr/bin/env python3
"""Supplementary readings, added after parts A-E were read (PROTOCOL.md, 偏离).

S1  the top bin (p >= 0.5) by layer -- where the one miscalibrated bin is made;
S2  the 2x3 cells at tradeable prices;
S3  the market's modal bucket: the model saying YES against the model saying NO;
S4  the legs the Proper rule v3 would rest (YES on the market's mode, bid + 1 tick under p);
S5  the four cells where the two centres agree against where they do not.
"""
import json
import os
import sys

import numpy as np

import analysis as a
import common as c

HERE = os.path.dirname(os.path.abspath(__file__))
TICK = 0.01


def ratio(num, den, codes, k):
    return c.boot_ratio(num, den, codes, k)


def s1_top_bin(rec, out):
    rec = [x for x in rec if x["lp"] is not None]
    codes_d, k = a.codes_of(rec)
    print("-- S1 最高概率档逐层（p≥0.5 的桶；仅有逐层记录的信封）")
    rows = []
    for name, key in (("单模型（权重最大的一家）", "lead"), ("线性池", "lp"), ("最终", "p"), ("市场", "q")):
        f = a.flat(rec, key)
        codes = codes_d[f["i"]]
        for lo, hi in ((0.4, 0.5), (0.5, 1.01)):
            m = (f["p"] >= lo) & (f["p"] < hi)
            if m.sum() == 0:
                continue
            w = f["wt"][m]
            pm = (w * f["p"][m]).sum() / w.sum()
            hit = (w * f["y"][m]).sum() / w.sum()
            diff = ratio(w * (f["y"][m] - f["p"][m]), w, codes[m], k)
            share = f["wt"][m].sum() / np.array([x["wt"] for x in rec]).sum()
            rows.append({"layer": name, "bin": [lo, hi], "buckets": int(m.sum()), "per_decision": share,
                         "mean_pred": pm, "hit": hit, "diff": diff})
            print(f"   {name:<12} {lo:.1f}-{min(hi,1):.1f}  桶 {int(m.sum()):5d}（每决策 {share:.2f} 个）  预测 {pm:.3f}  命中 {hit:.3f}  差 {c.fmt(diff,digits=3)}")
    out["s1_top_bin_by_layer"] = rows


def s2_tradeable(rec, out):
    f = a.cell_frame(rec)
    codes_d, k = a.codes_of(rec)
    codes = codes_d[f["i"]]
    print("-- S2 可成交口径 2×3（YES 对卖一、NO 对 1−买一，仅 p 过线的腿，未扣费）")
    rows = []
    for yes in (True, False):
        for dc, dname in enumerate(("众数桶", "±1档", "≥2档")):
            price = f["ask"] if yes else f["bid"]
            m = ~np.isnan(price) & (np.minimum(f["dist"], 2) == dc)
            m &= (f["p"] > price) if yes else (f["p"] < price)
            if m.sum() == 0:
                continue
            w = f["wt"][m]
            sgn = 1.0 if yes else -1.0
            claim = sgn * (f["p"][m] - price[m])
            real = sgn * (f["y"][m] - price[m])
            cost = price[m] if yes else 1.0 - price[m]
            row = {"cell": f"{'YES' if yes else 'NO'} {dname}", "legs": int(m.sum()),
                   "claimed": ratio(w * claim, w, codes[m], k), "realised": ratio(w * real, w, codes[m], k),
                   "roi": ratio(w * real, w * cost, codes[m], k)}
            rows.append(row)
            print(f"   {row['cell']:<10} 腿 {row['legs']:6d}  声称 {row['claimed'][0]*100:5.2f}¢  兑现 {c.fmt(row['realised'],100,2)}¢"
                  f"  每元回报 {c.fmt(row['roi'],100,1)}%")
    out["s2_tradeable_2x3"] = rows


def s3_mode_bucket(rec, out):
    codes_d, k = a.codes_of(rec)
    wt = np.array([x["wt"] for x in rec])
    pm = np.array([x["p"][x["mode"]] for x in rec])
    qm = np.array([x["q"][x["mode"]] for x in rec])
    bid = np.array([x["bid"][x["mode"]] for x in rec])
    ask = np.array([x["ask"][x["mode"]] for x in rec])
    hit = np.array([float(x["y"] == x["mode"]) for x in rec])
    print("-- S3 市场众数桶：模型说便宜 对 模型说贵")
    rows = []
    for name, m in (("全部", np.ones(len(rec), bool)), ("模型说便宜 p>q", pm > qm), ("模型说贵 p<q", pm < qm)):
        ok = m & ~np.isnan(bid) & ~np.isnan(ask)
        w = wt[ok]
        row = {"group": name, "decisions": int(ok.sum()),
               "p": float((w * pm[ok]).sum() / w.sum()), "q": float((w * qm[ok]).sum() / w.sum()),
               "bid": float((w * bid[ok]).sum() / w.sum()), "ask": float((w * ask[ok]).sum() / w.sum()),
               "hit": ratio(w * hit[ok], w, codes_d[ok], k),
               "hit_minus_q": ratio(w * (hit[ok] - qm[ok]), w, codes_d[ok], k),
               "hit_minus_bid": ratio(w * (hit[ok] - bid[ok]), w, codes_d[ok], k),
               "hit_minus_ask": ratio(w * (hit[ok] - ask[ok]), w, codes_d[ok], k),
               "hit_minus_p": ratio(w * (hit[ok] - pm[ok]), w, codes_d[ok], k)}
        rows.append(row)
        print(f"   {name:<12} 决策 {row['decisions']:5d}  p {row['p']:.3f}  q {row['q']:.3f}  买一 {row['bid']:.3f}  卖一 {row['ask']:.3f}"
              f"  命中 {row['hit'][0]:.3f}\n                命中−p {c.fmt(row['hit_minus_p'],100,1)}  命中−q {c.fmt(row['hit_minus_q'],100,1)}"
              f"  命中−买一 {c.fmt(row['hit_minus_bid'],100,1)}  命中−卖一 {c.fmt(row['hit_minus_ask'],100,1)}  (点)")
    out["s3_mode_bucket"] = rows


def s4_rule_legs(rec, out):
    codes_d, k = a.codes_of(rec)
    wt = np.array([x["wt"] for x in rec])
    pm = np.array([x["p"][x["mode"]] for x in rec])
    qm = np.array([x["q"][x["mode"]] for x in rec])
    bid = np.array([x["bid"][x["mode"]] for x in rec])
    ask = np.array([x["ask"][x["mode"]] for x in rec])
    hit = np.array([float(x["y"] == x["mode"]) for x in rec])
    price = np.minimum(bid + TICK, ask - TICK)
    price = np.where(price > bid, price, np.nan)          # no room inside a one-tick spread: the rung is the bid
    price = np.where(np.isnan(price), bid, price)
    sel = ~np.isnan(price) & ~np.isnan(ask) & (pm > qm) & (price < pm)
    print("-- S4 车道 v3 会挂的腿（市场众数桶 YES，买一+1 档且低于 p；按「挂上就算成交」读，不是成交样本）")
    rows = []
    groups = [("全部被选中", sel), ("p<0.4", sel & (pm < 0.4)), ("0.4≤p<0.5", sel & (pm >= 0.4) & (pm < 0.5)),
              ("p≥0.5", sel & (pm >= 0.5)), ("未被选中（p≤挂价或 p≤q）", ~sel & ~np.isnan(bid) & ~np.isnan(ask))]
    for name, m in groups:
        if m.sum() == 0:
            continue
        w = wt[m]
        row = {"group": name, "decisions": int(m.sum()), "station_days": len({(rec[i]["st"], rec[i]["d"]) for i in np.where(m)[0]}),
               "p": float((w * pm[m]).sum() / w.sum()), "q": float((w * qm[m]).sum() / w.sum()),
               "price": float((w * price[m]).sum() / w.sum()), "ask": float((w * ask[m]).sum() / w.sum()),
               "hit": ratio(w * hit[m], w, codes_d[m], k),
               "claimed": float((w * (pm[m] - price[m])).sum() / w.sum()),
               "hit_minus_p": ratio(w * (hit[m] - pm[m]), w, codes_d[m], k),
               "hit_minus_price": ratio(w * (hit[m] - price[m]), w, codes_d[m], k),
               "hit_minus_ask": ratio(w * (hit[m] - ask[m]), w, codes_d[m], k)}
        rows.append(row)
        print(f"   {name:<22} 站日 {row['station_days']:4d}  p {row['p']:.3f}  q {row['q']:.3f}  挂价 {row['price']:.3f}  卖一 {row['ask']:.3f}"
              f"  命中 {row['hit'][0]:.3f}\n                          声称 {row['claimed']*100:.1f} 点  命中−p {c.fmt(row['hit_minus_p'],100,1)}"
              f"  命中−挂价 {c.fmt(row['hit_minus_price'],100,1)}  命中−卖一 {c.fmt(row['hit_minus_ask'],100,1)}  (点)")
    out["s4_rule_v3_legs"] = rows


def s5_centre_agreement(rec, out):
    mp = np.array([c.moments(x["p"])[0] for x in rec])
    mq = np.array([c.moments(x["q"])[0] for x in rec])
    gap = np.abs(mp - mq)
    cut = float(np.quantile(gap, 1 / 3))
    names4 = [f"{s} {d}" for s, d in a.CELLS4]
    names6 = [f"{s} {d}" for s in ("YES", "NO") for d in ("众数桶", "±1档", "≥2档")]
    print(f"-- S5 两个中心一致（|中心差|≤{cut:.2f} 档，最小的三分之一）对 不一致（其余）")
    out["s5_centre_agreement"] = {"cut": cut}
    for name, keep in (("中心一致", gap <= cut), ("中心不一致", gap > cut)):
        sub = [x for x, m in zip(rec, keep) if m]
        codes_d, k = a.codes_of(sub)
        f = a.cell_frame(sub)
        codes = codes_d[f["i"]]
        print(f"   [{name}] 决策 {len(sub)}  站日 {len({(x['st'], x['d']) for x in sub})}")
        rows = a.cell_table(f, codes, k, f["wt"], f["cell6"], names6, "对归一中价 q，每条腿等权")
        trade = []
        for yes in (True, False):
            for dc, dname in enumerate(("众数桶", "±1档", "≥2档")):
                price = f["ask"] if yes else f["bid"]
                m = ~np.isnan(price) & (np.minimum(f["dist"], 2) == dc)
                m &= (f["p"] > price) if yes else (f["p"] < price)
                if m.sum() < 30:
                    continue
                w = f["wt"][m]
                sgn = 1.0 if yes else -1.0
                real = sgn * (f["y"][m] - price[m])
                claim = sgn * (f["p"][m] - price[m])
                trade.append({"cell": f"{'YES' if yes else 'NO'} {dname}", "legs": int(m.sum()),
                              "claimed": float((w * claim).sum() / w.sum()),
                              "realised": ratio(w * real, w, codes[m], k)})
                print(f"     吃单 {trade[-1]['cell']:<10} 腿 {int(m.sum()):6d}  声称 {trade[-1]['claimed']*100:5.2f}¢  兑现 {c.fmt(trade[-1]['realised'],100,2)}¢")
        out["s5_centre_agreement"][name] = {"cells": rows, "tradeable": trade, "single_w": a.single_w(sub)}
        w1 = out["s5_centre_agreement"][name]["single_w"]
        print(f"     单一混合系数 w = {w1[0]:.3f} [{w1[1]:.3f},{w1[2]:.3f}]")


if __name__ == "__main__":
    results = {}
    for label, since in (("主样本 09-12 起", a.MAIN), ("敏感性 09-26 起", a.LATE)):
        rec = c.load(since)
        print(f"\n== 补充 [{label}]  决策 {len(rec)}")
        out = {}
        s1_top_bin(rec, out)
        s2_tradeable(rec, out)
        s3_mode_bucket(rec, out)
        s4_rule_legs(rec, out)
        s5_centre_agreement(rec, out)
        results[label] = out
    with open(os.path.join(HERE, "results_S.json"), "w") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1, default=float)


def s6_pool_narrowed(rec):
    """Is the top bin's overclaim the width layer's?  The plain pool, squeezed
    about its own mean to the served vector's sd -- nothing else moved."""
    rec = [x for x in rec if x["lp"] is not None]
    codes_d, k = a.codes_of(rec)
    for x in rec:
        if "lpn" not in x:
            x["lpn"] = c.floor_vec(c.matched_to(x["lp"], c.moments(x["lp"])[0], c.moments(x["p"])[1]))
    rows = []
    print("-- S6 线性池只做收窄（绕自身均值压到最终向量的标准差，别的不动）")
    for name, key in (("线性池", "lp"), ("线性池只收窄", "lpn"), ("最终", "p")):
        f = a.flat(rec, key)
        codes = codes_d[f["i"]]
        ls, _ = a.scores(rec, key)
        m = f["p"] >= 0.5
        w = f["wt"][m]
        diff = ratio(w * (f["y"][m] - f["p"][m]), w, codes[m], k)
        rows.append({"vector": name, "buckets": int(m.sum()), "mean_pred": float((w * f["p"][m]).sum() / w.sum()),
                     "hit": float((w * f["y"][m]).sum() / w.sum()), "diff": diff, "logscore": a.mean_ci(ls, rec)})
        print(f"   {name:<8} p≥0.5 桶 {int(m.sum()):5d}  预测 {rows[-1]['mean_pred']:.3f}  命中 {rows[-1]['hit']:.3f}"
              f"  差 {c.fmt(diff,digits=3)}   LogScore {rows[-1]['logscore'][0]:.4f}")
    return rows


if __name__ == "__main__" and "--s6" in sys.argv:
    extra = {}
    for label, since in (("主样本 09-12 起", a.MAIN), ("敏感性 09-26 起", a.LATE)):
        print(f"\n== 补充 S6 [{label}]")
        extra[label] = s6_pool_narrowed(c.load(since))
    with open(os.path.join(HERE, "results_S6.json"), "w") as fh:
        json.dump(extra, fh, ensure_ascii=False, indent=1, default=float)
