"""Round 28 part 3 (熊先生 10-11 「我说的偏度表是否有方向信息」): does the served
vector's skew say which side of the mode the truth lands on -- and does it say
more than the market's own lean at the same instant?

Same decisions as skew_market_check.py.  Two references:
 (1) the vector's own mode (the table's axis);
 (2) the market's mode (the axis the cluster's direction and our trades use).
For each: station-days whose truth is off that mode; the side the vector leans
(mass right minus left of the mode), the side the market leans, where the truth
went.  Skewed = |lean| >= 0.25 of the vector.  Reported: how often each lean
calls the truth's side, and on the station-days where the vector's and the
market's leans point opposite ways, who was right.
"""
import collections
import json
import sqlite3
import sys

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402

exec(open(__file__.replace("skew_direction_check.py", "skew_market_check.py")).read().split("cells = collections.defaultdict")[0])

acc = collections.defaultdict(lambda: collections.Counter())
for (st, ed), stages in hist.items():
    for stage, (at, hid) in pick(stages).items():
        rows = conn.execute("SELECT bucket, ordinal, probability FROM basket_probability_bridge_bucket_probability WHERE history_id=?",
                            (hid,)).fetchall()
        tot = sum(r[2] for r in rows) if rows else 0
        if tot <= 0:
            continue
        vec = {str(b): (int(o), float(p) / tot) for b, o, p in rows}
        truth = labels[(st, ed)]
        mkt = market_at(st, ed, at)
        if truth not in vec or not mkt or not all(b in vec for b in mkt):
            continue
        tor = vec[truth][0]
        v_mode = vec[max(vec, key=lambda b: (vec[b][1], -vec[b][0]))][0]
        m_mode = vec[max(mkt, key=lambda b: (mkt[b], -vec[b][0]))][0]
        for ref_name, mo in (("vector's own mode", v_mode), ("market's mode", m_mode)):
            if tor == mo:
                continue
            truth_side = 1 if tor > mo else -1
            v_lean = sum(p for b, (o, p) in vec.items() if o > mo) - sum(p for b, (o, p) in vec.items() if o < mo)
            m_lean = sum(p for b, p in mkt.items() if vec[b][0] > mo) - sum(p for b, p in mkt.items() if vec[b][0] < mo)
            skewed = abs(v_lean) >= 0.25
            for subset in (("all", True), ("vector skewed |lean|>=0.25", skewed)):
                if not subset[1]:
                    continue
                c = acc[(stage, ref_name, subset[0])]
                c["n"] += 1
                c["v_right"] += (1 if v_lean > 0 else -1) == truth_side
                c["m_right"] += (1 if m_lean > 0 else -1) == truth_side
                if (v_lean > 0) != (m_lean > 0):
                    c["disagree"] += 1
                    c["dis_v_right"] += (1 if v_lean > 0 else -1) == truth_side

print("stage  axis                subset                        n   vector lean right   market lean right   leans disagree: n, vector right")
for stage in ("P0", "P0-M", "P1"):
    for ref_name in ("vector's own mode", "market's mode"):
        for subset in ("all", "vector skewed |lean|>=0.25"):
            c = acc[(stage, ref_name, subset)]
            if not c["n"]:
                continue
            dis = c["disagree"]
            print(f"{stage:5s}  {ref_name:18s}  {subset:27s} {c['n']:5d}   {c['v_right']/c['n']:.3f}               {c['m_right']/c['n']:.3f}"
                  f"               {dis:4d}, {c['dis_v_right']/dis if dis else float('nan'):.3f}")
