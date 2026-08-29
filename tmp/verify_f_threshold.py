"""Does the old constant explain every exclusion we actually see?

If the leak is the unit mix-up in the margin, then the committed exclusion sets
should match what the old formula predicts, bucket for bucket, and the
corrected constant should match the true half-open edges instead.  Anything the
new constant would kill that the true edge keeps alive would be over-exclusion,
which is the one failure mode worth more than the leak.
"""
import json, re, sqlite3, sys
sys.path.insert(0, "tmp")
from stage_pick import DB

def upper_label(bucket):
    """The bucket's upper label value in market units, None if open-ended."""
    if "or higher" in bucket or "or above" in bucket:
        return None
    m = re.findall(r"(-?\d+)", bucket)
    return float(m[-1]) if m else None

old = lambda v, u: v + 1.0 if u == "C" else (v - 30.6) / 1.8
new = lambda v, u: v + 1.0 if u == "C" else (v - 31.0) / 1.8

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
stats = {"rows": 0, "old_match": 0, "leak_old": 0, "leak_new": 0,
         "over_new": 0, "over_old": 0}
examples = []
for r in conn.execute(
    "SELECT station_id,event_date,bucket_intervals_json,envelope_json"
    " FROM basket_probability_bridge_history"
    " WHERE state='COMMITTED' AND stage='P1'"
):
    env = json.loads(r[3])
    lin = env.get("lineage") or {}
    run = lin.get("metar_trajectory_running_high_c")
    if run in (None, ""):
        continue
    run = float(run)
    iv = json.loads(r[2])
    o = env["bucket_ordinals"]
    unit = "F" if any("°F" in b for b in o) else "C"
    exc = str(lin.get("hard_excluded_buckets") or "NONE")
    actual = set() if exc == "NONE" else set(exc.split(","))
    pred_old, pred_new, pred_true = set(), set(), set()
    ok = True
    for b in o:
        U = upper_label(b)
        edge = iv[b][1]
        if U is None or edge is None:
            continue
        if old(U, unit) <= run + 1e-9:
            pred_old.add(b)
        if new(U, unit) <= run + 1e-9:
            pred_new.add(b)
        if edge <= run + 1e-9:
            pred_true.add(b)
    stats["rows"] += 1
    if pred_old == actual:
        stats["old_match"] += 1
    elif len(examples) < 3:
        examples.append((r[0], r[1], unit, run, sorted(actual), sorted(pred_old)))
    leak_o = pred_true - actual
    leak_n = pred_true - pred_new
    over_n = pred_new - pred_true
    over_o = pred_old - pred_true
    if leak_o and any(env["probabilities"].get(b, 0) > 0.01 for b in leak_o):
        stats["leak_old"] += 1
    if leak_n:
        stats["leak_new"] += 1
    if over_n:
        stats["over_new"] += 1
    if over_o:
        stats["over_old"] += 1

print("检验 %d 条 P1 行（带 running_high）" % stats["rows"])
print("  实际排除集 == 旧公式预测:  %d (%.1f%%)   ← 诊断是否成立"
      % (stats["old_match"], 100 * stats["old_match"] / stats["rows"]))
print("  旧公式漏切且漏的桶带>1%%质量: %d 行" % stats["leak_old"])
print("  新公式仍漏切:              %d 行" % stats["leak_new"])
print("  新公式过度排除(杀了活桶):   %d 行   ← 必须是 0" % stats["over_new"])
print("  旧公式过度排除:            %d 行" % stats["over_old"])
if examples:
    print("\n不匹配样例:")
    for st, d, u, run, a, p in examples:
        print("  %s %s [%s] rh=%.2f\n    实际=%s\n    旧预测=%s" % (st, d, u, run, a, p))
