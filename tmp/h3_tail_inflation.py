"""H3 follow-through: with the soft LR off, what is left is the hard lower
bound's renormalisation -- which can only push the untouched tail UP."""
import json, math, statistics
rows = json.load(open("tmp/h3_cases.json"))
for r in rows:
    r["excluded"] = set(r["excluded"]); r["held"] = set(r["held"])
    r["edge"] = r["p_t"] - r["ask"]

print("=== 6  硬下界砍掉多少先验质量 → 剩下的桶被整体放大多少倍")
ok = [r for r in rows if r["prior"]]
for name, sel in (("全部", ok),
                  ("边际>0.25", [r for r in ok if r["edge"] > .25]),
                  ("边际>0.10", [r for r in ok if r["edge"] > .10])):
    cut = [sum(r["prior"].get(b, 0.0) for b in r["excluded"]) for r in sel]
    lift = [1.0 / (1.0 - c) for c in cut if c < 1]
    tgt = [r["p"][r["target"]] / r["prior"][r["target"]]
           for r in sel if r["prior"].get(r["target"])]
    print("  %-10s n=%-3d 被砍先验质量中位 %.0f%%  纯重归一化放大 %.2f×  "
          "目标桶实际放大 %.2f×" % (
        name, len(sel), 100*statistics.median(cut),
        statistics.median(lift), statistics.median(tgt)))

print("\n=== 7  '已测高点之上'的那部分质量，按距当地午夜还有多久分档")
buk = []
for r in rows:
    for b, v in r["p"].items():
        c = r["centers"].get(b)
        if c is None or c <= r["rh"]:
            continue
        buk.append({"p": v, "won": b == r["truth"], "lead": r["lead_h"],
                    "s": r["strength"]})
print("  剩余小时      桶数   模型声称   实际发生")
for lo, hi in ((0, 1), (1, 3), (3, 6), (6, 12), (12, 99)):
    sel = [x for x in buk if lo <= x["lead"] < hi]
    if sel:
        print("  %2d~%-3d h    %5d    %5.1f%%    %5.1f%%" % (
            lo, hi, len(sel), 100*statistics.mean(x["p"] for x in sel),
            100*sum(x["won"] for x in sel)/len(sel)))
print("  合计       %5d    %5.1f%%    %5.1f%%" % (
    len(buk), 100*statistics.mean(x["p"] for x in buk),
    100*sum(x["won"] for x in buk)/len(buk)))

print("\n=== 8  软似然开着 vs 关着，目标桶表现有没有差别")
for name, sel in (("strength=0 (似然恒等)", [r for r in rows if r["strength"] == 0]),
                  ("strength>0 (似然真的动)", [r for r in rows if r["strength"] > 0])):
    print("  %-24s n=%-3d 模型说 %4.1f%%  实际 %4.1f%%  目标在高点上方 %3.0f%%" % (
        name, len(sel), 100*statistics.mean(r["p_t"] for r in sel),
        100*sum(1 for r in sel if r["target"] == r["truth"])/len(sel),
        100*sum(1 for r in sel
                if (r["centers"].get(r["target"]) or -99) > r["rh"])/len(sel)))

print("\n=== 9  最后一次重算离当地午夜多久 / 目标桶要求再升多少度")
lh = [r["lead_h"] for r in rows]
print("  lead 中位 %.2f h（最大 %.1f h）" % (statistics.median(lh), max(lh)))
need = [r["centers"][r["target"]] - r["rh"] for r in rows
        if r["centers"].get(r["target"]) is not None]
print("  目标桶中心比已测高点高出：中位 %+.1f°C，>=+2°C 的占 %.0f%%，"
      ">=+3°C 的占 %.0f%%" % (
    statistics.median(need), 100*sum(1 for d in need if d >= 2)/len(need),
    100*sum(1 for d in need if d >= 3)/len(need)))
