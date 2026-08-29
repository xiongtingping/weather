"""Replay the real geometry builder over every P0 root, before and after.

Calls _bucket_geometry_c itself rather than restating its rule, so the check
cannot drift from the code the way a hand-written predicate does.
"""
import json, re, sqlite3, sys, importlib, statistics
sys.path.insert(0, ".")
import bayes_basket_target.p1_compiler as pc

class Root:
    def __init__(self, probs, ords, lineage):
        self.probabilities = probs
        self.bucket_ordinals = ords
        self.lineage = lineage

def upper_label(bucket):
    if "or higher" in bucket or "or above" in bucket:
        return None
    m = re.findall(r"(-?\d+)", bucket)
    return float(m[-1]) if m else None

conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",
                       uri=True, timeout=120.0)
rows = conn.execute(
    "SELECT checkpoint_id,probabilities_json,bucket_ordinals_json,lineage_json"
    " FROM basket_probability_envelope WHERE stage='P0'").fetchall()

changed = same = failed = 0
deltas = []
for cp, pj, oj, lj in rows:
    probs = json.loads(pj); ords = json.loads(oj)
    lineage = json.loads(lj or "{}")
    unit = "F" if any("°F" in b for b in probs) else "C"
    upper_by = {b: upper_label(b) for b in probs}
    root = Root(probs, ords, lineage)
    try:
        c_new, u_new = pc._bucket_geometry_c(root, unit=unit, upper_by_bucket=upper_by)
    except Exception:
        failed += 1
        continue
    iv = lineage.get("bucket_intervals_c_json")
    if not iv:
        continue
    truth = json.loads(iv)
    diff = max(
        abs(u_new[b] - float(truth[b][1]))
        for b in u_new if truth.get(b) and truth[b][1] is not None)
    deltas.append(diff)
    if diff > 1e-9:
        changed += 1
    else:
        same += 1

print("P0 根 %d 个；几何重建失败 %d" % (len(rows), failed))
print("修后上界与根自报区间完全一致: %d" % same)
print("仍有偏差:                   %d" % changed)
if deltas:
    print("最大偏差 %.2e，中位 %.2e" % (max(deltas), statistics.median(deltas)))
