#!/usr/bin/env python3
"""现役模型链重测：只用 P0 posterior_version ≥ v19（09-25 09:03Z 起：六家、216/217/219/222、199 放开 ASOS）
的实际服务 P0 向量，事件日 09-26..10-01 的新建数据集；规则、成交与 replay.py 相同（成交用触价≤）。
另报只 v20 的敏感性与逐日读数。"""
import gzip, json, sys, tempfile
from collections import defaultdict
import numpy as np
import replay as R

def subset(path, keep):
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False).name
    with gzip.open(tmp, "wt") as f:
        for l in gzip.open(path, "rt"):
            r = json.loads(l)
            if keep(int((r.get("p0_version") or "v0").rsplit("_v", 1)[1])):
                f.write(l)
    return R.load(tmp)

def run(rows, sides, mode):
    by = defaultdict(list)
    for r in rows: by[(r["st"], r["day"])].append(r)
    for v in by.values(): v.sort(key=lambda r: -r["H"])
    items = []
    for seq in by.values():
        i = next((i for i, r in enumerate(seq) if R.legs(r, sides)), None)
        if i is None: continue
        r = seq[i]
        for k, s, w in R.legs(r, sides):
            p = r["p"][k]
            c = R.touch_fill(seq, i, k, s, w, p, strict=False) if mode == "touch" else R.price(r, k, s, mode, p)
            if c is None or not (0 < c < 1): continue
            b = R.bet(k, s, w, c, r["y"]); b["day"] = r["day"]; items.append(b)
    return items, len(by)

rng = np.random.default_rng(R.SEED)
def f(x):
    o = R.pooled(x, rng)
    return "—" if not o else f"{100*o['roi']:+6.1f}% [{100*o['lo']:+6.1f},{100*o['hi']:+6.1f}] n={o['n']:4d} d={o['days']} 胜={o['win']:.0%} 成本={o['cost']:.0f}"
res = {}
for lab, keep in (("v19+", lambda v: v >= 19), ("v20", lambda v: v == 20)):
    rows = subset(sys.argv[1], keep)
    print(f"#### {lab}: 行 {len(rows)} 天 {sorted({r['day'] for r in rows})}")
    for sides in (("YES", "NO"), ("NO",), ("YES",)):
        for mode, name in (("taker", "吃卖一"), ("maker", "挂单全成交上界"), ("touch", "现行挂单重挂")):
            it, nsd = run(rows, sides, mode)
            print(f"{'+'.join(sides):7s} {name:8s} {f(it)}")
            res[f"{lab}|{'+'.join(sides)}|{mode}"] = R.pooled(it, rng)
            if mode in ("taker", "touch") and lab == "v19+":
                bd = defaultdict(lambda: [0.0, 0.0])
                for b in it: bd[b["day"]][0] += b["pnl"]; bd[b["day"]][1] += b["cost"]
                print("        逐日 " + " ".join(f"{d[5:]}:{100*p/c:+.1f}%" for d, (p, c) in sorted(bd.items()) if c > 0))
json.dump(res, open("results_current.json", "w"), indent=1, default=str)
