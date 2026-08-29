"""H2 verdict: depth behind the 1-cent ask on the out-of-window target."""
import pickle, statistics
SCRATCH = ("/private/tmp/claude-501/-Users-xiong-weather/"
           "4ef1fc86-6e89-403a-8a21-dfce91c6b976/scratchpad/h2_rows.pkl")
rows = pickle.load(open(SCRATCH, "rb"))


def lad(b):
    return sorted([(float(l["price"]), float(l["shares"]))
                   for l in (b.get("ask_levels") or [])])


def at_best(b):
    L = lad(b)
    if not L:
        return 0.0, 0.0
    p0 = L[0][0]
    sh = sum(s for p, s in L if p == p0)
    return sh, sh * p0


def cum_upto(b, cap):
    """shares / notional available at prices strictly below `cap`."""
    sh = sum(s for p, s in lad(b) if p < cap)
    no = sum(s * p for p, s in lad(b) if p < cap)
    return sh, no


BANDS = [("edge>0.25", lambda r: r["edge"] > .25),
         ("edge0.10-0.25", lambda r: .10 < r["edge"] <= .25),
         ("edge0-0.10", lambda r: 0 < r["edge"] <= .10),
         ("edge<=0", lambda r: r["edge"] <= 0)]

print("=== 1. 目标桶 best_ask 处的深度 ===")
print("%-16s %3s %6s %8s %9s %9s %8s" % (
    "band", "n", "ask中位", "档位股数中位", "档位名义$中位", "≥100股占比", "≥$10占比"))
for name, f in BANDS:
    s = [r for r in rows if f(r)]
    sh = [at_best(r["tgt"])[0] for r in s]
    no = [at_best(r["tgt"])[1] for r in s]
    print("%-16s %3d %6.3f %8.0f %9.2f %9.0f%% %8.0f%%" % (
        name, len(s), statistics.median(r["ask"] for r in s),
        statistics.median(sh), statistics.median(no),
        100 * sum(1 for x in sh if x >= 100) / len(s),
        100 * sum(1 for x in no if x >= 10) / len(s)))

print()
print("=== 2. 能在'模型概率以下'买到多少（真实可套利容量） ===")
for name, f in BANDS:
    s = [r for r in rows if f(r)]
    cs = [cum_upto(r["tgt"], r["p_t"]) for r in s]
    print("%-16s n=%-3d 累计股数中位 %8.0f  累计名义$中位 %8.2f  "
          "上限$>=30占比 %.0f%%" % (
        name, len(s), statistics.median(x[0] for x in cs),
        statistics.median(x[1] for x in cs),
        100 * sum(1 for x in cs if x[1] >= 30) / len(s)))

print()
print("=== 3. 买方是否也在这个价位（bid 侧） ===")
for name, f in BANDS:
    s = [r for r in rows if f(r)]
    ba = [r["tgt"].get("bid_available") for r in s]
    bp = [float(r["tgt"].get("bid_price") or 0) for r in s]
    spr = [float(r["ask"]) - b for r, b in zip(s, bp)]
    print("%-16s n=%-3d bid_available %3.0f%%  bid_price中位 %.3f  "
          "价差中位 %.3f" % (name, len(s), 100 * sum(1 for x in ba if x) / len(s),
                            statistics.median(bp), statistics.median(spr)))

print()
print("=== 4. 同一快照内：目标桶 vs 市场定价高的桶（best_ask>=0.10） ===")
for name, f in BANDS:
    s = [r for r in rows if f(r)]
    rich_sh, rich_no, tsh, tno = [], [], [], []
    for r in s:
        for b, bb in r["book"].items():
            if not bb.get("best_ask"):
                continue
            a, n = at_best(bb)
            if float(bb["best_ask"]) >= .10:
                rich_sh.append(a); rich_no.append(n)
        a, n = at_best(r["tgt"])
        tsh.append(a); tno.append(n)
    if not rich_sh:
        continue
    print("%-16s 目标桶 股数中位 %7.0f / $%7.2f   富价桶 n=%4d 股数中位 %7.0f / $%7.2f"
          % (name, statistics.median(tsh), statistics.median(tno),
             len(rich_sh), statistics.median(rich_sh), statistics.median(rich_no)))

print()
print("=== 5. 19 个尾部案例逐条 ===")
print("%-22s %-18s %5s %5s %8s %9s %8s %6s" % (
    "station-day", "target", "p", "ask", "股数@best", "$@best", "累计$<p", "bid"))
for r in sorted([x for x in rows if x["edge"] > .25], key=lambda x: -x["edge"]):
    sh, no = at_best(r["tgt"])
    cs, cn = cum_upto(r["tgt"], r["p_t"])
    print("%-22s %-18s %5.2f %5.3f %8.0f %9.2f %8.2f %6.3f" % (
        "%s %s" % r["k"], r["target"][:18], r["p_t"], r["ask"], sh, no, cn,
        float(r["tgt"].get("bid_price") or 0)))
