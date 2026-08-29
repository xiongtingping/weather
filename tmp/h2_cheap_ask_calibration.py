"""Is a 1-cent ask a verdict or noise? Three tests.

T1  Pool every out-of-window bucket in the cohort's snapshots and ask how often
    a bucket the market priced at <=0.02 actually settled -- split by the depth
    resting at that price. If the cheap quote were a token order, cheap+deep
    and cheap+thin would settle at the same rate.
T2  Poisson-binomial tail probability of the observed 0/19 under the model's
    own claims vs under the market's asks.
T3  Book staleness: how old is book_observed_at at the decision instant.
"""
import pickle, statistics
from datetime import datetime
SCRATCH = ("/private/tmp/claude-501/-Users-xiong-weather/"
           "4ef1fc86-6e89-403a-8a21-dfce91c6b976/scratchpad/h2_rows.pkl")
rows = pickle.load(open(SCRATCH, "rb"))


def parse(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def at_best(b):
    L = [(float(l["price"]), float(l["shares"])) for l in (b.get("ask_levels") or [])]
    if not L:
        return 0.0
    p0 = min(p for p, _ in L)
    return sum(s for p, s in L if p == p0)


pairs = []
for r in rows:
    for b, bb in r["book"].items():
        if b in r["held"] or not bb.get("best_ask"):
            continue
        pairs.append({"ask": float(bb["best_ask"]), "depth": at_best(bb),
                      "won": b == r["truth"], "p": r["probs"].get(b, 0.0)})

print("=== T1 市场报 <=0.02 的桶，实际兑现率 × 深度 ===")
cheap = [x for x in pairs if x["ask"] <= .02]
print("全部 cheap n=%d  实际兑现 %.1f%%  模型平均声称 %.1f%%" % (
    len(cheap), 100 * sum(x["won"] for x in cheap) / len(cheap),
    100 * statistics.mean(x["p"] for x in cheap)))
for lo, hi, name in ((0, 50, "薄 <50股"), (50, 500, "中 50-500股"),
                     (500, 5000, "厚 500-5000股"), (5000, 1e18, "很厚 >5000股")):
    s = [x for x in cheap if lo <= x["depth"] < hi]
    if s:
        print("  %-14s n=%4d  实际兑现 %5.2f%%  模型声称 %5.1f%%" % (
            name, len(s), 100 * sum(x["won"] for x in s) / len(s),
            100 * statistics.mean(x["p"] for x in s)))
rich = [x for x in pairs if x["ask"] > .02]
print("对照 ask>0.02 n=%d  实际兑现 %.1f%%  模型声称 %.1f%%" % (
    len(rich), 100 * sum(x["won"] for x in rich) / len(rich),
    100 * statistics.mean(x["p"] for x in rich)))

print()
print("=== T2 0/19 在两种信念下的概率 ===")
tail = [r for r in rows if r["edge"] > .25]


def p_zero(ps):
    q = 1.0
    for p in ps:
        q *= (1 - p)
    return q


print("模型信念 (均值 %.3f): P(0 命中) = %.2e" % (
    statistics.mean(r["p_t"] for r in tail), p_zero([r["p_t"] for r in tail])))
print("市场信念 (均值 %.3f): P(0 命中) = %.3f" % (
    statistics.mean(r["ask"] for r in tail), p_zero([r["ask"] for r in tail])))

print()
print("=== T3 盘口新鲜度（book_observed_at 相对决策时刻） ===")
for name, f in (("edge>0.25", lambda r: r["edge"] > .25),
                ("edge<=0", lambda r: r["edge"] <= 0)):
    s = [r for r in rows if f(r)]
    ages = []
    for r in s:
        bo = r["tgt"].get("book_observed_at")
        if bo:
            ages.append((parse(r["at"]) - parse(bo)).total_seconds() / 60.0)
    print("  %-10s n=%-3d 盘口年龄中位 %.1f 分钟, 90分位 %.1f 分钟, 最大 %.1f" % (
        name, len(s), statistics.median(ages),
        sorted(ages)[int(.9 * len(ages))], max(ages)))

print()
print("=== T4 那 19 个里，目标桶的 ask 有没有真的被 0.001 地板压住 ===")
n_floor = sum(1 for r in tail if r["ask"] <= .001)
print("ask==0.001(最小报价单位) 的有 %d/%d；ask>0.001 的 %d 个 ask 中位 %.3f" % (
    n_floor, len(tail), len(tail) - n_floor,
    statistics.median([r["ask"] for r in tail if r["ask"] > .001])))

print()
print("=== T5 便宜(ask<=0.02)的桶里，按模型声称分层的实际兑现 ===")
for lo, hi in ((0, .05), (.05, .12), (.12, .20), (.20, 1.01)):
    s = [x for x in cheap if lo <= x["p"] < hi]
    if s:
        print("  模型声称 %.2f-%.2f  n=%4d  实际兑现 %.1f%% (%d/%d)  "
              "深度中位 %.0f股" % (lo, hi, len(s),
              100 * sum(x["won"] for x in s) / len(s),
              sum(x["won"] for x in s), len(s),
              statistics.median(x["depth"] for x in s)))
print("  对照 ask>0.02 且 模型声称>=0.20: n=%d 实际兑现 %.1f%%" % (
    len([x for x in rich if x["p"] >= .20]),
    100 * sum(x["won"] for x in rich if x["p"] >= .20)
    / max(1, len([x for x in rich if x["p"] >= .20]))))
