"""Where does the truth land on the mean-mode axis?

For every settled P0 intended station-day: mean = the continuous
mass-weighted centre ordinal of the served distribution, mode = the
heaviest bucket's ordinal, truth = the settled bucket's ordinal.
Project the truth onto the mean->mode axis:

    proj = (truth - mean) * sign(mode - mean)   in rungs

so proj < 0 means the truth sits on the far side of the mean (away from
the mode), 0 <= proj <= |mode - mean| means it landed between the two,
and proj > |mode - mean| means it overshot past the mode.  Charter-55
days (|mean - mode| >= half a rung) are the population the claim is
about; the looser any-gap cut is shown for reference.
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, "/Users/xiong/weather")

SNAPSHOT = sys.argv[1] if len(sys.argv) > 1 else ""
if SNAPSHOT and os.path.exists(SNAPSHOT):
    decoded = json.load(open(SNAPSHOT))
    rows = [
        (r["event_date"], r["station_id"], r["probabilities_json"],
         r["settled_bucket"])
        for r in decoded
    ]
    print(f"(snapshot: {SNAPSHOT}, {len(rows)} rows)")
else:
    conn = sqlite3.connect(
        "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",
        uri=True,
    )
    rows = conn.execute(
        "SELECT p.event_date, p.station_id, p.probabilities_json,"
        " s.settled_bucket"
        " FROM paper_trade_position p"
        " JOIN paper_trade_settlement s"
        " ON s.station_id = p.station_id AND s.event_date = p.event_date"
        " WHERE p.stage = 'P0'"
    ).fetchall()

seen = set()
samples = []
for event_date, station, prob_json, settled in rows:
    if (event_date, station) in seen:
        continue
    seen.add((event_date, station))
    probabilities = {
        str(k): float(v) for k, v in (json.loads(prob_json) or {}).items()
    }
    ordinals = {b: i for i, b in enumerate(probabilities)}
    if str(settled) not in ordinals:
        continue
    total = sum(probabilities.values())
    if total <= 0:
        continue
    mean_ord = sum(
        p * ordinals[b] for b, p in probabilities.items()
    ) / total
    mode_ord = max(ordinals, key=lambda b: (probabilities[b], -ordinals[b]))
    mode_ord = ordinals[mode_ord]
    truth_ord = ordinals[str(settled)]
    samples.append((event_date, station, mean_ord, mode_ord, truth_ord))


def report(title, members):
    n = len(members)
    if not n:
        return
    d_mean = [abs(t - me) for _, _, me, mo, t in members]
    d_mode = [abs(t - mo) for _, _, me, mo, t in members]
    print(f"=== {title}  n={n} ===")
    print(
        f"  |真值-mean| 均值 {sum(d_mean)/n:.2f} 桶"
        f"   |真值-mode| 均值 {sum(d_mode)/n:.2f} 桶"
        f"   mean更近的日子 {sum(1 for a, b in zip(d_mean, d_mode) if a < b)}"
        f" / mode更近 {sum(1 for a, b in zip(d_mean, d_mode) if b < a)}"
        f" / 等距 {sum(1 for a, b in zip(d_mean, d_mode) if a == b)}"
    )
    toward = 0
    away = 0
    between = 0
    far_mean = 0
    past_mode = 0
    projs = []
    for _, _, me, mo, t in members:
        gap = mo - me
        sign = 1 if gap > 0 else -1
        proj = (t - me) * sign
        projs.append(proj / abs(gap))
        if proj > 0:
            toward += 1
        elif proj < 0:
            away += 1
        if proj < 0:
            far_mean += 1
        elif proj <= abs(gap):
            between += 1
        else:
            past_mode += 1
    print(
        f"  真值偏向 mode 一侧 {toward}/{n}={toward/n:.0%}"
        f"   偏向 mean 外侧 {away}/{n}={away/n:.0%}"
    )
    print(
        f"  落点三段: mean外侧 {far_mean}"
        f" | mean与mode之间(含两端) {between}"
        f" | 越过mode {past_mode}"
    )
    projs.sort()
    med = projs[len(projs) // 2]
    print(
        f"  归一化落点 t=(truth-mean)/(mode-mean):"
        f" 中位 {med:+.2f}  均值 {sum(projs)/len(projs):+.2f}"
        f"   (0=mean, 1=mode)"
    )
    print()


half = [s for s in samples if abs(s[3] - s[2]) >= 0.5]
any_gap = [s for s in samples if abs(s[3] - s[2]) > 1e-9]
report("宪章55偏斜日 |mean-mode|>=0.5桶", half)
report("任意分歧 gap>0 (参考)", any_gap)
print(f"全体已结算 {len(samples)}，其中零分歧(mean恰在mode) "
      f"{len(samples) - len(any_gap)}")
