"""Round 28 part 4 (熊先生 10-11 「偏度表能否解释对比的方向」): does our served
vector's skew -- mass right minus left of the market's mode at the fill --
explain the cluster's side on its off-mode first takers?  Also the market's own
lean, and the cluster's direction skill split by whether the skew agrees.

Events: rounds 25-27 (research window + forward); direction +1 = warmer than
the market mode (YES off the mode: the bucket's side; NO off the mode: the
other side).  Skill = truth's side matches theirs (truth off the mode only).
"""
import collections
import gzip
import json
import sqlite3
import sys
from pathlib import Path

B = Path(sys.argv[1])
E = json.load(gzip.open(B / "replicate_rows.json.gz", "rt"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
conn.execute("PRAGMA query_only=1")
labels = {(st, ed): b for st, ed, b in conn.execute(
    "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l WHERE event_date >= '2026-09-20'"
    " AND truth_revision_no=(SELECT max(truth_revision_no) FROM basket_station_day_settlement_label x"
    " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)")}
rows = []
for e in E:
    p = {int(k): v for k, v in e["p"].items()}
    q = {int(k): v for k, v in e["q"].items()}
    mode = e["mode_ord"]
    o, side = e["chosen"]["ord"], e["chosen"]["side"]
    if o == mode:
        continue
    their = (1 if o > mode else -1) * (1 if side == "Yes" else -1)
    v = sum(x for k, x in p.items() if k > mode) - sum(x for k, x in p.items() if k < mode)
    m = sum(x for k, x in q.items() if k > mode) - sum(x for k, x in q.items() if k < mode)
    lab = labels.get((e["st"], e["ed"]))
    inv = {val: int(k) for k, val in e["labels"].items()}
    t = inv.get(lab) if lab else None
    rows.append(dict(their=their, v=v, m=m, t=t, mode=mode, stage=e["stage"]))
print(f"off-mode first takers: {len(rows)}")


def line(label, sel):
    if len(sel) < 30:
        print(f"   {label:46s} n={len(sel)}")
        return
    agree = sum(1 for r in sel if (1 if r["v"] > 0 else -1) == r["their"]) / len(sel)
    magree = sum(1 for r in sel if (1 if r["m"] > 0 else -1) == r["their"]) / len(sel)
    print(f"   {label:46s} n={len(sel):4d}  their side = our skew's side {agree:.3f}   = market lean's side {magree:.3f}")


line("all", rows)
line("our vector skewed |lean| >= 0.25", [r for r in rows if abs(r["v"]) >= 0.25])
line("our vector skewed |lean| >= 0.40", [r for r in rows if abs(r["v"]) >= 0.40])
line("our vector near symmetric |lean| < 0.10", [r for r in rows if abs(r["v"]) < 0.10])
line("our skew and market lean point opposite ways", [r for r in rows if (r["v"] > 0) != (r["m"] > 0)])
for st in ("P0", "P0-M", "P1"):
    line(f"stage {st}", [r for r in rows if r["stage"] == st])

print("\n-- their direction skill (truth off the mode, settled): right side share")
scored = [r for r in rows if r["t"] is not None and r["t"] != r["mode"]]


def skill(label, sel):
    if len(sel) < 30:
        print(f"   {label:46s} n={len(sel)}")
        return
    right = sum(1 for r in sel if (1 if r["t"] > r["mode"] else -1) == r["their"]) / len(sel)
    vr = sum(1 for r in sel if (1 if r["t"] > r["mode"] else -1) == (1 if r["v"] > 0 else -1)) / len(sel)
    print(f"   {label:46s} n={len(sel):4d}  they are right {right:.3f}   our skew alone right {vr:.3f}")


skill("all", scored)
skill("our skew agrees with them", [r for r in scored if (1 if r["v"] > 0 else -1) == r["their"]])
skill("our skew disagrees with them", [r for r in scored if (1 if r["v"] > 0 else -1) != r["their"]])
skill("skewed |lean|>=0.25 and agrees", [r for r in scored if abs(r["v"]) >= 0.25 and (1 if r["v"] > 0 else -1) == r["their"]])
skill("skewed |lean|>=0.25 and disagrees", [r for r in scored if abs(r["v"]) >= 0.25 and (1 if r["v"] > 0 else -1) != r["their"]])
