"""H2 follow-ups: (a) does book staleness manufacture the tail edge?
(b) exactly how many of the 19 carry executable size?"""
import pickle, statistics
from datetime import datetime
SCRATCH = ("/private/tmp/claude-501/-Users-xiong-weather/"
           "4ef1fc86-6e89-403a-8a21-dfce91c6b976/scratchpad/h2_rows.pkl")
rows = pickle.load(open(SCRATCH, "rb"))
p = lambda s: datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def age(r):
    bo = r["tgt"].get("book_observed_at")
    return None if not bo else (p(r["at"]) - p(bo)).total_seconds() / 60.0


def lad(b):
    return sorted([(float(l["price"]), float(l["shares"]))
                   for l in (b.get("ask_levels") or [])])


BANDS = [("edge<=0", lambda r: r["edge"] <= 0),
         ("0..0.10", lambda r: 0 < r["edge"] <= .10),
         ("0.10-0.25", lambda r: .10 < r["edge"] <= .25),
         (">0.25", lambda r: r["edge"] > .25)]
print("=== (a) 只保留盘口年龄 <60 分钟的行 ===")
fresh = [r for r in rows if (age(r) is not None and age(r) < 60)]
print("fresh n=%d / %d" % (len(fresh), len(rows)))
for name, f in BANDS:
    s = [r for r in fresh if f(r)]
    if not s:
        print("  %-10s n=0"% name); continue
    print("  %-10s n=%-3d  model %.0f%%  market %.0f%%  happened %.0f%% (%d/%d)" % (
        name, len(s), 100 * statistics.mean(x["p_t"] for x in s),
        100 * statistics.mean(x["ask"] for x in s),
        100 * sum(x["won"] for x in s) / len(s), sum(x["won"] for x in s), len(s)))

print()
print("=== (b) 19 个尾部案例的可执行规模 ===")
tail = [r for r in rows if r["edge"] > .25]
for thr in (10, 30, 100, 300):
    n = sum(1 for r in tail
            if sum(s * pr for pr, s in lad(r["tgt"]) if pr < r["p_t"]) >= thr)
    print("  在模型概率之下能吃到 >= $%-4d 的: %2d/19 (%.0f%%)" % (thr, n, 100*n/19))
for thr in (10, 100, 1000):
    n = sum(1 for r in tail
            if sum(s for pr, s in lad(r["tgt"]) if pr == lad(r["tgt"])[0][0]) >= thr)
    print("  best_ask 档位股数 >= %-5d 的: %2d/19 (%.0f%%)" % (thr, n, 100*n/19))
print("  ask_levels 档位数中位 %.0f（不是孤零零一张挂单）" % statistics.median(
    len(lad(r["tgt"])) for r in tail))
print("  目标桶 best_ask 档位股数 总和 %.0f 股，全部 19 例合计名义 $%.2f" % (
    sum(sum(s for pr, s in lad(r["tgt"]) if pr == lad(r["tgt"])[0][0]) for r in tail),
    sum(sum(s*pr for pr, s in lad(r["tgt"]) if pr == lad(r["tgt"])[0][0]) for r in tail)))
