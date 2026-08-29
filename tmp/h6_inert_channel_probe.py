"""H6 closing measurements.

(a) cross-check my reconstructed running high against the chain's OWN
    lineage.hard_excluded_buckets (production authority, not a hand copy);
(b) count rows where the missed observations are provably INERT: the target
    sits above the chain's running high (hard bound cannot reach it) AND the
    soft likelihood channel is off (applied_strength == 0), so a perfectly
    fresh observation set could not have moved the target's probability;
(c) ask the market side: was the 1-cent price already standing back when the
    chain WAS current, i.e. before the observations the chain missed existed?
"""
import json, pickle, sqlite3, statistics, sys
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import DB, parse

CACHE = ("/private/tmp/claude-501/-Users-xiong-weather/"
         "4ef1fc86-6e89-403a-8a21-dfce91c6b976/scratchpad/h6_rows.pkl")
rows = pickle.load(open(CACHE, "rb"))
conn = sqlite3.connect(DB, uri=True, timeout=120.0)
arc = sqlite3.connect("file:data/archive.db?mode=ro", uri=True, timeout=120.0)

obs = {}
for r in arc.execute("SELECT station_id,observation_id,valid_at,reported_temperature_c"
                     " FROM reported_trajectory_observations"):
    obs.setdefault(str(r[0]), []).append((str(r[2]), str(r[1]), r[3]))
for v in obs.values():
    v.sort()

keys = sorted({r["k"] for r in rows})
ph = ",".join(["(?,?)"] * len(keys))
books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot"
                      " WHERE (station_id,event_date) IN (VALUES %s)"
                      " ORDER BY observed_at" % ph,
                      [x for k in keys for x in k]):
    books.setdefault((str(r[0]), str(r[1])), []).append((str(r[2]), r[3]))

def ask_at(k, when, bucket):
    h = books.get(k) or []
    i = bisect_right([x[0] for x in h], when)
    if not i:
        return None
    for b in json.loads(h[i - 1][1]):
        if b["bucket"] == bucket:
            return b.get("best_ask")
    return None

EPS = 1e-9
tail = []
for r in rows:
    lo, hi, at = r["open_local"], r["close"], r["at"]
    day = [o for o in obs.get(r["k"][0], []) if lo <= o[0] < hi and o[2] is not None]
    consumed = set(r["ids"])
    got = [o for o in day if o[1] in consumed]
    avail = [o for o in day if o[0] <= at]
    r["chain_max"] = max([o[2] for o in got], default=None)
    r["avail_max"] = max([o[2] for o in avail], default=None)
    r["newest_got"] = max([o[0] for o in got], default=None)
    r["n_missed"] = len([o for o in avail if o[1] not in consumed])
    r["rh"] = float(r["run_high_c"]) if r["run_high_c"] not in (None, "") else None
    r["ex"] = set(x for x in r["hard_ex"].split(",") if x)
    iv = r["iv"].get(r["target"]) or [None, None]
    r["t_upper"] = iv[1]
    r["strength_f"] = float(r["strength"]) if r["strength"] not in (None, "") else None
    r["target_excluded_by_chain"] = r["target"] in r["ex"]
    r["above_rh"] = (r["rh"] is None) or (r["t_upper"] is None) or (r["t_upper"] > r["rh"] + EPS)

print("== A. 我重建的 running high 与链上 lineage 的一致性（不手抄规则，用链上自己的字段核对）")
ok = [r for r in rows if r["rh"] is not None and r["chain_max"] is not None]
same = sum(1 for r in ok if abs(r["rh"] - r["chain_max"]) < 0.51)
print("   n=%d  |lineage running_high - 重建chain_max| < 0.51°C 的占 %.0f%%" % (len(ok), 100*same/len(ok)))
mis = [r for r in ok if abs(r["rh"] - r["chain_max"]) >= 0.51]
for r in mis[:8]:
    print("     不一致 %s %s lineage=%.1f 重建=%.1f" % (r["k"][0], r["k"][1], r["rh"], r["chain_max"]))
print()
print("   目标桶被链上 hard_excluded_buckets 判死的行数：%d / %d（全样本）" % (
    sum(r["target_excluded_by_chain"] for r in rows), len(rows)))
print()

BANDS = (("edge>0.25", lambda r: r["edge"] > .25),
         ("edge0.10-0.25", lambda r: .10 < r["edge"] <= .25),
         ("edge0-0.10", lambda r: 0 < r["edge"] <= .10),
         ("edge<=0", lambda r: r["edge"] <= 0))

print("== B. 漏掉的观测是否 provably 无效（目标在硬下界够不着的上方 且 软项 strength=0）")
print("   band            n   目标在running high之上  strength=0  两者兼具(观测再新也动不了)")
for nm, f in BANDS:
    s = [r for r in rows if f(r)]
    a = [r for r in s if r["above_rh"]]
    b = [r for r in s if r["strength_f"] == 0.0]
    c = [r for r in s if r["above_rh"] and r["strength_f"] == 0.0]
    print("   %-15s %-3d  %5d (%3.0f%%)  %5d (%3.0f%%)  %5d (%3.0f%%)" % (
        nm, len(s), len(a), 100*len(a)/len(s), len(b), 100*len(b)/len(s),
        len(c), 100*len(c)/len(s)))
print()

print("== C. 市场的 1 分钱是不是等新观测才出现的")
print("   tail 行：目标桶的 best_ask 在'链上最后一条已消费观测的时刻' vs 决策时刻")
tail = [r for r in rows if r["edge"] > .25]
pairs = []
for r in tail:
    if not r["newest_got"]:
        continue
    a_then = ask_at(r["k"], r["newest_got"], r["target"])
    if a_then is None:
        continue
    pairs.append((r, float(a_then)))
print("   n=%d  链上最后已消费观测时刻的 ask 中位 %.3f  决策时刻 ask 中位 %.3f" % (
    len(pairs), statistics.median(x[1] for x in pairs),
    statistics.median(r["ask"] for r, _ in pairs)))
print("   那一刻 ask 已 <=0.05 的占 %.0f%%   模型当时的声称 %.0f%%" % (
    100*sum(1 for _, a in pairs if a <= .05)/len(pairs),
    100*statistics.mean(r["p_t"] for r, _ in pairs)))
print()
print("   最早出现 <=0.05 报价的时刻，距离链上最后已消费观测多久（负=早于它，市场没等新观测）")
early = []
for r in tail:
    h = books.get(r["k"]) or []
    first = None
    for ts, blob in h:
        for b in json.loads(blob):
            if b["bucket"] == r["target"] and b.get("best_ask") is not None \
                    and float(b["best_ask"]) <= .05:
                first = ts
                break
        if first:
            break
    if first and r["newest_got"]:
        early.append((r, (parse(first) - parse(r["newest_got"])).total_seconds()/60.0))
print("   n=%d  中位 %+.0f 分钟  早于链上最后已消费观测的占 %.0f%%" % (
    len(early), statistics.median(x[1] for x in early),
    100*sum(1 for _, d in early if d < 0)/len(early)))
print()

print("== D. 零滞后子集逐行（链上已消费当时全部可得观测）")
for r in sorted([r for r in rows if r["n_missed"] == 0], key=lambda x: -x["edge"])[:12]:
    print("   %-6s %s  target=%-13s p=%.2f ask=%.2f edge=%+.2f won=%s  strength=%s" % (
        r["k"][0], r["k"][1], r["target"][:13], r["p_t"], r["ask"], r["edge"],
        r["won"], r["strength"]))
