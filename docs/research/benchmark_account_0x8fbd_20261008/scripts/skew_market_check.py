"""Round 28 part 2 (熊先生 10-11, on a table of our vectors' skew: 「这个结论有没
有信息？」): a skewed served vector -- more mass on one side of its own mode --
does see the truth land on that side more often.  Is that beyond the market?

Per station-day and stage, the stage's decision vector (P0 and P0-M: the last
one before the next stage took over; P1: the first), grouped by L - R (mass
left minus right of the vector's own mode bucket): left-skewed L-R >= +0.25,
right-skewed <= -0.25, else symmetric.  For each group: where the truth
landed (left / the vector's mode bucket / right), what the vector said, and
what the market said on the same three segments at the same instant (newest
committed WS snapshot at most 30 minutes before the decision, normalised mid,
charter 249's market vector).  Then a three-way log loss of vector vs market
vs a 50/50 blend on the truth's segment.  Event days 09-12..10-09.
"""
import collections
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402

conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
labels = {(st, ed): b for st, ed, b in conn.execute(
    "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l WHERE event_date BETWEEN '2026-09-12' AND '2026-10-09'"
    " AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
    " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)")}
hist = collections.defaultdict(lambda: collections.defaultdict(list))
for hid, st, ed, stage, at in conn.execute(
        "SELECT history_id, station_id, event_date, stage, decision_at_utc FROM basket_probability_bridge_history"
        " WHERE state='COMMITTED' AND event_date BETWEEN '2026-09-12' AND '2026-10-09'"):
    if (st, ed) in labels:
        hist[(st, ed)][stage].append((str(at), hid))


def pick(stages):
    """The stage decisions the table reads: last P0 before the first P0-M, last P0-M before the first P1, first P1."""
    out = {}
    first = {s: min(v)[0] for s, v in stages.items() if v}
    for stage, nxt in (("P0", "P0-M"), ("P0-M", "P1")):
        rows = sorted(stages.get(stage, []))
        if not rows:
            continue
        if nxt in first:
            rows = [r for r in rows if r[0] < first[nxt]]
        if rows:
            out[stage] = rows[-1]
    if stages.get("P1"):
        out["P1"] = min(stages["P1"])
    return out


def market_at(st, ed, at):
    row = conn.execute(
        "SELECT buckets_json, observed_at FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (st, ed, at)).fetchone()
    if not row:
        return None
    from datetime import datetime
    age = (datetime.fromisoformat(at.replace("Z", "+00:00")) - datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))).total_seconds()
    if age > 1800:
        return None
    ref = market_reference_v1(json.loads(row[0]))
    return {str(k): float(v) for k, v in ref["vector"].items()} if ref.get("state") == "complete" else None


cells = collections.defaultdict(lambda: {"n": 0, "truth": [0, 0, 0], "vec": [0.0, 0.0, 0.0], "mkt": [0.0, 0.0, 0.0],
                                           "ll_v": 0.0, "ll_m": 0.0, "ll_b": 0.0, "with_mkt": 0})
for (st, ed), stages in hist.items():
    for stage, (at, hid) in pick(stages).items():
        rows = conn.execute("SELECT bucket, ordinal, probability FROM basket_probability_bridge_bucket_probability WHERE history_id=?",
                            (hid,)).fetchall()
        if not rows:
            continue
        tot = sum(r[2] for r in rows)
        if tot <= 0:
            continue
        vec = {str(b): (int(o), float(p) / tot) for b, o, p in rows}
        mode_b = max(vec, key=lambda b: (vec[b][1], -vec[b][0]))
        mo = vec[mode_b][0]
        L = sum(p for b, (o, p) in vec.items() if o < mo)
        R = sum(p for b, (o, p) in vec.items() if o > mo)
        M = vec[mode_b][1]
        group = "left-skewed" if L - R >= 0.25 else "right-skewed" if R - L >= 0.25 else "symmetric"
        truth = labels[(st, ed)]
        if truth not in vec:
            continue
        seg = 0 if vec[truth][0] < mo else 1 if vec[truth][0] == mo else 2
        c = cells[(stage, group)]
        c["n"] += 1
        c["truth"][seg] += 1
        for i, v in enumerate((L, M, R)):
            c["vec"][i] += v
        mkt = market_at(st, ed, at)
        if mkt and all(b in vec for b in mkt):
            mL = sum(p for b, p in mkt.items() if vec[b][0] < mo)
            mM = mkt.get(mode_b, 0.0)
            mR = sum(p for b, p in mkt.items() if vec[b][0] > mo)
            s = mL + mM + mR
            if s > 0:
                mv = [mL / s, mM / s, mR / s]
                vv = [L, M, R]
                bv = [(a + b) / 2 for a, b in zip(vv, mv)]
                c["with_mkt"] += 1
                for i in range(3):
                    c["mkt"][i] += mv[i]
                c["ll_v"] += -math.log(max(vv[seg], 1e-3))
                c["ll_m"] += -math.log(max(mv[seg], 1e-3))
                c["ll_b"] += -math.log(max(bv[seg], 1e-3))

print("stage  group          n    truth L/M/R          vector said           market said (n with a book)    3-way log loss vector / market / blend")
for stage in ("P0", "P0-M", "P1"):
    for group in ("left-skewed", "right-skewed", "symmetric"):
        c = cells[(stage, group)]
        if not c["n"]:
            continue
        t = [100 * x / c["n"] for x in c["truth"]]
        v = [100 * x / c["n"] for x in c["vec"]]
        k = c["with_mkt"] or 1
        m = [100 * x / k for x in c["mkt"]]
        print(f"{stage:5s}  {group:13s} {c['n']:5d}   {t[0]:3.0f}% / {t[1]:3.0f}% / {t[2]:3.0f}%   {v[0]:3.0f}% / {v[1]:3.0f}% / {v[2]:3.0f}%"
              f"   {m[0]:3.0f}% / {m[1]:3.0f}% / {m[2]:3.0f}% ({c['with_mkt']:4d})"
              f"   {c['ll_v']/k:.3f} / {c['ll_m']/k:.3f} / {c['ll_b']/k:.3f}")
