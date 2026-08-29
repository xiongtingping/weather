"""Did standing still cost anything?  Price the move the book did not make.

Two counterfactuals at the last top-up-able instant, both priced on the book
that was actually there: buy the single out-of-window bucket the model likes
most, or rebuild a same-width contiguous window on the model's new centre.
Edge is the model's probability minus the all-in ask, so a positive edge is a
top-up the objective could have justified and a non-positive one is the
objective correctly refusing.
"""
import json, statistics

rows = [r for r in json.load(open("tmp/divergence_split.json")) if r["ask"] is not None]

def window_at(rec, centre_ord, width):
    lo = centre_ord - (width - 1) // 2
    picks = [b for b, o in rec["ord"].items() if lo <= o < lo + width]
    if not all(b in rec["asks"] for b in picks) or not picks:
        return None
    return picks, sum(rec["p_all"].get(b, 0.0) for b in picks), sum(rec["asks"][b] for b in picks)

for r in rows:
    r["edge1"] = r["p_target"] - r["ask"]
    r["cf1"] = (1.0 / r["ask"] - 1.0) if r["truth"] == r["target"] else -1.0
    r["actual"] = r["pnl"] / r["stake"] if r["stake"] else 0.0
    best = max(r["p_all"], key=r["p_all"].get)
    w = window_at(r, r["ord"][best], r["nheld"])
    r["cfw"] = None
    if w and w[2] < 1.0:
        picks, pw, cost = w
        r["edgew"] = pw - cost
        r["cfw"] = (1.0 / cost - 1.0) if r["truth"] in picks else -1.0
        r["moved"] = set(picks) != set(r["held"])

def block(name, sel):
    if not sel:
        print("  %-22s  (none)" % name); return
    print("  %-22s n=%3d  边际中位数 %+.3f  跟着动的回报 %+.2f/u  实际不动 %+.2f/u  跟着动命中 %2.0f%%"
          % (name, len(sel),
             statistics.median(r["edge1"] for r in sel),
             statistics.mean(r["cf1"] for r in sel),
             statistics.mean(r["actual"] for r in sel),
             100 * sum(1 for r in sel if r["truth"] == r["target"]) / len(sel)))

for label, pool in (("全部有盘口的站日", rows),
                    ("分家 >50%", [r for r in rows if r["out"] > .5]),
                    ("分家 30-50%", [r for r in rows if .3 < r["out"] <= .5]),
                    ("分家 <=30%", [r for r in rows if r["out"] <= .3])):
    print("\n%s  (n=%d)" % (label, len(pool)))
    block("边际>0 该买没买", [r for r in pool if r["edge1"] > 0])
    block("边际<=0 不买是对的", [r for r in pool if r["edge1"] <= 0])

hot = [r for r in rows if r["out"] > .5]
pos = [r for r in hot if r["edge1"] > 0]
print("\n--- >50%% 那一堆的判决 ---")
print("有盘口可判的: %d" % len(hot))
print("模型跑去的那格当时还有正边际(该买没买): %d (%.0f%%)"
      % (len(pos), 100 * len(pos) / len(hot)))
print("当时买不划算(不动是对的):          %d (%.0f%%)"
      % (len(hot) - len(pos), 100 * (len(hot) - len(pos)) / len(hot)))
print("\n如果全部跟着模型换到它最看好的那一格:")
print("  命中 %.0f%%   平均回报 %+.2f / 每 1u   合计 %+.1fu"
      % (100 * sum(1 for r in hot if r["truth"] == r["target"]) / len(hot),
         statistics.mean(r["cf1"] for r in hot), sum(r["cf1"] for r in hot)))
print("实际站着不动:")
print("  命中 %.0f%%   平均回报 %+.2f / 每 1u   合计 %+.1fu"
      % (100 * sum(r["hit"] for r in hot) / len(hot),
         statistics.mean(r["actual"] for r in hot), sum(r["actual"] for r in hot)))
wsel = [r for r in hot if r["cfw"] is not None]
if wsel:
    print("\n换成同宽度的窗、整篮重挂在模型新中心 (n=%d):" % len(wsel))
    print("  命中 %.0f%%   平均回报 %+.2f / 每 1u   合计 %+.1fu   其中真需要挪窗的 %d"
          % (100 * sum(1 for r in wsel if r["cfw"] > 0) / len(wsel),
             statistics.mean(r["cfw"] for r in wsel), sum(r["cfw"] for r in wsel),
             sum(1 for r in wsel if r["moved"])))
    print("  同一批站日实际不动: 平均 %+.2f / 每 1u  合计 %+.1fu"
          % (statistics.mean(r["actual"] for r in wsel), sum(r["actual"] for r in wsel)))
