#!/usr/bin/env python3
"""Proper 下注线（宪章 231）在距结算 >36h 的回放：是否跨零。

p = P0 向量（root；行在 P0-M 时取其 P0 根检查点），q = 盘口归一中价（市场完整才算），
s = log(p/q)；p > YES 卖一买 YES、p < YES 买一买 NO；份额 ∝ |s|（λ 在比率里约掉）。
三种成交价：taker 吃一档深度（下界）、maker 乐观（买一+1 全成交，上界，忽略逆选择）、
maker 穿价成交（挂买一+1，下一时点对侧卖一穿过我们的价才算成交，未成交按新盘口重挂，
直到 H37；p 固定为首决策）。总体：每站日首个可下注时点（车道口径，一站日一票）与逐时点。
ROI = Σ盈亏 / Σ成本；95% 区间按事件日自助。只读，不碰 SSOT。

    PYTHONPATH=<repo> python3 replay.py dataset.jsonl.gz
"""
import gzip, json, math, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

OV = json.loads((ROOT / "docs/research/log_proper_betting_20260926/venue_truth_overrides_full.json").read_text())["overrides"]
TICK, DRAWS, SEED = 0.01, 4000, 20261002


def load(path):
    rows = []
    for line in gzip.open(path, "rt"):
        r = json.loads(line)
        if r.get("m_state") != "complete" or not r.get("root") or not r.get("m"):
            continue
        keys = list(r["m"])
        truth = OV.get(f"{r['st']}|{r['day']}", r["truth"])
        if truth not in keys or set(keys) != set(r["root"]):
            continue
        raw = np.array([float(r["root"][k]) for k in keys]); raw /= raw.sum()
        fl, _ = floor_probabilities({k: float(v) for k, v in zip(keys, raw)})
        rows.append(dict(st=r["st"], day=r["day"], H=int(r["H"]), keys=keys, y=keys.index(truth),
                         p=[fl[k] for k in keys], q=[float(r["m"][k]) for k in keys],
                         ask=[(r["asks"].get(k) or [[None]])[0][0] for k in keys],
                         bid=[(r["bids"].get(k) or [[None]])[0][0] for k in keys]))
    return rows


def legs(r, sides):
    """车道触发的腿：(k, side, |s|)。"""
    out = []
    for k, (p, q, a, b) in enumerate(zip(r["p"], r["q"], r["ask"], r["bid"])):
        if q <= 0:
            continue
        s = math.log(p / q)
        if "YES" in sides and a is not None and p > a and s > 0:
            out.append((k, "YES", abs(s)))
        elif "NO" in sides and b is not None and p < b and s < 0:
            out.append((k, "NO", abs(s)))
    return out


def price(r, k, side, mode, p):
    a, b = r["ask"][k], r["bid"][k]
    if side == "YES":
        if mode == "taker":
            return a
        c = (b + TICK) if b is not None else None
        c = min(c, a - TICK) if c is not None and a is not None else c
        return c if c is not None and 0 < c < p else None
    # NO 盘口 = YES 镜像：NO 卖一 = 1 - YES 买一，NO 买一 = 1 - YES 卖一
    if mode == "taker":
        return 1 - b
    c = (1 - a + TICK) if a is not None else None
    c = min(c, (1 - b) - TICK) if c is not None and b is not None else c
    return c if c is not None and 0 < c < 1 - p else None


def bet(k, side, w, c, y):
    hit = (k == y) if side == "YES" else (k != y)
    return dict(cost=w * c, pnl=w * (float(hit) - c), win=float(hit), side=side)


def touch_fill(seq, i0, k, side, w, p, strict=True):
    """挂在 seq[i0] 的买一+1，逐时点检查是否被穿；未成交按新盘口重挂。"""
    c = price(seq[i0], k, side, "maker", p)
    for r in seq[i0 + 1:]:
        if c is None:
            return None
        if side == "YES":
            opp = r["ask"][k]
        else:
            opp = (1 - r["bid"][k]) if r["bid"][k] is not None else None
        if opp is not None and (opp < c if strict else opp <= c):
            return c
        c = price(r, k, side, "maker", p)
    return None


def pooled(items, rng):
    if not items:
        return None
    days = sorted({d["day"] for d in items}); di = {d: i for i, d in enumerate(days)}
    tot = np.zeros((len(days), 3))
    for d in items:
        tot[di[d["day"]]] += [d["pnl"], d["cost"], 1]
    pt = tot[:, 0].sum() / tot[:, 1].sum()
    boots = []
    for _ in range(DRAWS):
        w = np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days)).astype(float)
        boots.append(w @ tot[:, 0] / (w @ tot[:, 1]))
    return dict(roi=pt, lo=float(np.percentile(boots, 2.5)), hi=float(np.percentile(boots, 97.5)),
                n=int(tot[:, 2].sum()), days=len(days), cost=float(tot[:, 1].sum()),
                win=sum(d["win"] for d in items) / len(items))


def main():
    rows = load(sys.argv[1])
    by_sd = defaultdict(list)
    for r in rows:
        by_sd[(r["st"], r["day"])].append(r)
    for v in by_sd.values():
        v.sort(key=lambda r: -r["H"])
    rng = np.random.default_rng(SEED)
    res = {"rows": len(rows), "station_days": len(by_sd), "readings": {}}
    days_all = sorted({r["day"] for r in rows}); cut = days_all[len(days_all) // 2]

    def run(label, sides, mode, population):
        items = []
        for seq in by_sd.values():
            idxs = range(len(seq)) if population == "every_H" else None
            if population == "first":
                first = next((i for i, r in enumerate(seq) if legs(r, sides)), None)
                idxs = [] if first is None else [first]
            for i in idxs:
                r = seq[i]
                for k, side, w in legs(r, sides):
                    p = r["p"][k]
                    if mode == "touch":
                        c = touch_fill(seq, i, k, side, w, p)
                    else:
                        c = price(r, k, side, mode, p)
                    if c is None or not (0 < c < 1):
                        continue
                    b = bet(k, side, w, c, r["y"]); b.update(day=r["day"], H=r["H"], st=r["st"])
                    items.append(b)
        out = {"all": pooled(items, rng),
               "first_half": pooled([b for b in items if b["day"] < cut], rng),
               "second_half": pooled([b for b in items if b["day"] >= cut], rng),
               "since_0926": pooled([b for b in items if b["day"] >= "2026-09-26"], rng)}
        if population == "every_H":
            out["by_H"] = {H: pooled([b for b in items if b["H"] == H], rng) for H in (60, 54, 48, 44, 40, 37)}
        res["readings"][label] = out
        return out

    def f(x):
        return "—" if not x else f"{100*x['roi']:+6.1f}% [{100*x['lo']:+6.1f},{100*x['hi']:+6.1f}] n={x['n']:4d} d={x['days']:2d} 胜={x['win']:.0%}"

    print(f"rows {len(rows)} station-days {len(by_sd)} days {days_all[0]}..{days_all[-1]} 半分点 {cut}")
    for pop in ("first", "every_H"):
        for sides in (("NO",), ("YES", "NO"), ("YES",)):
            for mode in ("taker", "maker", "touch"):
                lab = f"{pop}|{'+'.join(sides)}|{mode}"
                o = run(lab, sides, mode, pop)
                print(f"{lab:22s} 全 {f(o['all'])} | 前半 {f(o['first_half'])[:7]} 后半 {f(o['second_half'])[:7]} | 09-26起 {f(o['since_0926'])}")
                if pop == "every_H" and sides == ("NO",):
                    print("   by H: " + " | ".join(f"H{H} {f(x)[:24]}" for H, x in o["by_H"].items()))
    (Path(__file__).parent / "results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
