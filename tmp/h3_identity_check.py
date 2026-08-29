import json, statistics
rows = json.load(open("tmp/h3_cases.json"))
for r in rows:
    r["excluded"] = set(r["excluded"]); r["edge"] = r["p_t"] - r["ask"]

print("=== 13  strength=0 的行：P1 后验 == P0 先验 × 硬下界重归一化？")
res = []
for r in rows:
    if r["strength"] != 0 or not r["prior"]:
        continue
    cut = sum(r["prior"].get(b, 0.0) for b in r["excluded"])
    if cut >= 1 or not r["prior"].get(r["target"]):
        continue
    res.append((r["p"][r["target"]] / r["prior"][r["target"]]) * (1 - cut))
print("  (后验/先验)×(1-被砍质量)  n=%d  中位 %.3f  10/90分位 %.3f / %.3f"
      "  （=1.000 表示 P1 除了重归一化什么都没做）" % (
    len(res), statistics.median(res),
    sorted(res)[len(res)//10], sorted(res)[9*len(res)//10]))

print("\n=== 14  观测似然在这 104 个站日里的全部作用力")
lrs = [float(v) for r in rows for b, v in r["lr"].items()
       if b not in r["excluded"]]
print("  最强下压 ×%.3f   最强上抬 ×%.3f   恰好 ×1.000 的桶 %.0f%%" % (
    min(lrs), max(lrs), 100*sum(1 for v in lrs if v == 1.0)/len(lrs)))

print("\n=== 15  19 例的账")
n19 = [r for r in rows if r["edge"] > .25]
up = [r for r in n19 if r["prior"] and r["p"][r["target"]] > r["prior"].get(r["target"], 9)]
ab = [r for r in n19 if (r["centers"].get(r["target"]) or -99) > r["rh"]]
print("  n=19：似然恒等 %d/19；目标桶在已测高点之上 %d/19；"
      "观测之后目标桶被抬高而非压低 %d/19；实际发生 %d/19" % (
    sum(1 for r in n19 if r["strength"] == 0), len(ab), len(up),
    sum(1 for r in n19 if r["target"] == r["truth"])))
