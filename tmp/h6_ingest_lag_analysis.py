"""H6 decisive cut: was the chain missing an observation the market had?

Uses the cached phenomenon rows. For each row rebuilds, from
archive.reported_trajectory_observations, the set of observations for the
station's LOCAL day with valid_at <= decision_at -- everything physically
knowable at that instant -- and compares it against the chain's consumed set
(lineage canonical_observation_ids).
"""
import json, pickle, sqlite3, statistics, sys
sys.path.insert(0, "tmp")
from stage_pick import parse

CACHE = ("/private/tmp/claude-501/-Users-xiong-weather/"
         "4ef1fc86-6e89-403a-8a21-dfce91c6b976/scratchpad/h6_rows.pkl")
rows = pickle.load(open(CACHE, "rb"))
arc = sqlite3.connect("file:data/archive.db?mode=ro", uri=True, timeout=120.0)

obs_by_station = {}
for r in arc.execute("SELECT station_id,observation_id,valid_at,"
                     "reported_temperature_c,first_seen_at,canonical_committed_at,"
                     "report_type,correction_suspected"
                     " FROM reported_trajectory_observations"):
    obs_by_station.setdefault(str(r[0]), []).append(
        {"id": str(r[1]), "valid": str(r[2]), "t": r[3],
         "seen": str(r[4] or ""), "committed": str(r[5] or ""),
         "rt": str(r[6] or ""), "corr": r[7]})
for v in obs_by_station.values():
    v.sort(key=lambda x: x["valid"])

BANDS = (("edge>0.25", lambda r: r["edge"] > .25),
         ("edge0.10-0.25", lambda r: .10 < r["edge"] <= .25),
         ("edge0-0.10", lambda r: 0 < r["edge"] <= .10),
         ("edge<=0", lambda r: r["edge"] <= 0))

for r in rows:
    lo, hi, at = r["open_local"], r["close"], r["at"]
    day = [o for o in obs_by_station.get(r["k"][0], [])
           if lo <= o["valid"] < hi and o["t"] is not None]
    avail = [o for o in day if o["valid"] <= at and (o["seen"] or o["valid"]) <= at]
    consumed = set(r["ids"])
    got = [o for o in day if o["id"] in consumed]
    missed = [o for o in avail if o["id"] not in consumed]
    r["n_avail"] = len(avail)
    r["n_got"] = len(got)
    r["n_missed"] = len(missed)
    r["chain_max"] = max([o["t"] for o in got], default=None)
    r["avail_max"] = max([o["t"] for o in avail], default=None)
    r["missed_max"] = max([o["t"] for o in missed], default=None)
    r["newest_got"] = max([o["valid"] for o in got], default=None)
    r["newest_avail"] = max([o["valid"] for o in avail], default=None)
    iv = r["iv"].get(r["target"]) or [None, None]
    r["t_upper"] = iv[1]
    # production rule (metar_trajectory.py:1608): a bucket dies iff
    # upper_bound <= running_high + eps.  Applied to the running high the
    # chain HAD vs the one it COULD have had.
    EPS = 1e-9
    r["target_dead_chain"] = (r["t_upper"] is not None and r["chain_max"] is not None
                              and r["t_upper"] <= r["chain_max"] + EPS)
    r["target_dead_avail"] = (r["t_upper"] is not None and r["avail_max"] is not None
                              and r["t_upper"] <= r["avail_max"] + EPS)
    r["lag_min"] = ((parse(at) - parse(r["newest_got"])).total_seconds() / 60.0
                    if r["newest_got"] else None)
    r["ingest_gap_min"] = ((parse(r["newest_avail"]) - parse(r["newest_got"])).total_seconds() / 60.0
                           if r["newest_got"] and r["newest_avail"] else None)

print("== 1. 摄取滞后：决策时刻链上已消费 vs 当时物理可得的 METAR")
print("  band            n    可得条数 已消费 漏掉  最新已消费距决策(分,中位)  漏掉的最新观测(分,中位)")
for nm, f in BANDS:
    s = [r for r in rows if f(r) and r["n_avail"]]
    lag = [r["lag_min"] for r in s if r["lag_min"] is not None]
    gap = [r["ingest_gap_min"] for r in s if r["ingest_gap_min"] is not None]
    print("  %-15s %-3d  %6.1f %6.1f %5.1f  %10.0f  %10.0f" % (
        nm, len(s), statistics.mean(r["n_avail"] for r in s),
        statistics.mean(r["n_got"] for r in s),
        statistics.mean(r["n_missed"] for r in s),
        statistics.median(lag) if lag else float("nan"),
        statistics.median(gap) if gap else float("nan")))
print()

print("== 2. 漏掉的观测里有没有能杀死目标桶的（生产规则 upper<=running_high）")
print("  band            n   链上已判死 全量可得判死  '只有市场知道'的行数")
for nm, f in BANDS:
    s = [r for r in rows if f(r) and r["n_avail"]]
    only = [r for r in s if r["target_dead_avail"] and not r["target_dead_chain"]]
    print("  %-15s %-3d  %5d %5d  %5d  %s" % (
        nm, len(s), sum(r["target_dead_chain"] for r in s),
        sum(r["target_dead_avail"] for r in s), len(only),
        ",".join("%s %s" % (x["k"][0], x["k"][1][5:]) for x in only)))
print()

print("== 3. 决定性子集：链上已消费当时全部可得观测（漏掉=0）")
for nm, f in BANDS:
    s = [r for r in rows if f(r) and r["n_avail"] and r["n_missed"] == 0]
    if not s:
        print("  %-15s n=0" % nm); continue
    print("  %-15s n=%-3d 声称 %.0f%%  ask %.0f%%  实际发生 %.0f%%" % (
        nm, len(s), 100*statistics.mean(r["p_t"] for r in s),
        100*statistics.mean(r["ask"] for r in s),
        100*sum(r["won"] for r in s)/len(s)))
print()

print("== 3b. 放宽：链上已握住当日已出现的最高温（chain_max >= avail_max）")
for nm, f in BANDS:
    s = [r for r in rows if f(r) and r["chain_max"] is not None
         and r["avail_max"] is not None and r["chain_max"] >= r["avail_max"] - 1e-9]
    if not s:
        print("  %-15s n=0" % nm); continue
    print("  %-15s n=%-3d 声称 %.0f%%  ask %.0f%%  实际发生 %.0f%%  目标桶在已测高点之上 %.0f%%" % (
        nm, len(s), 100*statistics.mean(r["p_t"] for r in s),
        100*statistics.mean(r["ask"] for r in s),
        100*sum(r["won"] for r in s)/len(s),
        100*sum(1 for r in s if r["t_upper"] is None or r["t_upper"] > r["chain_max"])/len(s)))
print()

print("== 4. 目标桶相对链上已知高点的位置（全部行）")
for nm, f in BANDS:
    s = [r for r in rows if f(r) and r["chain_max"] is not None]
    above = [r for r in s if r["t_upper"] is None or r["t_upper"] > r["chain_max"]]
    print("  %-15s n=%-3d  目标桶在链上已知高点之上 %.0f%% (硬下界结构上砍不到)" % (
        nm, len(s), 100*len(above)/len(s)))
print()

print("== 5. 软项强度（strength=0 时观测除硬下界外完全不说话）")
for nm, f in BANDS:
    s = [r for r in rows if f(r)]
    st = [float(r["strength"]) for r in s if r["strength"] not in (None, "")]
    zero = sum(1 for x in st if x <= 1e-12)
    print("  %-15s n=%-3d  strength 中位 %.3f  为0的占 %.0f%%  常见降级原因 %s" % (
        nm, len(s), statistics.median(st) if st else float("nan"),
        100*zero/max(len(st),1),
        statistics.mode([r["degrade"] or "-" for r in s]) if s else "-"))
print()

print("== 6. tail 逐行 (edge>0.25)")
print("  %-14s %-12s %5s %5s %6s %6s %6s %5s %5s %6s" % (
    "station-day","target","p","ask","chainHi","availHi","上界","可得","消费","滞后h"))
for r in sorted([r for r in rows if r["edge"] > .25], key=lambda x: -x["edge"]):
    print("  %-14s %-12s %5.2f %5.2f %6s %6s %6s %5d %5d %6.1f  deadChain=%s deadAvail=%s" % (
        r["k"][0]+" "+r["k"][1][5:], r["target"][:12], r["p_t"], r["ask"],
        r["chain_max"], r["avail_max"],
        ("%.1f" % r["t_upper"]) if r["t_upper"] is not None else "inf",
        r["n_avail"], r["n_got"], (r["lag_min"] or 0)/60.0,
        r["target_dead_chain"], r["target_dead_avail"]))
