import json, statistics
rows = json.load(open("tmp/h3_cases.json"))
for r in rows:
    r["excluded"] = set(r["excluded"]); r["edge"] = r["p_t"] - r["ask"]
    c = r["centers"].get(r["target"])
    r["d"] = (c - r["rh"]) if c is not None else None

print("=== 10  边际×似然开关 交叉表（目标桶实际发生率）")
print("  边际档        strength=0            strength>0")
for name, f in (("<=0", lambda e: e <= 0), ("0~0.10", lambda e: 0 < e <= .10),
                ("0.10-0.25", lambda e: .10 < e <= .25), (">0.25", lambda e: e > .25)):
    a = [r for r in rows if f(r["edge"]) and r["strength"] == 0]
    b = [r for r in rows if f(r["edge"]) and r["strength"] > 0]
    def s(x):
        if not x: return "    —      "
        return "n=%-3d 说%3.0f%% 中%3.0f%%" % (
            len(x), 100*statistics.mean(y["p_t"] for y in x),
            100*sum(1 for y in x if y["target"] == y["truth"])/len(x))
    print("  %-11s %s   %s" % (name, s(a), s(b)))

print("\n=== 11  只看 lead<3h（当地深夜，高点早已过）")
late = [r for r in rows if r["lead_h"] < 3]
for name, sel in (("全部", late),
                  ("strength=0", [r for r in late if r["strength"] == 0]),
                  ("strength>0", [r for r in late if r["strength"] > 0])):
    if sel:
        print("  %-11s n=%-3d 目标桶模型说 %4.1f%%  实际 %4.1f%%  "
              "目标要求再升 %+.1f°C" % (
            name, len(sel), 100*statistics.mean(r["p_t"] for r in sel),
            100*sum(1 for r in sel if r["target"] == r["truth"])/len(sel),
            statistics.median([r["d"] for r in sel if r["d"] is not None])))

print("\n=== 12  目标桶要求的升温幅度 × 实际发生率（全部 104）")
print("  需要再升      n   模型说   实际")
for lo, hi in ((-99, 1), (1, 2), (2, 3), (3, 99)):
    sel = [r for r in rows if r["d"] is not None and lo <= r["d"] < hi]
    if sel:
        print("  %+4.0f~%+3.0f°C   %3d   %4.0f%%   %4.0f%%" % (
            lo, hi, len(sel), 100*statistics.mean(r["p_t"] for r in sel),
            100*sum(1 for r in sel if r["target"] == r["truth"])/len(sel)))
