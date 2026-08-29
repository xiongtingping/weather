"""H5: is the out-of-window argmax failure just "the distribution is too fat
N buckets from centre"?  Measure claimed-vs-actual by distance from the
distribution's own mode, over EVERY bucket at the same instants, then place
the high-edge targets on that curve.
"""
import json, pickle, sys, statistics, collections
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import local_close, parse

C = pickle.load(open("tmp/h5_cache.pkl", "rb"))
pos, truth, tz, chains, books = (C["pos"], C["truth"], C["tz"], C["chains"], C["books"])
RANK = {"P1": 2, "P0-M": 1, "P0": 0}
ALIVE = 0.10          # reproduces the reference n=104 population


def asks_at(k, when):
    h = books.get(k)
    if not h:
        return {}
    i = bisect_right([x[0] for x in h], when)
    return h[i - 1][1] if i else {}


def select():
    """Stage-first, then latest, before local close, held window still alive."""
    out = {}
    for k, e in pos.items():
        if k not in truth or k not in chains or k not in tz:
            continue
        close = local_close(k[1], tz[k])
        best = None
        for stage, at, env in chains[k]:
            if at < e["opened"] or parse(at) >= close:
                continue
            if sum(v for b, v in env["probabilities"].items()
                   if b in e["held"]) <= ALIVE:
                continue
            r = RANK.get(stage, 0)
            if best is None or (r, at) >= (best[0], best[1]):
                best = (r, at, stage, env)
        if best is not None:
            out[k] = best
    return out


sel = select()
print("station-days with a believable instant: %d" % len(sel))

# ---------- every bucket at those instants, tagged with distance ----------
allb, targets = [], []
for k, (_, at, stage, env) in sel.items():
    p, o = env["probabilities"], env["bucket_ordinals"]
    tb = truth[k][0]
    if tb not in o:
        continue
    mode = max(p, key=p.get)
    mode_ord = o[mode]
    lin = env.get("lineage") or {}
    mc = lin.get("model_center_bucket")
    mc_ord = o.get(mc) if mc else None
    held_ords = sorted(o[b] for b in pos[k]["held"] if b in o)
    asks = asks_at(k, at)
    for b, v in p.items():
        if b not in o:
            continue
        allb.append({"k": k, "b": b, "p": v, "d": o[b] - mode_ord,
                     "dmc": (o[b] - mc_ord) if mc_ord is not None else None,
                     "won": b == tb, "held": b in pos[k]["held"],
                     "ask": asks.get(b),
                     "d_held": 0 if o[b] in held_ords else
                               (o[b] - held_ords[-1] if o[b] > held_ords[-1]
                                else o[b] - held_ords[0]) if held_ords else None})
    outside = {b: v for b, v in p.items() if b not in pos[k]["held"] and b in o}
    if not outside:
        continue
    t = max(outside, key=outside.get)
    ask = asks.get(t)
    if ask is None:
        continue
    targets.append({"k": k, "b": t, "p": outside[t], "ask": ask,
                    "edge": outside[t] - ask, "d": o[t] - mode_ord,
                    "dmc": (o[t] - mc_ord) if mc_ord is not None else None,
                    "won": t == tb, "truth_d": o[tb] - mode_ord,
                    "mode_ord": mode_ord, "held": held_ords,
                    "d_held": (o[t] - held_ords[-1] if o[t] > held_ords[-1]
                               else o[t] - held_ords[0]) if held_ords else None})
print("targets with a book quote: %d" % len(targets))
json.dump({"n_allb": len(allb)}, open("/dev/null", "w"))


def rel(name, sel_rows):
    if not sel_rows:
        print("  %-24s (none)" % name); return
    c = sum(r["p"] for r in sel_rows) / len(sel_rows)
    a = sum(r["won"] for r in sel_rows) / len(sel_rows)
    print("  %-24s n=%-5d 声称 %5.1f%%  实际 %5.1f%%  实际/声称 %s" % (
        name, len(sel_rows), 100 * c, 100 * a,
        ("%.2f" % (a / c)) if c > 0 else "—"))


print("\n=== 1. 全部桶：按'离本分布众数的距离'分箱 (|d|) ===")
for lo, hi, nm in ((0, 0, "d=0 (众数本身)"), (1, 1, "|d|=1"), (2, 2, "|d|=2"),
                   (3, 3, "|d|=3"), (4, 99, "|d|>=4")):
    rel(nm, [r for r in allb if lo <= abs(r["d"]) <= hi])

print("\n=== 2. 同样的距离，拆高侧 / 低侧 ===")
for d in (-6, -5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5, 6):
    s = [r for r in allb if r["d"] == d]
    if len(s) >= 15:
        rel("d=%+d" % d, s)
print("  --- 合并 ---")
rel("高侧 d>=+3", [r for r in allb if r["d"] >= 3])
rel("低侧 d<=-3", [r for r in allb if r["d"] <= -3])
rel("高侧 d>=+2", [r for r in allb if r["d"] >= 2])
rel("低侧 d<=-2", [r for r in allb if r["d"] <= -2])

print("\n=== 3. 那些高边际标的，离众数有多远？ ===")
hot = [t for t in targets if t["edge"] > .25]
mid = [t for t in targets if .10 < t["edge"] <= .25]
low = [t for t in targets if 0 < t["edge"] <= .10]
neg = [t for t in targets if t["edge"] <= 0]
for nm, s in (("边际>0.25", hot), ("边际0.10-0.25", mid),
              ("边际0-0.10", low), ("边际<=0", neg)):
    if not s:
        continue
    dist = collections.Counter(r["d"] for r in s)
    print("  %-14s n=%-3d  d中位=%+.0f  |d|中位=%.0f  声称%.0f%% 实际%.0f%%  d分布=%s"
          % (nm, len(s), statistics.median(r["d"] for r in s),
             statistics.median(abs(r["d"]) for r in s),
             100 * statistics.mean(r["p"] for r in s),
             100 * sum(r["won"] for r in s) / len(s),
             dict(sorted(dist.items()))))

print("\n=== 4. 关键判决：同距离 + 同声称概率的 '普通桶' 兑现了吗？ ===")
print("  (若普通桶也全灭 -> 就是宽度问题；若普通桶兑现 -> 是选桶/市场问题)")
for dmin, plo in ((1, .15), (2, .15), (3, .15), (1, .25), (2, .25)):
    pool = [r for r in allb if abs(r["d"]) >= dmin and r["p"] >= plo]
    tg = [t for t in targets if abs(t["d"]) >= dmin and t["p"] >= plo]
    tgk = {(t["k"], t["b"]) for t in tg}
    other = [r for r in pool if (r["k"], r["b"]) not in tgk]
    print("\n  |d|>=%d 且 声称>=%.2f" % (dmin, plo))
    rel("    全部这样的桶", pool)
    rel("    其中是高边际标的", [r for r in tg if r["edge"] > .25])
    rel("    其中是任意标的", tg)
    rel("    其余普通桶", other)
